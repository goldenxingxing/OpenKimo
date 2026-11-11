import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  decodeOpenPathHref,
  extractMentionedPaths,
  isLikelyStandalonePath,
  linkifyPathsMarkdown,
  resolveLocalLinkPath,
} from "./message-paths.ts";

const DMG_OPEN_PATH_LINK =
  /\[OpenKimo\/dist\/app\.dmg\]\(open-path:\/\/%2FUsers%2Fqunwei%2FDocuments%2FACLS_2013%2FOpenKimo%2Fdist%2Fapp\.dmg\)/;
const REPORT_OPEN_PATH_LINK =
  /\[\/Users\/qunwei\/proj\/对比报告\.md\]\(open-path:\/\/%2FUsers%2Fqunwei%2Fproj%2F%E5%AF%B9%E6%AF%94%E6%8A%A5%E5%91%8A\.md\)/;
const OPEN_PATH_HREF = /open-path:\/\/([^)]+)/;

describe("extractMentionedPaths", () => {
  const workDir = "/Users/qunwei/Documents/ACLS_2013";

  it("finds an absolute path followed by Chinese punctuation", () => {
    const text = "报告已生成在 /Users/qunwei/acls_minisim/对比报告.md，请查收。";
    const paths = extractMentionedPaths(text);
    assert.deepEqual(paths, [
      { raw: "/Users/qunwei/acls_minisim/对比报告.md", resolved: "/Users/qunwei/acls_minisim/对比报告.md" },
    ]);
  });

  it("expands ~ paths and keeps them for the server to expand", () => {
    const text = "脚本放在 ~/Documents/ACLS_2013/tools/verify.py 了";
    const paths = extractMentionedPaths(text);
    assert.equal(paths[0]?.resolved, "~/Documents/ACLS_2013/tools/verify.py");
  });

  it("resolves a relative path against the work directory", () => {
    const text = "实现改动在 `acls2013/controller/mpc_controller.py`。";
    const paths = extractMentionedPaths(text, workDir);
    assert.deepEqual(paths, [
      {
        raw: "acls2013/controller/mpc_controller.py",
        resolved: "/Users/qunwei/Documents/ACLS_2013/acls2013/controller/mpc_controller.py",
      },
    ]);
  });

  it("accepts installer artifacts (dmg/zip/pkg) in bare relative mentions", () => {
    const text = "新 DMG 已就绪：OpenKimo/dist/OpenKimo-0.4.5-arm64.dmg。";
    const paths = extractMentionedPaths(text, workDir);
    assert.deepEqual(paths, [
      {
        raw: "OpenKimo/dist/OpenKimo-0.4.5-arm64.dmg",
        resolved: "/Users/qunwei/Documents/ACLS_2013/OpenKimo/dist/OpenKimo-0.4.5-arm64.dmg",
      },
    ]);
  });

  it("linkifies a bare dmg path into an open-path link", () => {
    const out = linkifyPathsMarkdown("下载 OpenKimo/dist/app.dmg 试用", workDir);
    assert.match(out, DMG_OPEN_PATH_LINK);
  });

  it("keeps chapter references and urls inert in code spans", () => {
    assert.equal(isLikelyStandalonePath("6.4.1/6.2.11", workDir), false);
    assert.equal(isLikelyStandalonePath("https://example.com/a/b.dmg", workDir), false);
    assert.equal(isLikelyStandalonePath("//shared/build", workDir), false);
  });

  it("accepts any letter-led extension in cited (backticked) paths", () => {
    assert.equal(
      isLikelyStandalonePath("OpenKimo/dist/OpenKimo-0.4.5-arm64.dmg", workDir),
      true,
    );
    assert.equal(isLikelyStandalonePath("dist/app.whl", workDir), true);
    assert.equal(isLikelyStandalonePath("release/app.tar.gz", workDir), true);
    // Extensionless multi-segment stays inert unless cited as a directory.
    assert.equal(isLikelyStandalonePath("src/kimo_cli/web/api", workDir), false);
    assert.equal(isLikelyStandalonePath("src/kimo_cli/web/api/", workDir), true);
  });

  it("accepts letter-led extensions in book-title-mark citations", () => {
    const paths = extractMentionedPaths("交付物见《dist/OpenKimo-arm64.dmg》", workDir);
    assert.deepEqual(paths, [
      {
        raw: "dist/OpenKimo-arm64.dmg",
        resolved: "/Users/qunwei/Documents/ACLS_2013/dist/OpenKimo-arm64.dmg",
      },
    ]);
  });

  it("ignores relative paths when there is no work directory", () => {
    const text = "实现改动在 `acls2013/controller/mpc_controller.py`。";
    assert.deepEqual(extractMentionedPaths(text), []);
  });

  it("does not treat bare words without a slash as relative paths", () => {
    const text = "这个 README.md 写得很清楚。";
    assert.deepEqual(extractMentionedPaths(text, workDir), []);
  });

  it("skips URLs and email addresses", () => {
    const text = "参考 https://example.com/a/b.md 或者 mail me at dev.example/x@y.com";
    assert.deepEqual(extractMentionedPaths(text, workDir), []);
  });

  it("trims closing brackets and quotes off the path", () => {
    const text = "配置在 /etc/kimo/config.toml）里，详见 /var/log/kimo/app.log.";
    const paths = extractMentionedPaths(text);
    assert.deepEqual(
      paths.map((p) => p.raw),
      ["/etc/kimo/config.toml", "/var/log/kimo/app.log"],
    );
  });

  it("deduplicates paths that resolve to the same target", () => {
    const text =
      "先看 /Users/qunwei/proj/main.py，然后 /Users/qunwei/proj/main.py 再总结。";
    const paths = extractMentionedPaths(text);
    assert.equal(paths.length, 1);
  });

  it("caps the number of offered paths", () => {
    const mentions = Array.from(
      { length: 20 },
      (_, i) => `/tmp/dir${i}/file${i}.py`,
    ).join(" ");
    assert.equal(extractMentionedPaths(mentions).length, 8);
  });

  it("returns nothing for empty or trivial text", () => {
    assert.deepEqual(extractMentionedPaths(""), []);
    assert.deepEqual(extractMentionedPaths("ok"), []);
  });

  it("includes backticked bare filenames when a work directory exists", () => {
    const text = "脚本在 `.build_srd_docx.py` 里，文档是 `HCP-021_系统需求文档_SRD_2026-10-15.docx`。";
    const paths = extractMentionedPaths(text, workDir);
    assert.deepEqual(
      paths.map((p) => p.resolved),
      [
        "/Users/qunwei/Documents/ACLS_2013/.build_srd_docx.py",
        "/Users/qunwei/Documents/ACLS_2013/HCP-021_系统需求文档_SRD_2026-10-15.docx",
      ],
    );
  });

  it("ignores bare filenames without a work directory", () => {
    const text = "脚本在 `.build_srd_docx.py` 里。";
    assert.deepEqual(extractMentionedPaths(text), []);
  });

  it("never turns version numbers or doc anchors into files", () => {
    const text = "章节 `7.4.8.1`、编号 `SYS-047`、范围 `SYS-005～SYS-010`。";
    assert.deepEqual(extractMentionedPaths(text, workDir), []);
  });

  it("rejects chapter-number ranges that merely look like paths", () => {
    const text = "部件文档推导值（SOC 6.2.1~6.2.6、MCU 6.4.1/6.2.11、APP 6.1.8）";
    assert.deepEqual(extractMentionedPaths(text, workDir), []);
  });
});

