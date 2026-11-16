// The server's rule is Python's `[\w][\w-]*` with Unicode `\w`. This is the
// same shape for letters, digits and underscore; the server has the last word.
const SLUG = /^[\p{L}\p{M}\p{N}_][\p{L}\p{M}\p{N}_-]*$/u;
const SLUG_BREAK = /[^\p{L}\p{M}\p{N}_-]+/gu;
const REPEATED_HYPHENS = /-{2,}/g;
const EDGE_HYPHENS = /^-+|-+$/g;
const TAG_SEPARATOR = /[,，]/;
const WIKI_LINK = /\[\[([^[\]]+)\]\]/g;
const MD_SUFFIX = /\.md$/;

export function isValidSlug(slug: string): boolean {
  return SLUG.test(slug);
}

/** `Release Train!` → `Release-Train`; CJK is kept as it is. */
export function suggestSlug(title: string): string {
  return title
    .trim()
    .replace(SLUG_BREAK, "-")
    .replace(REPEATED_HYPHENS, "-")
    .replace(EDGE_HYPHENS, "");
}

/** Comma-separated, either width of comma; blanks and repeats dropped, order kept. */
export function parseTags(text: string): string[] {
  const seen = new Set<string>();
  for (const raw of text.split(TAG_SEPARATOR)) {
    const tag = raw.trim();
    if (tag) seen.add(tag);
  }
  return [...seen];
}

/** The `[[category/slug]]` links in a page body, as logical paths, in order. */
export function wikiLinks(body: string): string[] {
  const seen = new Set<string>();
  for (const match of body.matchAll(WIKI_LINK)) {
    seen.add(`${match[1]}.md`);
  }
  return [...seen];
}

export function pageSlug(path: string): string {
  return path.slice(path.indexOf("/") + 1).replace(MD_SUFFIX, "");
}
