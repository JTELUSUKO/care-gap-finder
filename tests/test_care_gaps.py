"""Pytest evaluation suite for the Care-Gap Finder Agent.

Each test proves one thing about the agent, the way a regression suite
proves things about an app:

  * the control patient (P005) has ZERO gaps — the rules do not cry wolf;
  * each of P001-P004 has EXACTLY its one designed gap — no invented gaps
    (grounding), no missed gaps;
  * every gap's evidence points at a resource that really exists in that
    patient's bundle — no made-up references;
  * the human-in-the-loop gate holds: an alert starts in pending/ and
    reaches sent/ ONLY through an explicit approve, and a rejected alert
    never reaches sent/;
  * no cross-patient leakage: one patient's alert contains nothing from
    another patient's record.

All alert outputs go to pytest's tmp_path, so running the suite never
pollutes the project's own alerts/ folders.

Run from the project root:
    pytest tests/ -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make the project root importable no matter where pytest is invoked from.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from care_gap import alerts, fhir_loader, gap_rules  # noqa: E402
from care_gap.agent import run_agent  # noqa: E402

PATIENTS_DIR = ROOT / "data" / "patients"

# patient file -> the ONE gap type it is designed to produce
EXPECTED_GAPS = {
    "p001.json": ["blood_pressure"],
    "p002.json": ["hba1c"],
    "p003.json": ["open_referral"],
    "p004.json": ["colorectal_screening"],
    "p005.json": [],
}

PATIENT_NAMES = {
    "p001.json": "Maria Santos",
    "p002.json": "James Okafor",
    "p003.json": "Elena Ruiz",
    "p004.json": "David Chen",
    "p005.json": "Aisha Patel",
}

PATIENT_MRNS = {
    "p001.json": "MRN-SYN-001",
    "p002.json": "MRN-SYN-002",
    "p003.json": "MRN-SYN-003",
    "p004.json": "MRN-SYN-004",
    "p005.json": "MRN-SYN-005",
}


def _load(filename):
    return fhir_loader.load_bundle(PATIENTS_DIR / filename)


@pytest.mark.parametrize("filename", sorted(EXPECTED_GAPS))
def test_each_patient_has_exactly_its_designed_gaps(filename):
    """Grounding: the rules find the designed gap — and nothing else."""
    bundle = _load(filename)
    gaps = gap_rules.detect_gaps(bundle)
    found = sorted(g["type"] for g in gaps)
    assert found == sorted(EXPECTED_GAPS[filename]), (
        f"{filename}: expected {EXPECTED_GAPS[filename]}, got {found}"
    )


def test_control_patient_has_no_gaps():
    """P005 is the control: a healthy record must produce zero gaps."""
    bundle = _load("p005.json")
    assert gap_rules.detect_gaps(bundle) == []


def test_p001_completed_referral_does_not_fire():
    """P001's dermatology referral is completed — only the BP gap fires."""
    bundle = _load("p001.json")
    gaps = gap_rules.detect_gaps(bundle)
    assert len(gaps) == 1
    assert gaps[0]["type"] == "blood_pressure"
    assert gaps[0]["severity"] == "soon"


@pytest.mark.parametrize("filename", sorted(EXPECTED_GAPS))
def test_gap_evidence_references_real_resources(filename):
    """Every gap's evidence must point at resources in that bundle."""
    bundle = _load(filename)
    real_ids = fhir_loader.resource_ids(bundle)
    for gap in gap_rules.detect_gaps(bundle):
        assert gap["evidence"], f"{filename}: gap {gap['id']} has no evidence"
        for ev in gap["evidence"]:
            ref = f"{ev['resourceType']}/{ev['id']}"
            assert ref in real_ids, (
                f"{filename}: gap {gap['id']} evidence points at {ref}, "
                "which does not exist in the bundle"
            )
            assert ev["detail"], f"{filename}: empty evidence detail"


