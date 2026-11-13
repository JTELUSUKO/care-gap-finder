"""Care-Gap Finder Agent — command-line entry point.

Run the agent on one patient:
    python app.py --patient data/patients/p001.json

Run it on all five synthetic patients:
    python app.py --all

Review the human-approval queue:
    python app.py --list-pending

Approve or reject a pending alert (the human-in-the-loop step):
    python app.py --approve ALERT-patient-p001-20261112-143022-a1b2c3
    python app.py --reject  ALERT-patient-p001-20261112-143022-a1b2c3 --reason "Visit already scheduled"

Evaluate against a different reference date:
    python app.py --patient data/patients/p001.json --as-of 2026-06-01

Explanation mode: mock by default (no network, no keys). Set
CARE_GAP_LLM=claude to draft explanations with the Claude Code CLI.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

# Make the project root importable no matter where the script is run from.
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from care_gap import agent, alerts, gap_rules  # noqa: E402

PATIENTS_DIR = ROOT / "data" / "patients"
ALERTS_DIR = ROOT / "alerts"


def _print_run(result, patient_path):
    """Print the agent's result for one patient in plain language."""
    name = result["patient_name"]
    gaps = result.get("gaps", [])
    print(f"\n=== {name} ({Path(patient_path).name}) ===")
    if not gaps:
        print("No care gaps found. No alert written.")
        return
    print(f"Found {len(gaps)} care gap(s):")
    for gap in gaps:
        print(f"\n  [{gap['severity'].upper()}] {gap['title']}")
        print(f"  {gap['explanation']}")
        for ev in gap["evidence"]:
            print(f"    evidence: {ev['resourceType']}/{ev['id']} — {ev['detail']}")
    alert = result.get("alert")
    if alert:
        print(
            f"\nDraft alert written to alerts/pending/{alert['alert_id']}.json"
            "\nIt has NOT been sent. Review it with --list-pending, then"
            " approve or reject it."
        )


def _run_one(patient_path, as_of, llm_mode):
    try:
        result = agent.run_agent(
            patient_path, alerts_dir=ALERTS_DIR, as_of=as_of, llm_mode=llm_mode
        )
    except (ValueError, RuntimeError) as exc:
        # A clear one-line error beats a traceback for a missing file,
        # a bad bundle, or a missing `claude` CLI.
        print(f"\nError: {exc}")
        raise SystemExit(1)
    _print_run(result, patient_path)


def _list_pending():
    pending = alerts.list_pending(ALERTS_DIR)
    if not pending:
        print("No pending alerts. Run the agent first (see --patient).")
        return
    print(f"{len(pending)} pending alert(s) — waiting for a human decision:\n")
    for alert in pending:
        print(f"  {alert['alert_id']}")
        print(f"    Patient: {alert['patient_name']} ({alert['patient_id']})")
        for gap in alert["gaps"]:
            print(f"    - [{gap['severity']}] {gap['title']}")
        print(f"    Created: {alert['created_at']}\n")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Care-Gap Finder Agent — find care gaps in synthetic "
        "FHIR records and draft alerts for human approval."
    )
    parser.add_argument("--patient", help="Path to one patient bundle JSON.")
    parser.add_argument(
        "--all", action="store_true", help="Run the agent on all 5 patients."
    )
    parser.add_argument(
        "--list-pending", action="store_true", help="List alerts awaiting approval."
    )
    parser.add_argument("--approve", metavar="ALERT-ID", help="Approve a pending alert.")
    parser.add_argument("--reject", metavar="ALERT-ID", help="Reject a pending alert.")
    parser.add_argument(
        "--reason", default="", help="Reason for a rejection (with --reject)."
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="Reference date YYYY-MM-DD (default: the demo date "
        f"{gap_rules.DEFAULT_AS_OF.isoformat()}).",
    )
    args = parser.parse_args(argv)

    llm_mode = os.environ.get("CARE_GAP_LLM", "mock").lower()
    as_of = (
        date.fromisoformat(args.as_of)
        if args.as_of
        else gap_rules.DEFAULT_AS_OF
    )

    if args.list_pending:
        _list_pending()
        return 0
    if args.approve:
        alert = alerts.approve(ALERTS_DIR, args.approve)
        print(f"Approved: {alert['alert_id']} -> alerts/sent/ (audit log updated)")
        return 0
    if args.reject:
        alert = alerts.reject(ALERTS_DIR, args.reject, args.reason or "No reason given")
        print(
            f"Rejected: {alert['alert_id']} -> alerts/rejected/ "
            "(audit log updated)"
        )
        return 0
    if args.all:
        for path in sorted(PATIENTS_DIR.glob("p*.json")):
            _run_one(path, as_of, llm_mode)
        return 0
    if args.patient:
        _run_one(args.patient, as_of, llm_mode)
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
