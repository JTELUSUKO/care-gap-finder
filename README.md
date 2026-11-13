# Care-Gap Finder Agent

A small working agent that reads a patient's FHIR record, finds care gaps
(missing tests, overdue labs, open referrals), explains each gap in plain
language, and drafts an alert for a human to approve. It is the "Agentic
AI in Healthcare" infographic brought to life: data in, agent in the
middle, a human approving the important calls, actions out.

> **SYNTHETIC DATA — fictional patients, for learning only.**
> Every patient in `data/patients/` is invented. No real patient data is
> used anywhere in this project. The clinical codes (LOINC, SNOMED CT,
> ICD-10, RxNorm, CPT) are real standards, so the patterns transfer.

No API keys, no paid services, no model calls in the default mode — the
agent runs entirely on your Mac with Python + LangGraph + pytest.

## The infographic, mapped to files

| Infographic layer | What it does here | Where it lives |
|---|---|---|
| Data Sources (EHR/EMR, Labs, Payers) | Synthetic FHIR R4 patient bundles | `data/patients/p001.json` … `p005.json` |
| Interoperability & Integration Platform | Load + query the FHIR bundles (FHIR APIs, data mapping) | `care_gap/fhir_loader.py` |
| AI Agent — Reason · Plan · Act | Deterministic gap rules (the reason core) | `care_gap/gap_rules.py` |
| AI Agent — the graph | LangGraph flow: load → detect → explain → draft alert | `care_gap/agent.py` |
| AI Agent — plain-language writing | Explanation adapter (mock by default) | `care_gap/llm.py` |
| Human in the Loop — Review · Approve · Intervene | Approval queue: pending → sent / rejected | `care_gap/alerts.py` |
| Actions / Workflows — Alerts & Notifications | The draft alert (written, never sent by the agent) | `alerts/pending/` |
| Audit, Logging & Traceability | One timestamped line per decision | `alerts/audit.log` |

## Layout

```
care-gap-finder/
  app.py                  the command line: run, list, approve, reject
  requirements.txt        langgraph, langchain-core, pytest
  care_gap/
    fhir_loader.py        read + query a FHIR bundle
    gap_rules.py          the four deterministic gap rules
    llm.py                explanation adapter (mock / claude)
    agent.py              the LangGraph graph
    alerts.py             the human approval queue
  data/patients/          5 synthetic FHIR bundles + README
  alerts/                 pending/ sent/ rejected/ audit.log
  tests/test_care_gaps.py the pytest suite
```

## Setup (macOS Terminal, one command at a time)

