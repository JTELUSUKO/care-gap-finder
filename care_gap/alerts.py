"""The human-in-the-loop approval queue.

This is the infographic's "Human in the Loop" box (Review · Approve ·
Intervene). The agent never sends anything: it writes a draft alert to
`alerts/pending/`, and the alert only reaches `alerts/sent/` when a human
explicitly approves it. Every decision is written to the audit log
(`alerts/audit.log`) — the infographic's "Audit, Logging & Traceability"
layer.

Layout under the alerts directory:
  pending/    drafts waiting for a human decision
  sent/       approved alerts (in a real system, "sent" would hand off to
              the care-coordination system; here the move IS the send)
  rejected/   alerts a human turned down, with the reason kept
  audit.log   one line per decision, timestamped
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

PENDING = "pending"
SENT = "sent"
REJECTED = "rejected"
AUDIT_LOG = "audit.log"


def ensure_dirs(alerts_dir):
    """Create pending/sent/rejected folders if they do not exist yet."""
    alerts_dir = Path(alerts_dir)
    for sub in (PENDING, SENT, REJECTED):
        (alerts_dir / sub).mkdir(parents=True, exist_ok=True)
    return alerts_dir


def write_pending_alert(alerts_dir, alert):
    """Save a draft alert to pending/ and return its file path."""
    alerts_dir = ensure_dirs(alerts_dir)
    path = alerts_dir / PENDING / f"{alert['alert_id']}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(alert, f, indent=2)
    return path


def list_pending(alerts_dir):
    """All pending alerts, oldest first, as a list of alert dicts."""
    alerts_dir = ensure_dirs(alerts_dir)
    alerts = []
    for path in sorted((alerts_dir / PENDING).glob("*.json")):
        with path.open(encoding="utf-8") as f:
            alerts.append(json.load(f))
    return alerts


def _find_alert(alerts_dir, alert_id):
    """Locate an alert file by id in any of the three queues."""
    for sub in (PENDING, SENT, REJECTED):
        path = Path(alerts_dir) / sub / f"{alert_id}.json"
        if path.exists():
            return sub, path
    return None, None


def _audit(alerts_dir, action, alert_id, patient_id, note):
    """Append one timestamped line to the audit log."""
    stamp = datetime.now().isoformat(timespec="seconds")
    line = f"{stamp} | {action} | {alert_id} | {patient_id} | {note}\n"
    with (Path(alerts_dir) / AUDIT_LOG).open("a", encoding="utf-8") as f:
        f.write(line)


def approve(alerts_dir, alert_id):
    """Approve a pending alert: move it to sent/ and audit the decision.

    Only a pending alert can be approved — approving something already
    sent or rejected is an error, not a silent no-op.
    """
    alerts_dir = ensure_dirs(alerts_dir)
    queue, path = _find_alert(alerts_dir, alert_id)
    if path is None:
        raise ValueError(f"No alert with id {alert_id} — check --list-pending.")
    if queue != PENDING:
        raise ValueError(
            f"Alert {alert_id} is already {queue}; only pending alerts "
            "can be approved."
        )
    with path.open(encoding="utf-8") as f:
        alert = json.load(f)
    alert["status"] = SENT
    alert["decided_at"] = datetime.now().isoformat(timespec="seconds")
    destination = alerts_dir / SENT / path.name
    with destination.open("w", encoding="utf-8") as f:
        json.dump(alert, f, indent=2)
    path.unlink()
    _audit(alerts_dir, "APPROVED", alert_id, alert.get("patient_id", "?"), "-")
    return alert


def reject(alerts_dir, alert_id, reason):
    """Reject a pending alert: move it to rejected/ and audit the decision."""
    alerts_dir = ensure_dirs(alerts_dir)
    queue, path = _find_alert(alerts_dir, alert_id)
    if path is None:
        raise ValueError(f"No alert with id {alert_id} — check --list-pending.")
    if queue != PENDING:
        raise ValueError(
            f"Alert {alert_id} is already {queue}; only pending alerts "
            "can be rejected."
        )
    with path.open(encoding="utf-8") as f:
        alert = json.load(f)
    alert["status"] = REJECTED
    alert["decision_reason"] = reason
    alert["decided_at"] = datetime.now().isoformat(timespec="seconds")
    destination = alerts_dir / REJECTED / path.name
    with destination.open("w", encoding="utf-8") as f:
        json.dump(alert, f, indent=2)
    path.unlink()
    _audit(
        alerts_dir, "REJECTED", alert_id, alert.get("patient_id", "?"), reason
    )
    return alert
