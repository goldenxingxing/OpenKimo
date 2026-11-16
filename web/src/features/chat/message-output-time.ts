import { createElement, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { LiveMessage } from "../../hooks/types";

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;
const RELATIVE_DAY_LIMIT = 7;

/** Words for the relative time, in the active UI language. */
export interface OutputTimeLabels {
  justNow: string;
  minutesAgo: (count: number) => string;
  hoursAgo: (count: number) => string;
  daysAgo: (count: number) => string;
  /** BCP 47 tag for dates older than a week, e.g. "en" or "zh-CN". */
  locale?: string;
}

export function formatMessageOutputTime(
  completedAt: number,
  now: number,
  labels: OutputTimeLabels,
): string {
  const elapsed = Math.max(0, now - completedAt);

  if (elapsed < MINUTE_MS) {
    return labels.justNow;
  }
  if (elapsed < HOUR_MS) {
    return labels.minutesAgo(Math.floor(elapsed / MINUTE_MS));
  }
  if (elapsed < DAY_MS) {
    return labels.hoursAgo(Math.floor(elapsed / HOUR_MS));
  }
  if (elapsed < RELATIVE_DAY_LIMIT * DAY_MS) {
    return labels.daysAgo(Math.floor(elapsed / DAY_MS));
  }
  return new Date(completedAt).toLocaleDateString(labels.locale);
}

export function MessageOutputTime({
  completedAt,
  now,
}: {
  completedAt: number;
  now?: number;
}) {
  const { t, i18n } = useTranslation("chat");
  const [currentTime, setCurrentTime] = useState(() => now ?? Date.now());

  useEffect(() => {
    if (now !== undefined) {
      setCurrentTime(now);
      return;
    }

    const interval = window.setInterval(() => {
      setCurrentTime(Date.now());
    }, MINUTE_MS);
    return () => window.clearInterval(interval);
  }, [now]);

  const date = new Date(completedAt);
  const labels: OutputTimeLabels = {
    justNow: t("outputTime.justNow"),
    minutesAgo: (count) => t("outputTime.minutesAgo", { count }),
    hoursAgo: (count) => t("outputTime.hoursAgo", { count }),
    daysAgo: (count) => t("outputTime.daysAgo", { count }),
    locale: i18n.language,
  };
  return createElement(
    "time",
    {
      className: "ml-1 text-[11px] text-muted-foreground/70 tabular-nums",
      dateTime: date.toISOString(),
      title: date.toLocaleString(i18n.language),
    },
    formatMessageOutputTime(completedAt, currentTime, labels),
  );
}

export function shouldShowMessageOutputTime(
  message: LiveMessage,
): boolean {
  return (
    message.role === "assistant" &&
    !message.isStreaming &&
    (!message.variant || message.variant === "text") &&
    message.completedAt !== undefined
  );
}
