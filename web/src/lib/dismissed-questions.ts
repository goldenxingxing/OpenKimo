/**
 * Questions the user closed with "Stop run".
 *
 * A question asked by a turn that later died (app quit, worker restart) can
 * never be answered or cancelled server-side: the request only ever lived in
 * the dead worker's memory, while the wire history keeps resurrecting the
 * dialog on every reconnect. Remembering the dismissal per session keeps it
 * closed for good.
 */

const KEY_PREFIX = "openkimo:dismissed-questions:";

function storageKey(sessionId: string): string {
  return `${KEY_PREFIX}${sessionId}`;
}

export function loadDismissedQuestions(
  sessionId: string | null | undefined,
): Set<string> {
  if (!sessionId) return new Set();
  try {
    const raw = window.localStorage.getItem(storageKey(sessionId));
    if (!raw) return new Set();
    const parsed: unknown = JSON.parse(raw);
    return new Set(Array.isArray(parsed) ? (parsed as string[]) : []);
  } catch {
    return new Set();
  }
}

export function recordDismissedQuestion(
  sessionId: string | null | undefined,
  requestId: string,
): void {
  if (!sessionId) return;
  try {
    const ids = loadDismissedQuestions(sessionId);
    ids.add(requestId);
    window.localStorage.setItem(storageKey(sessionId), JSON.stringify([...ids]));
  } catch {
    // Storage unavailable (private mode, quota) — the in-memory set in the
    // stream hook still covers this page's lifetime.
  }
}
