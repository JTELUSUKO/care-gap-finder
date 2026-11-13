"""Bundle-integrity tests for the Care-Gap Finder Agent.

The reconciliation layer — the in-project version of the iCEDQ-style
source-to-target check. These tests prove the data going INTO the agent
is complete and well-formed:

  * all five patient bundles exist and load as valid FHIR Bundles;
  * each bundle holds exactly ONE Patient — no patient dropped, none
    doubled up in a single file;
  * the patient-ID set is exactly the five expected IDs — a dropped or
    renamed patient fails the set comparison;
  * no patient ID appears in more than one bundle.

If a bundle is deleted, corrupted, or duplicated, these tests fail
before any gap rule ever runs — the same way a reconciliation rule in
a real pipeline fails the load before downstream reporting.

Run from the project root:
    pytest tests/ -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the project root importable no matter where pytest is invoked from.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from care_gap import fhir_loader  # noqa: E402

PATIENTS_DIR = ROOT / "data" / "patients"

EXPECTED_FILES = ["p001.json", "p002.json", "p003.json", "p004.json", "p005.json"]

EXPECTED_PATIENT_IDS = {
    "patient-p001",
    "patient-p002",
    "patient-p003",
    "patient-p004",
    "patient-p005",
}


def _load(filename):
    return fhir_loader.load_bundle(PATIENTS_DIR / filename)


def test_all_five_bundles_exist_and_load():
    """Reconciliation count: 5 bundle files, all valid FHIR Bundles."""
    found = sorted(p.name for p in PATIENTS_DIR.glob("*.json"))
    assert found == sorted(EXPECTED_FILES), (
        f"expected bundles {sorted(EXPECTED_FILES)}, found {found}"
    )
    for filename in EXPECTED_FILES:
        bundle = _load(filename)  # raises if not a Bundle / no Patient
        assert bundle["resourceType"] == "Bundle"


@pytest.mark.parametrize("filename", EXPECTED_FILES)
def test_each_bundle_has_exactly_one_patient(filename):
    """One Patient per bundle — no patient dropped, none doubled up."""
    bundle = _load(filename)
    patients = fhir_loader.resources_of_type(bundle, "Patient")
    assert len(patients) == 1, (
        f"{filename}: expected 1 Patient resource, found {len(patients)}"
    )


def test_patient_id_set_is_complete():
    """The five patient IDs are exactly the expected set — none missing."""
    ids = set()
    for filename in EXPECTED_FILES:
        patient = fhir_loader.get_patient(_load(filename))
        ids.add(patient["id"])
    assert ids == EXPECTED_PATIENT_IDS, (
        f"expected patient IDs {sorted(EXPECTED_PATIENT_IDS)}, "
        f"got {sorted(ids)}"
    )


def test_no_duplicate_patient_ids():
    """No patient ID appears in more than one bundle."""
    ids = []
    for filename in EXPECTED_FILES:
        patient = fhir_loader.get_patient(_load(filename))
        ids.append(patient["id"])
    assert len(ids) == len(set(ids)), f"duplicate patient IDs found: {ids}"