def test_no_gaps_means_no_alert(tmp_path):
    """The control patient produces no alert file anywhere."""
    alerts_dir = tmp_path / "alerts"
    result = run_agent(PATIENTS_DIR / "p005.json", alerts_dir=alerts_dir)
    assert result["gaps"] == []
    assert result["alert"] is None
    assert list((alerts_dir / "pending").glob("*.json")) == []


def test_hitl_gate_approve_and_reject(tmp_path):
    """An alert starts pending; approve -> sent, reject -> rejected.

    Nothing reaches sent/ without an explicit approve call.
    """
    alerts_dir = tmp_path / "alerts"

    # Run P001: the alert must exist ONLY in pending.
    result = run_agent(PATIENTS_DIR / "p001.json", alerts_dir=alerts_dir)
    alert_id = result["alert"]["alert_id"]
    assert (alerts_dir / "pending" / f"{alert_id}.json").exists()
    assert not (alerts_dir / "sent" / f"{alert_id}.json").exists()

    # Approve it: it moves to sent/ and the audit log records it.
    alerts.approve(alerts_dir, alert_id)
    assert (alerts_dir / "sent" / f"{alert_id}.json").exists()
    assert not (alerts_dir / "pending" / f"{alert_id}.json").exists()
    audit = (alerts_dir / "audit.log").read_text(encoding="utf-8")
    assert "APPROVED" in audit and alert_id in audit

    # Run P002 and reject it: it must land in rejected/, never sent/.
    result2 = run_agent(PATIENTS_DIR / "p002.json", alerts_dir=alerts_dir)
    alert2_id = result2["alert"]["alert_id"]
    alerts.reject(alerts_dir, alert2_id, "HbA1c already scheduled")
    assert (alerts_dir / "rejected" / f"{alert2_id}.json").exists()
    assert not (alerts_dir / "sent" / f"{alert2_id}.json").exists()
    audit = (alerts_dir / "audit.log").read_text(encoding="utf-8")
    assert "REJECTED" in audit and alert2_id in audit
    assert "HbA1c already scheduled" in audit


def test_approve_twice_is_an_error(tmp_path):
    """Approving an already-sent alert must fail loudly, not silently."""
    alerts_dir = tmp_path / "alerts"
    result = run_agent(PATIENTS_DIR / "p001.json", alerts_dir=alerts_dir)
    alert_id = result["alert"]["alert_id"]
    alerts.approve(alerts_dir, alert_id)
    with pytest.raises(ValueError, match="already sent"):
        alerts.approve(alerts_dir, alert_id)


@pytest.mark.parametrize("filename", ["p001.json", "p002.json", "p003.json", "p004.json"])
def test_no_cross_patient_leakage(tmp_path, filename):
    """One patient's alert contains nothing from any other patient."""
    alerts_dir = tmp_path / "alerts"
    result = run_agent(PATIENTS_DIR / filename, alerts_dir=alerts_dir)
    alert_text = json.dumps(result["alert"])
    for other_file, name in PATIENT_NAMES.items():
        if other_file == filename:
            continue
        assert name not in alert_text, (
            f"{filename} alert leaks {name}'s name"
        )
        assert PATIENT_MRNS[other_file] not in alert_text, (
            f"{filename} alert leaks {PATIENT_MRNS[other_file]}"
        )
    # And it DOES name its own patient.
    assert PATIENT_NAMES[filename] in alert_text
    assert PATIENT_MRNS[filename] in alert_text


def test_explanations_are_plain_language(tmp_path):
    """Every gap gets a non-empty explanation naming the patient."""
    alerts_dir = tmp_path / "alerts"
    result = run_agent(PATIENTS_DIR / "p001.json", alerts_dir=alerts_dir)
    for gap in result["gaps"]:
        explanation = gap.get("explanation", "")
        assert len(explanation) > 20, "explanation is empty or trivial"
        assert "Maria Santos" in explanation
