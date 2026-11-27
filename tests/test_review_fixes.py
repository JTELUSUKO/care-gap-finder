"""One group of tests per finding from the November 24, 2026 code review.

   1  Cancel on the reject prompt decides nothing
   2  an alert can only ever be decided once
   3  Claude runs without tools, MCP servers or other customizations
   4  patients whose FHIR id contains a dot still get alerts
   5  one damaged file does not hide the rest of the queue
   6  the page never keeps showing an outdated status
   7  audit-log fields cannot be forged
   8  running a patient twice does not create duplicate alerts
   9  id checks refuse a trailing newline
  10  the API returns only what the page shows
  11  fixes from the November 26, 2026 review: file errors, drafts that
      change after a reviewer saw them, and the last-approved date

Everything writes to pytest's tmp_path, never to the project's alerts/.
The page tests (1 and 6) need Node.js and are skipped without it, except
on CI, where a missing Node.js is a failure.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app as cli  # noqa: E402
from care_gap import alerts, gap_rules, llm  # noqa: E402
from care_gap.agent import run_agent  # noqa: E402

PATIENTS_DIR = ROOT / "data" / "patients"
UI_HEADERS = {"X-Requested-With": "care-gap-finder"}


def _run(alerts_dir, patient="p002"):
    path = patient if isinstance(patient, Path) else PATIENTS_DIR / f"{patient}.json"
    return run_agent(path, alerts_dir=alerts_dir, as_of=gap_rules.DEFAULT_AS_OF)


def _bundle_with_patient_id(path, new_id):
    """Save a copy of P002's bundle whose Patient has another id."""
    bundle = json.loads((PATIENTS_DIR / "p002.json").read_text())
    for entry in bundle["entry"]:
        if entry["resource"]["resourceType"] == "Patient":
            entry["resource"]["id"] = new_id
    path.write_text(json.dumps(bundle))
    return path


def _files(alerts_dir, queue):
    return sorted(p.name for p in (alerts_dir / queue).glob("*.json"))


def _audit_lines(alerts_dir):
    log = alerts_dir / "audit.log"
    return log.read_text().splitlines() if log.exists() else []


@pytest.fixture
def alerts_dir(tmp_path):
    return tmp_path / "alerts"


