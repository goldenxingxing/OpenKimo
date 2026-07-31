from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGIN_PAGE = ROOT / "kimi-cli" / "web" / "src" / "features" / "auth" / "login-page.tsx"


def test_login_subtitle_uses_openkimo_brand_copy() -> None:
    source = LOGIN_PAGE.read_text(encoding="utf-8")

    assert "Enter your credentials to access Kimi Code" not in source
    assert "Enter your credentials to access OpenKimo" in source
