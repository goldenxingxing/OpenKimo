/**
 * Which sessions stopped and are waiting for the person, derived from the
 * `/api/sessions/activity` stream.
 *
 * One reason covers all of it: a reply that finished, an approval or a question
 * left open, an error. The person is told a session needs them, not why.
 */

export type SessionRunState = "stopped" | "idle" | "busy" | "restarting" | "error";

export interface SessionActivity {
  session_id: string;
  state: SessionRunState;
  awaiting_input: boolean;
  seq: number;
}

interface Seen {
  state: SessionRunState;
  awaiting: boolean;
  seq: number;
}

export interface AttentionState {
  last: ReadonlyMap<string, Seen>;
  /** Sessions waiting for the person, oldest first. */
  pending: readonly string[];
}

export const EMPTY_ATTENTION: AttentionState = { last: new Map(), pending: [] };

// "restarting" is not here: a restart is not a turn. Saving config.toml
// restarts every idle worker, and counting that as running made each one's
// return to idle read as a finished turn — sessions untouched for days lit up.
const RUNNING: ReadonlySet<SessionRunState> = new Set(["busy"]);

function without(pending: readonly string[], id: string): readonly string[] {
  return pending.includes(id) ? pending.filter((item) => item !== id) : pending;
}

/**
 * A fresh connection's view. Mostly a baseline, not news: a session that was
 * already idle when the page connected is not announced. One holding an
 * unanswered approval or question is, though — it is stuck until someone
 * answers, and a page that just opened has not shown it to anyone. Sessions
 * already pending stay pending unless they are running again.
 */
export function applySnapshot(
  state: AttentionState,
  sessions: readonly SessionActivity[],
  viewing: string | null,
): AttentionState {
  const last = new Map<string, Seen>();
  let pending = state.pending;
  for (const item of sessions) {
    last.set(item.session_id, {
      state: item.state,
      awaiting: item.awaiting_input,
      seq: item.seq,
    });
    if (item.awaiting_input) {
      if (item.session_id !== viewing && !pending.includes(item.session_id)) {
        pending = [...pending, item.session_id];
      }
    } else if (RUNNING.has(item.state)) {
      pending = without(pending, item.session_id);
    }
  }
  return { last, pending };
}

/**
 * @param viewing the session the person is looking at right now — open, with
 *   the page in front — or null. That one is never announced: they saw it.
 */
export function applyActivity(
  state: AttentionState,
  activity: SessionActivity,
  viewing: string | null,
): AttentionState {
  const id = activity.session_id;
  const before = state.last.get(id);
  if (before && activity.seq <= before.seq) {
    return state;
  }
  const last = new Map(state.last);
  if (activity.state === "restarting") {
    // Transparent: what comes after is judged against what came before, and
    // a session already waiting for the person keeps waiting.
    last.set(id, {
      state: before?.state ?? "idle",
      awaiting: before?.awaiting ?? false,
      seq: activity.seq,
    });
    return { last, pending: state.pending };
  }
  last.set(id, { state: activity.state, awaiting: activity.awaiting_input, seq: activity.seq });

  let pending = state.pending;
  if (activity.awaiting_input) {
    // Newly waiting on an approval or a question.
    if (id !== viewing && !pending.includes(id)) pending = [...pending, id];
  } else if (RUNNING.has(activity.state)) {
    // Working again: whatever it was waiting for has been dealt with.
    pending = without(pending, id);
  } else {
    // Stopped. Only news if it had been running or waiting.
    const wasActive = before !== undefined && (RUNNING.has(before.state) || before.awaiting);
    // A crash straight out of idle is news too: there was no running turn to
    // fade out, but the session still stopped with an error.
    const crashed = activity.state === "error";
    if ((wasActive || crashed) && id !== viewing && !pending.includes(id)) pending = [...pending, id];
  }
  return { last, pending };
}

/** The person opened the session, with the page in front of them. */
export function markSeen(state: AttentionState, id: string | null): AttentionState {
  if (id === null) return state;
  const pending = without(state.pending, id);
  return pending === state.pending ? state : { ...state, pending };
}

export interface AttentionLabels {
  one: (title: string) => string;
  many: (count: number) => string;
  untitled: string;
}

/** The tab title to show, or null when nothing is waiting. */
export function attentionTitle(
  pending: readonly string[],
  titleOf: (id: string) => string | undefined,
  labels: AttentionLabels,
): string | null {
  if (pending.length === 0) return null;
  if (pending.length === 1) {
    return labels.one(titleOf(pending[0])?.trim() || labels.untitled);
  }
  return labels.many(pending.length);
}
