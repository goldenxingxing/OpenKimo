/**
 * Session stream hook - connects to the session WebSocket for real-time chat
 * This hook manages the WebSocket connection and processes wire protocol messages
 *
 * -----------------------------------------------------------------------------
 * High-level architecture (read this before editing)
 * -----------------------------------------------------------------------------
 *
 * This hook is the "transport + reducer" for the live chat stream:
 * - Transport: maintain exactly one active WebSocket for the currently selected `sessionId`
 * - Reducer: transform the server's JSON-RPC event stream into `LiveMessage[]` for the UI
 *
 * The UI contract is intentionally simple:
 * - `messages`: append-only timeline (with in-place updates while streaming)
 * - `status`: "ready" | "submitted" | "streaming" | "error"
 * - `contextUsage/currentStep`: lightweight progress info
 *
 * -------------------------
 * Data flow / event pipeline
 * -------------------------
 *
 *   Server (JSON-RPC) ─┐
 *                      │ WebSocket `.onmessage` (string)
 *                      ▼
 *                `handleMessage(data)`
 *                      │ JSON.parse → `WireMessage`
 *                      │ extractEvent → `WireEvent`
 *                      ▼
 *                `processEvent(event)`
 *                      │
 *                      ├─ updates small scalar states (status/contextUsage/step)
 *                      ├─ updates "current streaming buffers" (refs)
 *                      └─ updates `messages` via `setMessages(...)`
 *
 * The "streaming buffers" are refs (not state) because they are just accumulators
 * used to build the next message content (think/text/tool args) without fighting
 * React's async render model.
 *
 * ---------------------------------------
 * The hard constraint: no cross-session leak
 * ---------------------------------------
 *
 * Session switches (including "enter draft mode" which sets `sessionId = null`)
 * must be atomic from the UI's perspective:
 * - stop old stream
 * - clear per-session accumulators
 * - (optionally) connect to the new session
 *
 * Why this is tricky:
 * - WebSocket callbacks are async and can fire after we "switch pages".
 * - Calling `ws.close()` does NOT guarantee that previously scheduled callbacks
 *   won't run afterwards.
 *
 * Our solution is two layers:
 * 1) `useLayoutEffect([sessionId])` for teardown before paint (reduces visual flicker).
 * 2) WebSocket identity guards in every callback:
 *      `if (wsRef.current !== ws) return;`
 *    This makes late events harmless: only the currently active socket is allowed
 *    to mutate React state.
 *
 * ---------------------------------------------
 * Three "tabs" people may mean (disambiguation)
 * ---------------------------------------------
 *
 * 1) UI sidebar switching (switching between sessions):
 *
 *    This is in a single React tree. It changes the active context by changing
 *    `sessionId`.
 *
 *    Correctness requirement:
 *    - After a UI switch, no events from the previous session are allowed to
 *      mutate the new screen's state.
 *
 *    Mechanism used here:
 *    - `useLayoutEffect([sessionId])` teardown before paint
 *    - identity guard `if (wsRef.current !== ws) return;` in every callback
 *
 * 2) Browser tabs (two Kimo pages open in Chrome, etc.):
 *
 *    Each browser tab is its own JS runtime, so it has its own hook instance and
 *    its own `wsRef/messages/state`. They are naturally isolated on the client.
 *
 *    The only coupling is server-side (e.g. concurrent session limits), which
 *    shows up as close codes or errors. That policy is *handled* here but is not
 *    part of the core state model.
 *
 * 3) Multi-stream in one UI (render multiple sessions at once inside one page):
 *
 *    NOT supported by this hook by design. This hook intentionally enforces
 *    "one active stream → one message timeline" to stay easy to reason about.
 *
 *    If we ever need true multi-stream in one page, the clean design is:
 *
 *      ┌──────────────────────────┐
 *      │ Map<sessionId, ViewState>│   (store)
 *      └───────────┬──────────────┘
 *                  │ route by connection/session
 *          ┌───────▼────────┐
 *          │ reducer(event) │   (per session entry)
 *          └───────┬────────┘
 *                  │ select by sessionId
 *          ┌───────▼───────────┐
 *          │ UI renders one key │
 *          └────────────────────┘
 *
 *    Key property: events must be routed to the store entry that *owns* the
 *    connection that produced them.
 */
import {
  loadDismissedQuestions,
  recordDismissedQuestion,
} from "@/lib/dismissed-questions";
import {
  useState,
  useCallback,
  useRef,
  useEffect,
  useLayoutEffect,
} from "react";
import type { ChatStatus, ToolUIPart } from "ai";
import type { LiveMessage, MessageAttachmentPart, SubagentStep } from "./types";
import type { SessionStatus } from "@/lib/api/models";
import i18n from "../i18n";
import {
  type AttachmentKind,
  attachmentKind,
  makeFileToken,
} from "@/components/ai-elements/prompt-input-paste";
import {
  type RetryLabels,
  formatDuration,
  formatStepRetry,
} from "@/features/chat/step-retry-status";
import { getAuthToken } from "@/lib/auth";
import { shouldClearSubagentRunning } from "./subagent-state";
import {
  type ContentPart,
  type TokenUsage,
  type WireMessage,
  type WireEvent,
  type ToolCallState,
  type JsonRpcRequest,
  type JsonRpcResponse,
  type ApprovalRequestEvent,
  type ApprovalRequestResolvedEvent,
  type ApprovalResponseDecision,
  type QuestionRequestEvent,
  type SessionStatusPayload,
  type StepRetryEvent,
  type SubagentEventWire,
  type PlanDisplayEvent,
  extractEvent,
} from "./wireTypes";
import { createMessageId, getApiBaseUrl } from "./utils";
import { summarizeMcpStatus } from "./mcpStatusSummary";
import type { MCPStatusSnapshot } from "./wireTypes";
import { kimoCliVersion } from "@/lib/version";
import { handleToolResult, useToolEventsStore, type TodoItem } from "@/features/tool/store";
import { isSystemReminderOnly } from "./system-reminder";
import { notifySessionExpired } from "@/lib/session-expiry";
import {
  decideOnClose,
  shouldReconnectOnReturn,
  shouldReportErrorEvent,
} from "./connection-policy";
import { startsIncomingConversation } from "./conversation-reset";
import {
  createDeliveryWindow,
  isDuplicateRender,
  type DeliveryWindow,
} from "./duplicate-delivery";
import {
  LEGACY_UPLOADS_REGEX,
  MEDIA_TAG_PATH_REGEX,
  isUploadedFilePath,
  uploadedFileName,
} from "./upload-paths";
import { reconcileApprovalRequestIds } from "@/lib/approval-snapshot";
import { classifyQuestion } from "@/lib/question-snapshot";
import { v4 as uuidV4 } from "uuid";
import { setMessageOutputTime } from "./message-output-time";
import { endThinking } from "./thinking-done";