describe("linkifyPathsMarkdown", () => {
  const workDir = "/Users/qunwei/Documents/ACLS_2013";

  it("rewrites a bare absolute path into an open-path link", () => {
    const out = linkifyPathsMarkdown("报告在 /Users/qunwei/proj/对比报告.md，请查收。");
    assert.match(out, REPORT_OPEN_PATH_LINK);
    const href = out.match(OPEN_PATH_HREF)?.[1] ?? "";
    assert.equal(decodeOpenPathHref(`open-path://${href}`), "/Users/qunwei/proj/对比报告.md");
  });

  it("leaves fenced code blocks untouched", () => {
    const md = "看这个：\n```bash\ncat /Users/qunwei/proj/main.py\n```\n完毕";
    assert.equal(linkifyPathsMarkdown(md, workDir), md);
  });

  it("leaves inline code spans untouched by linkify", () => {
    const md = "改动在 `acls2013/controller/mpc_controller.py` 里。";
    assert.equal(linkifyPathsMarkdown(md, workDir), md);
  });

  it("leaves existing markdown links untouched", () => {
    const md = "见 [文档](/Users/qunwei/proj/README.md) 和 /Users/qunwei/proj/notes.md。";
    const out = linkifyPathsMarkdown(md);
    assert.ok(out.startsWith("见 [文档](/Users/qunwei/proj/README.md) 和 ["));
    assert.ok(!out.includes("[文档](open-path"));
  });

  it("resolves relative paths against the work directory", () => {
    const out = linkifyPathsMarkdown("实现见 acls2013/search/cost.py。", workDir);
    assert.ok(out.includes("](open-path://%2FUsers%2Fqunwei%2FDocuments%2FACLS_2013%2Facls2013%2Fsearch%2Fcost.py)"));
  });

  it("does not linkify when nothing matches", () => {
    const md = "没有路径的一句话。";
    assert.equal(linkifyPathsMarkdown(md, workDir), md);
  });
});

