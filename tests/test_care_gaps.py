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
from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest

# Make the project root importable no matter where pytest is invoked from.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from care_gap import agent, alerts, fhir_loader, gap_rules, llm  # noqa: E402
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


def _gap_types(bundle, as_of=gap_rules.DEFAULT_AS_OF):
    return [gap["type"] for gap in gap_rules.detect_gaps(bundle, as_of=as_of)]


def _add_resource(bundle, resource):
    bundle["entry"].append({"resource": resource})


def _colonoscopy(date_value, status="completed", code="73761001"):
    procedure = {
        "resourceType": "Procedure",
        "id": "test-colonoscopy",
        "status": status,
        "code": {
            "coding": [
                {"system": "http://snomed.info/sct", "code": code}
            ]
        },
    }
    if date_value is not None:
        procedure["performedDateTime"] = date_value
    return procedure


def _fit_observation(date_value, status="final"):
    observation = {
        "resourceType": "Observation",
        "id": "test-fit",
        "status": status,
        "code": {
            "coding": [{"system": "http://loinc.org", "code": "2335-8"}]
        },
    }
    if date_value is not None:
        observation["effectiveDateTime"] = date_value
    return observation


def _referral_followup(resource_type, status, event_date):
    followup = {
        "resourceType": resource_type,
        "id": "test-referral-followup",
        "status": status,
        "basedOn": [{"reference": "ServiceRequest/ref-p003-cardio"}],
    }
    if resource_type == "Encounter":
        followup["period"] = {}
        if event_date is not None:
            followup["period"]["start"] = event_date
    elif event_date is not None:
        followup["performedDateTime"] = event_date
    return followup


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


@pytest.mark.parametrize(
    ("filename", "gap_type"),
    [
        ("p001.json", "blood_pressure"),
        ("p002.json", "hba1c"),
        ("p003.json", "open_referral"),
        ("p004.json", "colorectal_screening"),
    ],
)
def test_each_gap_is_explained_by_its_specialist(tmp_path, filename, gap_type):
    result = run_agent(PATIENTS_DIR / filename, alerts_dir=tmp_path)

    assert len(result["gaps"]) == 1
    assert result["gaps"][0]["type"] == gap_type
    assert result["gaps"][0]["explanation_agent"] == f"{gap_type}_agent"


def test_parallel_specialists_coordinate_all_findings(monkeypatch, tmp_path):
    gap_types = [
        "blood_pressure",
        "hba1c",
        "colorectal_screening",
        "open_referral",
    ]
    gaps = [
        {
            "id": f"test-gap-{gap_type}",
            "type": gap_type,
            "title": f"Test {gap_type}",
            "severity": "routine",
            "detail": f"Test detail for {gap_type}.",
            "evidence": [
                {
                    "resourceType": "Patient",
                    "id": "patient-p005",
                    "detail": "Synthetic test evidence.",
                }
            ],
        }
        for gap_type in gap_types
    ]
    monkeypatch.setattr(agent.gap_rules, "detect_gaps", lambda bundle, as_of: gaps)
    monkeypatch.setattr(
        agent,
        "explain_gap",
        lambda gap, patient_name, mode: f"{gap['type']} explanation",
    )

    result = run_agent(PATIENTS_DIR / "p005.json", alerts_dir=tmp_path)

    assert [gap["type"] for gap in result["gaps"]] == gap_types
    assert [gap["explanation_agent"] for gap in result["gaps"]] == [
        f"{gap_type}_agent" for gap_type in gap_types
    ]
    assert len(result["agent_outputs"]) == len(gap_types)


def test_coordinator_rejects_output_for_an_unknown_gap():
    gap = gap_rules.detect_gaps(_load("p001.json"))[0]

    with pytest.raises(ValueError, match="unknown gap ID"):
        agent._node_coordinate_explanations(
            {"gaps": [gap], "agent_outputs": [{"gap_id": "not-a-real-gap"}]}
        )


def test_verifier_rejects_specialist_evidence_mismatch():
    gap = gap_rules.detect_gaps(_load("p001.json"))[0]
    coordinated = {
        "gap_id": gap["id"],
        "gap_type": gap["type"],
        "agent": "blood_pressure_agent",
        "evidence_refs": ["Observation/not-in-the-gap"],
        "explanation": "A test explanation.",
    }

    with pytest.raises(ValueError, match="evidence mismatch"):
        agent._node_verify_explanations(
            {"gaps": [gap], "coordinated_explanations": [coordinated]}
        )


