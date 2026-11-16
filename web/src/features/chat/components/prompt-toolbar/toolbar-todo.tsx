import { type ReactElement, memo } from "react";
import {
  CheckCircle2Icon,
  CheckSquare2Icon,
  ChevronDownIcon,
  CircleDotIcon,
  CircleIcon,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import type { TodoItem, TodoStaleReason } from "@/features/tool/store";

// ─── Stale badge ─────────────────────────────────────────────

type StaleBadgeProps = {
  stale: TodoStaleReason | null;
};

/** Small chip marking the list as not-live: interrupted or carried over. */
const TodoStaleBadge = memo(function TodoStaleBadgeComponent({
  stale,
}: StaleBadgeProps): ReactElement | null {
  const { t } = useTranslation("chat");
  if (!stale) return null;
  return (
    <span
      className={cn(
        "rounded px-1 py-px text-[10px] leading-4 font-medium",
        stale === "interrupted"
          ? "bg-amber-500/15 text-amber-600 dark:text-amber-400"
          : "bg-muted text-muted-foreground",
      )}
    >
      {stale === "interrupted"
        ? t("todo.staleInterrupted")
        : t("todo.staleCarried")}
    </span>
  );
});

// ─── Panel ───────────────────────────────────────────────────

type ToolbarTodoPanelProps = {
  items: TodoItem[];
  stale?: TodoStaleReason | null;
};

export const ToolbarTodoPanel = memo(function ToolbarTodoPanelComponent({
  items,
  stale,
}: ToolbarTodoPanelProps): ReactElement {
  return (
    <>
      {stale && (
        <div className="px-3 pb-0.5 pt-1">
          <TodoStaleBadge stale={stale} />
        </div>
      )}
      {items.map((item, index) => (
        <div
          key={`${index}-${item.title}`}
          className="flex items-center gap-2 px-3 py-1 text-xs"
        >
          {item.status === "done" && (
            <CheckCircle2Icon className="size-3 flex-shrink-0 text-emerald-500" />
          )}
          {item.status === "in_progress" && (
            <CircleDotIcon className="size-3 flex-shrink-0 text-blue-500" />
          )}
          {item.status === "pending" && (
            <CircleIcon className="size-3 flex-shrink-0 text-muted-foreground" />
          )}
          <span
            className={cn(
              "truncate",
              item.status === "done"
                ? "line-through text-muted-foreground"
                : item.status === "in_progress"
                  ? "text-foreground font-medium"
                  : "text-muted-foreground",
            )}
          >
            {item.title}
          </span>
        </div>
      ))}
    </>
  );
});

// ─── Tab ─────────────────────────────────────────────────────

type ToolbarTodoTabProps = {
  items: TodoItem[];
  stale?: TodoStaleReason | null;
  isActive: boolean;
  onToggle: () => void;
};

export const ToolbarTodoTab = memo(function ToolbarTodoTabComponent({
  items,
  stale,
  isActive,
  onToggle,
}: ToolbarTodoTabProps): ReactElement {
  const { t } = useTranslation("chat");
  const doneCount = items.filter((i) => i.status === "done").length;
  const totalCount = items.length;

  return (
    <button
      type="button"
      onClick={onToggle}
      className={cn(
        "flex items-center gap-1.5 h-7 px-2.5 rounded-full text-xs font-medium transition-colors cursor-pointer border",
        isActive
          ? "bg-secondary text-foreground border-border shadow-sm"
          : "bg-transparent text-muted-foreground border-border/60 hover:text-foreground hover:border-border",
      )}
    >
      <CheckSquare2Icon className="size-3" />
      <span>
        {t("todo.progress", { done: doneCount, total: totalCount })}
      </span>
      <TodoStaleBadge stale={stale ?? null} />
      <ChevronDownIcon
        className={cn(
          "size-3 transition-transform duration-200",
          isActive && "rotate-180",
        )}
      />
    </button>
  );
});
