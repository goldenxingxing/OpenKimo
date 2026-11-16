/**
 * Container component that subscribes to useSessionStream.
 *
 * This exists to isolate high-frequency message updates from App, preventing
 * unnecessary re-renders of SessionsSidebar. When messages update, only this
 * container and ChatWorkspace re-render, not App.
 *
 * TODO: This layer could be simplified by moving useSessionStream directly
 * into ChatWorkspace. The Container/Presentational split here doesn't provide
 * much value since ChatWorkspace receives `messages` as a prop and re-renders
 * on every update anyway.
 */
import { type ReactElement, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ChatStatus, FileUIPart } from "ai";
import { useTranslation } from "react-i18next";
import type { PromptInputMessage } from "@ai-elements";
import { nameAttachments } from "@/components/ai-elements/prompt-input-paste";
import { toast } from "sonner";
import type {
  Session,
  SessionStatus,
  UploadSessionFileResponse,
} from "@/lib/api/models";
import { useGlobalConfig } from "@/hooks/useGlobalConfig";
import type { SessionFileEntry } from "@/hooks/useSessions";
import { getApiBaseUrl, isMacOS } from "@/hooks/utils";
import { useSessionStream } from "@/hooks/useSessionStream";
import { useToolEventsStore } from "@/features/tool/store";
import { usePluginEventEmitter, usePluginRegistry, usePluginSystem } from "@/plugins";
import { getInstalledPlugins, seedBuiltins } from "@/plugins/registry-store";
import ThinkingAnimationPlugin from "@/plugins/builtins/ThinkingAnimationPlugin";
import SubagentAnimationPlugin from "@/plugins/builtins/SubagentAnimationPlugin";
import SubagentClusterPlugin from "@/plugins/builtins/SubagentClusterPlugin";
import { useQueueStore } from "./queue-store";
import { ChatWorkspace } from "./chat";

type PendingMessage = {
  text: string;
  targetSessionId: string;
};

type ChatWorkspaceContainerProps = {
  selectedSessionId: string;
  currentSession?: Session;
  sessionDescription?: string;
  onSessionStatus: (status: SessionStatus) => void;
  onStreamStatusChange?: (status: ChatStatus) => void;
  uploadSessionFile: (
    sessionId: string,
    file: File,
  ) => Promise<UploadSessionFileResponse>;
  onListSessionDirectory?: (
    sessionId: string,
    path?: string,
  ) => Promise<SessionFileEntry[]>;
  onGetSessionFileUrl?: (sessionId: string, path: string) => string;
  onGetSessionFile?: (sessionId: string, path: string) => Promise<Blob>;
  onOpenCreateDialog?: () => void;
  onOpenSidebar?: () => void;
  generateTitle?: (sessionId: string) => Promise<string | null>;
  onRenameSession?: (sessionId: string, newTitle: string) => Promise<boolean>;
  onForkSession?: (sessionId: string, turnIndex: number) => Promise<void>;
  onSelectSessionModel?: (sessionId: string, model: string) => Promise<boolean>;
  /** Staged draft model shown when no session exists yet */
  draftModel?: string | null;
  /** Called when the draft (pre-session) model selection changes */
  onDraftModelChange?: (model: string) => void;
};

