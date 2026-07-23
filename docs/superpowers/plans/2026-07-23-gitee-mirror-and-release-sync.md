# Gitee Mirror and Release Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically mirror Git refs and copy each successfully completed GitHub Release, including every attachment, to Gitee.

**Architecture:** Keep `.github/workflows/sync-to-gitee.yml` independent from the build workflow. A mirror job handles Git refs over SSH, while a tested Python standard-library client synchronizes Release metadata and attachments through the GitHub and Gitee APIs after the `Release` workflow succeeds.

**Tech Stack:** GitHub Actions YAML, Python 3 standard library, `unittest`, GitHub REST API, Gitee API v5.

## Global Constraints

- GitHub repository `goldenxingxing/OpenKimo` is the only publication source.
- Gitee repository `qunwei/OpenKimo` is a mirror; Gitee-only refs may be deleted by `git push --mirror`.
- `GITEE_RSA_PRIVATE_KEY` is used only for Git mirroring.
- `GITEE_ACCESS_TOKEN` is used only for Gitee Release API calls.
- Gitee synchronization failure must not alter the result or artifacts of the GitHub `Release` workflow.
- Release synchronization must be idempotent and enumerate all attachments instead of hard-coding platform filenames.
- Existing `v*` releases must be manually synchronizable without rebuilding platform packages.
- No secret value may be logged or committed.

---

### Task 1: Implement and test the Release synchronization client

**Files:**
- Create: `.github/scripts/sync_gitee_release.py`
- Create: `tests/test_sync_gitee_release.py`

**Interfaces:**
- Consumes environment variables `GITHUB_TOKEN`, `GITEE_ACCESS_TOKEN`, `GITHUB_REPOSITORY`, `GITEE_REPOSITORY`, and CLI argument `--tag`.
- Produces an idempotent Gitee Release whose metadata and named attachments match the GitHub Release for the supplied tag.
- Exposes `ApiClient.request_json()`, `ApiClient.download()`, `GitHubReleaseSource.get_release()`, and `GiteeReleaseTarget.sync()` for unit testing.

- [ ] **Step 1: Write failing tests for metadata creation/update and attachment replacement**

Create `tests/test_sync_gitee_release.py` with a fake HTTP transport that records method, URL, fields, and uploaded filenames. Cover these exact behaviors:

```python
def test_creates_release_when_tag_is_absent():
    target = GiteeReleaseTarget(fake_api(returning=[HttpError(404), {"id": 7}]), "qunwei", "OpenKimo")
    release = Release(tag="v0.1.19", name="v0.1.19", body="notes", assets=[])
    target.sync_metadata(release)
    assert fake_api.calls[-1].method == "POST"
    assert fake_api.calls[-1].path == "/repos/qunwei/OpenKimo/releases"
    assert fake_api.calls[-1].fields["tag_name"] == "v0.1.19"


def test_updates_release_when_tag_exists():
    target = GiteeReleaseTarget(fake_api(returning=[{"id": 7}, {"id": 7}]), "qunwei", "OpenKimo")
    target.sync_metadata(Release(tag="v0.1.19", name="OpenKimo 0.1.19", body="notes", assets=[]))
    assert fake_api.calls[-1].method == "PATCH"
    assert fake_api.calls[-1].path == "/repos/qunwei/OpenKimo/releases/7"


def test_replaces_same_named_asset_and_keeps_other_assets():
    existing = [{"id": 3, "name": "OpenKimo.dmg"}, {"id": 4, "name": "notes.txt"}]
    target = GiteeReleaseTarget(fake_api(returning=[existing, None, {"id": 5}]), "qunwei", "OpenKimo")
    target.replace_asset(7, Asset("OpenKimo.dmg", "https://example.invalid/app"), Path("OpenKimo.dmg"))
    assert [call.method for call in fake_api.calls] == ["GET", "DELETE", "POST"]
    assert "/attach_files/3" in fake_api.calls[1].path
```

