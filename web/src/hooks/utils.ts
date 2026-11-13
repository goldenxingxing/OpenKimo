import { v4 as uuidV4 } from "uuid";

import i18n from "@/i18n";

const _isMac =
  typeof navigator !== "undefined" &&
  navigator.platform.toLowerCase().includes("mac");

/**
 * Check if running on macOS.
 */
export function isMacOS(): boolean {
  return _isMac;
}

/**
 * Check if the platform-specific modifier key is pressed (Cmd on macOS, Ctrl elsewhere).
 */
export function hasPlatformModifier(
  e: Pick<KeyboardEvent | MouseEvent, "metaKey" | "ctrlKey">,
): boolean {
  return _isMac ? e.metaKey : e.ctrlKey;
}

/**
 * Get the API base URL for connecting to the Kimo backend.
 * - Vite dev: uses Vite proxy, so empty string (relative URLs like /api/...)
 * - Production web: same-origin, so empty string
 */
export function getApiBaseUrl(): string {
  return "";
}

/**
 * Generate a unique message ID from a UUID v4, avoiding key collisions when
 * switching sessions or reconnecting the WebSocket.
 */
export const createMessageId = (prefix: "user" | "assistant"): string => {
  return `${prefix}-${uuidV4()}`;
};

/**
 * Format relative time for session display
 */
export const formatRelativeTime = (date: Date): string => {
  // A null or invalid date would otherwise throw / produce a NaN "days" label.
  if (!(date instanceof Date) || Number.isNaN(date.getTime())) {
    return i18n.t("common:relativeTime.justNow");
  }
  const now = new Date();
  const diff = now.getTime() - date.getTime();
  const minutes = Math.floor(diff / 60000);

  if (minutes < 1) {
    return i18n.t("common:relativeTime.justNow");
  } else if (minutes < 60) {
    return i18n.t("common:relativeTime.minutes", { count: minutes });
  } else {
    const hours = Math.floor(minutes / 60);
    if (hours < 24) {
      return i18n.t("common:relativeTime.hours", { count: hours });
    } else {
      const days = Math.floor(hours / 24);
      return i18n.t("common:relativeTime.days", { count: days });
    }
  }
};
