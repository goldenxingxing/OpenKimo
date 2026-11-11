"""Admin evolution API tests: auth, gating, job mutex, generations, adoption."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kimo_cli.evolution import library
from kimo_cli.web.api import admin_evolution
from kimo_cli.web.api.admin_evolution import router
from kimo_cli.web.user_auth import require_admin

BASE = "/api/admin/evolution"


@pytest.fixture(autouse=True)
def _reset_job_state(monkeypatch: pytest.MonkeyPatch) -> None:
    admin_evolution._job = None


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool = True) -> TestClient:
    monkeypatch.setenv("KIMO_SHARE_DIR", str(tmp_path / "share"))
    config_path = tmp_path / "share" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(f"default_self_evolve = {'true' if enabled else 'false'}\n")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_admin] = lambda: {
        "id": "admin",
        "username": "root",
        "role": "admin",
    }
    return TestClient(app)


@pytest.fixture
def inline_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run evolution jobs inline: TestClient cancels detached tasks when the
    response completes (real uvicorn does not), so completion-path tests need
    the spawner to await the job before responding."""

    async def _inline(coro: Any) -> None:
        await coro

    monkeypatch.setattr(admin_evolution, "_job_spawner", _inline)


def _wait_job(client: TestClient, timeout_s: float = 5.0) -> dict[str, Any]:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        job = client.get(BASE + "/jobs/current").json()
        if job["status"] in ("succeeded", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish in time")


def _write_records(share: Path, task: str = "fix the bug") -> None:
    d = share / "sessions" / "s1" / "evolution"
    d.mkdir(parents=True)
    (d / "records.jsonl").write_text(
        json.dumps(
            {
                "outcome": "completed",
                "steps": 5,
                "input_tokens": 100,
                "output_tokens": 50,
                "reasoning_tokens": 0,
                "duration_ms": 1000,
                "task": task,
            }
        )
        + "\n"
    )


def _make_proposal(share: Path, pid: str) -> None:
    p = share / "evolution" / "proposals" / pid
    p.mkdir(parents=True)
    library.save_meta(p, {"id": pid, "status": "proposed", "created_at": 1.0})
    (p / "PROPOSAL.md").write_text("# Proposal\n\n## Rationale\n\ndo less\n", encoding="utf-8")


def test_overview_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    client = _client(tmp_path, monkeypatch)
    data = client.get(BASE + "/overview").json()
    assert data["enabled"] is True
    assert data["evidence_turns"] == 0
    assert data["generation_current"] == 0


def test_overview_counts_workdir_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Regression: the admin overview scanned only share App sessions
    (``get_share_dir()/sessions``) and reported 0 evidence turns despite real
    records under ``<work_dir>/session-data``.  It must aggregate workspace
    evidence too."""
    client = _client(tmp_path, monkeypatch)
    ws_root = tmp_path / "work" / "session-data"
    rec = ws_root / "s1" / "evolution" / "records.jsonl"
    rec.parent.mkdir(parents=True)
    rec.write_text(
        json.dumps(
            {
                "outcome": "completed",
                "steps": 3,
                "input_tokens": 10,
                "output_tokens": 5,
                "reasoning_tokens": 0,
                "duration_ms": 100,
                "task": "ws task",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(library, "_local_work_dir_session_roots", lambda: [ws_root])

    data = client.get(BASE + "/overview").json()
    assert data["evidence_turns"] == 1
    assert data["evidence_tasks"] == 1

    groups = client.get(BASE + "/evidence").json()
    assert len(groups) == 1
    assert groups[0]["task"] == "ws task"


def test_evidence_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    share = tmp_path / "share"
    client = _client(tmp_path, monkeypatch)
    _write_records(share)
    groups = client.get(BASE + "/evidence").json()
    assert len(groups) == 1
    assert groups[0]["task"] == "fix the bug"
    assert groups[0]["runs"] == 1
    assert client.get(BASE + "/evidence", params={"pattern": "nomatch"}).json() == []


def test_propose_refused_when_switch_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    client = _client(tmp_path, monkeypatch, enabled=False)
    resp = client.post(BASE + "/proposals", json={})
    assert resp.status_code == 403
    assert "Self-evolution is off" in resp.json()["detail"]


def test_verify_refused_when_switch_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    client = _client(tmp_path, monkeypatch, enabled=False)
    assert client.post(BASE + "/proposals/x/verify", json={"limit": 1}).status_code == 403


def test_adopt_refused_when_switch_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    client = _client(tmp_path, monkeypatch, enabled=False)
    assert client.post(BASE + "/proposals/x/adopt").status_code == 403


def test_propose_job_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inline_jobs: None
) -> None:
    share = tmp_path / "share"
    client = _client(tmp_path, monkeypatch)
    _write_records(share)

    async def _fake_propose(config, **kwargs):
        from kimo_cli.evolution import library as lib

        entry_path = lib.proposals_dir() / "fake1"
        entry_path.mkdir(parents=True)
        lib.save_meta(entry_path, {"id": "fake1", "status": "proposed"})
        entry = lib._load_entry(entry_path)
        from kimo_cli.evolution.editor import ProposalResult

        return ProposalResult(entry=entry, rationale="be terse")

    monkeypatch.setattr(admin_evolution.editor, "propose", _fake_propose)

    resp = client.post(BASE + "/proposals", json={})
    assert resp.status_code == 202
    # Inline spawner: the job finished before the response returned.
    job = client.get(BASE + "/jobs/current").json()
    assert job["status"] == "succeeded"
    assert job["result"]["proposal_id"] == "fake1"
    proposals = client.get(BASE + "/proposals").json()
    assert [p["id"] for p in proposals] == ["fake1"]
    # Audit recorded.
    actions = client.get(BASE + "/actions").json()
    assert any(a["action"] == "propose" for a in actions)


async def test_job_mutex() -> None:
    """Two concurrent jobs: the second is rejected with 409.

    Exercised at unit level on _start_job: TestClient cancels detached tasks
    at response completion, so the mutex cannot be observed through HTTP in
    tests (in production uvicorn keeps the job alive across requests).
    """
    from fastapi import HTTPException

    async def _slow() -> None:
        await asyncio.sleep(0.2)

    first = await admin_evolution._start_job("propose", None, _slow())
    assert first.status == "running"
    with pytest.raises(HTTPException) as exc:
        await admin_evolution._start_job("propose", None, _slow())
    assert exc.value.status_code == 409
    await asyncio.sleep(0.4)
    assert admin_evolution._job["status"] == "succeeded"


def test_verify_then_adopt_then_generations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inline_jobs: None
) -> None:
    share = tmp_path / "share"
    client = _client(tmp_path, monkeypatch)
    _write_records(share)
    _make_proposal(share, "p1")

    # Adopt without evidence → 409.
    assert client.post(BASE + "/proposals/p1/adopt").status_code == 409

    async def _fake_verify(config, proposal_id, **kwargs):
        ev = library.verify_runs_dir() / proposal_id / "evidence.json"
        ev.parent.mkdir(parents=True, exist_ok=True)
        ev.write_text(
            json.dumps(
                {
                    "proposal_id": proposal_id,
                    "overall_verdict_suggestion": "candidate_looks_better",
                    "tasks": [],
                }
            )
        )
        return ev

    monkeypatch.setattr(admin_evolution.verify, "verify", _fake_verify)
    assert client.post(BASE + "/proposals/p1/verify", json={"limit": 1}).status_code == 202
    job = client.get(BASE + "/jobs/current").json()
    assert job["status"] == "succeeded"

    # Evidence readable.
    ev = client.get(BASE + "/proposals/p1/evidence").json()
    assert ev["overall_verdict_suggestion"] == "candidate_looks_better"

    # Adopt creates generation 1.
    resp = client.post(BASE + "/proposals/p1/adopt")
    assert resp.status_code == 200
    assert resp.json() == {"adopted": "p1", "generation_current": 1}
    overview = client.get(BASE + "/overview").json()
    assert overview["generation_current"] == 1
    assert overview["adopted_total"] == 1


def test_rollback_and_advance_endpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    client = _client(tmp_path, monkeypatch)
    # No generations yet → rollback is 400.
    resp = client.post(BASE + "/generations/rollback")
    assert resp.status_code == 400

    share = tmp_path / "share"
    _make_proposal(share, "p1")
    library.adopt_proposal("p1", None, actor="test")
    assert client.post(BASE + "/generations/rollback").status_code == 400  # already at gen 1
    assert client.post(BASE + "/generations/advance").status_code == 400  # newest


def test_toggle_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    client = _client(tmp_path, monkeypatch)
    share = tmp_path / "share"
    _make_proposal(share, "p1")
    library.adopt_proposal("p1", None, actor="test")

    resp = client.post(BASE + "/adopted/p1/toggle", json={"enabled": False})
    assert resp.json() == {"adopted_id": "p1", "enabled": False}
    assert library.disabled_ids() == ["p1"]
    resp = client.post(BASE + "/adopted/p1/toggle", json={"enabled": True})
    assert resp.json()["enabled"] is True
    assert client.post(BASE + "/adopted/nope/toggle", json={"enabled": False}).status_code == 404


def test_switch_endpoint_persists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    client = _client(tmp_path, monkeypatch, enabled=False)
    resp = client.post(BASE + "/switch", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True
    config_text = (tmp_path / "share" / "config.toml").read_text()
    assert "default_self_evolve = true" in config_text


def test_path_segment_ids_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`%2e%2e` decodes into the path param; it must not reach the library paths."""
    client = _client(tmp_path, monkeypatch)
    _make_proposal(tmp_path / "share", "p1")

    # Percent-encoded so the HTTP client's dot-segment normalisation cannot
    # rewrite the URL before it reaches the router.
    for bad in ("%2e%2e", "%2E%2E", "%2e"):
        assert client.get(f"{BASE}/proposals/{bad}/evidence").status_code == 400
        assert client.post(f"{BASE}/proposals/{bad}/verify", json={"limit": 3}).status_code == 400
        assert client.post(f"{BASE}/proposals/{bad}/adopt").status_code == 400
        assert (
            client.post(f"{BASE}/adopted/{bad}/toggle", json={"enabled": True}).status_code == 400
        )

    # An encoded slash decodes before routing, so it stops matching the router
    # template entirely — it can never be a single path segment.
    assert client.get(f"{BASE}/proposals/a%2Fb/evidence").status_code == 404
    assert client.post(f"{BASE}/proposals/a%2Fb/verify", json={"limit": 3}).status_code == 404

    # The traversal attempt must leave the library intact and start no job.
    share = tmp_path / "share" / "evolution"
    assert (share / "proposals" / "p1").is_dir()
    assert admin_evolution._job is None


def test_validate_id_accepts_hex_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`library.new_id()` is 12 hex chars and may start with a digit."""
    client = _client(tmp_path, monkeypatch)
    for good in ("3f9a1b2c4d5e", "abcdef123456", "p1", "prop-20260101_x"):
        resp = client.get(f"{BASE}/proposals/{good}/evidence")
        assert resp.status_code == 404, (good, resp.status_code)
