# OpenKimo Login Copy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the login card's Kimo Code product reference with OpenKimo while preserving every other login-page behavior and presentation detail.

**Architecture:** Keep the login page as the single source of the rendered English copy, with no i18n plumbing or component restructuring. Add a small Python source-contract test in the repository's existing `tests/` suite that reads the exact component file and asserts the deprecated copy is absent and the approved copy is present.

**Tech Stack:** React 19, TypeScript, Vite, Python pytest, npm.

## Global Constraints

- Only modify the login-card subtitle in `kimo-cli/web/src/features/auth/login-page.tsx`; preserve its component structure, layout, styles, form behavior, and every other login-page string.
- The exact approved English copy is `Enter your credentials to access OpenKimo` in every system language; do not add i18n.
- Do not modify backend, authentication, packaging, or any other brand element.
- The source-contract test must assert both that `Enter your credentials to access Kimo Code` is absent and that `Enter your credentials to access OpenKimo` is present.
- Do not modify or stage `tests/test_wiki_approval_verification.py` or `tests/test_wiki_independent_verification.py`.
- Do not modify or stage `packaging/venvstacks.resolved.toml`, and do not touch uncommitted changes in the primary worktree.

---

### Task 1: Lock and update the login subtitle

**Files:**
- Create: `tests/test_login_brand_copy.py`
- Modify: `kimo-cli/web/src/features/auth/login-page.tsx:57-59`
- Test: `tests/test_login_brand_copy.py`

**Interfaces:**
- Consumes: UTF-8 source text from `kimo-cli/web/src/features/auth/login-page.tsx`.
- Produces: `test_login_subtitle_uses_openkimo_brand_copy() -> None`, a repository-level contract that rejects the old subtitle and accepts the approved OpenKimo subtitle.

- [ ] **Step 1: Write the failing source-contract test**

Create `tests/test_login_brand_copy.py` with this exact test:

```python
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOGIN_PAGE = ROOT / "kimo-cli" / "web" / "src" / "features" / "auth" / "login-page.tsx"


def test_login_subtitle_uses_openkimo_brand_copy() -> None:
    source = LOGIN_PAGE.read_text(encoding="utf-8")

    assert "Enter your credentials to access Kimo Code" not in source
    assert "Enter your credentials to access OpenKimo" in source
```

- [ ] **Step 2: Run the focused test to verify it fails against the old copy**

Run: `python3 -m pytest tests/test_login_brand_copy.py -q`

Expected: FAIL because `login-page.tsx` still contains `Enter your credentials to access Kimo Code` and does not contain the approved OpenKimo subtitle.

- [ ] **Step 3: Make the one-line approved copy replacement**

In `kimo-cli/web/src/features/auth/login-page.tsx`, replace only this JSX text node:

```tsx
Enter your credentials to access Kimo Code
```

with:

```tsx
Enter your credentials to access OpenKimo
```

Do not alter the surrounding `CardDescription`, imports, markup, styles, callbacks, or any other string.

- [ ] **Step 4: Run the focused source-contract test to verify it passes**

Run: `python3 -m pytest tests/test_login_brand_copy.py -q`

Expected: PASS with one passing test.

- [ ] **Step 5: Verify the web application typechecks and builds**

Run: `npm run typecheck --prefix kimo-cli/web`

Expected: TypeScript completes with exit status 0.

Run: `npm run build --prefix kimo-cli/web`

Expected: Vite production build completes with exit status 0.

- [ ] **Step 6: Confirm protected files remain untouched and commit the scoped change**

Run: `git status --short`

Expected: the only files staged for this task are `tests/test_login_brand_copy.py` and `kimo-cli/web/src/features/auth/login-page.tsx`; `tests/test_wiki_approval_verification.py`, `tests/test_wiki_independent_verification.py`, and `packaging/venvstacks.resolved.toml` are not staged or modified by this task.

Run:

```bash
git add tests/test_login_brand_copy.py kimo-cli/web/src/features/auth/login-page.tsx
git commit -m "fix: brand login subtitle as OpenKimo"
```

Expected: one commit containing only the source-contract test and the one-line login subtitle change.
