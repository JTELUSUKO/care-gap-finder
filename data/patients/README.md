# Synthetic patient data

> **SYNTHETIC DATA — fictional patients, for learning only.**
> Every name, date of birth, MRN, and clinical fact in these files is
> invented for this demo. No real patient data is used anywhere in this
> project. The bundles are tagged `HTEST` (test health data) in their
> `meta.tag`.

Five FHIR R4 bundles (type `collection`), one per patient:

| File | Patient | Age | Designed outcome |
|------|---------|-----|------------------|
| p001.json | Maria Santos | 58 | Exactly 1 gap: blood pressure (no BP reading on record at all — the missing-BP story) |
| p002.json | James Okafor | 61 | Exactly 1 gap: HbA1c overdue (diabetes, last HbA1c 2026-03-02) |
| p003.json | Elena Ruiz | 47 | Exactly 1 gap: open cardiology referral (active, no follow-up visit) |
| p004.json | David Chen | 52 | Exactly 1 gap: colorectal screening overdue (age 45–75, nothing on record) |
| p005.json | Aisha Patel | 39 | CONTROL: 0 gaps (recent BP, no chronic conditions, below screening age) |

The patients are fictional, but the **codes are real standards**, so the
patterns transfer to real FHIR data:

| Code | System | Meaning |
|------|--------|---------|
| 8480-6 | LOINC | Systolic blood pressure |
| 8462-4 | LOINC | Diastolic blood pressure |
| 4548-4 | LOINC | Hemoglobin A1c |
| 2335-8 | LOINC | Fecal occult blood test (FIT/FOBT stool screening) |
| 44054006 | SNOMED CT | Type 2 diabetes mellitus |
| 73761001 | SNOMED CT | Colonoscopy |
| 3457005 | SNOMED CT | Patient referral |
| E11.9 | ICD-10-CM | Type 2 diabetes mellitus without complications |
| Z12.11 | ICD-10-CM | Encounter for screening for malignant neoplasm of colon |
| 6809 | RxNorm | Metformin |
| 45378 | CPT | Colonoscopy |

Notes on the design:

- P001's bundle also carries a *completed* dermatology referral (status
  `completed`, with a follow-up visit pointing back at it) — the referral
  rule must skip it, so P001 still has exactly one gap.
- P003's routine office visit does *not* reference her cardiology
  referral, so the referral stays open — an unrelated visit must not
  close a referral.
- P005 is 39, below the 45–75 colorectal-screening range, so the
  screening rule does not apply to her at all.
- Dates are fixed relative to the demo reference date 2026-11-12 (see
  `gap_rules.DEFAULT_AS_OF`). Run with `--as-of` to evaluate against
  another date.
