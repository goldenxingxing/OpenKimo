/**
 * Diagnostics for duplicate message delivery on the session stream.
 *
 * A web session rebuilds its whole conversation from history replay on every
 * reconnect, and a 45s silence watchdog reconnects often during long tool
 * waits. A duplicate rendered reply that heals after revisiting the page means
 * some path delivered or merged one wire record twice in memory. These helpers
 * detect the two shapes that can produce that — the exact same raw frame
 * delivered twice, and two consecutive assistant messages with identical
 * content — and only log; they never change behavior.
 */

/** Evicting window of recently seen raw frames, for exact-duplicate detection. */
export type DeliveryWindow = {
  /** Record a frame; returns true when the identical frame was seen before. */
  checkAndAdd: (raw: string) => boolean;
  /** Forget everything (a new connection starts a fresh window). */
  clear: () => void;
};

export function createDeliveryWindow(maxEntries = 300): DeliveryWindow {
  let order: string[] = [];
  const seen = new Set<string>();
  return {
    checkAndAdd(raw: string): boolean {
      const duplicate = seen.has(raw);
      if (duplicate) return true;
      seen.add(raw);
      order.push(raw);
      if (order.length > maxEntries) {
        const evicted = order.shift();
        if (evicted !== undefined) seen.delete(evicted);
      }
      return false;
    },
    clear(): void {
      order = [];
      seen.clear();
    },
  };
}

type MessageLike = {
  role?: string;
  variant?: string;
  content?: string;
  thinking?: string;
};

/**
 * Whether `incoming` would render identically to `last` — the in-memory merge
 * produced a copy of a message that is already on screen.
 */
export function isDuplicateRender(
  last: MessageLike | undefined,
  incoming: MessageLike,
): boolean {
  if (!last || last.role !== incoming.role) return false;
  if (last.variant !== incoming.variant) return false;
  if (incoming.variant === "thinking") {
    return Boolean(incoming.thinking) && last.thinking === incoming.thinking;
  }
  return Boolean(incoming.content) && last.content === incoming.content;
}