Also test that non-2xx responses raise a sanitized error containing the HTTP status but not either token.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
python3 -m unittest tests.test_sync_gitee_release -v
```

Expected: FAIL because `.github/scripts/sync_gitee_release.py` and its interfaces do not exist.

- [ ] **Step 3: Implement the minimal standard-library client**

Create `.github/scripts/sync_gitee_release.py` with:

```python
@dataclass(frozen=True)
class Asset:
    name: str
    url: str


@dataclass(frozen=True)
class Release:
    tag: str
    name: str
    body: str
    assets: list[Asset]
```

Implement authenticated requests with `urllib.request`, JSON encoding for metadata, and `multipart/form-data` encoding for attachment upload. Use these endpoints:

```text
GET    https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}
GET    https://gitee.com/api/v5/repos/{owner}/{repo}/releases/tags/{tag}
POST   https://gitee.com/api/v5/repos/{owner}/{repo}/releases
PATCH  https://gitee.com/api/v5/repos/{owner}/{repo}/releases/{release_id}
GET    https://gitee.com/api/v5/repos/{owner}/{repo}/releases/{release_id}/attach_files
DELETE https://gitee.com/api/v5/repos/{owner}/{repo}/releases/{release_id}/attach_files/{asset_id}
POST   https://gitee.com/api/v5/repos/{owner}/{repo}/releases/{release_id}/attach_files
```

Send GitHub authentication as `Authorization: Bearer <token>`. Send Gitee authentication as `access_token` form/query data. Download GitHub assets with `Accept: application/octet-stream`. Retry HTTP 429 and 5xx responses up to three attempts with delays of 1 and 2 seconds. Never include request headers, query tokens, or response bodies in raised errors.

`main()` must validate that the tag begins with `v`, parse both `owner/repository` environment values, use `tempfile.TemporaryDirectory`, synchronize metadata first, then download and replace every GitHub attachment.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run:

```bash
python3 -m unittest tests.test_sync_gitee_release -v
python3 -m py_compile .github/scripts/sync_gitee_release.py
```

Expected: all synchronization tests pass and compilation exits 0.

- [ ] **Step 5: Commit the tested client**

```bash
git add .github/scripts/sync_gitee_release.py tests/test_sync_gitee_release.py
git commit -m "feat: add Gitee release synchronization client"
```

### Task 2: Wire mirror and Release events into GitHub Actions

**Files:**
- Modify: `.github/workflows/sync-to-gitee.yml`
- Test: `tests/test_sync_to_gitee_workflow.py`

**Interfaces:**
- Consumes the Task 1 command:
  `python3 .github/scripts/sync_gitee_release.py --tag "$RELEASE_TAG"`.
- Produces `mirror` for Git push/create/delete events and `sync-release` for successful `Release` workflow runs whose `head_branch` starts with `v`.

- [ ] **Step 1: Write a failing workflow contract test**

Create `tests/test_sync_to_gitee_workflow.py` using `pathlib.Path` and string assertions. Require:

```python
def test_workflow_has_git_and_release_triggers(self):
    text = WORKFLOW.read_text()
    self.assertIn("workflow_run:", text)
    self.assertIn("workflows: [Release]", text)
    self.assertIn("types: [completed]", text)
    self.assertIn("tags:", text)
    self.assertIn("- 'v*'", text)
    self.assertIn("delete:", text)


def test_release_job_waits_for_successful_version_run(self):
    text = WORKFLOW.read_text()
    self.assertIn("github.event.workflow_run.conclusion == 'success'", text)
    self.assertIn("startsWith(github.event.workflow_run.head_branch, 'v')", text)
    self.assertIn("GITEE_ACCESS_TOKEN", text)
    self.assertIn("sync_gitee_release.py", text)
```

Also assert the file contains two distinct concurrency group names and does not contain a literal token value.

- [ ] **Step 2: Run the workflow test and verify RED**

Run:

```bash
python3 -m unittest tests.test_sync_to_gitee_workflow -v
```

Expected: FAIL because the current workflow lacks `workflow_run`, tag/delete triggers, concurrency controls, and the Release job.

- [ ] **Step 3: Update the workflow with event-specific jobs**

Replace `.github/workflows/sync-to-gitee.yml` with a workflow shaped as follows:

```yaml
name: Sync to Gitee

