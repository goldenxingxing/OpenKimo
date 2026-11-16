from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCALES = ROOT / "web" / "src" / "i18n" / "locales"
LOGIN_PAGE = ROOT / "web" / "src" / "features" / "auth" / "login-page.tsx"


def test_login_subtitle_uses_openkimo_brand_copy() -> None:
    # The subtitle comes from the auth namespace, in each UI language.
    assert 't("login.description")' in LOGIN_PAGE.read_text(encoding="utf-8")
    subtitles = {
        lang: json.loads((LOCALES / lang / "auth.json").read_text(encoding="utf-8"))["login"][
            "description"
        ]
        for lang in ("en", "zh-CN")
    }

    assert subtitles["en"] == "Enter your credentials to access OpenKimo"
    for subtitle in subtitles.values():
        assert "OpenKimo" in subtitle
        assert "Kimo Code" not in subtitle
