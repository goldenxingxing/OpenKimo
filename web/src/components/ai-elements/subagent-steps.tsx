"use client";

import { cn } from "@/lib/utils";
import type { SubagentStep } from "@/hooks/types";
import type { TodoItem } from "@/features/tool/store";
import type { ComponentProps } from "react";
import { memo, useState } from "react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Shimmer } from "./shimmer";
import {
  CheckIcon,
  ChevronRightIcon,
  CircleDashedIcon,
  CircleIcon,
  Loader2Icon,
  XIcon,
} from "lucide-react";

// ---------------------------------------------------------------------------
// SubagentActivity — top-level wrapper rendered inside Tool's ToolContent area
// ---------------------------------------------------------------------------

export type SubagentActivityProps = ComponentProps<"div"> & {
  steps: SubagentStep[];
  isRunning?: boolean;
  defaultOpen?: boolean;
  /** Built-in subagent type (coder / explore / plan) */
  subagentType?: string;
};

export const SubagentActivity = memo(
  ({
    className,
    steps,
    isRunning = false,
    defaultOpen = false,
    subagentType,
    ...props
  }: SubagentActivityProps) => {
    const agentLabel = subagentType
      ? `${subagentType.charAt(0).toUpperCase() + subagentType.slice(1)} agent`
      : "Agent";
    const [isOpen, setIsOpen] = useState(defaultOpen);

    const toolCallCount = steps.filter((s) => s.kind === "tool-call").length;
    const hasError = steps.some(
      (s) => s.kind === "tool-call" && s.status === "error",
    );
    // Shown on the collapsed header too: that is all most people see of it.
    const retry = isRunning ? steps.find((s) => s.kind === "retry") : undefined;
    // The delegation's own todo breakdown, when the subagent keeps one. On the
    // collapsed header it is the only honest progress signal while it runs —
    // the root's Tasks panel is frozen for the whole delegation.
    const todoStep = [...steps].reverse().find((s) => s.kind === "todo");
    const todoCount =
      todoStep && todoStep.kind === "todo"
        ? {
            done: todoStep.items.filter((i) => i.status === "done").length,
            total: todoStep.items.length,
          }
        : null;

    return (
      <Collapsible
        className={cn("mt-2", className)}
        open={isOpen}
        onOpenChange={setIsOpen}
        {...props}
      >
        <CollapsibleTrigger className="flex items-center gap-1.5 text-xs text-muted-foreground group cursor-pointer">
          <span
            className={cn(
              "size-1.5 rounded-full shrink-0",
              isRunning
                ? "bg-blue-500 animate-pulse"
                : hasError
                  ? "bg-destructive"
                  : "bg-success",
            )}
          />
          <span>
            {retry ? (
              <span className="text-amber-600 dark:text-amber-400">
                {agentLabel} · {retry.text}
              </span>
            ) : isRunning ? (
              <>
                {agentLabel} working
                {todoCount && todoCount.total > 0 && (
                  <span className="text-muted-foreground">
                    {" "}
                    · {todoCount.done}/{todoCount.total}
                  </span>
                )}
                <Shimmer
                  as="span"
                  duration={1}
                  className="text-muted-foreground ml-0.5"
                >
                  ...
                </Shimmer>
              </>
            ) : toolCallCount > 0 ? (
              `${agentLabel} completed · ${toolCallCount} tool call${toolCallCount !== 1 ? "s" : ""}`
            ) : (
              `${agentLabel} completed`
            )}
          </span>
          <ChevronRightIcon
            className={cn(
              "size-3 text-muted-foreground transition-transform duration-200",
              isOpen && "rotate-90",
            )}
          />
        </CollapsibleTrigger>

        <CollapsibleContent
          className={cn(
            "mt-1.5 space-y-0.5 border-l-2 border-border pl-3",
            "data-[state=closed]:fade-out-0 data-[state=open]:slide-in-from-top-1 outline-none data-[state=closed]:animate-out data-[state=open]:animate-in",
          )}
        >
          {steps.map((step, index) => (
            <SubagentStepItem
              key={
                step.kind === "tool-call"
                  ? `sa-step-${step.toolCallId}`
                  : `sa-step-${index}`
              }
              step={step}
            />
          ))}
        </CollapsibleContent>
      </Collapsible>
    );
  },
);

SubagentActivity.displayName = "SubagentActivity";

// ---------------------------------------------------------------------------
// SubagentStepItem — renders a single step based on kind
// ---------------------------------------------------------------------------

