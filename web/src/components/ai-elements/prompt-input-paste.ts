/**
 * Long pastes sit in the composer as a placeholder at the caret, and turn back
 * into the full text when the message is sent.
 *
 * A pasted block is often part of the sentence being written — "translate
 * ⟦Pasted 1⟧, then compare it with ⟦Pasted 2⟧" — so it stays where it was put
 * instead of becoming a card beside the input. The model receives the whole
 * text in that position, wrapped in `<pasted_text>` so a replayed history can
 * fold it back into a placeholder.
 */

/** Pastes longer than this many characters become a placeholder. */
export const PASTE_PLACEHOLDER_THRESHOLD = 50;

export interface PasteLabels {
  // i18n-allow-cjk: an example of the Chinese label, in a doc comment
  /** The word naming a paste, e.g. "Pasted" or "粘贴". */
  noun: string;
  lines: (count: number) => string;
  chars: (count: number) => string;
}

// Either language's noun, so a placeholder typed under one UI language is
// still recognised after switching to the other.
// i18n-allow-cjk: matches the Chinese placeholder word
const TOKEN = /⟦(?:Pasted|粘贴) (\d+)(?: · [^⟦⟧\n]*)?⟧/g;
const PASTED_BLOCK = /<pasted_text index="(\d+)">([\s\S]*?)<\/pasted_text>/g;

export interface TokenSpan {
  id: number;
  start: number;
  end: number;
}

export function shouldUsePlaceholder(text: string): boolean {
  return [...text].length > PASTE_PLACEHOLDER_THRESHOLD;
}

export function lineCount(text: string): number {
  return text.replace(/\n+$/, "").split("\n").length;
}

/** `⟦Pasted 1 · 128 lines⟧`, or a character count for a single line. */
export function makeToken(id: number, content: string, labels: PasteLabels): string {
  const lines = lineCount(content);
  const size = lines > 1 ? labels.lines(lines) : labels.chars([...content].length);
  return `⟦${labels.noun} ${id} · ${size}⟧`;
}

export function findTokens(text: string): TokenSpan[] {
  return [...text.matchAll(TOKEN)].map((match) => ({
    id: Number(match[1]),
    start: match.index,
    end: match.index + match[0].length,
  }));
}

// ---------------------------------------------------------------------------
// Files. A pasted, dropped or attached file sits at the caret as
// `⟦Image 1 · shot.png⟧` / `⟦File 2 · report.pdf⟧`, so "compare ⟦Image 1⟧
// with ⟦File 2⟧" keeps saying which file goes where. On send each becomes
// `<attachment index="i"/>` (i = its place among the message's files); the
// composer's owner turns that into the uploaded file's name for the server,
// which puts the file's content at that spot.
// ---------------------------------------------------------------------------

export type AttachmentKind = "image" | "file";

export interface AttachmentLabels {
  image: string;
  file: string;
}

// i18n-allow-cjk: matches the Chinese placeholder words
const FILE_TOKEN = /⟦(?:Image|File|图片|文件) (\d+) · [^⟦⟧\n]*⟧/g;
const ATTACHMENT_INDEX_MARKER = /<attachment index="(\d+)"\/>/g;

export function attachmentKind(mediaType: string | undefined): AttachmentKind {
  return mediaType?.startsWith("image/") ? "image" : "file";
}

/** Brackets and newlines would end the token early; they are not worth keeping in a label. */
function labelName(name: string): string {
  return name.replace(/[⟦⟧\n]/g, " ").trim() || "?";
}

export function makeFileToken(
  id: number,
  kind: AttachmentKind,
  name: string,
  labels: AttachmentLabels,
): string {
  return `⟦${labels[kind]} ${id} · ${labelName(name)}⟧`;
}

export function findFileTokens(text: string): TokenSpan[] {
  return [...text.matchAll(FILE_TOKEN)].map((match) => ({
    id: Number(match[1]),
    start: match.index,
    end: match.index + match[0].length,
  }));
}

/** Remove the placeholders of the given ids, as when their attachment is removed. */
export function removeFileTokens(text: string, ids: ReadonlySet<number>): string {
  return text.replace(FILE_TOKEN, (token, raw: string) => (ids.has(Number(raw)) ? "" : token));
}

