/**
 * Extracts local filesystem paths mentioned in an assistant message so the UI
 * can offer "open document / reveal in folder" actions for them.
 *
 * Pure text processing — no React, no DOM — so it is unit-testable with the
 * plain node test runner like the other colocated helpers.
 */

export type MentionedPath = {
  /** Exactly as it appeared in the message (trimmed of punctuation). */
  raw: string;
  /** What to send to the backend: absolute, `~/` kept for server expansion. */
  resolved: string;
};

/** Characters that commonly trail a sentence but never belong to the path. */
const TRAILING_JUNK = /[.,;:!?)\]}>》」』"'"'。，；：！？、）】》…]+$/;

const URL_PREFIX = /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//;

const HAS_LETTER_OR_CJK = /[A-Za-z\u4e00-\u9fff]/;
const HAS_WHITESPACE = /\s/;
const TRAILING_SEPARATOR = /[\\/]$/;
const TRAILING_SEPARATORS = /[\\/]+$/;
const PATH_SEPARATOR = /[\\/]/;
const BACKSLASH = /\\/g;

/**
 * Extensions a bare (no directory component) filename must end with to count
 * as a file mention. Deliberately a whitelist: prose tokens like `7.4.8.1`
 * or `SYS-047` must never become clickable, while `.build_srd_docx.py` and
 * a CJK-named requirements document — the way agents actually cite files —
 * always do.
 */
const FILE_EXTENSIONS = new Set([
  "py", "md", "mdx", "txt", "doc", "docx", "pdf", "pptx", "xlsx", "xls",
  "csv", "tsv", "json", "jsonl", "yaml", "yml", "toml", "xml", "html", "htm",
  "js", "jsx", "ts", "tsx", "c", "h", "cpp", "hpp", "rs", "go", "java",
  "sql", "sh", "ipynb", "pkl", "log", "out", "svg", "png", "jpg", "jpeg",
  "dmg", "zip", "pkg",
]);

const BARE_FILENAME_SUFFIX = /\.([A-Za-z0-9]+)$/;

/**
 * A suffix after the last dot that STARTS with a letter: `dmg`, `whl`,
 * `tar.gz`. Chapter references (`6.4.1/6.2.11`) end in digit-led suffixes
 * (`11`) and are rejected; plain extensionless segments (`src/a/b/api`) are
 * rejected too — only an explicit trailing separator marks those as
 * directories. Used for explicitly cited (backticked / 《》) multi-segment
 * mentions, where the citation marks supply the trust the plain-text
 * whitelist otherwise provides.
 */
const LETTER_LED_EXTENSION = /\.[A-Za-z][A-Za-z0-9]*$/;

function hasLetterLedExtension(token: string): boolean {
  return LETTER_LED_EXTENSION.test(token);
}

function hasKnownFileExtension(token: string): boolean {
  const match = token.match(BARE_FILENAME_SUFFIX);
  if (!match) return false;
  const ext = match[1].toLowerCase();
  if (!FILE_EXTENSIONS.has(ext)) return false;
  // The stem before the extension must carry at least one letter — a bare
  // version number like `7.4.8` (ends in `.8`) never reaches here because
  // digits are no extension, but `x.8c` style oddities are filtered here.
  return HAS_LETTER_OR_CJK.test(token.slice(0, -match[0].length));
}

/**
 * Absolute paths: start at `/`, `~/`, or a Windows drive letter (`C:\`, `D:/`).
 * The lookbehind admits a path glued to Chinese punctuation or a quote but not
 * to a letter (which would make `e.g./x` a match). Inside the path we stop at
 * whitespace and at the bracket/quote characters that typically close the
 * mention. Backslashes are allowed in the body: Windows-style mentions must
 * survive, and on Unix a literal backslash in a name is rare enough that a
 * false candidate dies at resolution anyway.
 */
