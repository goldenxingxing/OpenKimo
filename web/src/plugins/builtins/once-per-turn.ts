/**
 * Which thinking block of a turn gets the animation: the first, played through.
 *
 * It used to start with every thinking block and vanish when the block ended,
 * so a turn of many short steps flashed it on and off.
 */
export type OncePerTurn = {
  /** A new turn: the next thinking block may play again. */
  beginTurn(): void;
  /** The block to play for, or null when this turn's has already played. */
  blockFor(messageId: string): string | null;
  /** The animation finished or was dismissed. */
  played(): void;
};

export function oncePerTurn(): OncePerTurn {
  let first: string | null = null;
  let done = false;
  return {
    beginTurn() {
      first = null;
      done = false;
    },
    blockFor(messageId) {
      first ??= messageId;
      return done ? null : first;
    },
    played() {
      done = true;
    },
  };
}
