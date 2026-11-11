import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { fileExtension, fileKind } from "./file-kind.ts";

describe("fileExtension", () => {
  it("returns the lower-cased extension", () => {
    assert.equal(fileExtension("Q3 Report.PDF"), "pdf");
    assert.equal(fileExtension("archive.tar.gz"), "gz");
  });

  it("returns empty for names without one", () => {
    assert.equal(fileExtension("Makefile"), "");
    assert.equal(fileExtension(".env"), "");
    assert.equal(fileExtension("trailing."), "");
  });
});

describe("fileKind", () => {
  it("tells common uploads apart by extension", () => {
    assert.equal(fileKind("report.pdf"), "pdf");
    assert.equal(fileKind("budget.xlsx"), "spreadsheet");
    assert.equal(fileKind("deck.pptx"), "presentation");
    assert.equal(fileKind("notes.docx"), "document");
    assert.equal(fileKind("bundle.zip"), "archive");
    assert.equal(fileKind("main.py"), "code");
  });

  it("falls back to the media type", () => {
    assert.equal(fileKind("scan", "application/pdf"), "pdf");
    assert.equal(fileKind("clip", "audio/mpeg"), "audio");
    assert.equal(fileKind("readme", "text/plain"), "text");
  });

  it("is 'other' when nothing is known", () => {
    assert.equal(fileKind("blob"), "other");
    assert.equal(fileKind("blob.xyz", "application/octet-stream"), "other");
  });
});
