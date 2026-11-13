"""Load and query a synthetic FHIR R4 patient bundle.

This is the project's "Data Sources" layer (the left side of the
infographic): it reads the JSON bundle for one patient and answers simple
questions about it — which observations exist, which conditions, which
referrals are still open, how old the patient is.

The bundles in data/patients/ are SYNTHETIC — fictional patients, for
learning only. They use real standard codes (LOINC, SNOMED CT, ICD-10,
RxNorm, CPT) so the patterns transfer to real FHIR data.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path


def load_bundle(path):
    """Load a patient bundle JSON file and validate its basic shape.

    Returns the bundle dict. Raises ValueError with a plain message if the
    file is not a Bundle or contains no Patient resource.
    """
    path = Path(path)
    with path.open(encoding="utf-8") as f:
        bundle = json.load(f)
    if not isinstance(bundle, dict) or bundle.get("resourceType") != "Bundle":
        raise ValueError(f"{path.name}: expected a FHIR Bundle JSON file.")
    if get_patient(bundle) is None:
        raise ValueError(f"{path.name}: bundle contains no Patient resource.")
    return bundle


def resources(bundle):
    """All resources in the bundle, in file order."""
    return [e["resource"] for e in bundle.get("entry", []) if "resource" in e]


def resources_of_type(bundle, resource_type):
    """All resources of one FHIR type, e.g. 'Observation'."""
    return [r for r in resources(bundle) if r.get("resourceType") == resource_type]


def resource_ids(bundle):
    """Set of 'ResourceType/id' strings for every resource in the bundle.

    The test suite uses this to prove every gap's evidence points at a
    resource that really exists in the patient's record.
    """
    return {f"{r.get('resourceType')}/{r.get('id')}" for r in resources(bundle)}


def get_patient(bundle):
    """The bundle's Patient resource, or None."""
    patients = resources_of_type(bundle, "Patient")
    return patients[0] if patients else None


def patient_name(patient):
    """Full name from a Patient resource, e.g. 'Maria Santos'."""
    names = patient.get("name", [])
    if not names:
        return "Unknown"
    name = names[0]
    given = " ".join(name.get("given", []))
    family = name.get("family", "")
    return f"{given} {family}".strip() or "Unknown"


def patient_mrn(patient):
    """The patient's MRN identifier value, or None."""
    for ident in patient.get("identifier", []):
        if ident.get("type", {}).get("text") == "Medical Record Number":
            return ident.get("value")
    return None


def patient_age(patient, as_of):
    """Age in whole years on the reference date `as_of` (a date object)."""
    birth = _parse_date(patient.get("birthDate"))
    if birth is None:
        raise ValueError("Patient has no usable birthDate.")
    years = as_of.year - birth.year
    if (as_of.month, as_of.day) < (birth.month, birth.day):
        years -= 1
    return years


def _codings(resource, field="code"):
    """All codings on a resource's codeable field (code, category, ...)."""
    field_value = resource.get(field, {})
    if isinstance(field_value, list):
        codings = []
        for item in field_value:
            codings.extend(item.get("coding", []))
        return codings
    return field_value.get("coding", [])


def has_code(resource, system, code, field="code"):
    """True if the resource carries this exact code in this system."""
    return any(
        c.get("system") == system and c.get("code") == code
        for c in _codings(resource, field)
    )


def observations_by_loinc(bundle, loinc_code):
    """Observations whose code is this LOINC code (e.g. '8480-6')."""
    return [
        o
        for o in resources_of_type(bundle, "Observation")
        if has_code(o, "http://loinc.org", loinc_code)
    ]


def conditions(bundle):
    """All Condition resources in the bundle."""
    return resources_of_type(bundle, "Condition")


def has_diabetes(bundle):
    """True if the patient has an active Type 2 diabetes Condition.

    Matches either coding a real chart would carry: ICD-10-CM E11.9 or
    SNOMED CT 44054006.
    """
    for cond in conditions(bundle):
        if cond.get("clinicalStatus", {}).get("coding", [{}])[0].get("code") not in (
            "active",
            None,
        ):
            continue
        if has_code(cond, "http://hl7.org/fhir/sid/icd-10-cm", "E11.9"):
            return True
        if has_code(cond, "http://snomed.info/sct", "44054006"):
            return True
    return False


def active_referrals(bundle):
    """ServiceRequests with status 'active' — referrals still open."""
    return [
        sr
        for sr in resources_of_type(bundle, "ServiceRequest")
        if sr.get("status") == "active"
    ]


def referral_is_resolved(bundle, service_request):
    """True if an Encounter or Procedure references this referral.

    In FHIR, the follow-up visit or procedure points back at the request
    through `basedOn`. A referral with no such follow-up is still open.
    """
    ref = f"ServiceRequest/{service_request.get('id')}"
    for resource in resources(bundle):
        if resource.get("resourceType") not in ("Encounter", "Procedure"):
            continue
        for based_on in resource.get("basedOn", []):
            if based_on.get("reference") == ref:
                return True
    return False


def latest_encounter(bundle):
    """The most recent Encounter, or None."""
    encounters = resources_of_type(bundle, "Encounter")
    dated = [(e, _parse_date((e.get("period") or {}).get("start"))) for e in encounters]
    dated = [(e, d) for e, d in dated if d is not None]
    if not dated:
        return None
    dated.sort(key=lambda pair: pair[1])
    return dated[-1][0]


def _parse_date(value):
    """Parse 'YYYY-MM-DD' (or a datetime string) into a date, or None."""
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def observation_date(observation):
    """The effective date of an Observation, or None."""
    return _parse_date(observation.get("effectiveDateTime"))


def latest_observation_date(observations):
    """The most recent effective date among observations, or None."""
    dates = [observation_date(o) for o in observations]
    dates = [d for d in dates if d is not None]
    return max(dates) if dates else None