const ABSOLUTE_PATH =
  /(?<=^|[\s"'`（(【《「『〈,，;；:：!！?？、。—–-])(?:~\/|\/|[A-Za-z]:[\\/])[^\s"'`（）()【】《「『〈〕》,,;；:：!！?？、。…，]+/g;

/**
 * Relative paths: only worth offering when the session has a work directory
 * to resolve against, and the token carries a separator. A multi-segment
 * mention must end in a file suffix (`a/b.md`) or a separator (`a/b/` — a
 * directory); a single segment only counts with a trailing separator
 * (`subdir/` — how agents cite a subdirectory of the topic's directory),
 * never bare. Backslashes are accepted so Windows-style mentions work too.
 */
const RELATIVE_PATH =
  /(?<=^|[\s"'`（(【《「『〈,，;；:：!！?？、。—–-])(?:\.{1,2}[\\/])?[\w.\u4e00-\u9fff-]+(?:(?:[\\/][^\s"'`（）()【】《「『〈〕》,,;；:：!！?？、。…，]+)+[\\/]?|[\\/])/g;

/** `C:\` or `D:/` — a Windows drive-letter absolute path. */
const WINDOWS_ABSOLUTE = /^[A-Za-z]:[\\/]/;

/**
 * Prose glue: in CJK text a path often runs straight into following words
 * with no separator (e.g. an extension followed by CJK prose). If what
 * trails the last extension-like suffix is pure CJK, that trailing run is
 * prose, not the file name.
 */
const CJK_PROSE_GLUE = /(\.[A-Za-z0-9]{1,8})[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+$/;

function stripWrappingQuotes(candidate: string): string {
  const paired: Record<string, string> = {
    "`": "`",
    "'": "'",
    '"': '"',
    "“": "”",
    "‘": "’",
  };
  const first = candidate[0];
  const last = candidate[candidate.length - 1];
  if (first && last && paired[first] === last && candidate.length >= 2) {
    return candidate.slice(1, -1);
  }
  return candidate;
}

function isPlausiblePath(candidate: string): boolean {
  if (candidate.length < 2) return false;
  if (URL_PREFIX.test(candidate)) return false;
  // The scheme:// of a URL is not part of a path mention; what the absolute
  // regex grabbed from `https://host/x` is the `//host/x` remnant.
  if (candidate.startsWith("//")) return false;
  // A lone `/` or bare `~` is noise.
  if (candidate === "/" || candidate === "~") return false;
  // Emails look like paths after the regex but never are one.
  if (candidate.includes("@")) return false;
  return true;
}

function resolveCandidate(raw: string, workDir?: string | null): string | null {
  if (
    raw.startsWith("/") ||
    raw.startsWith("~/") ||
    WINDOWS_ABSOLUTE.test(raw)
  ) {
    return raw;
  }
  if (!workDir) return null;
  // Normalize separators on both sides: Windows accepts forward slashes in
  // every downstream API (Path, open), and it keeps the dedup keys consistent.
  const base = workDir.replace(TRAILING_SEPARATORS, "").replace(BACKSLASH, "/");
  return `${base}/${raw.replace(BACKSLASH, "/")}`;
}

type RawCandidate = { text: string; index: number };

/**
 * CJK book-title marks: agents cite documents as `《report.md》` about as
 * often as with backticks, and the marks are just as strong a citation
 * signal. A span whose content is a bare filename (or a relative path, or a
 * directory with a trailing separator) counts as a mention.
 */
const BOOK_TITLE_SPAN = /《([^《》\n]+)》/g;

/** Strip wrapping CJK book-title marks from a cited token. */
function stripCitationMarks(token: string): string {
  const trimmed = token.trim();
  if (
    trimmed.length >= 2 &&
    trimmed.startsWith("《") &&
    trimmed.endsWith("》")
  ) {
    return trimmed.slice(1, -1);
  }
  return trimmed;
}

function isRelativeMention(token: string): boolean {
  /** Same acceptance rules as plain-text relative mentions, EXCEPT that an
   * explicitly cited multi-segment token (《a/b.dmg》) is trusted by its
   * separator alone: backticks and book-title marks are deliberate citation
   * signals, so whitelisting the extension there only blocks legitimate
   * deliverables (.dmg/.app/.whl/...). URLs stay excluded. */
  if (!token || HAS_WHITESPACE.test(token) || token.includes("@")) return false;
  if (token.startsWith("//")) return false;
  if (URL_PREFIX.test(token)) return false;
  const isDirectory = TRAILING_SEPARATOR.test(token);
  const segments = token.split(PATH_SEPARATOR).filter(Boolean);
  const lastSegment = segments.pop() ?? "";
  if (!isDirectory) {
    if (segments.length > 0) {
      return hasLetterLedExtension(lastSegment);
    }
    return hasKnownFileExtension(token);
  }
  // A bare `name/` needs a letter or CJK character, or `6.4.1/` would link.
  return segments.length === 0
    ? HAS_LETTER_OR_CJK.test(lastSegment)
    : true;
}

function collectCandidates(
  text: string,
  workDir?: string | null,
): RawCandidate[] {
  const candidates: RawCandidate[] = [];
  for (const match of text.matchAll(ABSOLUTE_PATH)) {
    candidates.push({ text: match[0], index: match.index });
  }
  if (workDir) {
    for (const match of text.matchAll(RELATIVE_PATH)) {
      // Either the last segment ends in a whitelisted file extension — so
      // chapter ranges like `6.4.1/6.2.11` never pass while `search/cost.py`
      // does — or the mention ends in a separator, which marks a directory:
      // `output/reports/` opens a Finder window, extension or not. A bare
      // `name/` (no directory component) still needs a letter or CJK
      // character, or a numeric chapter ref like `6.4.1/` would link.
      const mention = match[0];
      const segments = mention.split(PATH_SEPARATOR).filter(Boolean);
      const lastSegment = segments.pop() ?? "";
      const isDirectory = TRAILING_SEPARATOR.test(mention);
      const isBareName = isDirectory && segments.length === 0;
      const accepted =
        isDirectory
          ? !isBareName || HAS_LETTER_OR_CJK.test(lastSegment)
          : hasKnownFileExtension(lastSegment);
      if (accepted) {
        candidates.push({ text: mention, index: match.index });
      }
    }
    for (const match of text.matchAll(BOOK_TITLE_SPAN)) {
      // The link covers the inner token only — the marks themselves stay
      // plain text around it, like the backticks of a code span.
      const token = match[1];
      if (isRelativeMention(token)) {
        candidates.push({ text: token, index: match.index + 1 });
      }
    }
  }
  return candidates;
}

function cleanCandidate(candidate: string): string {
  return stripWrappingQuotes(candidate)
    .replace(TRAILING_JUNK, "")
    .replace(CJK_PROSE_GLUE, "$1")
    .replace(TRAILING_SEPARATORS, "");
}

/**
 * Backticked bare filenames (`report.docx`, `.build_alg_docx.py`) — the most
 * common way agents cite files that live directly in the work directory.
 * Requires the whitelist extension and is only offered when a work directory
 * exists to resolve against. Wrapping CJK book-title marks are stripped:
 * `` `《report.md》` `` cites the same file as `` `report.md` ``.
 */
const BACKTICKED_SPAN = /`([^`\n]+)`/g;

function bareFilenameCandidates(text: string): string[] {
  const found: string[] = [];
  for (const match of text.matchAll(BACKTICKED_SPAN)) {
    const token = stripCitationMarks(match[1]);
    if (token.includes("/") || HAS_WHITESPACE.test(token)) continue;
    if (!hasKnownFileExtension(token)) continue;
    found.push(token);
  }
  return found;
}

/**
 * Returns the deduplicated local paths mentioned in `text`, in order of first
 * appearance, capped so a path-heavy message cannot flood the action bar.
 */
export function extractMentionedPaths(
  text: string,
  workDir?: string | null,
): MentionedPath[] {
  if (!text || text.length < 2) return [];

  const seen = new Set<string>();
  const result: MentionedPath[] = [];
  const cap = 8;

  const push = (cleaned: string) => {
    if (result.length >= cap) return;
    if (!isPlausiblePath(cleaned)) return;
    const resolved = resolveCandidate(cleaned, workDir);
    if (!resolved || seen.has(resolved)) return;
    seen.add(resolved);
    result.push({ raw: cleaned, resolved });
  };

  for (const { text: candidate } of collectCandidates(text, workDir)) {
    if (result.length >= cap) break;
    push(cleanCandidate(candidate));
  }
  if (workDir) {
    for (const token of bareFilenameCandidates(text)) {
      if (result.length >= cap) break;
      push(token);
    }
  }
  return result;
}

// ---------------------------------------------------------------------------
// Inline linkification — turns path mentions in a finished markdown message
// into `open-path://` links that a custom anchor component renders clickable.
// ---------------------------------------------------------------------------

/** Regions where text must stay literal: fenced blocks and inline code. */
const LITERAL_REGION =
  /(^|\n)```[a-z]*\n[\s\S]*?\n```|`[^`\n]+`|~~[\s\S]*?~~/g;

/** Existing markdown links and images — never touch their contents. */
const MARKDOWN_LINK = /\[[^\]]*\]\([^)\s]*\)|!\[[^\]]*\]\([^)\s]*\)/g;

/** Custom scheme handled by the OpenPathAnchor markdown component. */
export const OPEN_PATH_SCHEME = "open-path://";

export function encodeOpenPathHref(resolved: string): string {
  return OPEN_PATH_SCHEME + encodeURIComponent(resolved);
}

export function isOpenPathHref(href: string | undefined): href is string {
  return Boolean(href?.startsWith(OPEN_PATH_SCHEME));
}

/**
 * Decode an `open-path://` href back to the path it points at.
 *
 * Returns null when the payload is not valid percent-encoding — a message
 * may carry a hand-written `open-path://bad%zz` link, and decoding that
 * throws a URIError while the message renders, which takes the whole view
 * down with it.
 */
export function decodeOpenPathHref(href: string): string | null {
  try {
    return decodeURIComponent(href.slice(OPEN_PATH_SCHEME.length));
  } catch {
    return null;
  }
}

/** Merge the literal regions and markdown links of `text` into skip ranges. */
function skipRanges(text: string): Array<[number, number]> {
  const ranges: Array<[number, number]> = [];
  for (const regex of [LITERAL_REGION, MARKDOWN_LINK]) {
    regex.lastIndex = 0;
    let match: RegExpExecArray | null = regex.exec(text);
    while (match !== null) {
      let start = match.index;
      // A fenced-block match may open with the newline before ```.
      if (match[0].startsWith("\n")) start += 1;
      ranges.push([start, match.index + match[0].length]);
      match = regex.exec(text);
    }
  }
  return ranges;
}

function inRanges(ranges: Array<[number, number]>, index: number): boolean {
  return ranges.some(([start, end]) => index >= start && index < end);
}

/**
 * Rewrites path mentions in a *finished* markdown message into
 * `[raw](open-path://…)` links. Fenced code blocks, inline code spans,
 * existing markdown links and images are left untouched.
 */
export function linkifyPathsMarkdown(
  markdown: string,
  workDir?: string | null,
): string {
  if (!markdown || markdown.length < 2) return markdown;

  const ranges = skipRanges(markdown);
  const candidates = collectCandidates(markdown, workDir);

  const edits: Array<{ start: number; end: number; replacement: string }> = [];
  for (const { text: candidate, index: matchStart } of candidates) {
    if (inRanges(ranges, matchStart)) continue;
    const cleaned = cleanCandidate(candidate);
    if (!isPlausiblePath(cleaned)) continue;
    const resolved = resolveCandidate(cleaned, workDir);
    if (!resolved) continue;
    const rawStart = matchStart + candidate.indexOf(cleaned);
    edits.push({
      start: rawStart,
      end: rawStart + cleaned.length,
      replacement: `[${cleaned}](${encodeOpenPathHref(resolved)})`,
    });
  }
  if (edits.length === 0) return markdown;

  edits.sort((a, b) => a.start - b.start);
  const parts: string[] = [];
  let cursor = 0;
  for (const edit of edits) {
    if (edit.start < cursor) continue; // overlapping — keep the first
    parts.push(markdown.slice(cursor, edit.start), edit.replacement);
    cursor = edit.end;
  }
  parts.push(markdown.slice(cursor));
  return parts.join("");
}

/**
 * Whether a standalone string (e.g. the content of an inline code span) is
 * itself one path mention. Used to make backticked paths clickable: absolute
 * / `~/` forms always qualify; a bare filename (`report.docx`) qualifies when
 * a work directory is available to resolve against, and so does a relative
 * path with directories (`output/reports/daily/D-2026-10-15.md`) — agents
 * cite work-directory-relative locations in backticks at least as often as
 * bare filenames.
 */
export function isLikelyStandalonePath(
  text: string,
  workDir?: string | null,
): boolean {
  const trimmed = text.trim();
  if (trimmed !== text || text.length < 2) return false;

  // A relative mention is acceptable only with a work directory to resolve
  // against. Bare filenames need a whitelisted extension; a path with
  // directories needs the same on its last segment; a trailing separator
  // marks a directory mention instead — `output/reports/`, or a bare
  // `subdir/` citing a subdirectory of the topic's directory (which
  // must still carry a letter or CJK character, or `6.4.1/` would link).
  // So `6.4.1/6.2.11` stays prose while `a/b/report.md` and `output/reports/`
  // both link.
  if (!(text.startsWith("/") || text.startsWith("~/") || WINDOWS_ABSOLUTE.test(text))) {
    // `《report.md》` inside a code span cites the same file as
    // `report.md` — evaluate the inner token.
    const token = stripCitationMarks(text);
    if (token !== text) {
      return isLikelyStandalonePath(token, workDir);
    }
    if (!workDir) return false;
    if (HAS_WHITESPACE.test(text)) return false;
    if (text.includes("@") || text.startsWith("//")) return false;
    if (URL_PREFIX.test(text)) return false;
    const isDirectory = TRAILING_SEPARATOR.test(text);
    const segments = text.split(PATH_SEPARATOR).filter(Boolean);
    const lastSegment = segments.pop() ?? "";
    if (!isDirectory) {
      if (segments.length > 0) {
        // A backticked multi-segment token is a deliberate citation, so the
        // plain-text extension whitelist is relaxed to any letter-led
        // extension: `OpenKimo/dist/app.dmg`, `dist/app.whl`, `x.tar.gz`.
        // Digit-led suffixes (`6.4.1/6.2.11`) and extensionless segments
        // (`src/kimo_cli/web/api`) stay inert; directories cite with a
        // trailing separator.
        return hasLetterLedExtension(lastSegment);
      }
      return hasKnownFileExtension(text);
    }
    if (segments.length === 0) {
      return HAS_LETTER_OR_CJK.test(lastSegment);
    }
    return true;
  }

  const bareDirectory = text.replace(TRAILING_SEPARATORS, "");
  const cleaned = cleanCandidate(text);
  if (!isPlausiblePath(cleaned) || cleaned !== bareDirectory) return false;
  // Reject things like a bare file system root fragment or a URL leftover.
  return collectCandidates(text, null).some(
    (candidate) => cleanCandidate(candidate.text) === bareDirectory,
  );
}

// ---------------------------------------------------------------------------
// React plumbing — the markdown components (a / inline code) rendered deep
// inside Streamdown need the session's work directory to resolve relative
// mentions; the message renderer provides it via this context.
// ---------------------------------------------------------------------------

import { createContext, useContext } from "react";

export const PathWorkDirContext = createContext<string | null | undefined>(
  undefined,
);

/** workDir from the nearest provider, or null when none is present. */
export function usePathWorkDir(): string | null {
  return useContext(PathWorkDirContext) ?? null;
}

/** Resolve a mentioned token into the absolute path to request. */
export function resolveMentionedPath(token: string, workDir: string | null): string {
  const resolved = resolveCandidate(token, workDir);
  return resolved ?? token;
}

/**
 * Percent-decode a path that arrived URL-encoded (a markdown link with a
 * non-ASCII destination). A path with no ``%`` escapes — the normal case for
 * a raw CJK or ASCII path — is returned unchanged; an invalid escape (a
 * literal ``%`` not followed by two hex digits) is also left alone rather
 * than throwing.
 */
export function decodePercentEscapes(value: string): string {
  if (!value.includes("%")) return value;
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

/**
 * Resolve a regular markdown href when it points to a local file or directory.
 * Web URLs, anchors, and other non-path links return null and keep their normal
 * browser behavior.
 */
export function resolveLocalLinkPath(
  href: string | undefined,
  workDir?: string | null,
): string | null {
  if (!href || isOpenPathHref(href)) return null;
  const decoded = decodePercentEscapes(href);
  if (!isLikelyStandalonePath(decoded, workDir)) {
    return null;
  }
  return resolveMentionedPath(decoded, workDir ?? null);
}