on:
  push:
    branches: [main]
    tags:
      - 'v*'
  delete:
  workflow_run:
    workflows: [Release]
    types: [completed]

jobs:
  mirror:
    if: github.event_name == 'push' || github.event_name == 'delete'
    concurrency:
      group: gitee-git-mirror
      cancel-in-progress: false
    runs-on: ubuntu-latest
    steps:
      - uses: wearerequired/git-mirror-action@v1
        env:
          SSH_PRIVATE_KEY: ${{ secrets.GITEE_RSA_PRIVATE_KEY }}
        with:
          source-repo: git@github.com:goldenxingxing/OpenKimo.git
          destination-repo: git@gitee.com:qunwei/OpenKimo.git

  sync-release:
    if: >-
      github.event_name == 'workflow_run' &&
      github.event.workflow_run.conclusion == 'success' &&
      startsWith(github.event.workflow_run.head_branch, 'v')
    concurrency:
      group: gitee-release-${{ github.event.workflow_run.head_branch }}
      cancel-in-progress: false
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.workflow_run.head_sha }}
      - name: Synchronize Gitee Release
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          GITEE_ACCESS_TOKEN: ${{ secrets.GITEE_ACCESS_TOKEN }}
          GITHUB_REPOSITORY: ${{ github.repository }}
          GITEE_REPOSITORY: qunwei/OpenKimo
          RELEASE_TAG: ${{ github.event.workflow_run.head_branch }}
        run: python3 .github/scripts/sync_gitee_release.py --tag "$RELEASE_TAG"
```

Do not use `actions/checkout` in the mirror job because the mirror Action clones its configured source itself.

- [ ] **Step 4: Run static and regression verification**

Run:

```bash
python3 -m unittest tests.test_sync_to_gitee_workflow tests.test_sync_gitee_release -v
ruby -e 'require "yaml"; YAML.load_file(".github/workflows/sync-to-gitee.yml"); puts "YAML parse: OK"'
git diff --check
```

If `actionlint` is available, also run:

```bash
actionlint .github/workflows/sync-to-gitee.yml
```

Expected: all unit tests pass, YAML prints `YAML parse: OK`, and all other commands exit 0.

- [ ] **Step 5: Commit the workflow**

```bash
git add .github/workflows/sync-to-gitee.yml tests/test_sync_to_gitee_workflow.py
git commit -m "ci: mirror releases to Gitee"
```

### Task 3: Push and perform remote acceptance checks

**Files:**
- No source changes expected.

**Interfaces:**
- Consumes the two commits from Tasks 1 and 2.
- Produces a successful GitHub Actions mirror run; full Release attachment acceptance occurs on the next `v*` release or a manually rerun successful Release workflow.

- [ ] **Step 1: Verify the exact outgoing commits and preserve unrelated local changes**

Run:

```bash
git status --short
git log --oneline release/main..HEAD
git diff -- packaging/venvstacks.resolved.toml
```

Expected: `packaging/venvstacks.resolved.toml` remains unstaged and unchanged by this implementation.

- [ ] **Step 2: Push `main` to the release remote**

Run:

```bash
git push release main
```

Expected: push succeeds without force.

- [ ] **Step 3: Verify the mirror run**

Query:

```bash
curl -fsSL \
  'https://api.github.com/repos/goldenxingxing/OpenKimo/actions/workflows/sync-to-gitee.yml/runs?per_page=1'
```

Expected: the newest push-triggered run completes successfully and Gitee `main` resolves to the same commit as GitHub `main`.

- [ ] **Step 4: Record Release acceptance requirements**

On the next successful `v*` GitHub Release workflow, verify:

```text
Gitee contains the same tag.
Gitee contains a Release with the same title and body.
Gitee attachment names and count equal the GitHub Release.
Re-running the sync-release job does not create duplicate attachments.
```

If no new release is authorized in this task, report this final remote acceptance as pending rather than creating a version tag.