describe("resolveLocalLinkPath", () => {
  const workDir = "/Users/qunwei/Documents/OpenKimo";

  it("resolves a local markdown href against the session work directory", () => {
    assert.equal(
      resolveLocalLinkPath("output/hcp021_hw_mech_merge/validation_results.txt", workDir),
      "/Users/qunwei/Documents/OpenKimo/output/hcp021_hw_mech_merge/validation_results.txt",
    );
    assert.equal(
      resolveLocalLinkPath("output/hcp021_hw_mech_merge/", workDir),
      "/Users/qunwei/Documents/OpenKimo/output/hcp021_hw_mech_merge/",
    );
  });

  it("decodes a percent-encoded non-ASCII markdown href", () => {
    const encoded =
      "output/hcp021_sysrd_revised_review/SEDW00001271_A001_HCP-021_SysRD_%E7%A1%AC%E4%BB%B6%E6%9C%BA%E6%A2%B0%E5%A4%8D%E7%94%A8%E8%A1%A5%E5%85%85_%E4%BF%AE%E8%AE%A2%E6%BE%84%E6%B8%85%E7%89%88.docx";
    assert.equal(
      resolveLocalLinkPath(encoded, workDir),
      "/Users/qunwei/Documents/OpenKimo/output/hcp021_sysrd_revised_review/SEDW00001271_A001_HCP-021_SysRD_硬件机械复用补充_修订澄清版.docx",
    );
  });

  it("leaves web links, anchors, and unresolved relative links inert", () => {
    assert.equal(resolveLocalLinkPath("https://example.com/a/b.docx", workDir), null);
    assert.equal(resolveLocalLinkPath("#delivery-files", workDir), null);
    assert.equal(resolveLocalLinkPath("output/report.md"), null);
  });
});

describe("isLikelyStandalonePath", () => {
  const workDir = "/Users/qunwei/Documents/ACLS_2013";

  it("accepts an absolute path exactly as given", () => {
    assert.equal(isLikelyStandalonePath("/Users/qunwei/proj/main.py"), true);
    assert.equal(isLikelyStandalonePath("~/Documents/ACLS_2013/notes.md"), true);
  });

  it("accepts a path containing CJK file names", () => {
    assert.equal(isLikelyStandalonePath("/Users/qunwei/proj/对比报告.md"), true);
  });

  it("rejects relative paths, URLs, prose and punctuation noise", () => {
    assert.equal(isLikelyStandalonePath("acls2013/search/cost.py"), false);
    assert.equal(isLikelyStandalonePath("https://example.com/a/b.md"), false);
    assert.equal(isLikelyStandalonePath("main.py 再总结"), false);
    assert.equal(isLikelyStandalonePath("/Users/qunwei/proj/main.py。"), false);
    assert.equal(isLikelyStandalonePath("/"), false);
  });

  it("accepts a backticked work-directory-relative path with a work directory", () => {
    // The daily-report case: agents cite relative locations in backticks as
    // often as bare filenames.
    assert.equal(
      isLikelyStandalonePath(
        "output/reports/daily/26-10月/D-2026-10-15.md",
        workDir,
      ),
      true,
    );
    assert.equal(isLikelyStandalonePath("src/kimo_cli/web/api/open_in.py", workDir), true);
  });

  it("rejects a relative path without a work directory or with a non-file last segment", () => {
    assert.equal(
      isLikelyStandalonePath("output/reports/daily/D-2026-10-15.md"),
      false,
    );
    // Chapter-range style tokens must stay prose.
    assert.equal(isLikelyStandalonePath("6.4.1/6.2.11", workDir), false);
    // A directory-only mention links nowhere.
    assert.equal(isLikelyStandalonePath("src/kimo_cli/web/api", workDir), false);
  });

  it("accepts a whitelisted bare filename with a work directory", () => {
    assert.equal(
      isLikelyStandalonePath(".build_srd_docx.py", workDir),
      true,
    );
    assert.equal(
      isLikelyStandalonePath("HCP-021_系统需求文档_SRD_2026-10-15.docx", workDir),
      true,
    );
  });

  it("rejects a bare filename without a work directory or with a bad suffix", () => {
    assert.equal(isLikelyStandalonePath(".build_srd_docx.py"), false);
    assert.equal(isLikelyStandalonePath("7.4.8.1", workDir), false);
    assert.equal(isLikelyStandalonePath("SYS-047", workDir), false);
  });
});