export function ChatWorkspaceContainer({
  selectedSessionId,
  currentSession,
  sessionDescription,
  onSessionStatus,
  onStreamStatusChange,
  uploadSessionFile,
  onListSessionDirectory,
  onGetSessionFileUrl,
  onGetSessionFile,
  onOpenCreateDialog,
  onOpenSidebar,
  generateTitle,
  onRenameSession,
  onForkSession,
  onSelectSessionModel,
  draftModel,
  onDraftModelChange,
}: ChatWorkspaceContainerProps): ReactElement {
  const [isUploadingFiles, setIsUploadingFiles] = useState(false);
  // Pending message state for when we need to create a session first
  const [pendingMessage, setPendingMessage] = useState<PendingMessage | null>(
    null,
  );
  const sessionId = selectedSessionId || null;
  const { t } = useTranslation("toasts");

  const { config } = useGlobalConfig();
  const maxContextSize = useMemo(() => {
    if (!config) return undefined;
    // The session's per-session model override wins; otherwise the global
    // default. ``defaultModel`` is the model-key under legacy configs but a
    // provider name under the new LLM_PROVIDERS scheme; check both to find
    // the entry.
    const effectiveModelName = currentSession?.model ?? config.defaultModel;
    const model =
      config.models.find((m) => m.name === effectiveModelName) ??
      config.models.find((m) => m.provider === effectiveModelName);
    return model?.maxContextSize;
  }, [config, currentSession?.model]);

  // Plugin system: event emitter + registration
  const { translateWireEvent } = usePluginEventEmitter();
  const pluginRegistry = usePluginRegistry();
  const { setActiveSessionId } = usePluginSystem();

  // Overlays (thinking / subagent animations) belong to the session on
  // screen; leaving the workspace retires them.
  useEffect(() => {
    setActiveSessionId(sessionId);
    return () => setActiveSessionId(null);
  }, [setActiveSessionId, sessionId]);

  useEffect(() => {
    seedBuiltins();
    const installed = getInstalledPlugins();

    const builtinMap: Record<string, import("@/plugins").UIPlugin> = {
      "builtin:thinking-animation": ThinkingAnimationPlugin,
      "builtin:subagent-animation": SubagentAnimationPlugin,
      "builtin:subagent-cluster": SubagentClusterPlugin,
    };

    const unregisters: (() => void)[] = [];
    for (const entry of installed) {
      if (!entry.enabled) continue;
      const builtin = builtinMap[entry.id];
      if (builtin) {
        unregisters.push(pluginRegistry.register(builtin));
      }
      // URL-based plugins are loaded asynchronously via importPluginFromUrl
      // and registered separately (admin panel handles this)
    }

    return () => { for (const u of unregisters) u(); };
  }, [pluginRegistry]);

  // A reply's error belongs to its session. As a toast it floated over
  // whichever session was open next; kept here, it shows in its own session,
  // survives switching away and back, and clears when the next message is
  // sent there or it is dismissed.
  const [sessionErrors, setSessionErrors] = useState<Record<string, string>>({});
  const sessionIdRef = useRef(sessionId);
  sessionIdRef.current = sessionId;
  const handleStreamError = useCallback((error: Error) => {
    const id = sessionIdRef.current;
    if (!id) return;
    setSessionErrors((current) => ({ ...current, [id]: error.message }));
  }, []);
  const clearSessionError = useCallback((id: string | null) => {
    if (!id) return;
    setSessionErrors((current) => {
      if (!(id in current)) return current;
      const { [id]: _cleared, ...rest } = current;
      return rest;
    });
  }, []);
  const sessionError = sessionId ? sessionErrors[sessionId] : undefined;

  // Handle first turn completion for auto-rename
  // Backend reads messages from wire.jsonl automatically
  const handleFirstTurnComplete = useCallback(async () => {
    if (!(selectedSessionId && generateTitle)) {
      return;
    }

    await generateTitle(selectedSessionId);
  }, [selectedSessionId, generateTitle]);

  // The refusal arrives on this session's socket, while its queue is active.
  const handlePromptRejected = useCallback((text: string) => {
    useQueueStore.getState().requeueFront(text);
  }, []);

  const sessionStream = useSessionStream({
    sessionId,
    baseUrl: getApiBaseUrl(),
    onError: handleStreamError,
    onSessionStatus,
    onPromptRejected: handlePromptRejected,
    onFirstTurnComplete: handleFirstTurnComplete,
    onWireEventForPlugin: translateWireEvent,
  });

  const {
    messages,
    status,
    isAwaitingFirstResponse,
    sendMessage,
    respondToApproval,
    respondToQuestion,
    dismissQuestion,
    cancel: cancelStream,
    contextUsage,
    waitingOnBackground,
    tokenUsage,
    currentStep,
    isConnected: isStreamConnected,
    isReplayingHistory,
    isHistoryLoaded,
    planMode,
    yolo,
    sendSetYolo,
    sendSetPlanMode,
    slashCommands,
    error: streamError,
    sessionStatus,
  } = sessionStream;

  const clearNewFiles = useToolEventsStore((state) => state.clearNewFiles);
  const enqueue = useQueueStore((s) => s.enqueue);
  const queueLength = useQueueStore((s) => s.queue.length);
  const dequeue = useQueueStore((s) => s.dequeue);
  const setActiveQueueSession = useQueueStore((s) => s.setActiveSession);

  useEffect(() => {
    if (status === "streaming") {
      clearNewFiles();
    }
  }, [status, clearNewFiles]);

  // Point the queue at this session (must run before auto-send, so a queue is
  // never sent to the session that happens to be open now). Each session keeps
  // its own queue, so switching away leaves it waiting rather than dropping it.
  useEffect(() => {
    setActiveQueueSession(selectedSessionId);
  }, [selectedSessionId, setActiveQueueSession]);

  // Send the next queued message once the turn is over — or immediately,
  // when the turn is only parked in a blocking wait on background task(s):
  // the server will replace the parked turn with this prompt.
  const queueArmedRef = useRef(false);
  const serverState = sessionStatus?.state;
  useEffect(() => {
    if (status === "streaming" || status === "submitted" || status === "error") {
      queueArmedRef.current = true;
    }
    const flushable =
      waitingOnBackground || (status === "ready" && (serverState === "idle" || serverState === "stopped"));
    if (flushable && queueArmedRef.current && queueLength > 0) {
      queueArmedRef.current = false;
      const next = dequeue();
      if (next) {
        sendMessage(next.text);
      }
    }
  }, [status, waitingOnBackground, serverState, queueLength, dequeue, sendMessage]);

  useEffect(() => {
    onStreamStatusChange?.(status);
  }, [status, onStreamStatusChange]);

  useEffect(() => {
    if (
      !pendingMessage ||
      pendingMessage.targetSessionId !== selectedSessionId ||
      !isStreamConnected ||
      (status !== "ready" && status !== "streaming")
    ) {
      return;
    }

    // Send only when the stream is connected to the intended session.
    // Using state (not ref) ensures this effect re-runs even if connection
    // happens before the pending message is set.
    setPendingMessage(null);
    sendMessage(pendingMessage.text);
  }, [
    isStreamConnected,
    status,
    selectedSessionId,
    sendMessage,
    pendingMessage,
  ]);

  useEffect(() => {
    if (
      !pendingMessage ||
      pendingMessage.targetSessionId === selectedSessionId
    ) {
      return;
    }

    // Drop stale pending messages if the user switches away before it is sent.
    setPendingMessage(null);
  }, [pendingMessage, selectedSessionId]);

  const uploadFilesToSession = useCallback(
    async (targetSessionId: string, files: FileUIPart[]): Promise<(string | null)[]> => {
      if (files.length === 0) {
        return [];
      }

      setIsUploadingFiles(true);
      try {
        // The stored name of each file, in the order given, or null for one
        // that did not upload: the message's markers point at them by position.
        const uploadResults = await Promise.all(
          files.map(async (filePart): Promise<string | null> => {
            if (!filePart.url) return null;

            const response = await fetch(filePart.url);
            if (!response.ok) return null;
            const blob = await response.blob();
            const file = new File([blob], filePart.filename ?? "unnamed_file", {
              type: filePart.mediaType ?? blob.type,
            });

            const uploadResult = await uploadSessionFile(targetSessionId, file);
            console.log(
              "[ChatWorkspaceContainer] File uploaded:",
              uploadResult,
            );
            return uploadResult.filename;
          }),
        );

        const uploadedCount = uploadResults.filter(Boolean).length;
        if (uploadedCount > 0) {
          toast.success(t("chatUpload.successTitle"), {
            description: t("chatUpload.successDesc", { count: uploadedCount }),
          });
        }
        return uploadResults;
      } catch (error) {
        console.error(
          "[ChatWorkspaceContainer] Failed to upload files:",
          error,
        );
        toast.error(t("chatUpload.errorTitle"), {
          description:
            error instanceof Error
              ? error.message
              : t("chatUpload.fallbackError"),
        });
        return files.map(() => null);
      } finally {
        setIsUploadingFiles(false);
      }
    },
    [uploadSessionFile, t],
  );

  const handlePromptSubmit = useCallback(
    async (message: PromptInputMessage) => {
      const hasPayload =
        message.text.trim().length > 0 || message.files.length > 0;
      if (!hasPayload) {
        toast.info(t("chatUpload.emptyTitle"), {
          description: t("chatUpload.emptyDesc"),
        });
        return;
      }

      if (isUploadingFiles) {
        toast.info(t("chatUpload.stillUploadingTitle"), {
          description: t("chatUpload.stillUploadingDesc"),
        });
        return;
      }

      if (
        (status === "streaming" || status === "submitted") &&
        !waitingOnBackground
      ) {
        // Queue text-only messages when AI is processing. Not when the turn
        // is only parked on background task(s): the server replaces that
        // wait with the new prompt, so this goes straight through.
        if (message.files.length > 0) {
          toast.info(t("queue.stillProcessingTitle"), {
            description: t("queue.stillProcessingDesc"),
          });
          return;
        }
        const messageText = message.text.trim();
        if (messageText) {
          enqueue(messageText);
          toast.info(t("queue.queuedTitle"), {
            description: t("queue.queuedDesc"),
          });
        }
        return;
      }

      // Note: This check is defensive - the submit button is disabled when no session exists
      if (!selectedSessionId) {
        return;
      }

      const targetSessionId = selectedSessionId;
      clearSessionError(targetSessionId);

      const storedNames =
        message.files.length > 0 && targetSessionId
          ? await uploadFilesToSession(targetSessionId, message.files)
          : [];
      // `<attachment index="i"/>` marks where the i-th file sits in the text;
      // the server needs its stored name to put the file's content there.
      const text = nameAttachments(message.text, (index) => storedNames[index] ?? undefined);

      const messageText =
        text.trim() ||
        (message.files.length > 0 ? "KIMO_FILE_UPLOAD_WITHOUT_MESSAGE" : "");

      await sendMessage(messageText);
    },
    [status, waitingOnBackground, isUploadingFiles, selectedSessionId, uploadFilesToSession, sendMessage, enqueue, t, clearSessionError],
  );

  const handlePlanModeChange = useCallback((enabled: boolean) => {
    sendSetPlanMode(enabled);
  }, [sendSetPlanMode]);

  const handleYoloChange = useCallback((enabled: boolean) => {
    sendSetYolo(enabled);
  }, [sendSetYolo]);

  const handleForkSession = useCallback(
    async (turnIndex: number) => {
      if (!(selectedSessionId && onForkSession)) {
        return;
      }
      try {
        await onForkSession(selectedSessionId, turnIndex);
        toast.success(t("fork.successTitle"));
      } catch (error) {
        toast.error(t("fork.errorTitle"), {
          description:
            error instanceof Error ? error.message : t("fork.fallbackError"),
        });
      }
    },
    [selectedSessionId, onForkSession, t],
  );

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented) {
        return;
      }

      if (event.key.toLowerCase() !== "o") {
        return;
      }

      const hasModifier = isMacOS() ? event.metaKey : event.ctrlKey;
      if (!(hasModifier && event.shiftKey)) {
        return;
      }

      event.preventDefault();
      onOpenCreateDialog?.();
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [onOpenCreateDialog]);

  return (
    <ChatWorkspace
      selectedSessionId={selectedSessionId}
      messages={messages}
      onSubmit={handlePromptSubmit}
      status={status}
      isUploadingFiles={isUploadingFiles}
      onCreateSession={onOpenCreateDialog}
      onCancel={cancelStream}
      onApprovalResponse={respondToApproval}
      onQuestionResponse={respondToQuestion}
      onQuestionDismiss={(requestId) => {
        dismissQuestion(requestId);
        cancelStream();
      }}
      sessionDescription={sessionDescription}
      contextUsage={contextUsage}
      maxContextSize={maxContextSize}
      tokenUsage={tokenUsage}
      currentStep={currentStep}
      currentSession={currentSession}
      isReplayingHistory={isReplayingHistory}
      isHistoryLoaded={isHistoryLoaded}
      isAwaitingFirstResponse={isAwaitingFirstResponse}
      onListSessionDirectory={onListSessionDirectory}
      onGetSessionFileUrl={onGetSessionFileUrl}
      onGetSessionFile={onGetSessionFile}
      onOpenSidebar={onOpenSidebar}
      onRenameSession={onRenameSession}
      slashCommands={slashCommands}
      planMode={planMode}
      onPlanModeChange={handlePlanModeChange}
      yolo={yolo}
      onYoloChange={handleYoloChange}
      onSelectSessionModel={onSelectSessionModel}
      draftModel={draftModel}
      onDraftModelChange={onDraftModelChange}
      errorMessage={sessionError ?? streamError?.message}
      sessionError={sessionError}
      onDismissSessionError={() => clearSessionError(sessionId)}
      onForkSession={onForkSession ? handleForkSession : undefined}
    />
  );
}