@pytest.mark.parametrize(
    ("gap_type", "role"),
    [
        ("blood_pressure", "blood-pressure care-gap specialist"),
        ("hba1c", "diabetes and HbA1c care-gap specialist"),
        ("colorectal_screening", "colorectal screening care-gap specialist"),
        ("open_referral", "referral follow-up care-gap specialist"),
    ],
)
def test_llm_prompt_uses_gap_specialist_and_only_attached_evidence(
    monkeypatch, gap_type, role
):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return llm.subprocess.CompletedProcess(command, 0, stdout="A grounded note.")

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    gap = {
        "type": gap_type,
        "title": f"Test {gap_type}",
        "severity": "routine",
        "detail": "A test rule finding.",
        "evidence": [
            {
                "resourceType": "Patient",
                "id": "patient-test",
                "detail": "The attached evidence.",
            }
        ],
    }

    assert llm.explain_gap(gap, "Synthetic Patient", mode="claude") == (
        "A grounded note."
    )
    prompt = captured["command"][2]
    assert role in prompt
    assert "patient-test" in prompt
    assert "Do not infer a diagnosis" in prompt


def test_p001_completed_referral_does_not_fire():
    """P001's dermatology referral is completed — only the BP gap fires."""
    bundle = _load("p001.json")
    gaps = gap_rules.detect_gaps(bundle)
    assert len(gaps) == 1
    assert gaps[0]["type"] == "blood_pressure"
    assert gaps[0]["severity"] == "soon"


@pytest.mark.parametrize(
    ("screening_date", "expected_gap"),
    [
        ("2016-11-12", False),
        ("2016-11-11", True),
    ],
)
def test_colonoscopy_uses_inclusive_ten_year_lookback(screening_date, expected_gap):
    bundle = deepcopy(_load("p004.json"))
    _add_resource(bundle, _colonoscopy(screening_date))

    assert ("colorectal_screening" in _gap_types(bundle)) is expected_gap


@pytest.mark.parametrize(
    ("screening_date", "expected_gap"),
    [
        ("2025-11-12", False),
        ("2025-11-11", True),
    ],
)
def test_fit_uses_inclusive_one_year_lookback(screening_date, expected_gap):
    bundle = deepcopy(_load("p004.json"))
    _add_resource(bundle, _fit_observation(screening_date))

    assert ("colorectal_screening" in _gap_types(bundle)) is expected_gap


@pytest.mark.parametrize("screening_date", [None, "not-a-date", "2026-11-13"])
def test_colonoscopy_without_valid_nonfuture_date_does_not_suppress_gap(
    screening_date,
):
    bundle = deepcopy(_load("p004.json"))
    _add_resource(bundle, _colonoscopy(screening_date))

    assert "colorectal_screening" in _gap_types(bundle)


def test_unknown_or_incomplete_screening_does_not_suppress_gap():
    bundle = deepcopy(_load("p004.json"))
    _add_resource(bundle, _colonoscopy("2026-10-01", code="unknown-code"))
    _add_resource(bundle, _colonoscopy("2026-10-01", status="cancelled"))
    _add_resource(bundle, _fit_observation(None))

    assert "colorectal_screening" in _gap_types(bundle)


@pytest.mark.parametrize(
    ("status", "screening_date"),
    [
        ("preliminary", "2026-11-01"),
        ("entered-in-error", "2026-11-01"),
        ("final", "not-a-date"),
        ("final", "2026-11-13"),
    ],
)
def test_fit_requires_final_result_with_valid_nonfuture_date(status, screening_date):
    bundle = deepcopy(_load("p004.json"))
    _add_resource(bundle, _fit_observation(screening_date, status=status))

    assert "colorectal_screening" in _gap_types(bundle)


@pytest.mark.parametrize(
    ("status", "screening_date", "expected_gap"),
    [
        ("finished", "2025-11-12", False),
        ("finished", "2025-11-11", True),
        ("planned", "2026-11-01", True),
        ("finished", "2026-11-13", True),
    ],
)
def test_screening_encounter_requires_finished_recent_date(
    status, screening_date, expected_gap
):
    bundle = deepcopy(_load("p004.json"))
    _add_resource(
        bundle,
        {
            "resourceType": "Encounter",
            "id": "test-screening-encounter",
            "status": status,
            "period": {"start": screening_date},
            "reasonCode": [
                {
                    "coding": [
                        {
                            "system": "http://hl7.org/fhir/sid/icd-10-cm",
                            "code": "Z12.11",
                        }
                    ]
                }
            ],
        },
    )

    assert ("colorectal_screening" in _gap_types(bundle)) is expected_gap


def test_valid_recent_screening_suppresses_gap_even_with_old_screening():
    bundle = deepcopy(_load("p004.json"))
    _add_resource(bundle, _colonoscopy("2000-01-01"))
    _add_resource(bundle, _fit_observation("2026-01-12"))

    assert "colorectal_screening" not in _gap_types(bundle)


