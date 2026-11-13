"""Deterministic care-gap rules — the agent's "reason" core.

RULES FIND, the LLM only EXPLAINS. Every gap below is produced by a plain
Python check against the patient's FHIR bundle. The language model never
invents, adds, or removes a gap; it only turns a found gap into a
plain-language sentence for the care team. That separation is the grounding
discipline from the agent-evaluation harness: the deterministic layer is the
source of truth, and the LLM is a writer, not a decider.

Reference date: the demo evaluates "overdue" against DEFAULT_AS_OF
(2026-11-12, the day this project was built) so results are stable and the
test suite is deterministic. Pass a different `as_of` to `detect_gaps`, or
use `app.py --as-of YYYY-MM-DD`, to evaluate against another date.

The four rules (all use real standard codes — see data/patients/README.md):
  1. blood_pressure — no systolic BP Observation (LOINC 8480-6) in 12 months.
  2. hba1c — diabetes Condition present AND no HbA1c (LOINC 4548-4) in 6 months.
  3. colorectal_screening — age 45-75 AND no colorectal screening on record.
  4. open_referral — an active ServiceRequest with no follow-up Encounter
     or Procedure referencing it.

Severities: BP overdue 12 months = soon; HbA1c overdue = soon; open
referral = routine; screening overdue = routine.

Every gap carries `evidence`: a list of {resourceType, id, detail} pointing
at resources that really exist in the patient's bundle. The test suite
verifies that — a gap with evidence pointing nowhere is a bug.
"""
from __future__ import annotations

from datetime import date, timedelta

from . import fhir_loader

# The demo's reference date. Fixed on purpose: the synthetic bundles have
# fixed dates, so a moving "today" would change the answers over time and
# break the control patient (P005). Override with --as-of when running.
DEFAULT_AS_OF = date(2026, 11, 12)

LOINC_SYSTOLIC_BP = "8480-6"
LOINC_DIASTOLIC_BP = "8462-4"
LOINC_HBA1C = "4548-4"
LOINC_FOBT = "2335-8"  # fecal occult blood test (FIT/FOBT stool screening)
SNOMED_COLONOSCOPY = "73761001"
CPT_COLONOSCOPY = "45378"
ICD10_SCREENING_COLON = "Z12.11"  # encounter for screening for colon cancer

BP_WINDOW = timedelta(days=365)  # 12 months
HBA1C_WINDOW = timedelta(days=183)  # ~6 months
SCREENING_AGE_MIN = 45
SCREENING_AGE_MAX = 75


def detect_gaps(bundle, as_of=DEFAULT_AS_OF):
    """Run all four rules against one patient bundle.

    Returns a list of gap dicts (empty list = no gaps found). Each gap has:
    id, patient_id, type, title, severity, evidence, detail.
    """
    patient = fhir_loader.get_patient(bundle)
    patient_id = patient.get("id") if patient else "unknown"
    gaps = []
    gaps.extend(_bp_gap(bundle, patient_id, as_of))
    gaps.extend(_hba1c_gap(bundle, patient_id, as_of))
    gaps.extend(_screening_gap(bundle, patient_id, as_of))
    gaps.extend(_referral_gaps(bundle, patient_id))
    return gaps


def _patient_evidence(bundle, detail):
    patient = fhir_loader.get_patient(bundle)
    return [
        {
            "resourceType": "Patient",
            "id": patient.get("id"),
            "detail": detail,
        }
    ]


def _encounter_evidence(bundle):
    """Evidence entry for the most recent encounter, if one exists."""
    encounter = fhir_loader.latest_encounter(bundle)
    if encounter is None:
        return []
    start = (encounter.get("period") or {}).get("start", "date unknown")
    return [
        {
            "resourceType": "Encounter",
            "id": encounter.get("id"),
            "detail": f"Most recent visit on record: {start}.",
        }
    ]


def _bp_gap(bundle, patient_id, as_of):
    """Rule 1: no systolic BP reading (LOINC 8480-6) in the last 12 months."""
    systolic = fhir_loader.observations_by_loinc(bundle, LOINC_SYSTOLIC_BP)
    latest = fhir_loader.latest_observation_date(systolic)
    if latest is not None and as_of - latest <= BP_WINDOW:
        return []
    if latest is None:
        detail = "No blood pressure reading on record at all."
    else:
        detail = f"Last blood pressure reading was {latest.isoformat()}."
    return [
        {
            "id": f"GAP-{patient_id}-BP",
            "patient_id": patient_id,
            "type": "blood_pressure",
            "title": "Blood pressure not measured in the last 12 months",
            "severity": "soon",
            "detail": detail,
            "evidence": _patient_evidence(bundle, detail)
            + _encounter_evidence(bundle),
        }
    ]


