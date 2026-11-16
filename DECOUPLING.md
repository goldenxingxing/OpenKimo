# Moonshot decoupling — what changed relative to Kimi Code CLI 0.4.11

OpenKimo is a modified version of Kimi Code CLI by Moonshot AI (see `NOTICE`).
This file itemizes the deliberate removals, so the change set can be audited
without diffing against upstream. `NOTICE` points at "the git history" for
details; this file is the itemized version of that list.

## Removed capabilities

| Capability | What went with it |
|---|---|
| Kimi account / Kimo Code OAuth login | `packages/kosong/src/kosong/chat_provider/kimi.py`; the platform-login flows in `kimo_cli/auth/oauth.py`; `tests/auth/`; ACP's `KIMO_CODE_OAUTH_KEY` check — authentication now accepts any provider carrying an `OAuthRef`, and a plain API-key setup needs no login |
| Kimi chat provider | `packages/kosong/src/kosong/chat_provider/kimi.py` and its API snapshot tests |
| Cloud knowledge base | `kimo_cli/memory/knowledge.py`, `memory/paths.py::get_knowledge_dir`, the knowledge endpoints in `web/api/memory.py`, and the `admin-knowledge-panel` admin page |
| Server-side web search | `kimo_cli/tools/web/search.py` — `tools/web` now ships `fetch` only |
| Dynamic completion budget | the Kimi-specific generation overrides and token safety margin in `llm.py`, and `tests/core/test_kimosoul_completion_budget.py` |
| kimo-sdk (this fork's own SDK) | `sdks/kimo-sdk/` and its CI/release workflows were removed; OpenKimo no longer ships an SDK. The `kimi-sdk` package on PyPI is Moonshot's and is unaffected — install it from upstream if you need it |
| Kimi-only docs | `docs/.vitepress/theme/EvolutionBanner.vue`, `klips/klip-7-kimo-sdk.md`, `klips/klip-14-kimo-code-oauth-login.md` |

## Version baseline

- Upstream base: Kimi Code CLI 0.4.11
- This fork: `kimo-cli` 0.5.0

## Behaviour changes worth knowing

- `/feedback` no longer posts to the kimo-code feedback endpoint; it opens the
  GitHub issue tracker (`https://github.com/goldenxingxing/OpenKimo/issues`).
- `wiki` no longer recognises a `web-search` evidence source: the search tool
  that produced it was removed, so the class was dropped from the evidence
  literals and the source-kind mapping.