const SubagentStepItem = ({ step }: { step: SubagentStep }) => {
  switch (step.kind) {
    case "thinking":
      return (
        <div className="text-muted-foreground/60 italic text-xs line-clamp-2">
          {step.text}
        </div>
      );

    case "text":
      return (
        <div className="text-foreground/70 text-xs line-clamp-2">
          {step.text}
        </div>
      );

    case "tool-call":
      return <SubToolCallItem step={step} />;

    case "todo":
      return <SubTodoList items={step.items} />;

    case "retry":
      return (
        <div className="text-amber-600 text-xs italic dark:text-amber-400">{step.text}</div>
      );

    default:
      return null;
  }
};

// ---------------------------------------------------------------------------
// SubTodoList — the subagent's current todo frame
// ---------------------------------------------------------------------------

const SubTodoList = ({ items }: { items: TodoItem[] }) => {
  if (!Array.isArray(items) || items.length === 0) return null;
  return (
    <div className="my-1 rounded-md border border-border/50 divide-y divide-border/40">
      {items.map((item, index) => {
        const done = item.status === "done";
        const inProgress = item.status === "in_progress";
        return (
          <div
            key={`sa-todo-${index}-${item.title}`}
            className="flex items-center gap-1.5 px-2 py-1 text-xs"
          >
            {done ? (
              <CheckIcon className="size-2.5 shrink-0 text-success" />
            ) : inProgress ? (
              <CircleDashedIcon className="size-2.5 shrink-0 text-blue-500" />
            ) : (
              <CircleIcon className="size-2.5 shrink-0 text-muted-foreground/40" />
            )}
            <span
              className={cn(
                "truncate",
                done
                  ? "text-muted-foreground line-through"
                  : inProgress
                    ? "text-foreground font-medium"
                    : "text-muted-foreground/80",
              )}
            >
              {item.title}
            </span>
          </div>
        );
      })}
    </div>
  );
};

// ---------------------------------------------------------------------------
// SubToolCallItem — expandable sub-tool-call with status + output
// ---------------------------------------------------------------------------

/** Extract a primary parameter value for inline display */
const getPrimaryParam = (input: unknown): string | null => {
  if (!input || typeof input !== "object") return null;
  const keys = ["path", "command", "pattern", "url", "query", "file_path"];
  for (const key of keys) {
    const val = (input as Record<string, unknown>)[key];
    if (typeof val === "string" && val.length > 0) {
      return val.length > 50 ? `${val.slice(0, 50)}…` : val;
    }
  }
  return null;
};

const getSubToolStatusIcon = (status: string) => {
  switch (status) {
    case "success":
      return <CheckIcon className="size-2.5 text-success shrink-0" />;
    case "error":
      return <XIcon className="size-2.5 text-destructive shrink-0" />;
    default:
      return <Loader2Icon className="size-2.5 text-muted-foreground animate-spin shrink-0" />;
  }
};

const SubToolCallItem = ({
  step,
}: {
  step: Extract<SubagentStep, { kind: "tool-call" }>;
}) => {
  const [expanded, setExpanded] = useState(false);
  const primaryParam = getPrimaryParam(step.input);
  const hasExpandableContent = Boolean(step.output || step.errorText);

  return (
    <div>
      <div
        className={cn(
          "flex items-center gap-1 text-xs",
          hasExpandableContent && "cursor-pointer",
        )}
        onClick={() => hasExpandableContent && setExpanded(!expanded)}
        onKeyDown={(e) => {
          if (hasExpandableContent && (e.key === "Enter" || e.key === " ")) {
            e.preventDefault();
            setExpanded(!expanded);
          }
        }}
        role={hasExpandableContent ? "button" : undefined}
        tabIndex={hasExpandableContent ? 0 : undefined}
      >
        {getSubToolStatusIcon(step.status)}
        <span className="text-primary/80 font-medium">{step.toolName}</span>
        {primaryParam && !expanded && (
          <span className="text-muted-foreground truncate">
            ({primaryParam})
          </span>
        )}
        {hasExpandableContent && (
          <ChevronRightIcon
            className={cn(
              "size-2.5 text-muted-foreground/50 transition-transform duration-200",
              expanded && "rotate-90",
            )}
          />
        )}
      </div>
      {expanded && (
        <div className="ml-4 mt-0.5">
          {step.errorText && (
            <pre className="text-xs text-destructive whitespace-pre-wrap max-h-24 overflow-y-auto">
              {step.errorText}
            </pre>
          )}
          {step.output && !step.errorText && (
            <pre className="text-xs text-foreground/60 whitespace-pre-wrap max-h-24 overflow-y-auto">
              {step.output.length > 500
                ? `${step.output.slice(0, 500)}…`
                : step.output}
            </pre>
          )}
        </div>
      )}
    </div>
  );
};
