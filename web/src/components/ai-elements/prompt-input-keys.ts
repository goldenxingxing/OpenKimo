type EnterKeyEvent = {
  key: string;
  metaKey: boolean;
  ctrlKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
};

/**
 * Whether a key press in the prompt textarea is the "insert a newline"
 * shortcut: Cmd+Enter on macOS, Ctrl+Enter elsewhere.
 *
 * Only the platform's primary modifier counts, so Ctrl+Enter on a Mac is not
 * claimed. Plain Enter submits, and Shift+Enter keeps the browser's own
 * newline, so neither belongs here.
 */
export function isNewlineShortcut(event: EnterKeyEvent, isMac: boolean): boolean {
  if (event.key !== "Enter" || event.altKey || event.shiftKey) {
    return false;
  }
  return isMac
    ? event.metaKey && !event.ctrlKey
    : event.ctrlKey && !event.metaKey;
}