```bash
cd care-gap-finder
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Run it

One patient — Maria Santos (P001), the missing-blood-pressure case:

```bash
python app.py --patient data/patients/p001.json
```

Expect this: **1 gap** — blood pressure, severity SOON — with the
explanation "Maria Santos has no blood pressure reading on record in the
last 12 months", evidence pointing at her Patient record and her most
recent visit, and a draft alert written to `alerts/pending/`. It has NOT
been sent.

The control patient — Aisha Patel (P005):

```bash
python app.py --patient data/patients/p005.json
```

Expect: **"No care gaps found. No alert written."** A healthy record
produces nothing. That is the test that matters most: the agent does not
cry wolf.

All five patients at once:

```bash
python app.py --all
```

Expect: P001 one gap (blood pressure), P002 one gap (HbA1c overdue),
P003 one gap (open cardiology referral), P004 one gap (colorectal
screening overdue), P005 zero gaps. Four pending alerts, one per patient
with a gap.

## The approval flow (the Human in the Loop)

The agent drafts; a human decides. Nothing reaches `sent/` without an
explicit approve.

```bash
python app.py --list-pending
```

Shows every waiting alert with its id, patient, and gaps. Then:

```bash
python app.py --approve ALERT-patient-p001-20261112-143022-a1b2c3
python app.py --reject ALERT-patient-p002-20261112-143105-c3d4e5 --reason "HbA1c already scheduled"
```

(Use the real alert id from `--list-pending` — the last part is random,
so copy it from the output.)

Approve moves the alert to `alerts/sent/`; reject moves it to
`alerts/rejected/` with your reason kept. Both write one timestamped
line to `alerts/audit.log`:

```
2026-11-12T14:31:05 | APPROVED | ALERT-patient-p001-... | patient-p001 | -
2026-11-12T14:32:40 | REJECTED | ALERT-patient-p002-... | patient-p002 | HbA1c already scheduled
```

Approving an alert that is already sent is an error, not a silent
no-op — the queue refuses to double-send.

## The grounding rule: rules find, the LLM explains

The four gap rules in `gap_rules.py` are plain Python checks against the
FHIR bundle. The language model never invents, adds, or removes a gap —
it only turns a found gap into a plain-language sentence, using only the
evidence the rules attached. That is the same discipline as the
agent-evaluation harness: the deterministic layer is the source of truth,
and the LLM is a writer, not a decider.

The four rules:

1. **Blood pressure** — no systolic BP Observation (LOINC 8480-6) in the
   last 12 months. Severity: soon.
2. **HbA1c** — the patient has a diabetes Condition (ICD-10 E11.9 /
   SNOMED 44054006) and no HbA1c Observation (LOINC 4548-4) in the last
   6 months. Severity: soon.
3. **Colorectal screening** — age 45–75 and no screening on record
   (colonoscopy Procedure, FIT/FOBT Observation, or screening Encounter).
   Severity: routine.
4. **Open referral** — a ServiceRequest with status `active` and no
   follow-up Encounter or Procedure pointing back at it. Severity:
   routine.

The demo evaluates "overdue" against a fixed reference date, 2026-11-12
(`gap_rules.DEFAULT_AS_OF`), so the answers are stable and the tests are
deterministic. Evaluate against another date with `--as-of YYYY-MM-DD`.

Two deliberate simplifications, both documented in the code: the
screening rule counts any screening on record (real HEDIS uses lookback
windows — 10 years for colonoscopy, 1 year for FIT), and the referral
rule matches follow-ups only through FHIR `basedOn` references.

## Switch to the real LLM (Claude Code)

The mock explanations are templates. To draft them with a real model:

```bash
CARE_GAP_LLM=claude python app.py --patient data/patients/p001.json
```

This shells out to `claude -p` (headless Claude Code) with ONLY the gap
the rules found and its evidence — the prompt forbids inventing anything
else. It needs Claude Code installed on your Mac and signed in; no API
key is stored anywhere in this project. If `claude` is not installed,
you get a clear error telling you so.

## Run the tests

```bash
pytest tests/ -v
```

Expect **all tests to pass**. What each one proves:

- **Each patient has exactly its designed gaps** — P001 only the BP gap,
  P002 only HbA1c, P003 only the referral, P004 only screening, P005
  none. No invented gaps, no missed gaps.
- **Control patient has no gaps** — a healthy record stays silent.
- **Completed referral does not fire** — P001's finished dermatology
  referral must not count as open.
- **Evidence references real resources** — every gap's evidence points
  at a resource id that exists in that patient's bundle.
- **No gaps means no alert** — the control patient writes no alert file.
- **HITL gate** — an alert starts in pending only; approve moves it to
  sent and writes the audit log; reject moves it to rejected and never
  to sent.
- **Approve twice is an error** — the queue refuses to double-send.
- **No cross-patient leakage** — one patient's alert contains no other
  patient's name or MRN.
- **Explanations are plain language** — every gap gets a real sentence
  naming the patient.

Tests write their alerts to temporary folders, so running the suite
never pollutes the project's own `alerts/` folders.

## Stretch ideas

- **LangGraph `interrupt()` instead of the CLI queue** — pause the graph
  itself at the approval step and resume it after the human decides.
- **FastAPI + a simple web UI** — a page per patient, an Approve/Reject
  button per alert, the audit log as a table.
- **Chroma RAG over screening-guideline text** — let the agent quote the
  guideline (e.g. "USPSTF recommends screening ages 45–75") in its
  explanation, with the retrieved passage as extra evidence.
- **Add a 6th patient** — copy `p005.json`, change the name/MRN/dates,
  and give them a gap the rules should catch; add one line to the test
  table in `tests/test_care_gaps.py`.
- **A second measure** — add a rule for breast-cancer screening
  (mammogram) using the same pattern as the colorectal rule.
