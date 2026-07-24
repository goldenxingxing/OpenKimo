from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APPROVAL_DIALOG = (
    ROOT / "kimi-cli" / "web" / "src" / "features" / "chat" / "components"
    / "approval-dialog.tsx"
)


def test_wiki_approval_reuses_three_actions_with_compact_copy() -> None:
    source = APPROVAL_DIALOG.read_text(encoding="utf-8")

    assert 'type === "wiki"' in source
    assert '"approve_for_session"' in source
    assert 't("chat:wikiApproval.title")' in source
    assert 't("chat:wikiApproval.details")' in source
    assert 't("chat:wikiApproval.sources")' in source
    assert 't("chat:wikiApproval.duplicates")' in source
    assert 't("chat:wikiApproval.conflicts")' in source
    assert "wikiApproval.omitted" in source
    assert 't("chat:wikiApproval.approveOnce")' in source
    assert 't("chat:wikiApproval.approveForSession")' in source
    assert 't("chat:wikiApproval.decline")' in source
    assert "<details" in source
    assert "machinePath" not in source


def test_wiki_approval_locales_define_compact_labels() -> None:
    en = json.loads(
        (
            ROOT / "kimi-cli" / "web" / "src" / "i18n" / "locales" / "en" / "chat.json"
        ).read_text(encoding="utf-8")
    )
    zh = json.loads(
        (
            ROOT / "kimi-cli" / "web" / "src" / "i18n" / "locales" / "zh-CN" / "chat.json"
        ).read_text(encoding="utf-8")
    )

    assert en["wikiApproval"] == {
        "title": "Write to global Wiki",
        "details": "Details",
        "paths": "Pages",
        "sources": "Sources",
        "duplicates": "Duplicates omitted",
        "conflicts": "Conflicts preserved",
        "omitted": "{{count}} more omitted",
        "approveOnce": "Allow once",
        "approveForSession": "Always allow this session",
        "decline": "Decline",
    }
    assert zh["wikiApproval"] == {
        "title": "写入全局 Wiki",
        "details": "详情",
        "paths": "页面",
        "sources": "来源",
        "duplicates": "已忽略的重复页面",
        "conflicts": "已保留的冲突页面",
        "omitted": "另有 {{count}} 项已省略",
        "approveOnce": "仅允许一次",
        "approveForSession": "本会话始终允许",
        "decline": "拒绝",
    }