@pytest.mark.parametrize(
    ("resource_type", "status", "event_date", "expected_gap"),
    [
        ("Encounter", "finished", "2026-11-12", False),
        ("Encounter", "cancelled", "2026-11-12", True),
        ("Encounter", "entered-in-error", "2026-11-12", True),
        ("Encounter", "planned", "2026-11-12", True),
        ("Encounter", "finished", "2026-11-13", True),
        ("Encounter", "finished", None, True),
        ("Procedure", "completed", "2026-11-12", False),
        ("Procedure", "cancelled", "2026-11-12", True),
        ("Procedure", "entered-in-error", "2026-11-12", True),
        ("Procedure", "completed", "2026-11-13", True),
        ("Procedure", "completed", None, True),
    ],
)
def test_referral_requires_completed_dated_nonfuture_followup(
    resource_type, status, event_date, expected_gap
):
    bundle = deepcopy(_load("p003.json"))
    _add_resource(
        bundle, _referral_followup(resource_type, status, event_date)
    )

    assert ("open_referral" in _gap_types(bundle)) is expected_gap


@pytest.mark.parametrize(
    ("status", "effective_date"),
    [
        ("cancelled", "2026-11-01"),
        ("entered-in-error", "2026-11-01"),
        ("preliminary", "2026-11-01"),
        ("final", None),
        ("final", "not-a-date"),
        ("final", "2026-11-13"),
    ],
)
def test_invalid_bp_observation_does_not_suppress_gap(status, effective_date):
    bundle = deepcopy(_load("p001.json"))
    observation = {
        "resourceType": "Observation",
        "id": "test-bp",
        "status": status,
        "code": {
            "coding": [{"system": "http://loinc.org", "code": "8480-6"}]
        },
    }
    if effective_date is not None:
        observation["effectiveDateTime"] = effective_date
    _add_resource(bundle, observation)

    assert "blood_pressure" in _gap_types(bundle)


@pytest.mark.parametrize("status", ["final", "amended", "corrected"])
def test_finalized_recent_bp_observation_suppresses_gap(status):
    bundle = deepcopy(_load("p001.json"))
    _add_resource(
        bundle,
        {
            "resourceType": "Observation",
            "id": "test-bp",
            "status": status,
            "effectiveDateTime": "2026-11-01",
            "code": {
                "coding": [{"system": "http://loinc.org", "code": "8480-6"}]
            },
        },
    )

    assert "blood_pressure" not in _gap_types(bundle)


def test_hba1c_uses_valid_results_and_ignores_newer_invalid_observation():
    bundle = deepcopy(_load("p002.json"))
    invalid = {
        "resourceType": "Observation",
        "id": "test-invalid-hba1c",
        "status": "entered-in-error",
        "effectiveDateTime": "2026-11-01",
        "code": {
            "coding": [{"system": "http://loinc.org", "code": "4548-4"}]
        },
    }
    _add_resource(bundle, invalid)

    gaps = gap_rules.detect_gaps(bundle)
    hba1c_gap = next(gap for gap in gaps if gap["type"] == "hba1c")
    assert "2026-03-02" in hba1c_gap["detail"]
    assert all(ev["id"] != "test-invalid-hba1c" for ev in hba1c_gap["evidence"])


@pytest.mark.parametrize(
    ("status", "effective_date"),
    [
        ("preliminary", "2026-11-01"),
        ("cancelled", "2026-11-01"),
        ("final", None),
        ("final", "not-a-date"),
        ("final", "2026-11-13"),
    ],
)
def test_invalid_hba1c_observation_does_not_suppress_gap(status, effective_date):
    bundle = deepcopy(_load("p002.json"))
    observation = {
        "resourceType": "Observation",
        "id": "test-invalid-hba1c",
        "status": status,
        "code": {
            "coding": [{"system": "http://loinc.org", "code": "4548-4"}]
        },
    }
    if effective_date is not None:
        observation["effectiveDateTime"] = effective_date
    _add_resource(bundle, observation)

    assert "hba1c" in _gap_types(bundle)


@pytest.mark.parametrize("status", ["final", "amended", "corrected"])
def test_finalized_recent_hba1c_suppresses_gap(status):
    bundle = deepcopy(_load("p002.json"))
    _add_resource(
        bundle,
        {
            "resourceType": "Observation",
            "id": "test-hba1c",
            "status": status,
            "effectiveDateTime": "2026-11-01",
            "code": {
                "coding": [{"system": "http://loinc.org", "code": "4548-4"}]
            },
        },
    )

    assert "hba1c" not in _gap_types(bundle)


@pytest.mark.parametrize(
    "birth_date",
    [None, "not-a-date", "1974", "1974-09"],
)
def test_unknown_or_partial_birth_date_preserves_other_gaps(birth_date):
    bundle = deepcopy(_load("p001.json"))
    patient = fhir_loader.get_patient(bundle)
    if birth_date is None:
        patient.pop("birthDate", None)
    else:
        patient["birthDate"] = birth_date

    assert fhir_loader.patient_age(patient, gap_rules.DEFAULT_AS_OF) is None
    assert _gap_types(bundle) == ["blood_pressure"]


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
