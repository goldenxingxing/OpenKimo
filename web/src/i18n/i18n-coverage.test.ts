/**
 * The language switch covers the whole UI.
 *
 * Text written straight into a component shows in one language whichever the user
 * picks, which is how the UI ended up mixing English and Chinese. These checks fail
 * when a component gains such text, when a key exists in only one language, or when
 * Chinese is written outside the locale files.
 */
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import ts from "typescript";

const SRC = join(dirname(fileURLToPath(import.meta.url)), "..");
const LOCALES = join(SRC, "i18n", "locales");

/** Text that is the same in every language: names, protocols, units, samples. */
const ALLOWED = new Set<string>([
  // keyboard keys, as printed on the key caps
  "Shift",
  "Enter",
  "Esc",
  // product names (Finder and Terminal are translated: macOS localizes them)
  "Cursor",
  "VS Code",
  "Antigravity",
  // command and credential samples in MCP form placeholders
  "npx chrome-devtools-mcp@latest",
  "Authorization: Bearer ...",
]);

const TEXT_ATTRIBUTES = new Set([
  "title",
  "placeholder",
  "aria-label",
  "alt",
  "label",
  "description",
  "tooltip",
  "content",
  "heading",
  "subtitle",
]);
const TEXT_PROPERTIES = new Set([
  "label",
  "title",
  "description",
  "placeholder",
  "text",
  "message",
  "tooltip",
  "heading",
  "subtitle",
  "hint",
  "error",
]);
const SOURCE_FILE = /\.(ts|tsx)$/;
const TEST_OR_DECLARATION = /\.(test|d)\.ts$/;
const LETTERS = /[A-Za-z]{2,}/;
/** ids, paths and single class names */
const IDENTIFIER_LIKE = /^[a-z0-9_\-./:#@]+$/;
const WHITESPACE_RUN = /\s+/;
const WHITESPACE_RUNS = /\s+/g;
/** a token of a CSS class list: has a -, : or [ */
const CLASS_TOKEN = /[-:[]/;
const HAS_SPACE = /\s/;
const SINGLE_WORD = /^[A-Z][a-z]+[.!?…:]?$/;
const QUOTES = /['"]/g;
/** i18next plural forms: English needs _one/_other, Chinese only _other */
const PLURAL_SUFFIX = /_(zero|one|two|few|many|other)$/;
const CJK = /[\u4e00-\u9fff]/;
const CJK_ALLOWED = /i18n-allow-cjk:/;
/** date/time formatting without a locale, or with the browser's (undefined) */
const BROWSER_LOCALE_DATE =
  /toLocale(Date|Time)String\((undefined|\))|new Date\([^)]*\)\.toLocaleString\(\)|date\.toLocaleString\(\)/;
const MESSAGE_CALLS = /^(toast(\.\w+)?|window\.confirm|confirm|window\.alert|alert)$/;

function sourceFiles(dir: string): string[] {
  const files: string[] = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) {
      // generated API client; locale data
      if (name === "api" || name === "i18n") continue;
      files.push(...sourceFiles(path));
    } else if (SOURCE_FILE.test(name) && !TEST_OR_DECLARATION.test(name)) {
      files.push(path);
    }
  }
  return files;
}

function looksLikeProse(text: string): boolean {
  const trimmed = text.trim();
  if (!LETTERS.test(trimmed)) return false;
  if (IDENTIFIER_LIKE.test(trimmed)) return false; // ids, paths, class names
  // a list of CSS classes: every token has a -, : or [ in it
  if (trimmed.split(WHITESPACE_RUN).every((token) => CLASS_TOKEN.test(token))) return false;
  return HAS_SPACE.test(trimmed) || SINGLE_WORD.test(trimmed);
}

function literal(node: ts.Node | undefined): string | null {
  if (!node) return null;
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) return node.text;
  if (ts.isTemplateExpression(node)) return node.getText().slice(1, -1);
  return null;
}

