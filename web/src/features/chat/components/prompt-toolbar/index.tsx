import {
  type ReactElement,
  memo,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { cn } from "@/lib/utils";
import type { TokenUsage } from "@/hooks/wireTypes";
import { useQueueStore } from "../../queue-store";
import { useToolEventsStore } from "@/features/tool/store";
import { ToolbarActivityIndicator, type ActivityDetail } from "../activity-status-indicator";
import { ToolbarQueuePanel, ToolbarQueueTab } from "./toolbar-queue";
import { ToolbarTodoPanel, ToolbarTodoTab } from "./toolbar-todo";
import { ToolbarContextIndicator } from "./toolbar-context";

// ─── Types ───────────────────────────────────────────────────

type TabId = "queue" | "todo";

type PromptToolbarProps = {
  planMode?: boolean;
  activityStatus?: ActivityDetail;
  usagePercent?: number;
  usedTokens?: number;
  maxTokens?: number;
  tokenUsage?: TokenUsage | null;
};

// ─── Main toolbar ────────────────────────────────────────────

export const PromptToolbar = memo(function PromptToolbarComponent({
  planMode = false,
  activityStatus,
  usagePercent,
  usedTokens,
  maxTokens,
  tokenUsage,
}: PromptToolbarProps): ReactElement | null {
  const queue = useQueueStore((s) => s.queue);
  const todoItems = useToolEventsStore((s) => s.todoItems);
  const todoStale = useToolEventsStore((s) => s.todoStale);
  const [activeTab, setActiveTab] = useState<TabId | null>(null);
  const prevQueueLenRef = useRef(0);

  const hasQueue = queue.length > 0;
  const hasTodo = todoItems.length > 0;
  const hasContext = usagePercent !== undefined && usedTokens !== undefined && maxTokens !== undefined;
  const hasTabs = hasQueue || hasTodo;

  // Auto-open queue tab when first item is added
  useEffect(() => {
    if (prevQueueLenRef.current === 0 && queue.length > 0) {
      setActiveTab("queue");
    }
    prevQueueLenRef.current = queue.length;
  }, [queue.length]);

  // Auto-close tab when its data becomes empty
  useEffect(() => {
    if (activeTab === "queue" && !hasQueue) setActiveTab(null);
    if (activeTab === "todo" && !hasTodo) setActiveTab(null);
  }, [activeTab, hasQueue, hasTodo]);

  const toggleTab = useCallback((tab: TabId) => {
    setActiveTab((prev) => (prev === tab ? null : tab));
  }, []);
  const toggleQueueTab = useCallback(() => toggleTab("queue"), [toggleTab]);
  const toggleTodoTab = useCallback(() => toggleTab("todo"), [toggleTab]);

  if (!(hasTabs || activityStatus || hasContext || planMode)) return null;

  return (
    <div className={cn("w-full px-1 sm:px-2 flex flex-col gap-1 mb-2")}>
      {/* ── Expanded panel ── */}
      {activeTab && (
        <div className={cn(
          "rounded-md border border-border bg-background",
          "max-h-32 overflow-y-auto py-1 px-0.5",
        )}>
          {activeTab === "queue" && <ToolbarQueuePanel queue={queue} />}
          {activeTab === "todo" && (
            <ToolbarTodoPanel items={todoItems} stale={todoStale} />
          )}
        </div>
      )}

      {/* ── Tab bar ── */}
      <div className="flex items-center gap-1.5 px-1">
{activityStatus && (
          <ToolbarActivityIndicator activity={activityStatus} />
        )}

        {hasQueue && (
          <ToolbarQueueTab
            count={queue.length}
            isActive={activeTab === "queue"}
            onToggle={toggleQueueTab}
          />
        )}

        {hasTodo && (
          <ToolbarTodoTab
            items={todoItems}
            stale={todoStale}
            isActive={activeTab === "todo"}
            onToggle={toggleTodoTab}
          />
        )}

        {hasContext && (
          <ToolbarContextIndicator
            usagePercent={usagePercent!}
            usedTokens={usedTokens!}
            maxTokens={maxTokens!}
            tokenUsage={tokenUsage ?? null}
            className="ml-auto"
          />
        )}
      </div>
    </div>
  );
});