def _hba1c_gap(bundle, patient_id, as_of):
    """Rule 2: diabetes + no HbA1c (LOINC 4548-4) in the last 6 months."""
    if not fhir_loader.has_diabetes(bundle):
        return []
    hba1c = fhir_loader.observations_by_loinc(bundle, LOINC_HBA1C)
    latest = fhir_loader.latest_observation_date(hba1c)
    if latest is not None and as_of - latest <= HBA1C_WINDOW:
        return []
    evidence = []
    for cond in fhir_loader.conditions(bundle):
        if fhir_loader.has_code(
            cond, "http://hl7.org/fhir/sid/icd-10-cm", "E11.9"
        ) or fhir_loader.has_code(cond, "http://snomed.info/sct", "44054006"):
            evidence.append(
                {
                    "resourceType": "Condition",
                    "id": cond.get("id"),
                    "detail": "Type 2 diabetes mellitus (ICD-10 E11.9 / "
                    "SNOMED 44054006).",
                }
            )
    if latest is None:
        detail = "No HbA1c result on record at all."
    else:
        detail = f"Last HbA1c was {latest.isoformat()}."
        latest_obs = max(
            hba1c, key=lambda o: fhir_loader.observation_date(o) or date.min
        )
        evidence.append(
            {
                "resourceType": "Observation",
                "id": latest_obs.get("id"),
                "detail": f"Most recent HbA1c (LOINC 4548-4): {latest.isoformat()}.",
            }
        )
    return [
        {
            "id": f"GAP-{patient_id}-HBA1C",
            "patient_id": patient_id,
            "type": "hba1c",
            "title": "HbA1c overdue for a patient with diabetes",
            "severity": "soon",
            "detail": detail,
            "evidence": evidence,
        }
    ]


def _has_colorectal_screening(bundle):
    """True if any colorectal screening is on record.

    Counts any of: a colonoscopy Procedure (SNOMED 73761001 / CPT 45378),
    a stool FIT/FOBT Observation (LOINC 2335-8), or an Encounter coded for
    colon-cancer screening (ICD-10 Z12.11). Real HEDIS lookback windows
    (10 years for colonoscopy, 1 year for FIT) are deliberately simplified
    here: any screening on record counts. See README.md.
    """
    for proc in fhir_loader.resources_of_type(bundle, "Procedure"):
        if fhir_loader.has_code(proc, "http://snomed.info/sct", SNOMED_COLONOSCOPY):
            return True
        if fhir_loader.has_code(
            proc, "http://www.ama-assn.org/go/cpt", CPT_COLONOSCOPY
        ):
            return True
    if fhir_loader.observations_by_loinc(bundle, LOINC_FOBT):
        return True
    for enc in fhir_loader.resources_of_type(bundle, "Encounter"):
        for reason in enc.get("reasonCode", []):
            if any(
                c.get("system") == "http://hl7.org/fhir/sid/icd-10-cm"
                and c.get("code") == ICD10_SCREENING_COLON
                for c in reason.get("coding", [])
            ):
                return True
    return False


def _screening_gap(bundle, patient_id, as_of):
    """Rule 3: age 45-75 AND no colorectal screening on record."""
    patient = fhir_loader.get_patient(bundle)
    age = fhir_loader.patient_age(patient, as_of)
    if not (SCREENING_AGE_MIN <= age <= SCREENING_AGE_MAX):
        return []
    if _has_colorectal_screening(bundle):
        return []
    detail = f"Age {age} (screening range is 45-75) with no colorectal screening on record."
    return [
        {
            "id": f"GAP-{patient_id}-SCREENING",
            "patient_id": patient_id,
            "type": "colorectal_screening",
            "title": "Colorectal cancer screening overdue",
            "severity": "routine",
            "detail": detail,
            "evidence": _patient_evidence(bundle, detail)
            + _encounter_evidence(bundle),
        }
    ]


def _referral_gaps(bundle, patient_id):
    """Rule 4: active ServiceRequest with no follow-up visit/procedure."""
    gaps = []
    for referral in fhir_loader.active_referrals(bundle):
        if fhir_loader.referral_is_resolved(bundle, referral):
            continue
        code = referral.get("code", {})
        description = code.get("text") or "Referral"
        authored = referral.get("authoredOn", "date unknown")
        detail = f"{description}, requested {authored}, with no completed visit or procedure on record."
        gaps.append(
            {
                "id": f"GAP-{patient_id}-REFERRAL-{referral.get('id')}",
                "patient_id": patient_id,
                "type": "open_referral",
                "title": "Open referral with no completed follow-up",
                "severity": "routine",
                "detail": detail,
                "evidence": [
                    {
                        "resourceType": "ServiceRequest",
                        "id": referral.get("id"),
                        "detail": detail,
                    }
                ],
            }
        )
    return gaps
