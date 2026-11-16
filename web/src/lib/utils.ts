import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/**
 * Normalize and shorten a title string.
 * - Replaces multiple whitespace with single space
 * - Trims leading/trailing whitespace
 * - Shortens to maxLength characters with ellipsis
 */
export function shortenTitle(
  title: string | undefined | null,
  maxLength = 50,
): string {
  if (!title) return "";

  // Normalize: collapse whitespace and trim
  const normalized = title.replace(/\s+/g, " ").trim();

  // Work in Unicode code points so an astral character (e.g. an emoji) is
  // never split into a lone surrogate by slice().
  const chars = Array.from(normalized);
  if (chars.length <= maxLength) {
    return normalized;
  }

  // Shorten with ellipsis
  return `${chars.slice(0, maxLength - 1).join("")}…`;
}
