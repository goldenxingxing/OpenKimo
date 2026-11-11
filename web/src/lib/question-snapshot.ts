/**
 * A question that arrives with the replayed history is history, not a live
 * prompt: the turn that asked it is gone unless the server still has it
 * pending, and the server re-sends those right after initialize.
 *
 * Making a replayed question actionable leaves the user answering into the
 * void — the server has no such request, drops the answer without a word, and
 * every reload brings the same question back.
 */
export function classifyQuestion(params: {
  isReplay: boolean;
  wasDismissed: boolean;
}): {
  state: "question-requested" | "question-responded";
  submitted: boolean;
  resolved: boolean;
  actionable: boolean;
} {
  const { isReplay, wasDismissed } = params;
  if (wasDismissed) {
    // Closed earlier via "Stop run"; the turn that asked is gone.
    return {
      state: "question-responded",
      submitted: true,
      resolved: true,
      actionable: false,
    };
  }
  if (isReplay) {
    return {
      state: "question-responded",
      submitted: true,
      resolved: false,
      actionable: false,
    };
  }
  return {
    state: "question-requested",
    submitted: false,
    resolved: false,
    actionable: true,
  };
}