describe("windows backslash paths", () => {
  const winWorkDir = "C:\\Users\\qunwei\\proj";

  it("finds a drive-letter absolute path in prose", () => {
    const text = "报告在 C:\\Users\\qunwei\\proj\\对比报告.md，请查收。";
    const paths = extractMentionedPaths(text);
    assert.deepEqual(paths, [
      {
        raw: "C:\\Users\\qunwei\\proj\\对比报告.md",
        resolved: "C:\\Users\\qunwei\\proj\\对比报告.md",
      },
    ]);
  });

  it("finds a backslash relative path and normalizes it against the work directory", () => {
    const text = "日报已写入 output\\reports\\daily\\26-10月\\D-2026-10-15.md";
    const paths = extractMentionedPaths(text, winWorkDir);
    assert.deepEqual(paths, [
      {
        raw: "output\\reports\\daily\\26-10月\\D-2026-10-15.md",
        resolved: "C:/Users/qunwei/proj/output/reports/daily/26-10月/D-2026-10-15.md",
      },
    ]);
  });

  it("keeps chapter-range backslash tokens out", () => {
    const text = "见 6.4.1\\6.2.11 一节";
    assert.deepEqual(extractMentionedPaths(text, winWorkDir), []);
  });

  it("linkifies a windows absolute path in markdown", () => {
    const markdown = "日志在 C:\\var\\log\\kimo\\app.log 里。";
    const linked = linkifyPathsMarkdown(markdown);
    assert.equal(
      linked.includes("[C:\\var\\log\\kimo\\app.log](open-path://"),
      true,
    );
    // decodeOpenPathHref strips the scheme itself, so pass the full href match.
    const href = linked.match(OPEN_PATH_HREF)?.[0] ?? "";
    assert.equal(decodeOpenPathHref(href), "C:\\var\\log\\kimo\\app.log");
  });

  it("standalone inline code: drive path needs no workDir, backslash relative does", () => {
    assert.equal(isLikelyStandalonePath("C:\\Users\\qunwei\\proj\\报告.md"), true);
    assert.equal(
      isLikelyStandalonePath(
        "output\\reports\\daily\\26-10月\\D-2026-10-15.md",
        winWorkDir,
      ),
      true,
    );
    assert.equal(
      isLikelyStandalonePath("output\\reports\\daily\\D-2026-10-15.md"),
      false,
    );
    assert.equal(isLikelyStandalonePath("6.4.1\\6.2.11", winWorkDir), false);
  });
});

