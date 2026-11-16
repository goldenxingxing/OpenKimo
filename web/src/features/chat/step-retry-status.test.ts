import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { type RetryLabels, formatDuration, formatStepRetry, retryReason } from "./step-retry-status.ts";

const zh: RetryLabels = {
  reason: (reason) => ({ rateLimit: "服务端限流", serverError: "服务端出错", timeout: "请求超时", connection: "连接中断", emptyResponse: "空回复", other: "出错" })[reason],
  attemptOf: (n, max) => `第 ${n}/${max} 次重试`,
  attempt: (n) => `第 ${n} 次重试`,
  elapsed: (d) => `已持续 ${d}`,
  nextIn: (d) => `${d}后重试`,
  stopHint: "点停止可中断",
  duration: (s) => formatDuration(s, { h: " 小时", m: " 分", s: " 秒" }),
};

describe("retry status line", () => {
  it("shows how long an unlimited retry has gone on, and how to stop it", () => {
    const line = formatStepRetry(
      { next_attempt: 12, max_attempts: 0, wait_s: 30, error_type: "APIStatusError", status_code: 429 },
      200,
      zh,
    );
    assert.equal(line, "服务端限流 · 第 12 次重试 · 已持续 3 分 20 秒 · 30 秒后重试 · 点停止可中断");
  });

  it("keeps the count out of a limited retry and omits an unknown duration", () => {
    const line = formatStepRetry(
      { next_attempt: 3, max_attempts: 8, wait_s: 2.4, error_type: "APITimeoutError" },
      null,
      zh,
    );
    assert.equal(line, "请求超时 · 第 3/8 次重试 · 2 秒后重试");
  });

  it("classifies by status first, then by error type", () => {
    assert.equal(retryReason({ next_attempt: 2, max_attempts: 0, wait_s: 1, error_type: "APIStatusError", status_code: 529 }), "serverError");
    assert.equal(retryReason({ next_attempt: 2, max_attempts: 0, wait_s: 1, error_type: "APIConnectionError" }), "connection");
    assert.equal(retryReason({ next_attempt: 2, max_attempts: 0, wait_s: 1, error_type: "Weird" }), "other");
  });

  it("formats durations", () => {
    assert.equal(formatDuration(8), "8s");
    assert.equal(formatDuration(65), "1m 05s");
    assert.equal(formatDuration(3725), "1h 02m");
  });
});
