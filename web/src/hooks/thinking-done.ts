/**
 * A thinking block is over once the model moves on from it: to text, to a
 * tool call, or to the next step.
 *
 * Only text used to end it. A step that thinks and then calls a tool never
 * writes text, so its block kept saying "Thinking..." until the whole turn
 * was over.
 */
export function endThinking<T extends { id: string; isStreaming?: boolean }>(
  messages: T[],
  thinkingId: string | null,
): T[] {
  if (thinkingId === null) return messages;
  return messages.map((msg) =>
    msg.id === thinkingId && msg.isStreaming
      ? { ...msg, isStreaming: false }
      : msg,
  );
}
