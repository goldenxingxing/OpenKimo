import type { LiveMessage } from "@/hooks/types";
import {
  Message,
  MessageActions,
  MessageAttachment,
  MessageAttachments,
  MessageContent,
  MessageCopyButton,
  MessageForkButton,
  UserMessageContent,
} from "@ai-elements";
import {
  AssistantMessage,
  type AssistantApprovalHandler,
} from "./assistant-message";

import type React from "react";
import {
  forwardRef,
  memo,
  type ComponentPropsWithoutRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";
import { cn } from "@/lib/utils";
import {
  MessageOutputTime,
  shouldShowMessageOutputTime,
} from "../message-output-time";
import { decideFollowOutput, nextCatchUp } from "./scroll-follow-policy";

export type VirtualizedMessageListProps = {
  messages: LiveMessage[];
  conversationKey: string;
  pendingApprovalMap: Record<string, boolean>;
  onApprovalAction?: AssistantApprovalHandler;
  canRespondToApproval: boolean;
  blocksExpanded: boolean;
  /** Index of message to highlight (for search) */
  highlightedMessageIndex?: number;
  /** Callback when scroll position changes */
  onAtBottomChange?: (atBottom: boolean) => void;
  /** Callback to fork session from before a specific turn */
  onForkSession?: (turnIndex: number) => void;
  /** True while history is being replayed into the list */
  isReplayingHistory?: boolean;
  /** Session work directory, used to resolve relative path mentions. */
  workDir?: string | null;
};

export type VirtualizedMessageListHandle = {
  scrollToIndex: (index: number, behavior?: "auto" | "smooth") => void;
  scrollToBottom: () => void;
};

/** Close enough to the bottom that a catch-up scroll would land where we already are. */
const CATCHUP_SETTLED_GAP_PX = 1;

function VirtuosoScrollerComponent(
  props: ComponentPropsWithoutRef<"div">,
  ref: React.Ref<HTMLDivElement>,
) {
  const { className, ...rest } = props;
  return (
    <div
      ref={ref}
      className={cn(
        "flex-1 overflow-y-auto overflow-x-hidden pr-1 sm:pr-2",
        className,
      )}
      {...rest}
    />
  );
}

/** Close enough to the bottom to show a freshly mounted list. */
const REVEAL_GAP_PX = 2;
/** Show it anyway after this long, rather than leave a list that never settles hidden. */
const REVEAL_TIMEOUT_MS = 400;

const VirtuosoScroller = forwardRef(VirtuosoScrollerComponent);

function VirtuosoListComponent(
  props: ComponentPropsWithoutRef<"div">,
  ref: React.Ref<HTMLDivElement>,
) {
  const { className, ...rest } = props;
  return (
    <div
      ref={ref}
      className={cn("flex flex-col px-3 py-4 sm:px-6 lg:px-8", className)}
      {...rest}
    />
  );
}

const VirtuosoList = forwardRef(VirtuosoListComponent);

VirtuosoScroller.displayName = "VirtuosoScroller";
VirtuosoList.displayName = "VirtuosoList";

function getMessageSpacingClass(
  message: LiveMessage,
  index: number,
  previousMessage: LiveMessage | undefined,
  nextMessage: LiveMessage | undefined,
): string | undefined {
  // Terminal-style message spacing - more compact
  // 1. User messages get breathing room (`mt-3`) from previous content
  // 2. Assistant messages flow naturally with minimal spacing
  // 3. Tool calls have subtle spacing to group related operations

  const classes: string[] = [];

  const isUser = message.role === "user";
  const isAssistant = message.role === "assistant";
  const isToolMessage = isAssistant && message.variant === "tool";
  const isThinkingMessage = isAssistant && message.variant === "thinking";
  const previousIsUser = previousMessage?.role === "user";
  const previousIsAssistant = previousMessage?.role === "assistant";
  const previousIsTool =
    previousIsAssistant && previousMessage?.variant === "tool";

  if (index > 0) {
    if (isUser) {
      // User messages get more space from previous content
      classes.push("mt-4");
    } else if (isAssistant) {
      if (isToolMessage) {
        // Tool calls: slightly more breathing room between consecutive calls
        classes.push(previousIsUser ? "mt-2" : "mt-1.5");
      } else if (isThinkingMessage) {
        // Thinking blocks have minimal spacing
        classes.push(previousIsUser ? "mt-2" : "mt-1");
      } else if (previousIsTool) {
        // Text after tool gets slight spacing
        classes.push("mt-2");
      } else if (previousIsAssistant) {
        // Consecutive assistant messages flow together
        classes.push("mt-1");
      } else {
        // After user message
        classes.push("mt-2");
      }
    }
  }

  // Add bottom margin for the last message to avoid clashing with UI below
  if (!nextMessage) {
    classes.push("mb-30");
  }

  return classes.length > 0 ? classes.join(" ") : undefined;
}

type MessageListItemProps = {
  message: LiveMessage;
  index: number;
  previousMessage: LiveMessage | undefined;
  nextMessage: LiveMessage | undefined;
  isHighlighted: boolean;
  pendingApprovalMap: Record<string, boolean>;
  onApprovalAction?: AssistantApprovalHandler;
  canRespondToApproval: boolean;
  blocksExpanded: boolean;
  onForkSession?: (turnIndex: number) => void;
  /** Session work directory, used to resolve relative path mentions. */
  workDir?: string | null;
};

/**
 * One row of the conversation. Memoized: during streaming every token
 * updates the message array, and without this memo every visible row
 * re-rendered (and re-parsed its markdown) at token frequency. The props
 * are reference-stable for messages the update did not touch —
 * useSessionStream returns the same object for those — so the memo
 * only lets the changed rows through.
 */
const MessageListItem = memo(function MessageListItem({
  message,
  index,
  previousMessage,
  nextMessage,
  isHighlighted,
  pendingApprovalMap,
  onApprovalAction,
  canRespondToApproval,
  blocksExpanded,
  onForkSession,
  workDir,
}: MessageListItemProps) {
  const spacingClass = getMessageSpacingClass(
    message,
    index,
    previousMessage,
    nextMessage,
  );

  return (
    <Message
      className={cn(
        spacingClass,
        isHighlighted && "rounded-lg ring-2 ring-primary/50",
      )}
      from={message.role}
    >
      {message.role === "user" ? (
        message.content ? (
          <UserMessageContent>{message.content}</UserMessageContent>
        ) : null
      ) : (
        <>
          <AssistantMessage
            message={message}
            pendingApprovalMap={pendingApprovalMap}
            onApprovalAction={onApprovalAction}
            canRespondToApproval={canRespondToApproval}
            blocksExpanded={blocksExpanded}
            workDir={workDir}
          />
          {!message.isStreaming &&
            (!message.variant || message.variant === "text") &&
            (message.content || (onForkSession && message.turnIndex !== undefined)) && (
            <MessageActions className="
            hover-reveal
             opacity-0 group-hover:opacity-100 transition-opacity mt-1">
              {message.content && <MessageCopyButton content={message.content} />}
              {onForkSession && message.turnIndex !== undefined && (
                <MessageForkButton onFork={() => onForkSession(message.turnIndex!)} />
              )}
              {shouldShowMessageOutputTime(message) && (
                <MessageOutputTime completedAt={message.completedAt!} />
              )}
            </MessageActions>
          )}
        </>
      )}
      {message.attachments && message.attachments.length > 0 ? (
        <MessageAttachments>
          {message.attachments.map((attachment, attIdx) => {
            const key =
              "kind" in attachment
                ? attachment.filename
                : (attachment.filename ??
                  attachment.url ??
                  `${message.id}-${attIdx}`);
            return (
              <MessageAttachment
                className="size-28 sm:size-32 lg:size-40"
                data={attachment}
                key={key}
              />
            );
          })}
        </MessageAttachments>
      ) : null}
    </Message>
  );
});

function VirtualizedMessageListComponent(
  {
    messages,
    conversationKey,
    pendingApprovalMap,
    onApprovalAction,
    canRespondToApproval,
    blocksExpanded,
    highlightedMessageIndex = -1,
    onAtBottomChange,
    onForkSession,
    isReplayingHistory = false,
    workDir,
  }: VirtualizedMessageListProps,
  ref: React.Ref<VirtualizedMessageListHandle>,
) {
  const virtuosoRef = useRef<VirtuosoHandle | null>(null);
  const scrollerRef = useRef<HTMLElement | null>(null);

  // When the list is rebuilt from empty (WS reconnect / worker restart
  // clears messages, then history streams back in), stick to the bottom
  // until the viewport has settled there once.  This is independent of
  // the isReplayingHistory flag, which a session_status event can clear
  // prematurely while replay events are still in flight — without this
  // guard the 1500px gap rule below would pin the viewport to the top.
  const catchUpRef = useRef(true);
  const prevItemCountRef = useRef(0);
  // This component is not remounted when the reader switches conversation —
  // only the inner Virtuoso is keyed — so the refs above outlive the
  // conversation they describe unless the switch is noticed here.
  const prevConversationKeyRef = useRef(conversationKey);

  // Set when the reader scrolls UP away from the bottom under their own
  // power; cleared when they come back to the bottom. While set, followOutput
  // refuses to pull the viewport down no matter how close to the bottom the
  // reader stayed — scrolling up to re-read something is an explicit "let me
  // stay here", even mid-stream. (The 1500px gap tolerance below is only for
  // height-estimate drift, not for a reader who has already left.)
  const readerAwayRef = useRef(false);

  // Filtered messages list (excluding message-id). Item references are
  // stable across token updates (useSessionStream maps and returns the same
  // object for untouched messages), which is what lets Virtuoso skip
  // re-rendering messages that did not change.
  const filteredMessages = useMemo(
    () => messages.filter((m) => m.variant !== "message-id"),
    [messages],
  );
  const filteredMessagesRef = useRef(filteredMessages);
  filteredMessagesRef.current = filteredMessages;

  // A different conversation starts at its newest message, always. Reading
  // position belongs to the conversation it was taken in: without this, having
  // scrolled up anywhere left catch-up off, and the next session opened
  // wherever the timing of its history happened to leave the viewport — the
  // top, as often as not.
  //
  // Both checks are assigned during render so the flags are set before Virtuoso
  // invokes followOutput for the incoming items (a useEffect would run after
  // Virtuoso's own effects — too late).
  if (prevConversationKeyRef.current !== conversationKey) {
    prevConversationKeyRef.current = conversationKey;
    prevItemCountRef.current = 0;
    readerAwayRef.current = false;
    catchUpRef.current = nextCatchUp(catchUpRef.current, {
      type: "conversation-switched",
    });
  }

  // Detect an empty -> non-empty transition (rebuild from scratch).
  if (prevItemCountRef.current === 0 && filteredMessages.length > 0) {
    catchUpRef.current = nextCatchUp(catchUpRef.current, {
      type: "list-rebuilt",
    });
  }
  prevItemCountRef.current = filteredMessages.length;

  // followOutput scrolls to the last item while the items it just added are
  // still sized by `defaultItemHeight`. Real messages are far taller than that
  // estimate, so as they get measured the content bottom moves away and the
  // viewport is left stranded above it — the more the estimate was wrong, the
  // further up. That is what made a replayed conversation open partway up its
  // own history despite the policy asking to follow every single append.
  //
  // So while catch-up is armed, follow the *height* too, not just the count.
  // Scrolling to an index cannot itself change the height, so this settles
  // rather than loops, and the reader's first wheel/touch/key/drag disarms it.
  //
  // Skip the scroll when the viewport is already at the end: during
  // streaming every token changes the total height, and repeatedly forcing
  // scrollToIndex onto a viewport that is already there showed as a constant
  // fight with the reader's own position.
  const handleTotalListHeightChanged = useCallback(() => {
    if (!catchUpRef.current) return;
    const count = filteredMessagesRef.current.length;
    if (count === 0) return;
    const scroller = scrollerRef.current;
    if (
      scroller &&
      scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <=
        CATCHUP_SETTLED_GAP_PX
    ) {
      return;
    }
    virtuosoRef.current?.scrollToIndex({
      index: count - 1,
      align: "end",
      behavior: "auto",
    });
  }, []);

  const handleAtBottomChange = useCallback(
    (atBottom: boolean) => {
      if (atBottom) {
        // Back at the bottom — by scrollbar, wheel, or a jump — following
        // new output resumes.
        readerAwayRef.current = false;
      }
      // Deliberately does not end catch-up. Early in a replay only a few
      // messages exist, so the viewport reaches the bottom trivially; ending
      // catch-up there strands the reader at the top once the rest of the
      // history arrives. Only the reader's own scrolling ends it — see
      // nextCatchUp() and its tests.
      onAtBottomChange?.(atBottom);
    },
    [onAtBottomChange],
  );

  // Catch-up ends when the reader moves the viewport themselves. Wheel, touch,
  // key, and scrollbar-drag (pointerdown) events are the intent signals;
  // programmatic scrolling (ours) is not, which is why this listens for input
  // rather than for scroll events. pointerdown matters on its own: dragging
  // the scrollbar fires no wheel/touch/key events, so without it a reader who
  // scrolled up by dragging was snapped back to the newest message on every
  // token — the streaming-page "shaking".
  const detachReaderIntentRef = useRef<(() => void) | null>(null);
  const handleScrollerRef = useCallback(
    (ref: HTMLElement | Window | null) => {
      detachReaderIntentRef.current?.();
      detachReaderIntentRef.current = null;

      const element = ref instanceof HTMLElement ? ref : null;
      scrollerRef.current = element;
      if (!element) return;

      const takeControl = () => {
        catchUpRef.current = nextCatchUp(catchUpRef.current, {
          type: "reader-took-control",
        });
      };
      // Marks "the reader left the bottom upward". Only up-intent counts:
      // a click on a message button (pointerdown bubbling from content) must
      // not disable following, and wheeling DOWN toward the bottom is
      // cleared by the at-bottom transition anyway.
      const markAway = () => {
        readerAwayRef.current = true;
      };
      const options = { passive: true } as const;
      let lastTouchY: number | null = null;
      const onWheel = (event: WheelEvent) => {
        takeControl();
        if (event.deltaY < 0) markAway();
      };
      const onTouchStart = (event: TouchEvent) => {
        lastTouchY = event.touches[0]?.clientY ?? null;
      };
      const onTouchMove = (event: TouchEvent) => {
        takeControl();
        const y = event.touches[0]?.clientY ?? null;
        // Finger moving down = the viewport travels up through the content.
        if (lastTouchY !== null && y !== null && y > lastTouchY) markAway();
        lastTouchY = y;
      };
      const onKeyDown = (event: KeyboardEvent) => {
        takeControl();
        if (
          event.key === "ArrowUp" ||
          event.key === "PageUp" ||
          event.key === "Home"
        ) {
          markAway();
        }
      };
      const onPointerDown = (event: PointerEvent) => {
        takeControl();
        // A pointerdown ON the scroller element itself (not bubbled from
        // message content) is a scrollbar interaction — the only way to drag
        // the scrollbar, which fires no wheel/touch/key events.
        if (event.target === element) markAway();
      };
      element.addEventListener("wheel", onWheel, options);
      element.addEventListener("touchstart", onTouchStart, options);
      element.addEventListener("touchmove", onTouchMove, options);
      element.addEventListener("keydown", onKeyDown, options);
      element.addEventListener("pointerdown", onPointerDown, options);
      detachReaderIntentRef.current = () => {
        element.removeEventListener("wheel", onWheel);
        element.removeEventListener("touchstart", onTouchStart);
        element.removeEventListener("touchmove", onTouchMove);
        element.removeEventListener("keydown", onKeyDown);
        element.removeEventListener("pointerdown", onPointerDown);
      };
    },
    [],
  );

  useEffect(
    () => () => {
      detachReaderIntentRef.current?.();
      detachReaderIntentRef.current = null;
    },
    [],
  );

  // Use a generous threshold to tolerate height estimation mismatches
  // when blocks are expanded (actual heights >> defaultItemHeight).
  // This is decoupled from atBottomStateChange which uses Virtuoso's
  // default tight threshold for the scroll-to-bottom button.
  const handleFollowOutput = useCallback(
    (isAtBottom: boolean) => {
      const scroller = scrollerRef.current;
      return decideFollowOutput({
        isAtBottom,
        isReplayingHistory,
        isCatchingUp: catchUpRef.current,
        readerAway: readerAwayRef.current,
        gapToBottom: scroller
          ? scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight
          : null,
      });
    },
    [isReplayingHistory],
  );

  // A list filled from empty — a conversation opened, or rebuilt — starts at
  // its newest message, but it is laid out with estimated heights first, so
  // the first frame can show the top before the measurements land. Kept
  // invisible until the bottom is actually in view, or a short while has
  // passed, so a list that cannot settle is never left hidden.
  const [revealed, setRevealed] = useState(filteredMessages.length === 0);
  const [wasEmpty, setWasEmpty] = useState(filteredMessages.length === 0);
  const isEmpty = filteredMessages.length === 0;
  if (isEmpty !== wasEmpty) {
    // Adjusted during render so the filled list is never painted unhidden.
    setWasEmpty(isEmpty);
    setRevealed(isEmpty);
  }
  useLayoutEffect(() => {
    if (revealed) return;
    const started = performance.now();
    let frame = 0;
    const check = () => {
      const scroller = scrollerRef.current;
      const last = filteredMessagesRef.current.length - 1;
      // Before anything is laid out the content is exactly as tall as the
      // viewport, which reads as "at the bottom" — so the newest message has
      // to actually be rendered, and in view, for this to count.
      const settled =
        scroller !== null &&
        scroller.querySelector(`[data-index="${last}"]`) !== null &&
        scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= REVEAL_GAP_PX;
      if (settled || performance.now() - started > REVEAL_TIMEOUT_MS) {
        setRevealed(true);
        return;
      }
      frame = requestAnimationFrame(check);
    };
    frame = requestAnimationFrame(check);
    return () => cancelAnimationFrame(frame);
  }, [revealed]);

  // An explicit jump is reader intent too: without this, catch-up stayed
  // armed after a search jump or a scroll-to-bottom click, and the next
  // height change yanked the viewport back to the newest message.
  const takeControlRef = useRef(() => {
    catchUpRef.current = nextCatchUp(catchUpRef.current, {
      type: "reader-took-control",
    });
  });
  useImperativeHandle(
    ref,
    () => ({
      scrollToIndex: (
        index: number,
        behavior: "auto" | "smooth" = "smooth",
      ) => {
        takeControlRef.current();
        virtuosoRef.current?.scrollToIndex({
          index,
          align: "center",
          behavior,
        });
      },
      scrollToBottom: () => {
        takeControlRef.current();
        const count = filteredMessagesRef.current.length;
        if (count > 0) {
          virtuosoRef.current?.scrollToIndex({
            index: count - 1,
            align: "end",
            behavior: "auto",
          });
        }
      },
    }),
    [],
  );

  const hasAnyMessages = messages.length > 0;

  // Kept referentially stable across token updates so Virtuoso can skip
  // re-rendering rows whose message did not change; neighbor lookups go
  // through the ref, keeping this callback's identity stable too.
  const itemContent = useCallback(
    (index: number, message: LiveMessage) => {
      if (message.variant === "status") {
        return (
          <Message
            className={hasAnyMessages ? "mt-2" : undefined}
            from="assistant"
          >
            <MessageContent className="text-xs text-muted-foreground">
              {message.content}
            </MessageContent>
          </Message>
        );
      }

      const all = filteredMessagesRef.current;
      return (
        <MessageListItem
          message={message}
          index={index}
          previousMessage={index > 0 ? all[index - 1] : undefined}
          nextMessage={index < all.length - 1 ? all[index + 1] : undefined}
          isHighlighted={index === highlightedMessageIndex}
          pendingApprovalMap={pendingApprovalMap}
          onApprovalAction={onApprovalAction}
          canRespondToApproval={canRespondToApproval}
          blocksExpanded={blocksExpanded}
          onForkSession={onForkSession}
          workDir={workDir}
        />
      );
    },
    [
      hasAnyMessages,
      highlightedMessageIndex,
      pendingApprovalMap,
      onApprovalAction,
      canRespondToApproval,
      blocksExpanded,
      onForkSession,
      workDir,
    ],
  );

  return (
    <Virtuoso
      key={conversationKey}
      ref={virtuosoRef}
      data={filteredMessages}
      // Through className: Virtuoso hands it to the scroller, its style it keeps.
      className={cn("h-full", !revealed && "invisible")}
      scrollerRef={handleScrollerRef}
      followOutput={handleFollowOutput}
      // A message above the view holds this height until it first renders and
      // is measured. Most rows of a working session are collapsed tool calls
      // about 30px tall, so 160 had every newly rendered stretch shrink by
      // hundreds of pixels, and the correction, a frame late, showed as a jump
      // on the first scroll up through a history (up to 617px, measured).
      // Guessing short is the safer side: rows that come in taller are taken
      // up without a visible shift. At 32 the worst first-pass shift measured
      // was 27px, in a tool-heavy and a text-heavy session alike.
      defaultItemHeight={32}
      increaseViewportBy={{ top: 400, bottom: 400 }}
      // Apply a measured size in the same frame, not the next one.
      skipAnimationFrameInResizeObserver
      overscan={200}
      minOverscanItemCount={4}
      atBottomStateChange={handleAtBottomChange}
      totalListHeightChanged={handleTotalListHeightChanged}
      initialTopMostItemIndex={{
        index: Math.max(0, filteredMessages.length - 1),
        align: "end",
      }}
      components={{
        Scroller: VirtuosoScroller,
        List: VirtuosoList,
      }}
      computeItemKey={(_index: number, message: LiveMessage) => message.id}
      itemContent={itemContent}
    />
  );
}

export const VirtualizedMessageList = forwardRef(
  VirtualizedMessageListComponent,
);
VirtualizedMessageList.displayName = "VirtualizedMessageList";