describe("directory mentions", () => {
  const workDir = "/Users/qunwei/Documents/local_agent_work";

  it("accepts a backticked relative directory with a trailing separator", () => {
    // The reported case: deliverable directories cited with a trailing slash
    // and a parenthetical note after the backticks.
    assert.equal(
      isLikelyStandalonePath(
        "output/projects/insulin_agent/HCP-021_SRD/风险分析评审/",
        workDir,
      ),
      true,
    );
  });

  it("accepts a backticked absolute directory with a trailing separator", () => {
    assert.equal(
      isLikelyStandalonePath("/Users/qunwei/Documents/local_agent_work/output/"),
      true,
    );
  });

  it("linkifies a plain-text relative directory mention", () => {
    const markdown = "交付物在 output/projects/insulin_agent/HCP-021_SRD/风险分析评审/（新建评审线目录）";
    const linked = linkifyPathsMarkdown(markdown, workDir);
    assert.equal(
      linked.includes(
        "[output/projects/insulin_agent/HCP-021_SRD/风险分析评审](open-path://",
      ),
      true,
    );
  });

  it("resolves a directory mention against the work directory", () => {
    const paths = extractMentionedPaths(
      "见 output/reports/daily/ 目录",
      workDir,
    );
    assert.deepEqual(paths, [
      {
        raw: "output/reports/daily",
        resolved: "/Users/qunwei/Documents/local_agent_work/output/reports/daily",
      },
    ]);
  });

  it("still rejects directory mentions without a trailing separator or known suffix", () => {
    // No trailing slash, no file suffix: this stays prose.
    assert.equal(isLikelyStandalonePath("src/kimo_cli/web/api", workDir), false);
    assert.deepEqual(extractMentionedPaths("见 a/b/c 一节", workDir), []);
    assert.equal(isLikelyStandalonePath("6.4.1/6.2.11", workDir), false);
  });

  it("rejects a relative directory mention without a work directory", () => {
    assert.equal(
      isLikelyStandalonePath("output/reports/daily/"),
      false,
    );
  });

  it("accepts a bare subdirectory name with a trailing separator", () => {
    // How the agent cites a subdirectory of the topic's directory in a
    // deliverables tree: no directory component, just the name and a slash.
    assert.equal(isLikelyStandalonePath("评审意见台账/", workDir), true);
    assert.equal(isLikelyStandalonePath("HCP-021_SRD/", workDir), true);
    assert.equal(isLikelyStandalonePath("doc_review\\", workDir), true);
  });

  it("rejects a bare numeric token with a trailing separator", () => {
    // A chapter ref like `6.4.1/` must stay prose: no letter, no link.
    assert.equal(isLikelyStandalonePath("6.4.1/", workDir), false);
  });

  it("extracts bare and nested directory mentions from a deliverables tree", () => {
    const text = [
      "主目录 output/projects/insulin_agent/HCP-021_SRD/：",
      "HCP-021_SRD/",
      "├── 评审意见台账/        13 项 — PRD/URD 评审线",
      "├── 设计输入评审/         5 项 — PDD000203 评审报告",
      "└── 网络架构与开发计划评审/ 4 项",
      "本轮移动：26 个文件/目录，来自 doc_review/（13）、reviews/（4+1）。",
    ].join("\n");
    const paths = extractMentionedPaths(text, workDir).map((p) => p.raw);
    assert.deepEqual(paths, [
      "output/projects/insulin_agent/HCP-021_SRD",
      "HCP-021_SRD",
      "评审意见台账",
      "设计输入评审",
      "网络架构与开发计划评审",
      "doc_review",
      "reviews",
    ]);
  });
});

describe("book-title-mark citations", () => {
  const workDir = "/Users/qunwei/Documents/local_agent_work";

  it("extracts bare filenames wrapped in CJK book-title marks", () => {
    const text =
      "**《HIL闭环测试结果.md》已原地改写为最终版**（36.5 KB，原 156 KB），" +
      "原稿完整保留为同目录 **《HIL闭环测试结果_工作底稿.md》**。";
    const paths = extractMentionedPaths(text, workDir);
    assert.deepEqual(paths.map((p) => p.raw), [
      "HIL闭环测试结果.md",
      "HIL闭环测试结果_工作底稿.md",
    ]);
    assert.equal(
      paths[0]?.resolved,
      "/Users/qunwei/Documents/local_agent_work/HIL闭环测试结果.md",
    );
  });

  it("linkifies the inner token, keeping the marks as plain text", () => {
    const text = "**《HIL闭环测试结果.md》已原地改写为最终版**";
    const linked = linkifyPathsMarkdown(text, workDir);
    assert.equal(
      linked.includes("**《[HIL闭环测试结果.md](open-path://"),
      true,
    );
  });

  it("accepts book-title-marked content in inline code spans", () => {
    assert.equal(
      isLikelyStandalonePath("《HIL闭环测试结果.md》", workDir),
      true,
    );
  });

  it("accepts a relative path or directory wrapped in book-title marks", () => {
    assert.equal(
      isLikelyStandalonePath("《output/reports/daily/D-2026-10-15.md》", workDir),
      true,
    );
    assert.equal(
      isLikelyStandalonePath("《评审意见台账/》", workDir),
      true,
    );
  });

  it("keeps prose book titles without a file suffix out", () => {
    assert.deepEqual(
      extractMentionedPaths("参考《代码整洁之道》一书。", workDir),
      [],
    );
    assert.equal(
      isLikelyStandalonePath("《代码整洁之道》", workDir),
      false,
    );
  });
});