/** Pasted-text and file placeholders together, in reading order. */
export function findAllTokens(text: string): TokenSpan[] {
  return [...findTokens(text), ...findFileTokens(text)].sort((a, b) => a.start - b.start);
}

/**
 * Turn file placeholders into markers naming the file's place among the
 * message's attachments. A placeholder whose file is gone is dropped rather
 * than sent as text the model could only puzzle over.
 */
export function markAttachments(text: string, indexOf: (id: number) => number | undefined): string {
  return text.replace(FILE_TOKEN, (_token, raw: string) => {
    const index = indexOf(Number(raw));
    return index === undefined ? "" : `<attachment index="${index}"/>`;
  });
}

/** Resolve `<attachment index="i"/>` to the uploaded file's stored name, or drop it. */
export function nameAttachments(text: string, nameOf: (index: number) => string | undefined): string {
  return text.replace(ATTACHMENT_INDEX_MARKER, (_marker, raw: string) => {
    const name = nameOf(Number(raw));
    return name === undefined ? "" : `<attachment file="${name.replace(/"/g, "")}"/>`;
  });
}

/** The smallest id not already holding a paste. */
export function nextPasteId(taken: Iterable<number>): number {
  const used = new Set(taken);
  let id = 1;
  while (used.has(id)) id += 1;
  return id;
}

/**
 * Replace each placeholder with its full text. Placeholders are numbered for
 * the model in the order they appear, so what it sees reads 1, 2, 3 whatever
 * ids the composer happened to hand out. A placeholder with no stored text is
 * left as written.
 */
export function expandPastes(text: string, contentOf: (id: number) => string | undefined): string {
  const order = new Map<number, number>();
  return text.replace(TOKEN, (token, raw: string) => {
    const id = Number(raw);
    const content = contentOf(id);
    if (content === undefined) return token;
    if (!order.has(id)) order.set(id, order.size + 1);
    return `<pasted_text index="${order.get(id)}">${content}</pasted_text>`;
  });
}

/** Ids of the placeholders in a text, so their stored text can be released once sent. */
export function referencedIds(text: string): Set<number> {
  return new Set(findTokens(text).map((token) => token.id));
}

/**
 * The span one keystroke should delete whole, or null to let the browser do it.
 *
 * A placeholder goes in one piece: Backspace right after it, Delete right
 * before it, or either from inside it (arrow keys can put the caret there).
 * A selection that clips a placeholder is widened to take all of it.
 */
export function deletionRange(
  text: string,
  selectionStart: number,
  selectionEnd: number,
  key: "Backspace" | "Delete",
): { start: number; end: number } | null {
  const tokens = findAllTokens(text);
  if (selectionStart !== selectionEnd) {
    let start = selectionStart;
    let end = selectionEnd;
    for (const token of tokens) {
      if (token.start < selectionEnd && token.end > selectionStart) {
        start = Math.min(start, token.start);
        end = Math.max(end, token.end);
      }
    }
    return start === selectionStart && end === selectionEnd ? null : { start, end };
  }
  const caret = selectionStart;
  const hit = tokens.find((token) =>
    key === "Backspace"
      ? token.start < caret && caret <= token.end
      : token.start <= caret && caret < token.end,
  );
  return hit ? { start: hit.start, end: hit.end } : null;
}

/** The placeholder the caret is in or touching, for the inline actions. */
export function tokenAtCaret(text: string, caret: number): TokenSpan | null {
  return findTokens(text).find((token) => token.start <= caret && caret <= token.end) ?? null;
}

export type MessageSegment =
  | { kind: "text"; text: string }
  | { kind: "paste"; index: number; content: string };

/** Split a sent message so its pasted blocks can be shown folded. */
export function splitPastedBlocks(message: string): MessageSegment[] {
  const segments: MessageSegment[] = [];
  let last = 0;
  for (const match of message.matchAll(PASTED_BLOCK)) {
    if (match.index > last) {
      segments.push({ kind: "text", text: message.slice(last, match.index) });
    }
    segments.push({ kind: "paste", index: Number(match[1]), content: match[2] });
    last = match.index + match[0].length;
  }
  if (last < message.length) {
    segments.push({ kind: "text", text: message.slice(last) });
  }
  return segments;
}