// Regex patterns moved to top level for performance
const DATA_URL_MEDIA_TYPE_REGEX = /^data:([^;,]+)[;,]/;
const NUMBERED_LIST_ITEM_REGEX = /^\d+\.\s+(.+)$/;
// `inline="1"` marks a file the person placed in their text; it goes back there.
const IMAGE_TAG_REGEX = /<image\s+path="([^"]+)"\s+content_type="([^"]+)"(\s+inline="1")?>/i;
const VIDEO_TAG_REGEX = /<video\s+path="([^"]+)"\s+content_type="([^"]+)"(\s+inline="1")?>/i;
const DOCUMENT_TAG_REGEX =
  /<document\s+path="([^"]+)"\s+content_type="([^"]+)"(\s+inline="1")?>/i;
/** The `_1a2b3c` the upload endpoint adds before the extension to keep names unique. */
const UPLOAD_SUFFIX_REGEX = /_[0-9a-f]{6}(\.[^./]*)?$/;

/** `shot_2b0fa2.png` → `shot.png`: the name the person gave the file. */
function originalUploadName(stored: string): string {
  return stored.replace(UPLOAD_SUFFIX_REGEX, "$1");
}

/** A placed file the model reads with its own tools (pdf, archive, …). */
const FILE_TAG_REGEX = /^<file\s+path="([^"]+)"\s+content_type="([^"]+)"\s+inline="1"\s*\/>$/i;
const HTTP_TO_WS_REGEX = /^http/;
const NEWLINE_REGEX = /\r?\n/;
const BROWSER_URL_PROTOCOLS = new Set(["http:", "https:", "data:", "blob:"]);
const WIRE_PROTOCOL_VERSION = "1.10";

type StepRetryPayload = StepRetryEvent["payload"];

const retryLabels = (): RetryLabels => {
  const units = {
    h: i18n.t("chat:retry.unitH"),
    m: i18n.t("chat:retry.unitM"),
    s: i18n.t("chat:retry.unitS"),
  };
  return {
    reason: (reason, errorType) => i18n.t(`chat:retry.${reason}`, { errorType }),
    attemptOf: (attempt, max) => i18n.t("chat:retry.attemptOf", { attempt, max }),
    attempt: (attempt) => i18n.t("chat:retry.attempt", { attempt }),
    elapsed: (duration) => i18n.t("chat:retry.elapsed", { duration }),
    nextIn: (duration) => i18n.t("chat:retry.nextIn", { duration }),
    stopHint: i18n.t("chat:retry.stopHint"),
    duration: (seconds) => formatDuration(seconds, units),
  };
};

const discardSubagentRetryAttempt = (steps: SubagentStep[]): SubagentStep[] => {
  const next = steps.filter(
    (step) => !(step.kind === "tool-call" && step.status === "running"),
  );
  while (next.length > 0) {
    const last = next[next.length - 1];
    if (last.kind !== "thinking" && last.kind !== "text") {
      break;
    }
    next.pop();
  }
  return next;
};

/** Extract the URL from a media output part (image_url or video_url) */
const extractMediaUrl = (part: Record<string, unknown>): string => {
  const imgUrl = (part.image_url as { url?: string })?.url;
  const vidUrl = (part.video_url as { url?: string })?.url;
  return imgUrl ?? vidUrl ?? "";
};

/** Check if a URL can be rendered in the browser (http/https/data/blob) */
const isBrowserUrl = (url: string): boolean => {
  try {
    return BROWSER_URL_PROTOCOLS.has(new URL(url).protocol);
  } catch {
    return false;
  }
};

export type SlashCommandDef = {
  name: string;
  description: string;
  aliases: string[];
};

type UseSessionStreamOptions = {
  /** Session ID to connect to */
  sessionId: string | null;
  /** Base URL for WebSocket connection (defaults to current host) */
  baseUrl?: string;
  /** Callback when messages change */
  onMessagesChange?: (messages: LiveMessage[]) => void;
  /** Callback when connection status changes */
  onConnectionChange?: (connected: boolean) => void;
  /** Callback when an error occurs */
  onError?: (error: Error) => void;
  /** Callback when session status changes */
  onSessionStatus?: (status: SessionStatus) => void;
  /**
   * A prompt the server refused because the session was still busy. Its text
   * is handed back so it can wait its turn instead of being lost.
   */
  onPromptRejected?: (text: string) => void;
  /** Callback when first turn is complete (for auto-renaming) */
  onFirstTurnComplete?: () => void;
  /** Callback to broadcast wire events to the plugin system */
  onWireEventForPlugin?: (event: import("./wireTypes").WireEvent, ctx: { sessionId: string; isReplay: boolean }) => void;
};

type UseSessionStreamReturn = {
  /** Current messages */
  messages: LiveMessage[];
  /** Chat status */
  status: ChatStatus;
  /** Latest runtime session status snapshot */
  sessionStatus: SessionStatus | null;
  /** Whether the stream is still replaying history */
  isReplayingHistory: boolean;
  /**
   * Whether this connection's history has fully arrived. Earlier than
   * `isReplayingHistory` turning false, which waits for the environment too.
   */
  isHistoryLoaded: boolean;
  /** Whether waiting for the first response after sending a prompt */
  isAwaitingFirstResponse: boolean;
  /** Current context usage (0-1) */
  contextUsage: number;
  /**
   * Whether the current turn is parked in a blocking wait on background
   * task(s) rather than doing substantive work. Prompts sent in this state
   * replace the parked turn instead of queueing.
   */
  waitingOnBackground: boolean;
  /** Current token usage for the active step, if available */
  tokenUsage: TokenUsage | null;
  /** Current step number */
  currentStep: number;
  /** Whether connected to the session stream */
  isConnected: boolean;
  /** Send a message to the session (will auto-connect if not connected) */
  sendMessage: (text: string) => Promise<void>;
  /** Respond to an approval request */
  respondToApproval: (
    requestId: string,
    response: ApprovalResponseDecision,
    reason?: string,
  ) => Promise<void>;
  /** Respond to a question request */
  respondToQuestion: (
    requestId: string,
    answers: Record<string, string>,
  ) => Promise<void>;
  /**
   * Permanently close a question ("Stop run"): hides it locally, remembers
   * the dismissal so replayed history cannot resurrect it, and leaves
   * cancelling the turn to the caller.
   */
  dismissQuestion: (requestId: string) => void;
  /** Send a cancel request for the current turn */
  cancel: () => void;
  /** Disconnect from the stream */
  disconnect: () => void;
  /** Reconnect to the session */
  reconnect: () => void;
  /** Connect to the session stream */
  connect: () => void;
  /** Set messages directly */
  setMessages: React.Dispatch<React.SetStateAction<LiveMessage[]>>;
  /** Clear all messages */
  clearMessages: () => void;
  /** Connection error if any */
  error: Error | null;
  /** Whether plan mode is active */
  planMode: boolean;
  /** Set plan mode via silent RPC (no context message) */
  sendSetPlanMode: (enabled: boolean) => void;
  /** Whether YOLO (auto-approve every action) is active */
  yolo: boolean;
  /** Set YOLO via silent RPC (no context message) */
  sendSetYolo: (enabled: boolean) => void;
  /** Available slash commands from the server */
  slashCommands: SlashCommandDef[];
};

type PendingApprovalEntry = {
  requestId: string;
  toolCallId: string;
  messageId?: string;
  rpcId?: string | number;
  submitted?: boolean;
};

type PendingQuestionEntry = {
  requestId: string;
  toolCallId: string;
  messageId?: string;
  rpcId?: string | number;
  submitted?: boolean;
};

/**
 * Hook for connecting to a session's WebSocket stream
 */
/** `INVALID_STATE` from the server: a prompt sent while a turn is running. */
const SESSION_BUSY_ERROR_CODE = -32000;

export function useSessionStream(
  options: UseSessionStreamOptions,
): UseSessionStreamReturn {
  const {
    sessionId,
    baseUrl,
    onMessagesChange,
    onConnectionChange,
    onError,
    onSessionStatus,
    onPromptRejected,
    onFirstTurnComplete,
    onWireEventForPlugin,
  } = options;

  const onPromptRejectedRef = useRef(onPromptRejected);
  onPromptRejectedRef.current = onPromptRejected;
  // Prompts sent and not yet answered, by request id, so a refusal can give
  // the text back.
  const sentPromptsRef = useRef(new Map<string, string>());

  const [messages, setMessagesInternal] = useState<LiveMessage[]>([]);
  const [status, setStatus] = useState<ChatStatus>("ready");
  const [sessionStatus, setSessionStatus] = useState<SessionStatus | null>(
    null,
  );
  const [contextUsage, setContextUsage] = useState(0);
  // True while the server reports the current turn parked in a blocking wait
  // on background task(s) (TaskOutput block=true). Input may then be sent
  // directly — the server replaces the parked turn instead of refusing busy.
  const [waitingOnBackground, setWaitingOnBackground] = useState(false);
  const [tokenUsage, setTokenUsage] = useState<TokenUsage | null>(null);
  const [planMode, setPlanMode] = useState(false);
  const [yolo, setYolo] = useState(false);
  const [currentStep, setCurrentStep] = useState(0);
  const [isConnected, setIsConnected] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const [isAwaitingFirstResponse, setIsAwaitingFirstResponse] = useState(false);
  const [isReplayingHistory, setIsReplayingHistory] = useState(true);
  // Which session's history has arrived, not just whether one has: on the
  // render that switches sessions, a bare flag still says "loaded" from the
  // previous session, and the new conversation was taken to be the old one.
  const [historyLoadedFor, setHistoryLoadedFor] = useState<string | null>(null);
  const sessionIdRef = useRef(sessionId);
  sessionIdRef.current = sessionId;
  const isHistoryLoaded = sessionId ? historyLoadedFor === sessionId : true;
  const [slashCommands, setSlashCommands] = useState<SlashCommandDef[]>([]);

  // Refs
  /**
   * The single source of truth for "which WebSocket is allowed to mutate React state".
   *
   * Important nuance: this ref represents the *current connection attempt*, not only
   * "the currently open socket".
   *
   * Why this exists:
   * - WebSocket callbacks (`onmessage/onclose/onerror/onopen`) are async and can fire
   *   after the UI has already switched to another session (or draft mode).
   * - Simply calling `ws.close()` or setting `wsRef.current = null` does NOT prevent
   *   already-scheduled callbacks from running.
   *
   * Our invariant:
   * - Only callbacks belonging to `wsRef.current` may call `setMessages`, `setStatus`, etc.
   * - Every callback starts with `if (wsRef.current !== ws) return;` to ignore late events.
   */
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<number | null>(null);
  // Consecutive failed reconnects; reset the moment a socket opens.
  const reconnectAttemptRef = useRef(0);
  // Set while the client is deliberately closing, so the handlers can tell
  // an intentional close from a dropped connection.
  const intentionalCloseRef = useRef(false);
  const connectRef = useRef<() => void>(() => undefined);
  const disconnectRef = useRef<() => void>(() => undefined);
  const reconnectRef = useRef<() => void>(() => undefined);
  const resetStateRef = useRef<(preserveSlashCommands?: boolean) => void>(() => undefined);
  const historyCompleteTimeoutRef = useRef<number | null>(null);
  const deliveryWindowRef = useRef<DeliveryWindow>(createDeliveryWindow());
  /** A connect owes the list a reset, paid when the replacement arrives. */
  const pendingConversationResetRef = useRef(false);
  const isReplayingRef = useRef(true); // Track if we're still replaying history
  const pendingMessageRef = useRef<string | null>(null); // Message to send after connection
  const awaitingIdleRef = useRef(false); // Track pending idle after cancel
  const awaitingFirstResponseRef = useRef(false); // Track if waiting for first event of a turn
  const lastStatusSeqRef = useRef<number | null>(null);
  const lastWsMessageTimeRef = useRef<number>(0); // Last time a WS message was received
  const watchdogIntervalRef = useRef<number | null>(null); // Stale connection watchdog
  const statusRef = useRef<ChatStatus>("ready"); // Synced copy of status for watchdog

  // First turn tracking for auto-rename (simplified: backend reads from wire.jsonl)
  const hasTurnStartedRef = useRef(false); // Whether at least one turn has started
  const firstTurnCompleteCalledRef = useRef(false); // Whether onFirstTurnComplete was called

  // Initialize message tracking
  const initializeIdRef = useRef<string | null>(null);
  const initializeRetryCountRef = useRef(0); // Track retry attempts for initialize
  const MAX_INITIALIZE_RETRIES = 5; // Maximum retry attempts

  // Plugin bridge ref — avoids stale closure in WebSocket handler chain
  const pluginBridgeRef = useRef(onWireEventForPlugin);
  pluginBridgeRef.current = onWireEventForPlugin;
  const usingCachedCommandsRef = useRef(false); // Track if using cached slash commands
  const slashCommandsLenRef = useRef(0); // Track slashCommands length without state dependency

  // Current state accumulators
  const currentThinkingRef = useRef("");
  const currentTextRef = useRef("");
  const currentToolCallsRef = useRef<Map<string, ToolCallState>>(new Map());
  const currentToolCallIdRef = useRef<string | null>(null);
  const thinkingMessageIdRef = useRef<string | null>(null);
  const textMessageIdRef = useRef<string | null>(null);
  const pendingApprovalRequestsRef = useRef<Map<string, PendingApprovalEntry>>(
    new Map(),
  );
  const pendingQuestionRequestsRef = useRef<Map<string, PendingQuestionEntry>>(
    new Map(),
  );
  // Questions the user closed for good; replayed history must not bring
  // them back (see lib/dismissed-questions.ts).
  const dismissedQuestionsRef = useRef<Set<string>>(new Set());

  // Track if current turn is a /clear command (needs UI clear on turn end)
  const pendingClearRef = useRef(false);

  // Turn counter for fork feature
  const turnCounterRef = useRef(0);

  // Track compaction indicator message so we can remove it on CompactionEnd
  const compactionMessageIdRef = useRef<string | null>(null);

  // Track MCP loading indicator message so we can remove it on MCPLoadingEnd
  const mcpLoadingMessageIdRef = useRef<string | null>(null);

  // Latest MCP startup snapshot. Kept in a ref as well as state because
  // MCPLoadingEnd is handled in the same tick as the StatusUpdate that carries
  // the final snapshot, and reads it to decide what to leave on screen.
  const mcpStatusRef = useRef<MCPStatusSnapshot | null>(null);

  // Track the temporary StepRetry status so the next attempt can replace it.
  const stepRetryStatusMessageIdRef = useRef<string | null>(null);
  /** When the current step first failed; NaN when it was replayed. */
  const stepRetryStartedAtRef = useRef<number | null>(null);

  // Wrapped setMessages
  const setMessages: typeof setMessagesInternal = useCallback((action) => {
    setMessagesInternal(action);
  }, []);

  const setAwaitingFirstResponse = useCallback((value: boolean) => {
    awaitingFirstResponseRef.current = value;
    setIsAwaitingFirstResponse(value);
  }, []);
  const clearAwaitingFirstResponse = useCallback(() => {
    if (!awaitingFirstResponseRef.current) {
      return;
    }
    setAwaitingFirstResponse(false);
  }, [setAwaitingFirstResponse]);

  const normalizeSessionStatus = useCallback(
    (payload: SessionStatusPayload): SessionStatus => ({
      sessionId: payload.session_id,
      state: payload.state,
      seq: payload.seq,
      workerId: payload.worker_id ?? undefined,
      reason: payload.reason ?? undefined,
      detail: payload.detail ?? undefined,
      updatedAt: new Date(payload.updated_at),
    }),
    [],
  );

  const completeStreamingMessages = useCallback(() => {
    setMessages((prev) =>
      prev.map((msg) => {
        let updated = msg;
        if (msg.isStreaming) {
          updated = { ...updated, isStreaming: false };
        }
        if (msg.toolCall?.subagentRunning) {
          updated = {
            ...updated,
            toolCall: { ...updated.toolCall!, subagentRunning: false },
          };
        }
        return updated;
      }),
    );
  }, [setMessages]);

  // Resolve a dangling compaction indicator. The backend only emits
  // CompactionEnd on success; if compaction fails or the turn is
  // interrupted, the "Compacting conversation history…" status message
  // would otherwise stay on screen forever.
  const resolveStaleCompaction = useCallback(
    (content: string) => {
      const compactMsgId = compactionMessageIdRef.current;
      if (!compactMsgId) return;
      compactionMessageIdRef.current = null;
      setMessages((prev) =>
        prev.map((m) =>
          m.id === compactMsgId ? { ...m, content, isStreaming: false } : m,
        ),
      );
    },
    [setMessages],
  );

  // Mark all non-terminal tool calls as interrupted and dismiss stale
  // approval/question dialogs.  Called only when the backend confirms no
  // active turn (idle / stopped / error), so it won't dismiss legitimate
  // pending approvals on a busy session (e.g. after a tab switch).
  //
  // preserveUnsubmitted: on a bare "idle" snapshot (e.g. after a worker
  // restart cleared the backend's in-flight set) keep approvals/questions
  // that are still registered as actionable.  The worker may re-issue the
  // request once it resumes; killing the dialog here would leave the user
  // stuck with no way to answer.  Explicit stop/error still clears all.
  const interruptStaleToolCalls = useCallback(
    (options?: { preserveUnsubmitted?: boolean; interrupted?: boolean }) => {
      const preserve = options?.preserveUnsubmitted ?? false;
      // Distinguish a user-initiated cancel/deny ("cancelled") from the
      // session worker dying mid-turn ("interrupted") — see the error branch
      // of applySessionStatus, which passes interrupted: true.
      const interrupted = options?.interrupted ?? false;
      const deniedState = interrupted ? "output-interrupted" : "output-denied";
      const liveApprovalIds = new Set<string>();
      const liveQuestionIds = new Set<string>();
      if (preserve) {
        for (const [id, p] of pendingApprovalRequestsRef.current) {
          if (p.submitted) {
            pendingApprovalRequestsRef.current.delete(id);
          } else {
            liveApprovalIds.add(p.requestId);
          }
        }
        for (const [id, p] of pendingQuestionRequestsRef.current) {
          if (p.submitted) {
            pendingQuestionRequestsRef.current.delete(id);
          } else {
            liveQuestionIds.add(p.requestId);
          }
        }
      } else {
        pendingApprovalRequestsRef.current.clear();
        pendingQuestionRequestsRef.current.clear();
      }
      setMessages((prev) =>
        prev.map((msg) => {
          if (msg.variant !== "tool" || !msg.toolCall) return msg;
          const state = msg.toolCall.state;
          if (
            preserve &&
            state === "approval-requested" &&
            msg.toolCall.approval &&
            liveApprovalIds.has(msg.toolCall.approval.id)
          ) {
            return msg;
          }
          if (
            preserve &&
            state === "question-requested" &&
            msg.toolCall.question &&
            liveQuestionIds.has(msg.toolCall.question.id)
          ) {
            return msg;
          }
          if (
            state === "approval-requested" ||
            state === "question-requested" ||
            state === "input-streaming" ||
            state === "input-available"
          ) {
          return {
            ...msg,
            isStreaming: false,
            toolCall: {
              ...msg.toolCall,
              state: deniedState,
              ...(state === "approval-requested" && msg.toolCall.approval
                ? {
                    approval: {
                      ...msg.toolCall.approval,
                      submitted: true,
                      resolved: true,
                      approved: false,
                      response: "reject",
                    },
                  }
                : {}),
              ...(state === "question-requested" && msg.toolCall.question
                ? {
                    question: {
                      ...msg.toolCall.question,
                      submitted: true,
                      resolved: true,
                    },
                  }
                : {}),
            },
          };
        }
          return msg;
        }),
      );
    },
    [setMessages],
  );

  const clearStepRetryStatus = useCallback(() => {
    stepRetryStartedAtRef.current = null;
    const statusMessageId = stepRetryStatusMessageIdRef.current;
    if (!statusMessageId) {
      return;
    }
    stepRetryStatusMessageIdRef.current = null;
    setMessages((prev) => prev.filter((msg) => msg.id !== statusMessageId));
  }, [setMessages]);

  const applySessionStatus = useCallback(
    (payload: SessionStatusPayload) => {
      const normalized = normalizeSessionStatus(payload);
      const lastSeq = lastStatusSeqRef.current;
      if (lastSeq !== null && normalized.seq <= lastSeq) {
        return;
      }
      lastStatusSeqRef.current = normalized.seq;
      setSessionStatus(normalized);
      onSessionStatus?.(normalized);
      isReplayingRef.current = false;
      setIsReplayingHistory(false);
      setHistoryLoadedFor(sessionIdRef.current ?? null);

      switch (normalized.state) {
        case "busy": {
          if (!awaitingIdleRef.current) {
            setStatus("streaming");
          }
          break;
        }
        case "restarting": {
          setStatus("submitted");
          break;
        }
        case "error": {
          setStatus("error");
          clearStepRetryStatus();
          setAwaitingFirstResponse(false);
          awaitingIdleRef.current = false;
          resolveStaleCompaction("Compaction failed.");
          completeStreamingMessages();
          interruptStaleToolCalls({ interrupted: true });
          break;
        }
        case "stopped":
        case "idle": {
          setStatus("ready");
          // Nothing is retrying on an idle session. A turn that ended without
          // a TurnEnd — the app was force-quit mid-retry — leaves its last
          // StepRetry as the end of the history, and a replay would show
          // "retry 16" for a turn that died with the old process.
          clearStepRetryStatus();
          setAwaitingFirstResponse(false);
          awaitingIdleRef.current = false;
          resolveStaleCompaction("Compaction did not complete.");
          completeStreamingMessages();
          // A bare "idle" may be a post-restart snapshot where the backend
          // lost its in-flight set but the worker can still re-issue pending
          // approvals — keep those dialogs alive.  An explicit "stopped"
          // (user cancel) clears everything.
          interruptStaleToolCalls({
            preserveUnsubmitted: normalized.state === "idle",
          });

          // Trigger onFirstTurnComplete only after at least one turn has completed
          if (hasTurnStartedRef.current && !firstTurnCompleteCalledRef.current) {
            firstTurnCompleteCalledRef.current = true;
            onFirstTurnComplete?.();
          }
          break;
        }
      }
    },
    [
      clearStepRetryStatus,
      completeStreamingMessages,
      resolveStaleCompaction,
      interruptStaleToolCalls,
      normalizeSessionStatus,
      onSessionStatus,
      setAwaitingFirstResponse,
      onFirstTurnComplete,
    ],
  );

  const updateMessageById = useCallback(
    (messageId: string, transform: (message: LiveMessage) => LiveMessage) => {
      setMessages((prev) =>
        prev.map((message) =>
          message.id === messageId ? transform(message) : message,
        ),
      );
    },
    [setMessages],
  );

  const safeStringify = useCallback((value: unknown): string => {
    if (value === null || value === undefined) {
      return "";
    }
    if (typeof value === "string") {
      return value;
    }
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }, []);

  type ParsedUserInput = { text: string; attachments: MessageAttachmentPart[] };

  const parseMediaTypeFromDataUrl = useCallback(
    (url: string): string | null => {
      if (!url.startsWith("data:")) {
        return null;
      }
      const match = DATA_URL_MEDIA_TYPE_REGEX.exec(url);
      return match?.[1] ?? null;
    },
    [],
  );

  const getSessionUploadUrl = useCallback(
    (filename?: string): string | undefined => {
      if (!(sessionId && filename)) {
        return undefined;
      }
      const basePath = baseUrl ?? getApiBaseUrl();
      const token = getAuthToken();
      const tokenParam = token ? `?token=${encodeURIComponent(token)}` : "";
      return `${basePath}/api/sessions/${encodeURIComponent(
        sessionId,
      )}/uploads/${encodeURIComponent(filename)}${tokenParam}`;
    },
    [baseUrl, sessionId],
  );

  const parseUserInput = useCallback(
    (input: string | ContentPart[]): ParsedUserInput => {
      if (typeof input === "string") {
        return { text: input, attachments: [] };
      }

      const textParts: string[] = [];
      const attachments: MessageAttachmentPart[] = [];
      const uploadedFilePaths: string[] = [];
      let inUploadedFilesBlock = false;
      const collectUploadedFilePath = (line: string): boolean => {
        const match = NUMBERED_LIST_ITEM_REGEX.exec(line.trim());
        if (!match) {
          return false;
        }
        const filePath = match[1].trim();
        if (!isUploadedFilePath(filePath)) {
          return false;
        }
        uploadedFilePaths.push(filePath);
        return true;
      };

      // Pending metadata for associating with next image_url part
      let pendingFilename: string | undefined;
      let pendingMediaType: string | undefined;
      let pendingInline = false;

      // A placed file comes back as its placeholder, in the text where it was
      // put; the text either side of it is joined straight on, not as a new
      // paragraph.
      const tokenLabels = {
        image: i18n.t("chat:attachmentToken.image"),
        file: i18n.t("chat:attachmentToken.file"),
      };
      let placedCount = 0;
      let glueNext = false;
      const placeInText = (kind: AttachmentKind, filename: string | undefined) => {
        placedCount += 1;
        const token = makeFileToken(
          placedCount,
          kind,
          filename ? originalUploadName(filename) : "file",
          tokenLabels,
        );
        if (textParts.length === 0) {
          textParts.push(token);
        } else {
          textParts[textParts.length - 1] += token;
        }
        glueNext = true;
      };

      // State for collecting document content
      let inDocument = false;
      let documentInline = false;
      let documentFilename: string | undefined;
      let documentMediaType: string | undefined;
      let documentContent: string[] = [];

      for (const part of input) {
        if (part.type === "text" || part.type === "input_text") {
          const text = part.text;

          // New format: <image path="/path/to/uploads/file.name" content_type="image/png">
          const imageTagMatch = IMAGE_TAG_REGEX.exec(text);
          if (imageTagMatch) {
            // Extract filename from path
            const fullPath = imageTagMatch[1];
            pendingFilename = uploadedFileName(fullPath);
            pendingMediaType = imageTagMatch[2];
            pendingInline = Boolean(imageTagMatch[3]);
            continue; // Skip this text part, it's just metadata
          }

          const fileTagMatch = FILE_TAG_REGEX.exec(text.trim());
          if (fileTagMatch) {
            const filename = uploadedFileName(fileTagMatch[1]);
            const mediaType = fileTagMatch[2];
            const url = filename ? getSessionUploadUrl(filename) : undefined;
            attachments.push(
              url
                ? { type: "file", mediaType, filename, url }
                : { kind: "nopreview", filename },
            );
            placeInText(attachmentKind(mediaType), filename);
            continue;
          }

          // New format: </image> closing tag - skip it
          if (text.trim() === "</image>") {
            continue;
          }

          // New format: <video path="/path/to/uploads/file.name" content_type="video/mp4">
          const videoTagMatch = VIDEO_TAG_REGEX.exec(text);
          if (videoTagMatch) {
            // Extract filename from path
            const fullPath = videoTagMatch[1];
            pendingFilename = uploadedFileName(fullPath);
            pendingMediaType = videoTagMatch[2];
            pendingInline = Boolean(videoTagMatch[3]);
            continue; // Skip this text part, it's just metadata
          }

          // New format: </video> closing tag - create attachment if no video_url follows
          if (text.trim() === "</video>") {
            // If we have pending video metadata but no video_url part will follow,
            // create a video attachment from the session uploads.
            if (pendingFilename && pendingMediaType?.startsWith("video/")) {
              const url = getSessionUploadUrl(pendingFilename);
              if (url) {
                attachments.push({
                  type: "file",
                  mediaType: pendingMediaType,
                  filename: pendingFilename,
                  url,
                });
              } else {
                attachments.push({
                  kind: "video-nopreview",
                  mediaType: pendingMediaType,
                  filename: pendingFilename,
                });
              }
              if (pendingInline) placeInText("file", pendingFilename);
              pendingFilename = undefined;
              pendingMediaType = undefined;
              pendingInline = false;
            }
            continue;
          }

          // New format: <document path="/path/to/uploads/..." content_type="..."> - start collecting
          const documentTagMatch = DOCUMENT_TAG_REGEX.exec(text);
          if (documentTagMatch) {
            inDocument = true;
            documentInline = Boolean(documentTagMatch[3]);
            // Extract filename from path
            const fullPath = documentTagMatch[1];
            documentFilename = uploadedFileName(fullPath);
            documentMediaType = documentTagMatch[2];
            documentContent = [];
            continue;
          }

          // New format: </document> - finalize document attachment
          if (text.trim() === "</document>") {
            if (inDocument && documentFilename) {
              const content = documentContent.join("");
              const bytes = new TextEncoder().encode(content);
              // Chunk the spread: String.fromCharCode(...bytes) throws a
              // RangeError (too many arguments) for multi-MB documents.
              let binary = "";
              const CHUNK_SIZE = 0x8000;
              for (let i = 0; i < bytes.length; i += CHUNK_SIZE) {
                binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK_SIZE));
              }
              const base64 = btoa(binary);
              const dataUrl = `data:${documentMediaType ?? "text/plain"};base64,${base64}`;
              attachments.push({
                type: "file",
                mediaType: documentMediaType ?? "text/plain",
                filename: documentFilename,
                url: dataUrl,
              });
              if (documentInline) placeInText("file", documentFilename);
            }
            inDocument = false;
            documentInline = false;
            documentFilename = undefined;
            documentMediaType = undefined;
            documentContent = [];
            continue;
          }

          // If inside document, collect content instead of adding to textParts
          if (inDocument) {
            documentContent.push(text);
            continue;
          }

          const lines = text.split(NEWLINE_REGEX);
          const filteredLines: string[] = [];

          for (const line of lines) {
            if (line.includes("<uploaded_files>")) {
              inUploadedFilesBlock = true;
              continue;
            }
            if (line.includes("</uploaded_files>")) {
              inUploadedFilesBlock = false;
              continue;
            }
            if (inUploadedFilesBlock) {
              collectUploadedFilePath(line);
              continue;
            }
            if (collectUploadedFilePath(line)) {
              continue;
            }
            filteredLines.push(line);
          }

          const filteredText = filteredLines.join("\n");

          // Legacy format: `uploads/file.name`
          const legacyMatch = LEGACY_UPLOADS_REGEX.exec(filteredText);
          if (legacyMatch) {
            pendingFilename = legacyMatch[1];
          }

          // Text right after a placed file continues its sentence.
          if (glueNext && textParts.length > 0) {
            textParts[textParts.length - 1] += filteredText;
            glueNext = false;
          } else if (filteredText.trim()) {
            // Only add non-metadata text parts
            textParts.push(filteredText);
          }
          continue;
        }

        if (part.type === "image_url") {
          const inferredMediaType = parseMediaTypeFromDataUrl(
            part.image_url.url,
          );
          attachments.push({
            type: "file",
            mediaType: pendingMediaType ?? inferredMediaType ?? "image/*",
            filename: pendingFilename,
            url: part.image_url.url,
          });
          if (pendingInline) placeInText("image", pendingFilename);
          pendingFilename = undefined;
          pendingMediaType = undefined;
          pendingInline = false;
        }

        if (part.type === "video_url") {
          const inferredMediaType = parseMediaTypeFromDataUrl(
            part.video_url.url,
          );
          attachments.push({
            type: "file",
            mediaType: pendingMediaType ?? inferredMediaType ?? "video/*",
            filename: pendingFilename,
            url: part.video_url.url,
          });
          pendingFilename = undefined;
          pendingMediaType = undefined;
        }
      }

      if (uploadedFilePaths.length > 0) {
        const existingFilenames = new Set(
          attachments
            .map((attachment) => attachment.filename)
            .filter((filename): filename is string => Boolean(filename)),
        );
        const seenUploadedFilenames = new Set<string>();
        for (const filePath of uploadedFilePaths) {
          const filename = uploadedFileName(filePath);
          if (!filename) {
            continue;
          }
          if (
            existingFilenames.has(filename) ||
            seenUploadedFilenames.has(filename)
          ) {
            continue;
          }
          attachments.push({
            kind: "nopreview",
            filename,
          });
          seenUploadedFilenames.add(filename);
        }
      }

      return { text: textParts.join("\n\n").trim(), attachments };
    },
    [getSessionUploadUrl, parseMediaTypeFromDataUrl],
  );

  const upsertMessage = useCallback(
    (incoming: LiveMessage) => {
      setMessages((prev) => {
        const index = prev.findIndex((message) => message.id === incoming.id);
        if (index === -1) {
          const last = prev[prev.length - 1];
          if (isDuplicateRender(last, incoming)) {
            // Diagnostics for the "reply rendered twice until the page is
            // revisited" bug: identical content appended as a NEW message
            // means the replay/live merge raced. Never drop — just log, so
            // the next occurrence pins the path with ids in hand.
            console.warn(
              "[SessionStream] duplicate render appended:",
              `last=${last.id} incoming=${incoming.id} variant=${incoming.variant ?? "?"} content=${(incoming.content ?? incoming.thinking ?? "").slice(0, 120)}`,
            );
          }
          return [...prev, incoming];
        }
        const next = [...prev];
        next[index] = { ...next[index], ...incoming };
        return next;
      });
    },
    [setMessages],
  );

  // Notify parent of changes
  useEffect(() => {
    onMessagesChange?.(messages);
  }, [messages, onMessagesChange]);

  // Notify parent of connection changes
  useEffect(() => {
    onConnectionChange?.(isConnected);
  }, [isConnected, onConnectionChange]);

  // Create unique message ID
  const getNextMessageId = useCallback(
    (prefix: "user" | "assistant"): string => createMessageId(prefix),
    [],
  );

  // Reset state for new step
  const resetStepState = useCallback(() => {
    currentThinkingRef.current = "";
    currentTextRef.current = "";
    thinkingMessageIdRef.current = null;
    textMessageIdRef.current = null;
  }, []);

  const showStepRetryStatus = useCallback(
    (retry: StepRetryPayload, isReplay: boolean) => {
      // Measured from the first failure of this step. A replay has no clock
      // for it, so its line leaves the duration out.
      if (stepRetryStartedAtRef.current === null) {
        stepRetryStartedAtRef.current = isReplay ? Number.NaN : Date.now();
      }
      const startedAt = stepRetryStartedAtRef.current;
      const elapsedS = Number.isNaN(startedAt) ? null : (Date.now() - startedAt) / 1000;
      const content = formatStepRetry(retry, elapsedS, retryLabels());
      const existingMessageId = stepRetryStatusMessageIdRef.current;

      if (existingMessageId) {
        setMessages((prev) =>
          prev.map((msg) =>
            msg.id === existingMessageId
              ? { ...msg, content, isStreaming: !isReplay }
              : msg,
          ),
        );
        return;
      }

      const statusMessageId = getNextMessageId("assistant");
      stepRetryStatusMessageIdRef.current = statusMessageId;
      setMessages((prev) => [
        ...prev,
        {
          id: statusMessageId,
          role: "assistant",
          variant: "status",
          content,
          isStreaming: !isReplay,
        },
      ]);
    },
    [getNextMessageId, setMessages],
  );

  const discardRetryAttemptMessages = useCallback(() => {
    const messageIds = new Set<string>();
    const discardedToolCallIds = new Set<string>();
    if (thinkingMessageIdRef.current) {
      messageIds.add(thinkingMessageIdRef.current);
    }
    if (textMessageIdRef.current) {
      messageIds.add(textMessageIdRef.current);
    }
    // Only discard tool calls that haven't produced a result yet — i.e. the
    // ones still in-flight when the retry fires. Tool calls from earlier
    // successful steps in the same turn already have `tc.result` set by
    // ToolResult and must be preserved.
    for (const toolCall of currentToolCallsRef.current.values()) {
      if (toolCall.result !== undefined) {
        continue;
      }
      discardedToolCallIds.add(toolCall.id);
      if (toolCall.messageId) {
        messageIds.add(toolCall.messageId);
      }
    }

    resetStepState();
    for (const id of discardedToolCallIds) {
      currentToolCallsRef.current.delete(id);
    }
    if (
      currentToolCallIdRef.current !== null &&
      discardedToolCallIds.has(currentToolCallIdRef.current)
    ) {
      currentToolCallIdRef.current = null;
    }
    for (const [requestId, request] of pendingApprovalRequestsRef.current) {
      if (discardedToolCallIds.has(request.toolCallId)) {
        pendingApprovalRequestsRef.current.delete(requestId);
      }
    }
    for (const [requestId, request] of pendingQuestionRequestsRef.current) {
      if (discardedToolCallIds.has(request.toolCallId)) {
        pendingQuestionRequestsRef.current.delete(requestId);
      }
    }

    if (messageIds.size > 0) {
      setMessages((prev) => prev.filter((msg) => !messageIds.has(msg.id)));
    }
  }, [resetStepState, setMessages]);

  // Reset all state
  const resetState = useCallback((preserveSlashCommands = false) => {
    resetStepState();
    stepRetryStatusMessageIdRef.current = null;
    currentToolCallsRef.current?.clear();
    currentToolCallIdRef.current = null;
    pendingApprovalRequestsRef.current?.clear();
    pendingQuestionRequestsRef.current?.clear();
    pendingClearRef.current = false;
    // Answers to them arrive on the old socket, if at all.
    sentPromptsRef.current.clear();
    setCurrentStep(0);
    setContextUsage(0);
    setTokenUsage(null);
    setPlanMode(false);
    setYolo(false);
    mcpStatusRef.current = null;
    setError(null);
    setSessionStatus(null);
    lastStatusSeqRef.current = null;
    isReplayingRef.current = true;
    setIsReplayingHistory(true);
    setHistoryLoadedFor(null);
    setAwaitingFirstResponse(false);
    setWaitingOnBackground(false);
    // Reset first turn tracking
    hasTurnStartedRef.current = false;
    firstTurnCompleteCalledRef.current = false;
    // Reset turn counter
    turnCounterRef.current = 0;
    // Clear history_complete timeout
    if (historyCompleteTimeoutRef.current) {
      window.clearTimeout(historyCompleteTimeoutRef.current);
      historyCompleteTimeoutRef.current = null;
    }
    // Handle slashCommands: preserve or clear
    if (!preserveSlashCommands) {
      setSlashCommands([]);
      slashCommandsLenRef.current = 0;
      usingCachedCommandsRef.current = false;
    } else if (slashCommandsLenRef.current > 0) {
      usingCachedCommandsRef.current = true;
    }
  }, [resetStepState, setAwaitingFirstResponse]);

  // Process a SubagentEvent: accumulate inner events into parent Agent tool's subagentSteps
  const processSubagentEvent = useCallback(
    (
      parentToolCallId: string,
      innerType: string,
      innerPayload: unknown,
      agentId?: string,
      subagentType?: string,
    ) => {
      setMessages((prev) => {
        // Find the parent Agent tool message by toolCallId
        const parentIdx = prev.findIndex(
          (msg) => msg.toolCall?.toolCallId === parentToolCallId,
        );
        if (parentIdx === -1) return prev;

        const parentMsg = prev[parentIdx];
        // Any sign of progress ends a retry: only a fresh StepRetry keeps one.
        const steps: SubagentStep[] = (parentMsg.toolCall?.subagentSteps ?? []).filter(
          (step) => step.kind !== "retry",
        );

        // Default: any inner event means the subagent is still active. The
        // one exception is an inner TurnEnd — the subagent's own turn ended.
        // That is the terminal state for the parent Agent tool call. The
        // parent's ToolResult may follow immediately (normal case) or never
        // arrive (user cancelled; the backend's `KimoSoul.run` finally
        // compensates, but even without it we can already stop showing
        // "Agent working…" for this subagent). Nested SubagentEvents stay
        // on the default branch — a nested turn ending means the parent
        // subagent is still running.
        let subagentRunning = !shouldClearSubagentRunning(innerType);

        switch (innerType) {
          case "ContentPart": {
            const cp = innerPayload as {
              type: string;
              think?: string;
              text?: string;
            };
            if (cp.type === "think" && cp.think) {
              const last = steps[steps.length - 1];
              if (last?.kind === "thinking") {
                steps[steps.length - 1] = {
                  ...last,
                  text: last.text + cp.think,
                };
              } else {
                steps.push({ kind: "thinking", text: cp.think });
              }
            } else if (cp.type === "text" && cp.text) {
              const last = steps[steps.length - 1];
              if (last?.kind === "text") {
                steps[steps.length - 1] = {
                  ...last,
                  text: last.text + cp.text,
                };
              } else if (cp.text.trim()) {
                // Skip whitespace-only placeholders (e.g. "\n\n") emitted
                // right before a tool call; they would show as an empty step.
                steps.push({ kind: "text", text: cp.text });
              }
            }
            break;
          }

          case "ToolCall": {
            const tc = innerPayload as {
              type: string;
              id: string;
              function: { name: string; arguments: string };
            };
            const initialArgs = tc.function.arguments || "";
            let parsedInput: unknown;
            try {
              parsedInput = JSON.parse(initialArgs || "{}");
            } catch {
              // not valid JSON yet
            }
            steps.push({
              kind: "tool-call",
              toolCallId: tc.id,
              toolName: tc.function.name,
              rawArgs: initialArgs,
              input: parsedInput,
              status: "running",
            });
            break;
          }

          case "ToolCallPart": {
            const tcp = innerPayload as { arguments_part: string };
            // Find the last running tool-call step and append arguments
            for (let i = steps.length - 1; i >= 0; i--) {
              const step = steps[i];
              if (step.kind === "tool-call" && step.status === "running") {
                const newArgs = (step.rawArgs ?? "") + tcp.arguments_part;
                let parsedInput: unknown;
                try {
                  parsedInput = JSON.parse(newArgs);
                } catch {
                  // not complete JSON yet
                }
                steps[i] = {
                  ...step,
                  rawArgs: newArgs,
                  input: parsedInput ?? step.input,
                };
                break;
              }
            }
            break;
          }

          case "ToolResult": {
            const tr = innerPayload as {
              tool_call_id: string;
              return_value: {
                is_error: boolean;
                output: Array<{ text?: string }> | string;
                message: string;
                display?: Array<{ type: string; items?: unknown }>;
              };
            };
            for (let i = steps.length - 1; i >= 0; i--) {
              const step = steps[i];
              if (
                step.kind === "tool-call" &&
                step.toolCallId === tr.tool_call_id
              ) {
                const outputStr = Array.isArray(tr.return_value.output)
                  ? tr.return_value.output
                      .map((p) => p.text ?? "")
                      .filter(Boolean)
                      .join("\n")
                  : tr.return_value.output;
                steps[i] = {
                  ...step,
                  status: tr.return_value.is_error ? "error" : "success",
                  output: outputStr || undefined,
                  errorText: tr.return_value.is_error
                    ? tr.return_value.message || undefined
                    : undefined,
                };
                break;
              }
            }
            // The subagent's own SetTodoList never reaches the Tasks panel
            // (its events arrive wrapped in SubagentEvent), but its progress
            // is exactly what the user is missing while the delegation runs.
            // Surface the latest frame inside the Agent card, replacing any
            // earlier one so the card shows state, not history.
            const subTodoBlock = Array.isArray(tr.return_value.display)
              ? tr.return_value.display.find((d) => d.type === "todo")
              : undefined;
            if (subTodoBlock && Array.isArray(subTodoBlock.items)) {
              // Explicit annotation: without it TS narrows the filter's
              // output to the non-todo members (inferred type predicate),
              // which would make the push below a type error.
              const filtered: SubagentStep[] = steps.filter(
                (s) => s.kind !== "todo",
              );
              filtered.push({
                kind: "todo",
                items: subTodoBlock.items as TodoItem[],
              });
              steps.length = 0;
              steps.push(...filtered);
            }
            break;
          }

          case "StepRetry": {
            const retainedSteps = discardSubagentRetryAttempt(steps);
            steps.length = 0;
            steps.push(...retainedSteps);
            // Say so in the subagent's card: without it, a rate-limited
            // subagent just looked stuck.
            steps.push({
              kind: "retry",
              text: formatStepRetry(innerPayload as StepRetryPayload, null, retryLabels()),
            });
            break;
          }

          case "SubagentEvent": {
            // Nested subagent — deep nesting is rare in practice.
            // For now we skip nested SubagentEvents; the parent subagent's
            // direct tool calls/text/thinking are already captured.
            break;
          }

          case "TurnEnd": {
            // The subagent's own turn ended. Parent Agent tool's ToolResult
            // may or may not follow (missing in the user_cancelled path
            // until the backend compensates). Either way, the subagent is
            // done, so stop showing "Agent working…" for this tool call.
            subagentRunning = false;
            break;
          }
          default:
            // Ignore StepBegin, TurnBegin, StatusUpdate, etc.
            break;
        }

        const next = [...prev];
        next[parentIdx] = {
          ...parentMsg,
          toolCall: {
            ...parentMsg.toolCall!,
            subagentSteps: steps,
            subagentRunning: subagentRunning,
            // Preserve existing values; only set if provided and not yet set
            subagentType:
              parentMsg.toolCall?.subagentType ?? subagentType,
            subagentAgentId:
              parentMsg.toolCall?.subagentAgentId ?? agentId,
          },
        };
        return next;
      });
    },
    [setMessages],
  );

  // Process a single wire event
  const processEvent = useCallback(
    (
      event: WireEvent,
      isReplay = false,
      rpcMessageId?: string | number,
      eventTimestamp?: number,
    ) => {
      switch (event.type) {
        case "TurnBegin": {
          // Reset step state to ensure slash commands create new messages
          clearStepRetryStatus();
          resetStepState();

          // The Tasks panel tracks the session's real todo list, not just
          // this turn's breakdown: TurnBegin carries the persisted list so a
          // follow-up turn keeps showing live progress instead of starting
          // blind. A fully-done snapshot is completed work, not progress —
          // start empty so it doesn't read as the plan for the new turn.
          // A partially-done snapshot is last-known state from earlier turns:
          // honest to show, but marked "carried" until the agent calls
          // SetTodoList for the work at hand, so a leftover list can't
          // masquerade as this turn's live progress.
          const snapshot = event.payload.todos;
          if (Array.isArray(snapshot)) {
            const items: TodoItem[] = snapshot.map((t) => ({
              title: t.title,
              status: t.status,
            }));
            if (items.some((i) => i.status !== "done")) {
              useToolEventsStore.getState().setTodoItems(items, "carried");
            } else {
              useToolEventsStore.getState().clearTodoItems();
            }
          } else {
            useToolEventsStore.getState().clearTodoItems();
          }

          const parsedUserInput = parseUserInput(event.payload.user_input);

          // Track turn index for fork feature
          const currentTurnIndex = turnCounterRef.current;
          turnCounterRef.current += 1;

          // Track that at least one turn has started (for auto-rename trigger)
          if (!isReplay) {
            hasTurnStartedRef.current = true;
          }

          // Check if this is a /clear or /reset command (needs UI clear)
          const userText = parsedUserInput.text.trim();
          pendingClearRef.current =
            userText === "/clear" || userText === "/reset";

          // A turn the agent started for itself — woken by a finished
          // background task — carries a system reminder as its input. It is
          // not something the user said, so it gets no user bubble; what the
          // agent then reports is the visible part.
          if (isSystemReminderOnly(parsedUserInput.text)) {
            break;
          }

          // Add user message
          const userMessageId = getNextMessageId("user");
          const userMessage: LiveMessage = {
            id: userMessageId,
            role: "user",
            turnIndex: currentTurnIndex,
            content:
              parsedUserInput.text ||
              (parsedUserInput.attachments.length > 0
                ? ""
                : safeStringify(event.payload.user_input ?? "")),
            attachments:
              parsedUserInput.attachments.length > 0
                ? parsedUserInput.attachments
                : undefined,
          };

          upsertMessage(userMessage);
          break;
        }

        case "UserReply": {
          // What the user typed when answering an approval or a question. It
          // reached the model inside a tool result, so without this the
          // transcript showed the agent asking and then carrying on, with the
          // user's own words nowhere in it.
          const text = event.payload.text?.trim();
          if (text) {
            upsertMessage({
              id: getNextMessageId("user"),
              role: "user",
              // The turn this reply belongs to is the one already running.
              // turnCounterRef was incremented by TurnBegin, so reading it
              // raw tags the reply with the turn that has not started yet —
              // the same off-by-one the assistant text avoids by subtracting.
              turnIndex: Math.max(0, turnCounterRef.current - 1),
              content: text,
            });
          }
          break;
        }

        case "TurnEnd": {
          // A turn stopped while it was retrying records only this: the
          // cancel's reply that clears the retry line live is not in the
          // history, so a replay would show "retry 11" for a turn long over.
          clearStepRetryStatus();
          // The turn ended early — whatever the todo list shows is a frozen
          // mid-task frame, not a finish line. Mark it so the panel says
          // "interrupted" instead of silently pinning stale progress. A later
          // SetTodoList (live or replayed) clears the mark on its own.
          if (event.payload?.interrupt_reason) {
            useToolEventsStore.getState().markTodoStale("interrupted");
          }
          break;
        }

        case "StepBegin": {
          setCurrentStep(event.payload.n);
          clearStepRetryStatus();
          if (thinkingMessageIdRef.current) {
            const thinkingId = thinkingMessageIdRef.current;
            setMessages((prev) => endThinking(prev, thinkingId));
          }
          resetStepState();
          if (!isReplay) {
            setStatus("streaming");
          }
          break;
        }

        case "StepRetry": {
          discardRetryAttemptMessages();
          showStepRetryStatus(event.payload, isReplay);
          if (!isReplay) {
            clearAwaitingFirstResponse();
            setStatus("streaming");
          }
          break;
        }

        case "ContentPart": {
          clearStepRetryStatus();
          if (!isReplay) {
            clearAwaitingFirstResponse();
          }
          if (event.payload.type === "think" && event.payload.think) {
            // Accumulate thinking content
            currentThinkingRef.current += event.payload.think;

            // Create or update thinking message
            if (!thinkingMessageIdRef.current) {
              thinkingMessageIdRef.current = getNextMessageId("assistant");
              const thinkingMsg: LiveMessage = {
                id: thinkingMessageIdRef.current!,
                role: "assistant",
                variant: "thinking",
                thinking: currentThinkingRef.current,
                isStreaming: !isReplay,
              };
              if (textMessageIdRef.current) {
                // Text message already exists, insert thinking before it
                setMessages((prev) => {
                  const textIdx = prev.findIndex(
                    (m) => m.id === textMessageIdRef.current,
                  );
                  if (textIdx !== -1) {
                    const next = [...prev];
                    next.splice(textIdx, 0, thinkingMsg);
                    return next;
                  }
                  return [...prev, thinkingMsg];
                });
              } else {
                upsertMessage(thinkingMsg);
              }
            } else {
              setMessages((prev) =>
                prev.map((msg) =>
                  msg.id === thinkingMessageIdRef.current
                    ? { ...msg, thinking: currentThinkingRef.current }
                    : msg,
                ),
              );
            }
          } else if (event.payload.type === "text" && event.payload.text) {
            if (thinkingMessageIdRef.current) {
              const thinkingId = thinkingMessageIdRef.current;
              setMessages((prev) => endThinking(prev, thinkingId));
            }

            // Accumulate text content
            currentTextRef.current += event.payload.text;

            // Models often emit a whitespace-only placeholder (e.g. "\n\n")
            // right before a tool call. Don't materialize a text message until
            // it carries visible text, otherwise an empty bubble is rendered.
            // Whitespace is still accumulated above so paragraph breaks and
            // indentation arriving in earlier chunks are preserved.
            if (!(textMessageIdRef.current || currentTextRef.current.trim())) {
              break;
            }

            // Create or update text message
            if (!textMessageIdRef.current) {
              textMessageIdRef.current = getNextMessageId("assistant");
              upsertMessage({
                id: textMessageIdRef.current!,
                role: "assistant",
                variant: "text",
                turnIndex: turnCounterRef.current > 0 ? turnCounterRef.current - 1 : undefined,
                content: currentTextRef.current,
                completedAt: eventTimestamp,
                isStreaming: !isReplay,
              });
            } else {
              setMessages((prev) =>
                setMessageOutputTime(
                  prev.map((msg) =>
                    msg.id === textMessageIdRef.current
                      ? { ...msg, content: currentTextRef.current }
                      : msg,
                  ),
                  textMessageIdRef.current,
                  eventTimestamp,
                ),
              );
            }
          }
          break;
        }

        case "ToolCall": {
          clearStepRetryStatus();
          if (!isReplay) {
            clearAwaitingFirstResponse();
          }
          if (thinkingMessageIdRef.current) {
            const thinkingId = thinkingMessageIdRef.current;
            setMessages((prev) => endThinking(prev, thinkingId));
          }
          const toolCall = event.payload;
          currentToolCallIdRef.current = toolCall.id;

          // Initialize tool call state
          const initialArgs = toolCall.function.arguments || "";
          currentToolCallsRef.current.set(toolCall.id, {
            id: toolCall.id,
            name: toolCall.function.name,
            arguments: initialArgs,
            argumentsComplete: false,
            messageId: undefined,
          });

          // Parse initial arguments if available
          let parsedInput: unknown;
          if (initialArgs) {
            try {
              parsedInput = JSON.parse(initialArgs);
            } catch {
              // Not valid JSON yet, leave as undefined
            }
          }

          // Create tool message
          const toolMessageId = getNextMessageId("assistant");
          upsertMessage({
            id: toolMessageId,
            role: "assistant",
            variant: "tool",
            toolCall: {
              title: toolCall.function.name,
              type: "tool-call" as ToolUIPart["type"],
              state: "input-streaming" as ToolUIPart["state"],
              toolCallId: toolCall.id,
              input: parsedInput,
            },
            isStreaming: !isReplay,
          });

          // Store message ID in tool call state for later updates
          const tc = currentToolCallsRef.current.get(toolCall.id);
          if (tc) {
            tc.messageId = toolMessageId;
          }
          break;
        }

        case "ToolCallPart": {
          if (currentToolCallIdRef.current) {
            const tc = currentToolCallsRef.current.get(
              currentToolCallIdRef.current,
            );
            if (tc) {
              tc.arguments += event.payload.arguments_part;

              const messageId = tc.messageId;
              if (messageId) {
                let parsedInput: unknown = tc.arguments;
                try {
                  parsedInput = JSON.parse(tc.arguments);
                } catch {
                  // Not complete JSON yet
                }

                setMessages((prev) =>
                  prev.map((msg) => {
                    if (msg.id !== messageId || !msg.toolCall) return msg;
                    // Never downgrade a pending approval back to
                    // "input-available" — a late/duplicate arguments part
                    // would otherwise silently dismiss the approval dialog
                    // while the backend is still waiting for an answer.
                    if (
                      msg.toolCall.state === "approval-requested" &&
                      !msg.toolCall.approval?.submitted
                    ) {
                      return msg;
                    }
                    return {
                      ...msg,
                      toolCall: {
                        ...msg.toolCall,
                        state: "input-available" as ToolUIPart["state"],
                        input: parsedInput,
                      },
                    };
                  }),
                );
              }
            }
          }
          break;
        }

        case "ToolResult": {
          clearStepRetryStatus();
          if (!isReplay) {
            clearAwaitingFirstResponse();
          }
          const { tool_call_id, return_value } = event.payload;
          const tc = currentToolCallsRef.current.get(tool_call_id);

          const outputStr = Array.isArray(return_value.output)
            ? return_value.output
                .map((part) => part.text ?? "")
                .filter(Boolean)
                .join("\n")
            : return_value.output;

          // Extract media parts (image_url/video_url) from output array
          let mediaParts: Array<{ type: "image_url" | "video_url"; url: string }> = [];
          if (Array.isArray(return_value.output)) {
            mediaParts = return_value.output
              .filter((part: Record<string, unknown>) => part.type === "image_url" || part.type === "video_url")
              .map((part: Record<string, unknown>) => ({
                type: part.type as "image_url" | "video_url",
                url: extractMediaUrl(part),
              }))
              .filter((p) => p.url);

            // For non-browser-renderable URLs (e.g. ms:// from Kimo model),
            // try to construct serving URLs from file paths in text output tags
            const hasNonBrowserUrl = mediaParts.some((p) => !isBrowserUrl(p.url));
            if (hasNonBrowserUrl) {
              const textOutput = return_value.output
                .map((p: Record<string, unknown>) => (p.text as string) ?? "")
                .filter(Boolean)
                .join("");
              // Collect all API URLs from media tags in order
              const apiUrls: string[] = [];
              for (const match of textOutput.matchAll(MEDIA_TAG_PATH_REGEX)) {
                const [, , sid, filename] = match;
                apiUrls.push(`/api/sessions/${sid}/uploads/${encodeURIComponent(filename)}`);
              }
              if (apiUrls.length > 0) {
                let apiIdx = 0;
                mediaParts = mediaParts.map((p) => {
                  if (isBrowserUrl(p.url)) return p;
                  const url = apiUrls[apiIdx] ?? apiUrls[apiUrls.length - 1];
                  apiIdx++;
                  return { ...p, url };
                });
              }
            }
          }

          const messageStr = return_value.message;

          if (tc) {
            tc.argumentsComplete = true;
            tc.result = {
              isError: return_value.is_error,
              output: outputStr || undefined,
              message: messageStr || undefined,
            };
          }

          // Match message by toolCallId directly - this is robust against:
          // 1. Out-of-order ToolResult (parallel tool calls)
          // 2. Missing tc.messageId (race conditions)
          // 3. Replay mode (messages already have toolCallId)
          setMessages((prev) =>
            prev.map((msg) => {
              if (msg.toolCall?.toolCallId !== tool_call_id) return msg;
              return {
                ...msg,
                toolCall: {
                  ...msg.toolCall,
                  state: return_value.is_error
                    ? ("output-error" as ToolUIPart["state"])
                    : ("output-available" as ToolUIPart["state"]),
                  // Aligned with backend ToolReturnValue
                  output: outputStr || undefined,
                  message: messageStr || undefined,
                  display: return_value.display,
                  extras: return_value.extras,
                  isError: return_value.is_error,
                  errorText: return_value.is_error
                    ? messageStr || undefined
                    : undefined,
                  mediaParts: mediaParts.length > 0 ? mediaParts : undefined,
                  // Mark subagent as complete when its parent Agent tool receives result
                  subagentRunning: msg.toolCall.subagentSteps
                    ? false
                    : msg.toolCall.subagentRunning,
                },
                isStreaming: false,
              };
            }),
          );

          if (currentToolCallIdRef.current === tool_call_id) {
            currentToolCallIdRef.current = null;
          }

          // Handle tool-specific events (e.g., WriteFile → new files notification)
          if (tc) {
            handleToolResult(
              tc.name,
              tc.arguments,
              return_value.is_error,
              isReplay,
            );
          }

          // Extract todo list from display blocks.  Also run during replay:
          // the wire is replayed in order, so the LAST todo block wins and
          // restores the latest task state when (re)opening a session —
          // otherwise the Tasks panel stays empty/stale after a session
          // switch until the agent happens to call SetTodoList again.
          if (Array.isArray(return_value.display)) {
            const todoBlock = return_value.display.find(
              (d: { type: string }) => d.type === "todo",
            );
            if (todoBlock) {
              const items = (todoBlock as unknown as { items: TodoItem[] })
                .items;
              // A fresh SetTodoList frame is live progress by definition —
              // it also clears any "interrupted"/"carried" mark, whether it
              // arrives live or via replay. A fully-done list is completed
              // work, not progress: once every item is done the panel has
              // nothing left to show, so drop it instead of pinning a
              // finished checklist to the session.
              if (items.length > 0 && items.every((i) => i.status === "done")) {
                useToolEventsStore.getState().clearTodoItems();
              } else {
                useToolEventsStore.getState().setTodoItems(items);
              }
            }
          }
          break;
        }

        case "ApprovalRequest": {
          if (!isReplay) {
            clearAwaitingFirstResponse();
          }
          const payload = (event as ApprovalRequestEvent).payload;
          const tc = currentToolCallsRef.current.get(payload.tool_call_id);
          const existingPending = pendingApprovalRequestsRef.current.get(
            payload.id,
          );

          // Reconnect snapshots and compatibility pushes can duplicate a request
          // that the user already answered, or arrive after its resolution event.
          // Never make either state actionable again.
          if (tc?.approval?.resolved || tc?.result !== undefined) {
            pendingApprovalRequestsRef.current.delete(payload.id);
            break;
          }
          if (existingPending?.submitted && tc?.messageId) {
            break;
          }

          const approvalState = {
            id: payload.id,
            action: payload.action,
            description: payload.description,
            sender: payload.sender,
            toolCallId: payload.tool_call_id,
            rpcMessageId,
            submitted: false,
            resolved: false,
            sourceKind: payload.source_kind ?? null,
            sourceDescription: payload.source_description ?? null,
          };

          if (tc) {
            tc.approval = approvalState;
          } else {
            const fallbackState: ToolCallState = {
              id: payload.tool_call_id,
              name: payload.action,
              arguments: "",
              argumentsComplete: false,
              messageId: undefined,
              approval: approvalState,
            };
            currentToolCallsRef.current.set(
              payload.tool_call_id,
              fallbackState,
            );
          }

          let messageId = tc?.messageId;

          const approvalDisplay = payload.display?.length
            ? payload.display
            : undefined;

          if (messageId) {
            updateMessageById(messageId, (message) => {
              if (!message.toolCall) {
                return message;
              }
              return {
                ...message,
                isStreaming: false,
                toolCall: {
                  ...message.toolCall,
                  state: "approval-requested",
                  approval: approvalState,
                  // Show approval preview (diff/command) if tool has no display yet
                  display: message.toolCall.display ?? approvalDisplay,
                },
              };
            });
          } else {
            const isSubagentOrigin = Boolean(payload.agent_id);
            const fallbackMessageId = getNextMessageId("assistant");
            const approvalMessage: LiveMessage = {
              id: fallbackMessageId,
              role: "assistant",
              variant: "tool",
              isStreaming: false,
              toolCall: {
                title: payload.action,
                type: "tool-call" as ToolUIPart["type"],
                state: "approval-requested",
                approval: approvalState,
                display: approvalDisplay,
                ...(isSubagentOrigin && {
                  isSubagentOrigin: true,
                  subagentType: payload.subagent_type ?? undefined,
                  subagentAgentId: payload.agent_id ?? undefined,
                }),
              },
            };

            currentToolCallsRef.current.set(payload.tool_call_id, {
              ...(currentToolCallsRef.current.get(payload.tool_call_id) ?? {
                id: payload.tool_call_id,
                name: payload.action,
                arguments: "",
                argumentsComplete: false,
              }),
              messageId: fallbackMessageId,
            });

            setMessages((prev) => [...prev, approvalMessage]);
            messageId = fallbackMessageId;
          }

          pendingApprovalRequestsRef.current.set(payload.id, {
            requestId: payload.id,
            toolCallId: payload.tool_call_id,
            messageId,
            rpcId: rpcMessageId,
            submitted: false,
          });

          break;
        }

        case "ApprovalRequestResolved": {
          const { request_id, response, feedback } =
            event.payload as ApprovalRequestResolvedEvent["payload"];
          const pending = pendingApprovalRequestsRef.current.get(request_id);

          let tc: ToolCallState | undefined;

          if (pending) {
            tc = currentToolCallsRef.current.get(pending.toolCallId);
          }

          if (!tc) {
            for (const entry of currentToolCallsRef.current.values()) {
              if (entry.approval?.id === request_id) {
                tc = entry;
                break;
              }
            }
          }

          const approval = tc?.approval ?? {
            id: request_id,
            action: "",
            description: "",
            sender: "",
            toolCallId: pending?.toolCallId ?? "",
          };

          let approved: boolean | undefined;
          let reason: string | undefined;

          if (typeof response === "boolean") {
            approved = response;
          } else if (response && typeof response === "object") {
            const candidate = response as {
              approved?: unknown;
              reason?: unknown;
            };
            if (typeof candidate.approved === "boolean") {
              approved = candidate.approved;
            }
            if (typeof candidate.reason === "string") {
              reason = candidate.reason;
            }
          } else if (typeof response === "string") {
            const normalizedResponse = response.toLowerCase();
            if (
              normalizedResponse === "approve" ||
              normalizedResponse === "approve_for_session" ||
              normalizedResponse === "approval" ||
              normalizedResponse === "approved"
            ) {
              approved = true;
            } else if (normalizedResponse === "reject") {
              approved = false;
            } else {
              reason = response;
            }
          }

          const effectiveReason = reason ?? feedback ?? approval.reason;
          const updatedApproval = {
            ...approval,
            response,
            resolved: true,
            submitted: true,
            approved,
            reason: effectiveReason,
          };

          if (tc) {
            tc.approval = updatedApproval;
          }

          const messageId = tc?.messageId ?? pending?.messageId;
          const nextState =
            approved === false ? "output-denied" : "input-available";
          const nextStreaming = approved !== false;

          if (messageId) {
            updateMessageById(messageId, (message) => {
              if (!message.toolCall) {
                return message;
              }

              // Don't overwrite terminal states — a late ApprovalRequestResolved
              // arriving after cancel() must not flip a denied tool back to active.
              const currentState = message.toolCall.state;
              if (
                currentState === "output-denied" ||
                currentState === "output-interrupted" ||
                currentState === "output-available" ||
                currentState === "output-error"
              ) {
                return {
                  ...message,
                  toolCall: {
                    ...message.toolCall,
                    approval: updatedApproval,
                  },
                };
              }

              return {
                ...message,
                isStreaming: nextStreaming,
                toolCall: {
                  ...message.toolCall,
                  state: nextState,
                  approval: updatedApproval,
                  errorText:
                    approved === false
                      ? (updatedApproval.reason ?? message.toolCall.errorText)
                      : message.toolCall.errorText,
                },
              };
            });
          }

          if (pending) {
            pendingApprovalRequestsRef.current.delete(pending.requestId);
          } else {
            pendingApprovalRequestsRef.current.delete(request_id);
          }

          break;
        }

        case "QuestionRequest": {
          if (!isReplay) {
            clearAwaitingFirstResponse();
          }
          const qPayload = (event as QuestionRequestEvent).payload;
          const qtc = currentToolCallsRef.current.get(qPayload.tool_call_id);
          const wasDismissed = dismissedQuestionsRef.current.has(qPayload.id);
          const questionKind = classifyQuestion({
            isReplay,
            wasDismissed,
          });

          const questionState = {
            id: qPayload.id,
            toolCallId: qPayload.tool_call_id,
            questions: qPayload.questions,
            rpcMessageId,
            submitted: questionKind.submitted,
            resolved: questionKind.resolved,
          };

          let qMessageId = qtc?.messageId;

          if (qMessageId) {
            updateMessageById(qMessageId, (message) => {
              if (!message.toolCall) {
                return message;
              }
              return {
                ...message,
                isStreaming: false,
                toolCall: {
                  ...message.toolCall,
                  state: questionKind.state,
                  question: questionState,
                },
              };
            });
          } else {
            const fallbackMessageId = getNextMessageId("assistant");
            const questionMessage: LiveMessage = {
              id: fallbackMessageId,
              role: "assistant",
              variant: "tool",
              isStreaming: false,
              toolCall: {
                title: "AskUserQuestion",
                type: "tool-call" as ToolUIPart["type"],
                state: questionKind.state,
                question: questionState,
              },
            };

            currentToolCallsRef.current.set(qPayload.tool_call_id, {
              ...(currentToolCallsRef.current.get(qPayload.tool_call_id) ?? {
                id: qPayload.tool_call_id,
                name: "AskUserQuestion",
                arguments: "",
                argumentsComplete: false,
              }),
              messageId: fallbackMessageId,
            });

            setMessages((prev) => [...prev, questionMessage]);
            qMessageId = fallbackMessageId;
          }

          if (!questionKind.actionable) {
            // Closed earlier via "Stop run" and the turn that asked is gone:
            // nobody can answer it, so don't register it as pending. A
            // replayed question is in the same position until the server
            // confirms it is still waiting for an answer.
            break;
          }

          pendingQuestionRequestsRef.current.set(qPayload.id, {
            requestId: qPayload.id,
            toolCallId: qPayload.tool_call_id,
            messageId: qMessageId,
            rpcId: rpcMessageId,
            submitted: false,
          });

          break;
        }

        case "SubagentEvent": {
          const subPayload = (event as SubagentEventWire).payload;
          // Wire 1.6 uses parent_tool_call_id; fall back to legacy task_tool_call_id
          const parentToolCallId =
            subPayload.parent_tool_call_id ??
            (subPayload as Record<string, unknown>).task_tool_call_id as string | undefined;
          if (parentToolCallId) {
            processSubagentEvent(
              parentToolCallId,
              subPayload.event.type,
              subPayload.event.payload,
              subPayload.agent_id ?? undefined,
              subPayload.subagent_type ?? undefined,
            );
          }
          break;
        }

        case "StatusUpdate": {
          clearStepRetryStatus();
          const nextContextUsage = event.payload.context_usage;
          if (typeof nextContextUsage === "number") {
            setContextUsage(nextContextUsage);
          }

          const nextTokenUsage = event.payload.token_usage;
          if (nextTokenUsage) {
            setTokenUsage(nextTokenUsage);
          }

          const nextPlanMode = event.payload.plan_mode;
          if (typeof nextPlanMode === "boolean") {
            setPlanMode(nextPlanMode);
          }

          const nextYolo = event.payload.yolo;
          if (typeof nextYolo === "boolean") {
            setYolo(nextYolo);
          }

          const nextMcpStatus = event.payload.mcp_status;
          if (nextMcpStatus) {
            mcpStatusRef.current = nextMcpStatus;
          }

          const nextWaitingOnBackground = event.payload.waiting_on_background;
          if (typeof nextWaitingOnBackground === "boolean") {
            setWaitingOnBackground(nextWaitingOnBackground);
          }

          // Server-side thinking suppression (e.g. Kimo-for-Coding skipping
          // reasoning for large contexts): surface a placeholder thinking
          // block so the empty reasoning is explained instead of invisible.
          if (event.payload.thinking_suppressed === true) {
            if (!thinkingMessageIdRef.current) {
              thinkingMessageIdRef.current = getNextMessageId("assistant");
              const thinkingMsg: LiveMessage = {
                id: thinkingMessageIdRef.current!,
                role: "assistant",
                variant: "thinking",
                thinking: "",
                thinkingSuppressed: true,
                isStreaming: !isReplay,
              };
              if (textMessageIdRef.current) {
                // Text message already exists, insert thinking before it
                setMessages((prev) => {
                  const textIdx = prev.findIndex(
                    (m) => m.id === textMessageIdRef.current,
                  );
                  if (textIdx !== -1) {
                    const next = [...prev];
                    next.splice(textIdx, 0, thinkingMsg);
                    return next;
                  }
                  return [...prev, thinkingMsg];
                });
              } else {
                upsertMessage(thinkingMsg);
              }
            }
          }

          // If we have a message_id, create a special message to display it
          const messageId = event.payload.message_id;
          if (messageId) {
            const displayMessageId = getNextMessageId("assistant");
            upsertMessage({
              id: displayMessageId,
              role: "assistant",
              variant: "message-id",
              messageId,
            });
          }

          // Clear UI for /clear command (triggered by StatusUpdate after clear)
          if (pendingClearRef.current) {
            pendingClearRef.current = false;
            setMessages((prev) => {
              let lastUserMsgIndex = -1;
              for (let i = prev.length - 1; i >= 0; i--) {
                if (prev[i].role === "user") {
                  lastUserMsgIndex = i;
                  break;
                }
              }
              return lastUserMsgIndex >= 0 ? prev.slice(lastUserMsgIndex) : [];
            });
          }
          break;
        }

        case "SessionNotice": {
          if (!isReplay) {
            clearAwaitingFirstResponse();
          }
          if (event.payload.text) {
            setMessages((prev) => [
              ...prev,
              {
                id: getNextMessageId("assistant"),
                role: "assistant",
                variant: "status",
                content: event.payload.text,
              },
            ]);
          }
          break;
        }

        case "StepInterrupted": {
          clearStepRetryStatus();
          // Clear pending approval and question requests
          pendingApprovalRequestsRef.current.clear();
          pendingQuestionRequestsRef.current.clear();

          setMessages((prev) =>
            prev.map((msg) => {
              let updated = msg;
              if (msg.isStreaming) {
                updated = { ...updated, isStreaming: false };
              }
              // Mark subagent as no longer running
              if (msg.toolCall?.subagentRunning) {
                updated = {
                  ...updated,
                  toolCall: {
                    ...updated.toolCall!,
                    subagentRunning: false,
                  },
                };
              }
              // Update pending approval tool states to denied
              if (
                msg.variant === "tool" &&
                msg.toolCall?.state === "approval-requested"
              ) {
                return {
                  ...updated,
                  toolCall: {
                    ...msg.toolCall,
                    ...updated.toolCall,
                    state: "output-denied",
                    approval: msg.toolCall.approval
                      ? {
                          ...msg.toolCall.approval,
                          submitted: true,
                          resolved: true,
                          approved: false,
                          response: "reject",
                        }
                      : undefined,
                  },
                };
              }
              // Update pending question tool states to responded
              if (
                msg.variant === "tool" &&
                msg.toolCall?.state === "question-requested"
              ) {
                return {
                  ...updated,
                  toolCall: {
                    ...msg.toolCall,
                    ...updated.toolCall,
                    state: "question-responded",
                    question: msg.toolCall.question
                      ? {
                          ...msg.toolCall.question,
                          submitted: true,
                          resolved: true,
                        }
                      : undefined,
                  },
                };
              }
              // Mark still-running tool calls as interrupted
              if (
                msg.variant === "tool" &&
                (updated.toolCall?.state === "input-streaming" ||
                  updated.toolCall?.state === "input-available")
              ) {
                return {
                  ...updated,
                  toolCall: {
                    ...updated.toolCall,
                    state: "output-denied",
                  },
                };
              }
              return updated;
            }),
          );
          setAwaitingFirstResponse(false);
          if (awaitingIdleRef.current) {
            setStatus("submitted");
          } else {
            setStatus("ready");
          }
          break;
        }

        case "CompactionBegin": {
          const compactionMsgId = getNextMessageId("assistant");
          compactionMessageIdRef.current = compactionMsgId;
          setMessages((prev) => [
            ...prev,
            {
              id: compactionMsgId,
              role: "assistant",
              variant: "status",
              content: "Compacting conversation history…",
              isStreaming: !isReplay,
            },
          ]);
          break;
        }

        case "CompactionRetry": {
          // A compact attempt failed and will be retried. Without this, the
          // user sees a "Compacting..." spinner with no live update while
          // the backend burns through its retry budget — the previous
          // behaviour was to update only the Python log, leaving the UI
          // looking stuck even when retry 21 of an uncapped loop was
          // already in flight. The retry is capped at the backend now,
          // so this is a bounded progress update rather than a spin.
          const retry = event.payload;
          const compactMsgId = compactionMessageIdRef.current;
          if (compactMsgId) {
            const attemptsText = retry.max_attempts > 0
              ? `attempt ${retry.next_attempt} of ${retry.max_attempts}`
              : `attempt ${retry.next_attempt}`;
            const waitText = retry.wait_s > 0
              ? `retrying in ${Math.round(retry.wait_s * 10) / 10}s`
              : "retrying now";
            setMessages((prev) =>
              prev.map((m) =>
                m.id === compactMsgId
                  ? {
                      ...m,
                      content: `Compacting conversation history… (${attemptsText}, ${retry.error_type}; ${waitText})`,
                      isStreaming: !isReplay,
                    }
                  : m,
              ),
            );
          }
          break;
        }

        case "CompactionEnd": {
          const compactMsgId = compactionMessageIdRef.current;
          compactionMessageIdRef.current = null;
          // `success` defaults to true so older backends that don't emit it
          // still see the success text; new backends pass success=false when
          // the compaction gave up, which lets us say so instead of
          // claiming "compacted" on a session whose history is untouched.
          const success = event.payload?.success ?? true;
          // Keep the conversation visible: history is preserved in wire.jsonl,
          // so just flip the compaction indicator to a finished state instead
          // of clearing all previous messages.
          setMessages((prev) =>
            prev.map((m) =>
              m.id === compactMsgId
                ? {
                    ...m,
                    content: success
                      ? "Conversation history compacted."
                      : "Compaction failed after retries; the session continues with the un-compacted history.",
                    isStreaming: false,
                  }
                : m,
            ),
          );
          break;
        }

        case "MCPLoadingBegin": {
          const mcpMsgId = getNextMessageId("assistant");
          mcpLoadingMessageIdRef.current = mcpMsgId;
          setMessages((prev) => [
            ...prev,
            {
              id: mcpMsgId,
              role: "assistant",
              variant: "status",
              content: "Connecting to MCP servers…",
              isStreaming: true,
            },
          ]);
          break;
        }

        case "MCPLoadingEnd": {
          const mcpMsgId = mcpLoadingMessageIdRef.current;
          mcpLoadingMessageIdRef.current = null;
          if (!mcpMsgId) break;
          // A server that never connected takes its tools with it for the rest
          // of the session, and nothing else would ever mention it — so the
          // placeholder turns into the report instead of vanishing. When every
          // server came up, it vanishes as before.
          const summary = summarizeMcpStatus(mcpStatusRef.current);
          setMessages((prev) =>
            summary
              ? prev.map((m) =>
                  m.id === mcpMsgId
                    ? { ...m, content: summary.text, isStreaming: false }
                    : m,
                )
              : prev.filter((m) => m.id !== mcpMsgId),
          );
          break;
        }

        case "PlanDisplay": {
          const planPayload = (event as PlanDisplayEvent).payload;
          const planMessageId = getNextMessageId("assistant");
          upsertMessage({
            id: planMessageId,
            role: "assistant",
            variant: "text",
            turnIndex:
              turnCounterRef.current > 0
                ? turnCounterRef.current - 1
                : undefined,
            content: planPayload.content,
            isStreaming: false,
          });
          break;
        }

        default:
          break;
      }

      // Broadcast to plugin system (side-channel, does not affect main logic)
      // Uses ref to avoid stale closure — WebSocket handler chain captures processEvent once
      if (sessionId) {
        pluginBridgeRef.current?.(event, { sessionId, isReplay });
      }
    },
    [
      getNextMessageId,
      setMessages,
      resetStepState,
      clearStepRetryStatus,
      discardRetryAttemptMessages,
      showStepRetryStatus,
      upsertMessage,
      parseUserInput,
      safeStringify,
      clearAwaitingFirstResponse,
      updateMessageById,
      setAwaitingFirstResponse,
      processSubagentEvent,
      sessionId,
    ],
  );

  // Helper to send initialize message
  const sendInitialize = useCallback((ws: WebSocket) => {
    const id = uuidV4();
    initializeIdRef.current = id;
    const message = {
      jsonrpc: "2.0",
      method: "initialize",
      id,
      params: {
        protocol_version: WIRE_PROTOCOL_VERSION,
        client: {
          name: "kiwi",
          version: kimoCliVersion,
        },
        capabilities: {
          supports_question: true,
          supports_plan_mode: true,
        },
      },
    };
    ws.send(JSON.stringify(message));
    console.log("[SessionStream] Sent initialize message");
  }, []);

  // Handle incoming WebSocket message
  const handleMessage = useCallback(
    (data: string) => {
      try {
        if (deliveryWindowRef.current.checkAndAdd(data)) {
          // Diagnostics for the "reply rendered twice until the page is
          // revisited" bug: the identical raw frame reached the client
          // twice. Log only — merging must stay untouched until the path
          // that double-delivers is pinned.
          console.warn(
            "[SessionStream] exact duplicate frame delivered:",
            data.slice(0, 200),
          );
        }
        const message: WireMessage = JSON.parse(data);

        if (
          pendingConversationResetRef.current &&
          startsIncomingConversation(message, initializeIdRef.current)
        ) {
          // Same tick as the append below, so React renders the replacement
          // without ever painting the empty list in between.
          pendingConversationResetRef.current = false;
          console.info(
            "[SessionStream] conversation reset on replay, first content message:",
            (message.params as { type?: string } | undefined)?.type ??
              message.method ??
              "?",
          );
          setMessages([]);
        }

        const promptText =
          typeof message.id === "string" ? sentPromptsRef.current.get(message.id) : undefined;
        if (promptText !== undefined && (message.error || message.result)) {
          sentPromptsRef.current.delete(message.id as string);
          if (message.error?.code === SESSION_BUSY_ERROR_CODE) {
            // Refused, not failed: the session is still on an earlier turn,
            // as its own status will say. Seen when a dropped socket read as
            // the turn ending and the queue sent its next message — which was
            // then gone from the queue and from the conversation alike.
            console.log("[SessionStream] Prompt refused while busy; handing it back");
            setAwaitingFirstResponse(false);
            onPromptRejectedRef.current?.(promptText);
            return;
          }
        }

        // Check for JSON-RPC error response
        if (message.error) {
          // Initialize failure during busy session is non-fatal - retry after delay
          if (message.id === initializeIdRef.current) {
            initializeRetryCountRef.current += 1;

            if (initializeRetryCountRef.current > MAX_INITIALIZE_RETRIES) {
              initializeIdRef.current = null;
              initializeRetryCountRef.current = 0;
              return;
            }

            initializeIdRef.current = null;

            // Auto-retry initialize after 2 seconds, but only if the socket
            // whose initialize failed is still the active one — a stale timer
            // must not send initialize on a newer socket.
            const failedWs = wsRef.current;
            setTimeout(() => {
              if (
                failedWs &&
                wsRef.current === failedWs &&
                failedWs.readyState === WebSocket.OPEN
              ) {
                sendInitialize(failedWs);
              }
            }, 2000);

            return;
          }

          // Other errors remain fatal
          console.error("[SessionStream] Received error:", message.error);
          const err = new Error(message.error.message || "Unknown error");
          setError(err);
          onError?.(err);
          setStatus("error");
          clearStepRetryStatus();
          setAwaitingFirstResponse(false);
          awaitingIdleRef.current = false;
          resolveStaleCompaction("Compaction failed.");
          // Mark all streaming/subagent messages as complete
          completeStreamingMessages();
          return;
        }

        if (message.method === "session_status") {
          if (historyCompleteTimeoutRef.current) {
            window.clearTimeout(historyCompleteTimeoutRef.current);
            historyCompleteTimeoutRef.current = null;
          }
          // A session_status message is the server's confirmation that this
          // socket is actually usable. Reset the reconnect budget here, not
          // on onopen — an open handshake that never reaches session_status
          // still counts against the budget, so Bug A/B (silent close with
          // no session_status) can exhaust it and stop instead of looping.
          reconnectAttemptRef.current = 0;
          applySessionStatus(message.params as SessionStatusPayload);
          return;
        }

        // Check for finished or cancelled status
        if (
          message.result?.status === "finished" ||
          message.result?.status === "cancelled"
        ) {
          console.log(
            `[SessionStream] Stream ${message.result.status}`,
          );
          setStatus("ready");
          clearStepRetryStatus();
          setAwaitingFirstResponse(false);
          awaitingIdleRef.current = false;
          isReplayingRef.current = false;
          setIsReplayingHistory(false);
          setHistoryLoadedFor(sessionIdRef.current ?? null);
          resolveStaleCompaction("Compaction did not complete.");
          completeStreamingMessages();
          return;
        }

        // Check for replay_complete marker (custom event from server)
        if (
          message.method === "event" &&
          (message.params as { type?: string })?.type === "ReplayComplete"
        ) {
          console.log("[SessionStream] Replay complete");
          isReplayingRef.current = false;
          setIsReplayingHistory(false);
          setHistoryLoadedFor(sessionIdRef.current ?? null);
          setStatus("ready");
          awaitingIdleRef.current = false;
          return;
        }

        // Check for history_complete - history loaded but environment not ready yet
        // This allows showing history while SSH connection is being established
        if (message.method === "history_complete") {
          console.log(
            "[SessionStream] History loaded, waiting for environment...",
          );
          isReplayingRef.current = false;
          setHistoryLoadedFor(sessionIdRef.current ?? null);
          // Keep status as "submitted" - input stays disabled until session_status
          setStatus((current) => (current === "ready" ? current : "submitted"));

          // Timeout fallback: reconnect if session_status not received within 15s.
          // Route through decideOnClose so the attempt budget still applies.
          // A server that never delivers session_status (e.g. a wedged worker
          // on Windows' ProactorEventLoop where returncode never updates) must
          // not drive an infinite reconnect loop.
          const currentWs = wsRef.current;
          if (historyCompleteTimeoutRef.current) {
            window.clearTimeout(historyCompleteTimeoutRef.current);
          }
          historyCompleteTimeoutRef.current = window.setTimeout(() => {
            if (wsRef.current !== currentWs) return;
            const decision = decideOnClose(
              1006,
              reconnectAttemptRef.current,
              intentionalCloseRef.current,
            );
            intentionalCloseRef.current = false;
            if (decision.action === "retry") {
              reconnectAttemptRef.current = decision.attempt;
              console.warn(
                `[SessionStream] session_status timeout after history_complete, ` +
                  `reconnecting (attempt ${decision.attempt} in ${decision.delayMs}ms)...`,
              );
              if (reconnectTimeoutRef.current !== null) {
                window.clearTimeout(reconnectTimeoutRef.current);
              }
              reconnectTimeoutRef.current = window.setTimeout(() => {
                reconnectTimeoutRef.current = null;
                reconnectRef.current();
              }, decision.delayMs);
            } else if (decision.action === "report") {
              setError(
                new Error(
                  "Lost connection to the session and could not reconnect",
                ),
              );
              // Same rationale as the streaming watchdog: hand the socket to
              // the onclose handler so status is corrected and recovery stays
              // possible. A socket that never got session_status is not
              // trustworthy.
              currentWs?.close();
            }
          }, 15000);
          return;
        }

        // Handle initialize response
        if (message.id && message.id === initializeIdRef.current && message.result) {
          initializeIdRef.current = null;
          initializeRetryCountRef.current = 0;

          const { slash_commands } = message.result;
          // YOLO can be on before any wire message exists (the --yolo flag) and
          // is not replayed, so initialize is the only place the toggle can
          // learn its starting position.
          if (typeof message.result.yolo === "boolean") {
            setYolo(message.result.yolo);
          }
          const approvalRequests = Array.isArray(
            message.result.approval_requests,
          )
            ? (message.result.approval_requests as ApprovalRequestEvent[])
            : [];
          const localUnsubmittedIds = new Set(
            Array.from(pendingApprovalRequestsRef.current.entries())
              .filter(([, pending]) => !pending.submitted)
              .map(([requestId]) => requestId),
          );
          const serverIds = new Set(
            approvalRequests.map((event) => event.payload.id),
          );

          for (const approvalEvent of approvalRequests) {
            processEvent(approvalEvent, false, approvalEvent.payload.id);
          }

          const { stale } = reconcileApprovalRequestIds(
            localUnsubmittedIds,
            serverIds,
          );
          if (stale.size > 0) {
            for (const requestId of stale) {
              pendingApprovalRequestsRef.current.delete(requestId);
            }
            setMessages((prev) =>
              prev.map((message) => {
                const toolCall = message.toolCall;
                const approval = toolCall?.approval;
                if (!(approval && stale.has(approval.id))) {
                  return message;
                }
                return {
                  ...message,
                  isStreaming: false,
                  toolCall: {
                    ...toolCall,
                    state: "input-available",
                    approval: {
                      ...approval,
                      resolved: true,
                    },
                  },
                };
              }),
            );
          }

          // The server is the authority on which questions can still be
          // answered. A question it does not know about belongs to a turn
          // that died with the worker that asked it; answering it would be
          // dropped silently, so close it instead of leaving it hanging in
          // the composer.
          const questionRequests = Array.isArray(
            message.result.question_requests,
          )
            ? (message.result.question_requests as QuestionRequestEvent[])
            : [];
          const localUnsubmittedQuestionIds = new Set(
            Array.from(pendingQuestionRequestsRef.current.entries())
              .filter(([, pending]) => !pending.submitted)
              .map(([requestId]) => requestId),
          );
          const serverQuestionIds = new Set(
            questionRequests.map((event) => event.payload.id),
          );

          for (const questionEvent of questionRequests) {
            processEvent(questionEvent, false, questionEvent.payload.id);
          }

          const { stale: staleQuestions } = reconcileApprovalRequestIds(
            localUnsubmittedQuestionIds,
            serverQuestionIds,
          );
          if (staleQuestions.size > 0) {
            for (const requestId of staleQuestions) {
              pendingQuestionRequestsRef.current.delete(requestId);
            }
            setMessages((prev) =>
              prev.map((message) => {
                const question = message.toolCall?.question;
                if (!(question && staleQuestions.has(question.id))) {
                  return message;
                }
                return {
                  ...message,
                  isStreaming: false,
                  toolCall: {
                    ...message.toolCall!,
                    state: "question-responded",
                    question: {
                      ...question,
                      submitted: true,
                      resolved: true,
                    },
                  },
                };
              }),
            );
          }

          if (slash_commands && slash_commands.length > 0) {
            setSlashCommands(slash_commands);
            slashCommandsLenRef.current = slash_commands.length;
            usingCachedCommandsRef.current = false;
          }
          return;
        }

        // Handle approval/question requests sent as JSON-RPC requests
        if (message.method === "request") {
          const params = message.params as {
            type?: string;
            payload?: unknown;
          };

          if (params?.type === "ApprovalRequest") {
            const approvalEvent: ApprovalRequestEvent = {
              type: "ApprovalRequest",
              payload: params.payload as ApprovalRequestEvent["payload"],
            };
            processEvent(
              approvalEvent,
              isReplayingRef.current,
              message.id ?? (approvalEvent.payload.id as string | number),
            );
            return;
          }

          if (params?.type === "QuestionRequest") {
            const questionEvent: QuestionRequestEvent = {
              type: "QuestionRequest",
              payload: params.payload as QuestionRequestEvent["payload"],
            };
            processEvent(
              questionEvent,
              isReplayingRef.current,
              message.id ?? (questionEvent.payload.id as string | number),
            );
            return;
          }
        }

        // Process event
        const event = extractEvent(message);
        if (event) {
          const eventTimestamp =
            typeof message.timestamp === "number"
              ? message.timestamp * 1000
              : undefined;
          processEvent(
            event,
            isReplayingRef.current,
            undefined,
            eventTimestamp,
          );
        }
      } catch (err) {
        console.warn(
          "[SessionStream] Failed to parse WebSocket message:",
          data,
          err,
        );
      }
    },
    [
      processEvent,
      onError,
      setAwaitingFirstResponse,
      applySessionStatus,
      completeStreamingMessages,
      resolveStaleCompaction,
      sendInitialize,
      clearStepRetryStatus,
      setMessages,
    ],
  );

  // Build WebSocket URL
  const getWebSocketUrl = useCallback(
    (sid: string): string => {
      const token = getAuthToken();
      if (baseUrl) {
        // Convert HTTP URL to WebSocket URL
        const url = baseUrl.replace(HTTP_TO_WS_REGEX, "ws");
        const wsUrl = `${url}/api/sessions/${sid}/stream`;
        return token ? `${wsUrl}?token=${encodeURIComponent(token)}` : wsUrl;
      }

      // Use current host
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const host = window.location.host;
      const wsUrl = `${protocol}//${host}/api/sessions/${sid}/stream`;
      return token ? `${wsUrl}?token=${encodeURIComponent(token)}` : wsUrl;
    },
    [baseUrl],
  );

  // Helper to send pending message
  const sendPendingMessage = useCallback(
    (ws: WebSocket) => {
      const pendingMessage = pendingMessageRef.current;
      if (pendingMessage) {
        pendingMessageRef.current = null;
        const id = uuidV4();
        const message: WireMessage = {
          jsonrpc: "2.0",
          method: "prompt",
          id,
          params: {
            user_input: pendingMessage,
          },
        };
        sentPromptsRef.current.set(id, pendingMessage);
        ws.send(JSON.stringify(message));
        setAwaitingFirstResponse(true);
        setStatus("streaming");
        console.log(
          "[SessionStream] Sent pending message after connect:",
          pendingMessage,
        );
      }
    },
    [setAwaitingFirstResponse],
  );

  const respondToApproval = useCallback(
    async (
      requestId: string,
      response: ApprovalResponseDecision,
      reason?: string,
    ) => {
      const ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        throw new Error("Not connected to session stream");
      }

      const pending = pendingApprovalRequestsRef.current.get(requestId);
      if (!pending) {
        throw new Error("Approval request not found");
      }

      if (pending.submitted) {
        return;
      }

      const trimmedReason =
        typeof reason === "string" && reason.trim().length > 0
          ? reason.trim()
          : undefined;

      const isApproved = response !== "reject";
      const rejectionReason = response === "reject" ? trimmedReason : undefined;
      const responseMessage: JsonRpcResponse = {
        jsonrpc: "2.0",
        id: pending.rpcId ?? requestId,
        result: {
          request_id: pending.requestId ?? requestId,
          response,
          ...(response === "reject" && trimmedReason
            ? { feedback: trimmedReason }
            : {}),
        },
      };

      try {
        ws.send(JSON.stringify(responseMessage));
      } catch (err) {
        throw err instanceof Error ? err : new Error(String(err));
      }

      pending.submitted = true;
      pendingApprovalRequestsRef.current.set(requestId, pending);

      const tc = currentToolCallsRef.current.get(pending.toolCallId);
      const nextState = isApproved ? "input-available" : "output-denied";
      const nextStreaming = isApproved;

      if (tc) {
        const existingApproval = tc.approval ?? {
          id: requestId,
          action: "",
          description: "",
          sender: "",
          toolCallId: pending.toolCallId,
        };

        const updatedApproval = {
          ...existingApproval,
          approved: isApproved,
          reason: isApproved
            ? existingApproval.reason
            : (rejectionReason ?? existingApproval.reason),
          submitted: true,
          resolved: isApproved ? existingApproval.resolved : true,
          response,
        };

        tc.approval = updatedApproval;

        if (tc.messageId) {
          updateMessageById(tc.messageId, (message) => {
            if (!message.toolCall) {
              return message;
            }

            return {
              ...message,
              isStreaming: nextStreaming,
              toolCall: {
                ...message.toolCall,
                state: nextState,
                approval: updatedApproval,
                errorText: isApproved
                  ? message.toolCall.errorText
                  : (rejectionReason ?? message.toolCall.errorText),
              },
            };
          });
        }
      }
    },
    [updateMessageById],
  );

  const respondToQuestion = useCallback(
    async (requestId: string, answers: Record<string, string>) => {
      const ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        throw new Error("Not connected to session stream");
      }

      const pending = pendingQuestionRequestsRef.current.get(requestId);
      if (!pending) {
        throw new Error("Question request not found");
      }

      if (pending.submitted) {
        return;
      }

      const responseMessage: JsonRpcResponse = {
        jsonrpc: "2.0",
        id: pending.rpcId ?? requestId,
        result: {
          request_id: pending.requestId ?? requestId,
          answers,
        },
      };

      try {
        ws.send(JSON.stringify(responseMessage));
      } catch (err) {
        throw err instanceof Error ? err : new Error(String(err));
      }

      pending.submitted = true;
      pendingQuestionRequestsRef.current.set(requestId, pending);

      const tc = currentToolCallsRef.current.get(pending.toolCallId);

      if (tc?.messageId) {
        updateMessageById(tc.messageId, (message) => {
          if (!message.toolCall) {
            return message;
          }

          return {
            ...message,
            isStreaming: true,
            toolCall: {
              ...message.toolCall,
              state: "question-responded",
              question: message.toolCall.question
                ? {
                    ...message.toolCall.question,
                    submitted: true,
                    answers,
                  }
                : undefined,
            },
          };
        });
      }
    },
    [updateMessageById],
  );

  const dismissQuestion = useCallback(
    (requestId: string) => {
      dismissedQuestionsRef.current.add(requestId);
      recordDismissedQuestion(sessionIdRef.current ?? sessionId, requestId);

      const pending = pendingQuestionRequestsRef.current.get(requestId);
      if (!pending) return;
      pendingQuestionRequestsRef.current.delete(requestId);

      if (pending.messageId) {
        updateMessageById(pending.messageId, (message) => {
          if (message.toolCall?.state !== "question-requested") {
            return message;
          }
          return {
            ...message,
            isStreaming: false,
            toolCall: {
              ...message.toolCall,
              state: "question-responded",
              question: message.toolCall.question
                ? {
                    ...message.toolCall.question,
                    submitted: true,
                    resolved: true,
                  }
                : undefined,
            },
          };
        });
      }
    },
    [sessionId, updateMessageById],
  );

  // Connect to WebSocket
  const connect = useCallback((preserveSessionState = false) => {
    if (!sessionId) return;

    initializeRetryCountRef.current = 0; // Reset retry count for new connection

    // Close existing connection
    if (wsRef.current) {
      console.log("[SessionStream] Closing existing WebSocket");
      wsRef.current.close();
      wsRef.current = null;
    }
    if (watchdogIntervalRef.current !== null) {
      window.clearInterval(watchdogIntervalRef.current);
      watchdogIntervalRef.current = null;
    }

    const retainedApprovals = preserveSessionState
      ? new Map(pendingApprovalRequestsRef.current)
      : null;
    const retainedQuestions = preserveSessionState
      ? new Map(pendingQuestionRequestsRef.current)
      : null;

    awaitingIdleRef.current = false;
    resetState(true);
    if (retainedApprovals) {
      pendingApprovalRequestsRef.current = retainedApprovals;
    }
    if (retainedQuestions) {
      pendingQuestionRequestsRef.current = retainedQuestions;
    }
    // Deferred, not done here: clearing now would drop the conversation the
    // reader is looking at before its replacement exists, and an empty list
    // swaps the whole message area out for a spinner until history replays.
    // A session switch has already cleared it; a reconnect keeps it until the
    // incoming conversation starts arriving. See `conversation-reset`.
    pendingConversationResetRef.current = true;
    deliveryWindowRef.current.clear();
    setStatus("submitted");
    setAwaitingFirstResponse(Boolean(pendingMessageRef.current));

    const wsUrl = getWebSocketUrl(sessionId);

    try {
      // The flag belongs to the socket that was closed on purpose, and that
      // socket's close handler returns early once it is no longer current,
      // so nothing else would clear it. Left set, every later drop of this
      // socket read as intentional: no reconnect, and the page showed the
      // turn as over, which sent the queue into a busy session.
      intentionalCloseRef.current = false;
      const ws = new WebSocket(wsUrl);
      // Mark this socket as the "current attempt" immediately.
      // If the user switches sessions before `onopen`, `disconnect()` will clear `wsRef.current`,
      // and any late callbacks from this `ws` will be ignored by the identity guard.
      wsRef.current = ws;

      ws.onopen = () => {
        if (wsRef.current !== ws) {
          ws.close();
          return;
        }

        console.log("[SessionStream] Connected to session:", sessionId);
        setIsConnected(true);
        setError(null);
        // Do NOT reset reconnectAttemptRef here. A bare successful handshake
        // says nothing about whether the session is actually usable — the
        // server may never deliver session_status, in which case Bug A/B
        // would reconnect forever if the counter were zeroed on every
        // onopen. Reset it only when session_status arrives (see handler
        // above) or on the reset points below.
        awaitingIdleRef.current = false;
        setStatus("streaming"); // Will receive replay, then switch to ready
        lastWsMessageTimeRef.current = Date.now();

        // Start stale-connection watchdog
        if (watchdogIntervalRef.current !== null) {
          window.clearInterval(watchdogIntervalRef.current);
          watchdogIntervalRef.current = null;
        }
        const watchdogIntervalId = window.setInterval(() => {
          if (!wsRef.current || wsRef.current !== ws) {
            // This ws is no longer current — stop checking this watchdog.
            window.clearInterval(watchdogIntervalId);
            if (watchdogIntervalRef.current === watchdogIntervalId) {
              watchdogIntervalRef.current = null;
            }
            return;
          }
          if (wsRef.current.readyState !== WebSocket.OPEN) return;
          const elapsed = Date.now() - lastWsMessageTimeRef.current;
          const hasUnsubmittedApproval = Array.from(
            pendingApprovalRequestsRef.current.values(),
          ).some((e) => !e.submitted);
          const hasUnsubmittedQuestion = Array.from(
            pendingQuestionRequestsRef.current.values(),
          ).some((e) => !e.submitted);
          const hasPendingInteraction =
            hasUnsubmittedApproval || hasUnsubmittedQuestion;
          if (
            elapsed > 45_000 &&
            (statusRef.current === "streaming" ||
              statusRef.current === "submitted") &&
            !hasPendingInteraction
          ) {
            // Route through decideOnClose so a wedged worker on Windows
            // (ProactorEventLoop half-closed pipe) cannot loop the client
            // past MAX_RECONNECT_ATTEMPTS. The 15s history_complete timeout
            // and this watchdog both used to call reconnectRef.current()
            // directly, which bypassed the attempt counter entirely.
            const decision = decideOnClose(
              1006,
              reconnectAttemptRef.current,
              intentionalCloseRef.current,
            );
            intentionalCloseRef.current = false;
            if (decision.action === "retry") {
              reconnectAttemptRef.current = decision.attempt;
              console.warn(
                `[SessionStream] Watchdog: no messages for ${Math.round(elapsed / 1000)}s while ${statusRef.current}, ` +
                  `reconnecting (attempt ${decision.attempt} in ${decision.delayMs}ms)...`,
              );
              if (reconnectTimeoutRef.current !== null) {
                window.clearTimeout(reconnectTimeoutRef.current);
              }
              reconnectTimeoutRef.current = window.setTimeout(() => {
                reconnectTimeoutRef.current = null;
                reconnectRef.current();
              }, decision.delayMs);
            } else if (decision.action === "report") {
              setError(
                new Error(
                  "Lost connection to the session and could not reconnect",
                ),
              );
              // Closing the socket lets the onclose handler run its cleanup:
              // status is corrected to non-busy, the watchdog stops, and a
              // later network-up event is allowed to reconnect. Without this
              // the UI stayed "streaming" forever on a dead socket.
              wsRef.current?.close();
            }
          }
        }, 10_000);
        watchdogIntervalRef.current = watchdogIntervalId;

        // Send initialize message to get slash commands
        sendInitialize(ws);

        // Send pending message immediately after connection
        sendPendingMessage(ws);
      };

      ws.onmessage = (event) => {
        if (wsRef.current !== ws) {
          return;
        }

        lastWsMessageTimeRef.current = Date.now();
        // Server keepalive probe: counts as connection liveness for the
        // watchdog but carries no conversation content. Skipping it here
        // keeps it out of the duplicate-delivery window and message log.
        if (
          typeof event.data === "string" &&
          event.data.includes('"method":"ping"')
        ) {
          return;
        }
        handleMessage(event.data);
      };

      ws.onerror = (event) => {
        if (wsRef.current !== ws) {
          return;
        }

        // The event carries no detail by design, and usually precedes a close
        // the client recovers from. The close handler decides what, if
        // anything, the reader is told.
        console.error("[SessionStream] WebSocket error:", event);
        if (shouldReportErrorEvent()) {
          const err = new Error("WebSocket connection error");
          setError(err);
          onError?.(err);
        }
        setAwaitingFirstResponse(false);
        clearStepRetryStatus();
        awaitingIdleRef.current = false;
        pendingMessageRef.current = null; // Clear pending message on error
      };

      ws.onclose = (event) => {
        if (wsRef.current !== ws) {
          return;
        }

        // Abnormal close (1006) means the runner was killed or crashed, not a
        // user-initiated Stop (which uses 1001 "Going Away"). Mark in-flight
        // tool calls as interrupted now, before a reconnect replays the
        // synthetic "stopped" status that would otherwise overwrite the label
        // as "cancelled" and mislead the reader about who stopped what.
        // interruptStaleToolCalls is idempotent: only non-terminal tool
        // calls are touched, so a later re-mark from applySessionStatus is a
        // no-op on anything already labelled.
        if (event.code === 1006 && !intentionalCloseRef.current) {
          interruptStaleToolCalls({ interrupted: true });
        }

        console.log("[SessionStream] Disconnected:", event.code, event.reason);
        setIsConnected(false);
        wsRef.current = null;
        pendingMessageRef.current = null; // Clear pending message on close
        awaitingIdleRef.current = false;
        setAwaitingFirstResponse(false);
        setSessionStatus(null);
        lastStatusSeqRef.current = null;
        if (watchdogIntervalRef.current !== null) {
          window.clearInterval(watchdogIntervalRef.current);
          watchdogIntervalRef.current = null;
        }

        const decision = decideOnClose(
          event.code,
          reconnectAttemptRef.current,
          intentionalCloseRef.current,
        );
        intentionalCloseRef.current = false;
        if (decision.action === "retry") {
          reconnectAttemptRef.current = decision.attempt;
          console.log(
            `[SessionStream] Reconnecting in ${decision.delayMs}ms ` +
              `(attempt ${decision.attempt})`,
          );
          if (reconnectTimeoutRef.current !== null) {
            window.clearTimeout(reconnectTimeoutRef.current);
          }
          reconnectTimeoutRef.current = window.setTimeout(() => {
            reconnectTimeoutRef.current = null;
            reconnectRef.current();
          }, decision.delayMs);
        } else if (decision.action === "report") {
          const REASONS: Record<string, string> = {
            sessionNotFound: "Session not found",
            tooManySessions: "Too many concurrent sessions",
            // Say what the server said. "Lost connection" reads as a network
            // problem and sends people to the wrong place.
            unauthorized: "Signed out — sign in again to continue",
            forbidden: "The server refused this connection (origin or network policy)",
          };
          if (decision.reason === "unauthorized") {
            // The socket is the first thing to notice an expired session, and
            // reconnecting cannot fix it. Hand it to the auth layer so the
            // login page comes up instead of a dead workspace.
            notifySessionExpired();
          }
          const err = new Error(
            REASONS[decision.reason] ??
              "Lost connection to the session and could not reconnect",
          );
          setError(err);
          onError?.(err);
        }

        // Mark all streaming/subagent messages as complete
        clearStepRetryStatus();
        completeStreamingMessages();
        // A drop about to be retried says nothing about the turn: reading it
        // as "ready" let the queue send into a session still busy.
        setStatus(decision.action === "retry" ? "submitted" : "ready");
      };
    } catch (err) {
      console.error("[SessionStream] Failed to connect:", err);
      const connectionError =
        err instanceof Error ? err : new Error(String(err));
      setError(connectionError);
      onError?.(connectionError);
      awaitingIdleRef.current = false;
      setAwaitingFirstResponse(false);
      setStatus("error");
      clearStepRetryStatus();
      pendingMessageRef.current = null; // Clear pending message on error
    }
  }, [
    sessionId,
    resetState,
    getWebSocketUrl,
    handleMessage,
    onError,
    sendInitialize,
    sendPendingMessage,
    setAwaitingFirstResponse,
    completeStreamingMessages,
    clearStepRetryStatus,
    interruptStaleToolCalls,
  ]);

  // Send cancel message to server
  // Disconnect
  const disconnect = useCallback(() => {
    if (reconnectTimeoutRef.current !== null) {
      window.clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }

    if (watchdogIntervalRef.current !== null) {
      window.clearInterval(watchdogIntervalRef.current);
      watchdogIntervalRef.current = null;
    }

    if (wsRef.current) {
      intentionalCloseRef.current = true;
      reconnectAttemptRef.current = 0;
      wsRef.current.close();
      wsRef.current = null;
    }

    awaitingIdleRef.current = false;
    setAwaitingFirstResponse(false);
    pendingMessageRef.current = null;
    clearStepRetryStatus();
    setIsConnected(false);
    setStatus("ready");
    setSessionStatus(null);
    lastStatusSeqRef.current = null;
    pendingApprovalRequestsRef.current.clear();
    pendingQuestionRequestsRef.current.clear();

    // Remove lingering MCP loading indicator (e.g. MCPLoadingEnd was never received)
    const mcpMsgId = mcpLoadingMessageIdRef.current;
    if (mcpMsgId) {
      mcpLoadingMessageIdRef.current = null;
      setMessages((prev) => prev.filter((m) => m.id !== mcpMsgId));
    }

    // Mark all streaming/subagent messages as complete
    completeStreamingMessages();
  }, [
    clearStepRetryStatus,
    completeStreamingMessages,
    setAwaitingFirstResponse,
    setMessages,
  ]);

  // Send cancel request or disconnect if stream not ready
  const cancel = useCallback(() => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      console.log(
        "[SessionStream] Cancel requested before stream is ready, disconnecting instead",
      );
      awaitingIdleRef.current = false;
      pendingMessageRef.current = null;
      // Clear pending approval/question requests and update message states
      pendingApprovalRequestsRef.current.clear();
      pendingQuestionRequestsRef.current.clear();
      setMessages((prev) =>
        prev.map((msg) => {
          if (
            msg.variant === "tool" &&
            msg.toolCall?.state === "approval-requested"
          ) {
            return {
              ...msg,
              isStreaming: false,
              toolCall: {
                ...msg.toolCall,
                state: "output-denied",
                approval: msg.toolCall.approval
                  ? {
                      ...msg.toolCall.approval,
                      submitted: true,
                      resolved: true,
                      approved: false,
                      response: "reject",
                    }
                  : undefined,
              },
            };
          }
          if (
            msg.variant === "tool" &&
            msg.toolCall?.state === "question-requested"
          ) {
            return {
              ...msg,
              isStreaming: false,
              toolCall: {
                ...msg.toolCall,
                state: "question-responded",
                question: msg.toolCall.question
                  ? {
                      ...msg.toolCall.question,
                      submitted: true,
                      resolved: true,
                    }
                  : undefined,
              },
            };
          }
          // Mark still-running tool calls as interrupted
          if (
            msg.variant === "tool" &&
            (msg.toolCall?.state === "input-streaming" ||
              msg.toolCall?.state === "input-available")
          ) {
            return {
              ...msg,
              isStreaming: false,
              toolCall: {
                ...msg.toolCall,
                state: "output-denied",
              },
            };
          }
          return msg;
        }),
      );
      disconnect();
      return;
    }

    // Clear all pending approval/question requests and update message states
    pendingApprovalRequestsRef.current.clear();
    pendingQuestionRequestsRef.current.clear();

    // Always update messages (consistent with StepInterrupted handler)
    setMessages((prev) =>
      prev.map((msg) => {
        if (
          msg.variant === "tool" &&
          msg.toolCall?.state === "approval-requested"
        ) {
          return {
            ...msg,
            isStreaming: false,
            toolCall: {
              ...msg.toolCall,
              state: "output-denied",
              approval: msg.toolCall.approval
                ? {
                    ...msg.toolCall.approval,
                    submitted: true,
                    resolved: true,
                    approved: false,
                    response: "reject",
                  }
                : undefined,
            },
          };
        }
        if (
          msg.variant === "tool" &&
          msg.toolCall?.state === "question-requested"
        ) {
          return {
            ...msg,
            isStreaming: false,
            toolCall: {
              ...msg.toolCall,
              state: "question-responded",
              question: msg.toolCall.question
                ? {
                    ...msg.toolCall.question,
                    submitted: true,
                    resolved: true,
                  }
                : undefined,
            },
          };
        }
        // Mark still-running tool calls as interrupted
        if (
          msg.variant === "tool" &&
          (msg.toolCall?.state === "input-streaming" ||
            msg.toolCall?.state === "input-available")
        ) {
          return {
            ...msg,
            isStreaming: false,
            toolCall: {
              ...msg.toolCall,
              state: "output-denied",
            },
          };
        }
        return msg;
      }),
    );

    const cancelMessage: JsonRpcRequest = {
      jsonrpc: "2.0",
      method: "cancel",
      id: uuidV4(),
    };

    try {
      console.log("[SessionStream] Sending cancel request");
      ws.send(JSON.stringify(cancelMessage));
      const shouldAwaitIdle = status === "streaming" || status === "submitted";
      awaitingIdleRef.current = shouldAwaitIdle;
      if (status === "streaming") {
        setStatus("submitted");
      }
      setAwaitingFirstResponse(false);
    } catch (err) {
      console.error("[SessionStream] Failed to send cancel request:", err);
    }
  }, [status, disconnect, setAwaitingFirstResponse, setMessages]);

  // Reconnect
  const reconnect = useCallback(() => {
    if (reconnectTimeoutRef.current !== null) {
      window.clearTimeout(reconnectTimeoutRef.current);
    }
    if (watchdogIntervalRef.current !== null) {
      window.clearInterval(watchdogIntervalRef.current);
      watchdogIntervalRef.current = null;
    }
    if (wsRef.current) {
      const ws = wsRef.current;
      wsRef.current = null;
      ws.close();
    }
    setIsConnected(false);
    // Small delay before reconnecting
    reconnectTimeoutRef.current = window.setTimeout(() => {
      reconnectTimeoutRef.current = null;
      connect(true);
    }, 100);
  }, [connect]);

  // Coming back to the page, or back online, is when a dead socket shows: the
  // reader expects what happened while they were away. See
  // `shouldReconnectOnReturn`.
  useEffect(() => {
    if (!sessionId) return;
    let hiddenAt: number | null = document.visibilityState === "hidden" ? Date.now() : null;
    const check = (hiddenForMs: number) => {
      const ws = wsRef.current;
      if (reconnectTimeoutRef.current !== null || ws?.readyState === WebSocket.CONNECTING) {
        return; // Already on its way back.
      }
      if (shouldReconnectOnReturn(ws?.readyState === WebSocket.OPEN, hiddenForMs)) {
        console.log("[SessionStream] Back on the page, reconnecting to catch up");
        reconnectAttemptRef.current = 0;
        reconnectRef.current();
      }
    };
    const onVisibility = () => {
      if (document.visibilityState === "hidden") {
        hiddenAt = Date.now();
        return;
      }
      const hiddenForMs = hiddenAt === null ? 0 : Date.now() - hiddenAt;
      hiddenAt = null;
      check(hiddenForMs);
    };
    const onOnline = () => {
      // Coming back online is the strongest reconnect signal there is: a
      // socket that read as OPEN through the outage may be half-dead and
      // will never deliver again. Reconnect unconditionally — a healthy
      // connection just replays and dedupes; an idempotent no-op otherwise.
      if (
        reconnectTimeoutRef.current !== null ||
        wsRef.current?.readyState === WebSocket.CONNECTING
      ) {
        return; // Already on its way back.
      }
      console.log("[SessionStream] Back online, reconnecting to catch up");
      reconnectAttemptRef.current = 0;
      reconnectRef.current();
    };
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("online", onOnline);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("online", onOnline);
    };
  }, [sessionId]);

  // Keep refs in sync so useLayoutEffect can use stable references
  connectRef.current = connect;
  disconnectRef.current = disconnect;
  reconnectRef.current = reconnect;
  resetStateRef.current = resetState;
  statusRef.current = status;

  // Send message to session (auto-connects if not connected)
  const sendMessage = useCallback(
    async (text: string) => {
      if (!text.trim()) return;

      const trimmedText = text.trim();
      setAwaitingFirstResponse(true);

      // If not connected, store the message and connect
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
        if (!sessionId) {
          throw new Error("No session selected");
        }

        pendingMessageRef.current = trimmedText;
        connect();
        return;
      }

      // Send as JSON-RPC prompt message
      const id = uuidV4();
      const message: WireMessage = {
        jsonrpc: "2.0",
        method: "prompt",
        id,
        params: {
          user_input: trimmedText,
        },
      };

      sentPromptsRef.current.set(id, trimmedText);
      wsRef.current.send(JSON.stringify(message));
      awaitingIdleRef.current = false;
      setStatus("streaming");
    },
    [sessionId, connect, setAwaitingFirstResponse],
  );

  // Clear messages
  const clearMessages = useCallback(() => {
    setMessages([]);
    resetStateRef.current(true);
  }, [setMessages]);

  // Set YOLO via silent RPC (no context message). The server echoes a
  // StatusUpdate, so the switch follows /yolo typed anywhere else too.
  const sendSetYolo = useCallback((enabled: boolean) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      return;
    }
    // Flip the switch immediately and let the echo reconcile. Waiting for
    // the worker's StatusUpdate made the toggle feel dead: mid-turn the echo
    // queues behind the turn's streaming output, and on a slower machine
    // that is seconds. The RPC result and any later StatusUpdate carry the
    // authoritative state, so a rejected change corrects itself.
    setYolo(enabled);
    const message: JsonRpcRequest = {
      jsonrpc: "2.0",
      method: "set_yolo",
      id: uuidV4(),
      params: { enabled },
    };
    wsRef.current.send(JSON.stringify(message));
  }, []);

  // Set plan mode via silent RPC (no context message)
  const sendSetPlanMode = useCallback((enabled: boolean) => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) {
      return;
    }
    // Same optimistic flip as YOLO: the echo and the RPC result reconcile.
    setPlanMode(enabled);
    const message: JsonRpcRequest = {
      jsonrpc: "2.0",
      method: "set_plan_mode",
      id: uuidV4(),
      params: { enabled },
    };
    wsRef.current.send(JSON.stringify(message));
  }, []);

  // Auto-connect when sessionId changes
  useLayoutEffect(() => {
    /**
     * Session switches must be "atomic" from the UI's perspective:
     * - stop old stream
     * - clear per-session accumulators
     * - optionally connect to the new session
     *
     * We use `useLayoutEffect` (instead of `useEffect`) so teardown happens before paint,
     * minimizing the chance that the next screen renders while the previous socket still
     * pushes messages.
     *
     * Even if a late event slips through, callback identity guards ensure it can't mutate
     * state unless it belongs to the current `wsRef.current`.
     *
     * We access connect/disconnect via refs to avoid re-running this effect when their
     * callback identity changes (which would cause disconnect→connect cycles).
     */
    // When sessionId changes, disconnect from previous session
    if (wsRef.current) {
      disconnectRef.current();
    }

    // Reset state for new session (preserve slash commands to avoid empty gap before initialize response)
    resetStateRef.current(true);
    setMessages([]);
    useToolEventsStore.getState().clearTodoItems();

    // Auto-connect if we have a valid sessionId
    if (sessionId) {
      // Small delay to ensure state is settled
      const timeoutId = window.setTimeout(() => {
        connectRef.current();
      }, 50);
      return () => {
        window.clearTimeout(timeoutId);
        disconnectRef.current();
      };
    }

    setIsReplayingHistory(false);
    setHistoryLoadedFor(sessionIdRef.current ?? null);
    setWaitingOnBackground(false);
    return () => {
      disconnectRef.current();
    };
  }, [sessionId, setMessages]);

  // Load questions the user already closed, so a reconnect's history replay
  // does not resurrect them.
  useEffect(() => {
    dismissedQuestionsRef.current = loadDismissedQuestions(sessionId);
  }, [sessionId]);

  // Cleanup on unmount
  useEffect(
    () => () => {
      if (reconnectTimeoutRef.current !== null) {
        window.clearTimeout(reconnectTimeoutRef.current);
      }
      if (watchdogIntervalRef.current !== null) {
        window.clearInterval(watchdogIntervalRef.current);
      }
      if (wsRef.current) {
        wsRef.current.close();
      }
    },
    [],
  );

  return {
    messages,
    status,
    sessionStatus,
    isAwaitingFirstResponse,
    contextUsage,
    waitingOnBackground,
    tokenUsage,
    currentStep,
    isConnected,
    isReplayingHistory,
    isHistoryLoaded,
    sendMessage,
    respondToApproval,
    respondToQuestion,
    dismissQuestion,
    cancel,
    disconnect,
    reconnect,
    connect,
    setMessages,
    clearMessages,
    error,
    planMode,
    yolo,
    sendSetYolo,
    sendSetPlanMode,
    slashCommands,
  };
}