function untranslated(file: string): string[] {
  const text = readFileSync(file, "utf8");
  const kind = file.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, kind);
  const found: string[] = [];
  const add = (node: ts.Node, value: string) => {
    const clean = value.replace(WHITESPACE_RUNS, " ").trim();
    if (looksLikeProse(clean) && !ALLOWED.has(clean)) {
      const line = source.getLineAndCharacterOfPosition(node.getStart()).line + 1;
      found.push(`${relative(SRC, file)}:${line}: ${clean.slice(0, 80)}`);
    }
  };
  const visit = (node: ts.Node) => {
    if (ts.isJsxText(node)) {
      add(node, node.getText());
    } else if (ts.isJsxAttribute(node) && node.initializer) {
      if (TEXT_ATTRIBUTES.has(node.name.getText())) {
        const init = node.initializer;
        const value = ts.isJsxExpression(init) ? literal(init.expression) : literal(init);
        if (value) add(node, value);
      }
    } else if (ts.isCallExpression(node) && MESSAGE_CALLS.test(node.expression.getText())) {
      const value = literal(node.arguments[0]);
      if (value) add(node, value);
    } else if (ts.isPropertyAssignment(node)) {
      if (TEXT_PROPERTIES.has(node.name.getText().replace(QUOTES, ""))) {
        const value = literal(node.initializer);
        if (value) add(node, value);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return found;
}

function flatten(value: unknown, prefix = ""): Map<string, unknown> {
  const out = new Map<string, unknown>();
  if (value && typeof value === "object" && !Array.isArray(value)) {
    for (const [key, child] of Object.entries(value)) {
      for (const [k, v] of flatten(child, prefix ? `${prefix}.${key}` : key)) out.set(k, v);
    }
  } else {
    out.set(prefix, value);
  }
  return out;
}

/** i18next plural forms: English needs _one/_other, Chinese only _other. */
function baseKey(key: string): string {
  return key.replace(PLURAL_SUFFIX, "");
}

describe("locale files", () => {
  const namespaces = readdirSync(join(LOCALES, "en")).filter((f) => f.endsWith(".json"));

  it("exist for the same namespaces in both languages", () => {
    const zh = readdirSync(join(LOCALES, "zh-CN")).filter((f) => f.endsWith(".json"));
    assert.deepEqual([...zh].sort(), [...namespaces].sort());
  });

  for (const ns of namespaces) {
    it(`${ns}: every key is in both languages, and not left empty`, () => {
      const en = flatten(JSON.parse(readFileSync(join(LOCALES, "en", ns), "utf8")));
      const zh = flatten(JSON.parse(readFileSync(join(LOCALES, "zh-CN", ns), "utf8")));
      const enKeys = new Set([...en.keys()].map(baseKey));
      const zhKeys = new Set([...zh.keys()].map(baseKey));
      assert.deepEqual([...enKeys].filter((k) => !zhKeys.has(k)), [], "missing in zh-CN");
      assert.deepEqual([...zhKeys].filter((k) => !enKeys.has(k)), [], "missing in en");
      for (const [key, value] of zh) {
        assert.ok(typeof value === "string" && value.trim(), `zh-CN ${ns}:${key} is empty`);
      }
    });
  }
});

describe("components", () => {
  const files = sourceFiles(SRC);

  it("show no text that bypasses the translations", () => {
    const found = files.flatMap(untranslated);
    assert.deepEqual(found, [], `untranslated text:\n${found.join("\n")}`);
  });

  it("format dates in the UI language, not the browser's", () => {
    // toLocaleDateString(undefined) follows the browser, so a user who picked
    // 中文 in an English browser still saw "Sep 26, 2026". Pass i18n.language.
    const found: string[] = [];
    for (const file of files) {
      readFileSync(file, "utf8")
        .split("\n")
        .forEach((line, i) => {
          if (BROWSER_LOCALE_DATE.test(line)) {
            found.push(`${relative(SRC, file)}:${i + 1}: ${line.trim().slice(0, 80)}`);
          }
        });
    }
    assert.deepEqual(found, [], `dates in the browser's locale:\n${found.join("\n")}`);
  });

  it("write no Chinese outside the locale files", () => {
    const found: string[] = [];
    for (const file of files) {
      // A line may keep Chinese when it, or the line above, says why with
      // "i18n-allow-cjk: <reason>" (e.g. a regex matching either language).
      const lines = readFileSync(file, "utf8").split("\n");
      lines.forEach((line, i) => {
        const allowed = CJK_ALLOWED.test(line) || CJK_ALLOWED.test(lines[i - 1] ?? "");
        if (CJK.test(line) && !allowed) {
          found.push(`${relative(SRC, file)}:${i + 1}: ${line.trim().slice(0, 80)}`);
        }
      });
    }
    assert.deepEqual(found, [], `Chinese in source:\n${found.join("\n")}`);
  });
});