@pytest.fixture
def client(alerts_dir, monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    import server

    monkeypatch.setattr(server, "ALERTS_DIR", alerts_dir)
    return TestClient(server.app, base_url="http://127.0.0.1",
                      raise_server_exceptions=False)


def _page(scenario):
    """Play one scenario on the real page script; returns what happened."""
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("Node.js is required to test the page on CI")
        pytest.skip("Node.js is not installed")
    done = subprocess.run(
        [node, str(ROOT / "tests" / "ui_harness.js"),
         str(ROOT / "static" / "index.html"), scenario],
        capture_output=True, text=True, timeout=30,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


# --- 1. Cancel decides nothing -------------------------------------------

def test_cancel_on_reject_sends_nothing():
    assert _page("cancel")["after"]["decisions"] == []


def test_reject_with_a_blank_reason_still_rejects():
    (sent,) = _page("blank-reason")["after"]["decisions"]
    assert sent["url"] == "/api/alerts/ALERT-1/reject"


# --- 2. Decided exactly once ---------------------------------------------

def test_simultaneous_decisions_take_effect_once(tmp_path):
    for trial in range(30):
        alerts_dir = tmp_path / str(trial)
        alert_id = _run(alerts_dir)["alert"]["alert_id"]
        barrier = threading.Barrier(2)
        outcomes = []

        def decide(action, *args):
            barrier.wait()
            try:
                action(alerts_dir, alert_id, *args)
                outcomes.append("ok")
            except ValueError:
                outcomes.append("refused")
            except Exception as exc:  # noqa: BLE001 - reported by the assert
                outcomes.append(repr(exc))

        threads = [
            threading.Thread(target=decide, args=(alerts.approve,)),
            threading.Thread(target=decide, args=(alerts.reject, "no")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(outcomes) == ["ok", "refused"]
        assert len(_files(alerts_dir, "sent") + _files(alerts_dir, "rejected")) == 1
        assert _files(alerts_dir, "pending") == []
        assert len(_audit_lines(alerts_dir)) == 1


def test_no_decision_without_an_audit_line(alerts_dir, monkeypatch):
    alert_id = _run(alerts_dir)["alert"]["alert_id"]

    def disk_full(*args):
        raise OSError("disk full")

    monkeypatch.setattr(alerts, "_audit", disk_full)
    with pytest.raises(OSError):
        alerts.approve(alerts_dir, alert_id)
    assert _files(alerts_dir, "sent") == []
    assert _files(alerts_dir, "pending") == [f"{alert_id}.json"]


# --- 3. Claude has nothing to act with -----------------------------------

def test_claude_starts_without_tools_mcp_or_customizations(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="A note.", stderr="")

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    gap = {"type": "hba1c", "title": "t", "severity": "soon", "detail": "d"}
    llm.explain_gap(gap, "Test Patient", mode="claude")

    flags = calls[0][3:]
    assert flags[:2] == ["--tools", ""]
    assert "--strict-mcp-config" in flags  # --tools alone keeps MCP tools
    assert "--safe-mode" in flags
    assert "--mcp-config" not in flags  # would add servers back
    assert "--bare" not in flags  # would break the normal sign-in


# --- 4. FHIR ids with dots -----------------------------------------------

def test_patient_id_with_a_dot_gets_an_alert(tmp_path, alerts_dir):
    path = _bundle_with_patient_id(tmp_path / "dot.json", "patient.p002")
    alert = _run(alerts_dir, path)["alert"]
    assert alert["patient_id"] == "patient.p002"  # the real id is kept
    assert _files(alerts_dir, "pending") == [f"{alert['alert_id']}.json"]
    alerts.approve(alerts_dir, alert["alert_id"])
    assert _files(alerts_dir, "sent") == [f"{alert['alert_id']}.json"]


@pytest.mark.parametrize("bad_id", [None, "", "a/b", "a b", "p1\n", "x" * 65, 7])
def test_invalid_patient_id_fails_before_explaining(
        tmp_path, alerts_dir, monkeypatch, bad_id):
    from care_gap import agent

    explained = []
    monkeypatch.setattr(agent, "explain_gap", lambda *a, **k: explained.append(a))
    path = _bundle_with_patient_id(tmp_path / "bad.json", bad_id)
    with pytest.raises(ValueError, match="Invalid patient id"):
        _run(alerts_dir, path)
    assert explained == []


def test_run_all_continues_past_a_bad_record(tmp_path, alerts_dir, monkeypatch, capsys):
    folder = tmp_path / "patients"
    folder.mkdir()
    _bundle_with_patient_id(folder / "p001.json", "../bad")
    shutil.copy(PATIENTS_DIR / "p002.json", folder / "p002.json")
    monkeypatch.setattr(cli, "PATIENTS_DIR", folder)
    monkeypatch.setattr(cli, "ALERTS_DIR", alerts_dir)
    monkeypatch.delenv("CARE_GAP_LLM", raising=False)

    assert cli.main(["--all"]) == 1  # the failure is still reported
    assert "Invalid patient id" in capsys.readouterr().out
    assert len(_files(alerts_dir, "pending")) == 1


# --- 5. One damaged file ---------------------------------------------------

@pytest.mark.parametrize("content", ["{", "[]", '{"alert_id": "other"}',
                                     '{"alert_id": "BROKEN", "gaps": [1]}'])
def test_damaged_file_does_not_hide_the_queue(client, alerts_dir, content):
    alert_id = _run(alerts_dir)["alert"]["alert_id"]
    (alerts_dir / "pending" / "BROKEN.json").write_text(content)

    res = client.get("/api/alerts/pending")
    assert res.status_code == 200
    assert [a["alert_id"] for a in res.json()["pending"]] == [alert_id]
    assert res.json()["unreadable"] == ["BROKEN.json"]
    res = client.post("/api/alerts/BROKEN/approve", headers=UI_HEADERS)
    assert res.status_code == 400
    assert "BROKEN.json" in res.json()["detail"]


def test_readers_never_see_a_half_written_alert(alerts_dir, monkeypatch):
    alert = _run(alerts_dir)["alert"]

    def crash_midway(data, f, **kwargs):
        f.write('{"alert_id": ')
        raise RuntimeError("crashed while writing")

    monkeypatch.setattr(alerts.json, "dump", crash_midway)
    with pytest.raises(RuntimeError):
        alerts.write_pending_alert(alerts_dir, alert)
    monkeypatch.undo()
    assert alerts.list_pending(alerts_dir) == [alert]
    assert len(list((alerts_dir / "pending").iterdir())) == 1  # no leftovers


# --- 6. The page stays up to date ----------------------------------------

def test_double_click_sends_one_decision_and_disables_buttons():
    out = _page("approve-from-card")
    during = out["during"]
    assert len(during["decisions"]) == 1
    for area in ("draft", "pending"):
        assert during[area].count("<button") == 2
        assert during[area].count(" disabled>") == 2


def test_card_shows_the_decision_and_loses_its_buttons():
    after = _page("approve-from-card")["after"]
    assert "approved" in after["draft"]
    assert "not sent" not in after["draft"]
    assert "<button" not in after["draft"]
    assert "ALERT-1" not in after["pending"]
    assert after["statusClass"] == "ok"


def test_failed_decision_refreshes_the_list_and_card():
    after = _page("decided-elsewhere")["after"]
    assert "already sent" in after["status"]
    assert "ALERT-1" not in after["pending"]
    assert "No longer pending" in after["draft"]
    assert "<button" not in after["draft"]


def test_server_crash_shows_a_plain_message_and_keeps_buttons_usable():
    after = _page("server-crash")["after"]
    assert "status 500" in after["status"]
    assert after["pending"].count("<button") == 2
    assert " disabled>" not in after["pending"]


# --- 7. Audit fields -------------------------------------------------------

def test_pipes_cannot_forge_audit_fields(alerts_dir):
    alert_id = _run(alerts_dir)["alert"]["alert_id"]
    reason = "x | APPROVED | ALERT-q | p9 | -"
    alerts.reject(alerts_dir, alert_id, reason)

    (line,) = _audit_lines(alerts_dir)
    assert len(line.split("|")) == 5
    assert line.split(" | ")[1:4] == ["REJECTED", alert_id, "patient-p002"]
    assert "| APPROVED" not in line
    # The rejected alert keeps the reason exactly as typed.
    saved = json.loads((alerts_dir / "rejected" / f"{alert_id}.json").read_text())
    assert saved["decision_reason"] == reason


# --- 8. No duplicate alerts ------------------------------------------------

def test_running_twice_with_the_same_result_keeps_the_same_alert(alerts_dir):
    first = _run(alerts_dir)["alert"]
    second = _run(alerts_dir)["alert"]

    assert second == first
    (stored,) = alerts.list_pending(alerts_dir)
    assert stored == first


def test_a_changed_draft_gets_a_new_id_and_replaces_the_old_one(
        alerts_dir, monkeypatch):
    from care_gap import agent

    first = _run(alerts_dir)["alert"]
    monkeypatch.setattr(agent, "explain_gap", lambda *a, **k: "A newer note.")
    second = _run(alerts_dir)["alert"]

    assert second["alert_id"] != first["alert_id"]
    (stored,) = alerts.list_pending(alerts_dir)
    assert stored == second  # what the run showed is what is stored
    assert stored["gaps"][0]["explanation"] == "A newer note."
    (line,) = _audit_lines(alerts_dir)
    assert line.split(" | ")[1:3] == ["REPLACED", first["alert_id"]]
    assert second["alert_id"] in line


def test_different_patients_or_gaps_stay_separate(alerts_dir):
    alert = _run(alerts_dir, "p001")["alert"]
    _run(alerts_dir, "p002")
    other = {**alert, "alert_id": "ALERT-other",
             "gaps": alert["gaps"] + [{"id": "GAP-extra"}]}
    alerts.save_draft(alerts_dir, other)
    assert len(alerts.list_pending(alerts_dir)) == 3


def test_simultaneous_runs_create_one_alert(alerts_dir):
    alert = _run(alerts_dir)["alert"]
    (alerts_dir / "pending" / f"{alert['alert_id']}.json").unlink()
    barrier = threading.Barrier(8)

    def save(n):
        barrier.wait()
        alerts.save_draft(alerts_dir, {**alert, "alert_id": f"ALERT-{n}"})

    threads = [threading.Thread(target=save, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(alerts.list_pending(alerts_dir)) == 1


def test_reviewer_is_told_when_the_same_gaps_were_already_sent(
        client, alerts_dir, monkeypatch, capsys):
    first = client.post("/api/run/p002", headers=UI_HEADERS).json()["alert"]
    assert first["previously_sent"] is None
    client.post(f"/api/alerts/{first['alert_id']}/approve", headers=UI_HEADERS)

    again = client.post("/api/run/p002", headers=UI_HEADERS).json()["alert"]
    assert again["alert_id"] != first["alert_id"]
    assert again["previously_sent"]
    (listed,) = client.get("/api/alerts/pending").json()["pending"]
    assert listed["previously_sent"] == again["previously_sent"]

    monkeypatch.setattr(cli, "ALERTS_DIR", alerts_dir)
    cli.main(["--list-pending"])
    assert "already approved" in capsys.readouterr().out


# --- 9. Trailing newlines --------------------------------------------------

def test_alert_id_with_trailing_newline_is_refused(alerts_dir):
    with pytest.raises(ValueError, match="Invalid alert id"):
        alerts.write_pending_alert(alerts_dir, {"alert_id": "ALERT-1\n"})
    with pytest.raises(ValueError, match="Invalid alert id"):
        alerts.approve(alerts_dir, "ALERT-1\n")


@pytest.mark.parametrize("bad_id", ["p001\n", "p٠٠١", "p", "p001x"])
def test_patient_id_must_match_completely(bad_id):
    from fastapi import HTTPException

    import server

    assert server.PATIENT_ID_RE.fullmatch(bad_id) is None
    with pytest.raises(HTTPException):
        server._patient_path(bad_id)


def test_patient_path_check_itself_refuses_a_newline(tmp_path, monkeypatch):
    """The file exists, so only the id check can refuse it."""
    from fastapi import HTTPException

    import server

    (tmp_path / "p001\n.json").write_text("{}")
    monkeypatch.setattr(server, "PATIENTS_DIR", tmp_path)
    with pytest.raises(HTTPException):
        server._patient_path("p001\n")


# --- 10. Only what the page shows ------------------------------------------

def test_no_endpoint_returns_the_mrn_or_the_whole_alert(client):
    allowed = {"alert_id", "patient_id", "patient_name", "created_at", "gaps",
               "previously_sent"}
    run = client.post("/api/run/p002", headers=UI_HEADERS)
    listing = client.get("/api/alerts/pending")
    alert_id = run.json()["alert"]["alert_id"]
    approved = client.post(f"/api/alerts/{alert_id}/approve", headers=UI_HEADERS)
    client.post("/api/run/p003", headers=UI_HEADERS)
    other = client.get("/api/alerts/pending").json()["pending"][0]["alert_id"]
    rejected = client.post(f"/api/alerts/{other}/reject", headers=UI_HEADERS)

    views = [run.json()["alert"], listing.json()["pending"][0],
             approved.json()["approved"], rejected.json()["rejected"]]
    for view in views:
        assert set(view) == allowed
        assert all(set(g) == {"severity", "title"} for g in view["gaps"])
    for res in (run, listing, approved, rejected):
        assert "mrn" not in res.text.lower()


def test_alert_file_still_keeps_the_mrn(alerts_dir):
    alert = _run(alerts_dir)["alert"]
    saved = json.loads((alerts_dir / "pending" / f"{alert['alert_id']}.json").read_text())
    assert saved["mrn"] == "MRN-SYN-002"


def test_docs_match_the_code():
    import server

    routes = [r for r in server.app.routes
              if r.path == "/" or r.path.startswith("/api/")]
    ui_readme = (ROOT / "UI-README.md").read_text()
    assert len(routes) == 6 and "Six endpoints" in ui_readme
    assert "three new packages" in ui_readme
    assert "Nothing about the agent changed" not in (ROOT / "server.py").read_text()


# --- 11. Fixes from the November 26, 2026 code review -----------------------

def test_cli_reports_a_missing_patient_file_in_one_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "ALERTS_DIR", tmp_path / "alerts")
    assert cli._run_one(tmp_path / "nope.json", gap_rules.DEFAULT_AS_OF, "mock") is False
    assert capsys.readouterr().out.strip().startswith("Error:")


def test_reviewer_cannot_approve_a_draft_that_changed_after_they_saw_it(
        client, alerts_dir, monkeypatch):
    from care_gap import agent

    seen = client.post("/api/run/p002", headers=UI_HEADERS).json()["alert"]
    monkeypatch.setattr(agent, "explain_gap", lambda *a, **k: "Unreviewed text.")
    client.post("/api/run/p002", headers=UI_HEADERS)

    res = client.post(f"/api/alerts/{seen['alert_id']}/approve", headers=UI_HEADERS)
    assert res.status_code == 400
    assert _files(alerts_dir, "sent") == []


def _sent(alerts_dir, alert, alert_id, decided_at):
    alerts.ensure_dirs(alerts_dir)
    record = {**alert, "alert_id": alert_id, "status": "sent"}
    if decided_at is None:
        record.pop("decided_at", None)
    else:
        record["decided_at"] = decided_at
    (alerts_dir / "sent" / f"{alert_id}.json").write_text(json.dumps(record))


def test_old_sent_alert_without_a_date_does_not_hide_the_real_date(alerts_dir):
    alert = _run(alerts_dir)["alert"]
    _sent(alerts_dir, alert, "ALERT-legacy", None)
    _sent(alerts_dir, alert, "ALERT-recent", "2026-11-20T10:00:00")
    assert alerts.previously_sent(alerts_dir, alert) == "2026-11-20T10:00:00"


def test_sent_alert_without_any_date_still_counts(alerts_dir):
    alert = _run(alerts_dir)["alert"]
    _sent(alerts_dir, alert, "ALERT-legacy", None)
    assert alerts.previously_sent(alerts_dir, alert) == "an earlier date"


def test_approving_a_first_alert_does_not_report_itself(client):
    alert_id = client.post("/api/run/p002", headers=UI_HEADERS).json()["alert"]["alert_id"]
    res = client.post(f"/api/alerts/{alert_id}/approve", headers=UI_HEADERS)
    assert res.json()["approved"]["previously_sent"] is None


def test_unreadable_patient_file_gives_a_json_error(client, monkeypatch):
    def unreadable(path):
        raise PermissionError(13, "Permission denied", str(path))

    from care_gap import fhir_loader
    monkeypatch.setattr(fhir_loader, "load_bundle", unreadable)
    res = client.post("/api/run/p002", headers=UI_HEADERS)
    assert res.status_code == 500
    assert res.json()["detail"] == (
        "The server could not read or write p002.json: Permission denied.")
