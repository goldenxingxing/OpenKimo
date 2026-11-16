/**
 * The line shown while a failed model call is being retried.
 *
 * A web session retries a rate limit or an unwell provider with no attempt
 * limit, so "attempt 8/8" becomes "retry 12, for 3m 20s": how long it has been
 * going on is what tells someone whether to keep waiting, and the stop button
 * is how they stop.
 */

export interface StepRetryInfo {
  next_attempt: number;
  /** 0 when there is no limit. */
  max_attempts: number;
  wait_s: number;
  error_type: string;
  status_code?: number | null;
}

export type RetryReason =
  | "rateLimit"
  | "serverError"
  | "timeout"
  | "connection"
  | "emptyResponse"
  | "other";

export interface RetryLabels {
  reason: (reason: RetryReason, errorType: string) => string;
  /** "retry 3/8" */
  attemptOf: (attempt: number, max: number) => string;
  /** "retry 12" */
  attempt: (attempt: number) => string;
  /** "for 3m 20s" */
  elapsed: (duration: string) => string;
  /** "next in 30s" */
  nextIn: (duration: string) => string;
  stopHint: string;
  /** Formats a duration in seconds for display. */
  duration: (seconds: number) => string;
}

export function retryReason(retry: StepRetryInfo): RetryReason {
  if (retry.status_code === 429) return "rateLimit";
  if (typeof retry.status_code === "number" && retry.status_code >= 500) return "serverError";
  switch (retry.error_type) {
    case "APITimeoutError":
      return "timeout";
    case "APIConnectionError":
      return "connection";
    case "APIEmptyResponseError":
      return "emptyResponse";
    default:
      return "other";
  }
}

/**
 * @param elapsedS seconds since the first failure of this step, or null when
 *   unknown (a replayed history has no clock to measure it by).
 */
export function formatStepRetry(
  retry: StepRetryInfo,
  elapsedS: number | null,
  labels: RetryLabels,
): string {
  const unlimited = retry.max_attempts === 0;
  const parts = [
    labels.reason(retryReason(retry), retry.error_type),
    unlimited
      ? labels.attempt(retry.next_attempt)
      : labels.attemptOf(retry.next_attempt, retry.max_attempts),
  ];
  if (elapsedS !== null && elapsedS >= 1) {
    parts.push(labels.elapsed(labels.duration(elapsedS)));
  }
  parts.push(labels.nextIn(labels.duration(retry.wait_s)));
  if (unlimited) parts.push(labels.stopHint);
  return parts.join(" · ");
}

/** `8s`, `1m 05s`, `1h 02m` — or the Chinese equivalents via the unit names. */
export function formatDuration(
  seconds: number,
  units: { h: string; m: string; s: string } = { h: "h", m: "m", s: "s" },
): string {
  if (!Number.isFinite(seconds)) return `?${units.s}`;
  const total = Math.max(0, Math.round(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (value: number) => String(value).padStart(2, "0");
  if (h > 0) return `${h}${units.h} ${pad(m)}${units.m}`;
  if (m > 0) return `${m}${units.m} ${pad(s)}${units.s}`;
  return `${s}${units.s}`;
}
