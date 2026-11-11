/**
 * Whether an inner `SubagentEvent` of the given `innerType` should clear
 * the parent Agent tool call's `subagentRunning` flag.
 *
 * Only the subagent's own `TurnEnd` does: that is the terminal state for
 * the parent Agent tool call. The parent's `ToolResult` may follow
 * immediately (normal completion) or never arrive (user cancelled; the
 * backend's `KimoSoul.run` finally compensates). Nested `SubagentEvent`s
 * do NOT clear — a nested turn ending means the parent subagent is still
 * running.
 */
export function shouldClearSubagentRunning(innerType: string): boolean {
  return innerType === "TurnEnd";
}
