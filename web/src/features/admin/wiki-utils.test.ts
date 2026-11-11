import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { isValidSlug, pageSlug, parseTags, suggestSlug, wikiLinks } from "./wiki-utils.ts";

describe("wiki slugs", () => {
  it("accepts what the server accepts", () => {
    assert.ok(isValidSlug("release-train"));
    assert.ok(isValidSlug("发布流程"));
    assert.ok(isValidSlug("SQLite_WAL"));
  });

  it("refuses a leading hyphen, dots, slashes and spaces", () => {
    for (const slug of ["-x", "a.b", "a/b", "a b", ""]) {
      assert.equal(isValidSlug(slug), false, slug);
    }
  });

  it("suggests a valid slug from a title", () => {
    assert.equal(suggestSlug("  Release Train!  "), "Release-Train");
    assert.equal(suggestSlug("发布 流程 (v2)"), "发布-流程-v2");
    assert.ok(isValidSlug(suggestSlug("A / B -- C")));
  });
});

describe("wiki tags and links", () => {
  it("splits tags on either comma and drops blanks and repeats", () => {
    assert.deepEqual(parseTags("a, b，c,, a , "), ["a", "b", "c"]);
  });

  it("lists wiki links as page paths once each", () => {
    const body = "See [[concepts/cache]] and [[entities/sqlite]], again [[concepts/cache]].";
    assert.deepEqual(wikiLinks(body), ["concepts/cache.md", "entities/sqlite.md"]);
  });

  it("takes the slug out of a logical path", () => {
    assert.equal(pageSlug("concepts/发布流程.md"), "发布流程");
  });
});
