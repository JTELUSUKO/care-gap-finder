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
  .lock       held while a draft is saved or a decision is made, so two
              decisions (UI + command line, two tabs) can never overlap
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

PENDING = "pending"
SENT = "sent"
REJECTED = "rejected"
AUDIT_LOG = "audit.log"
LOCK_FILE = ".lock"

# Alert ids become file names, so only letters, digits and hyphens are
# allowed — an id like "../x" can never point outside the alerts folders.
# Always use fullmatch: "$" would also accept a trailing newline.
ALERT_ID_RE = re.compile(r"[A-Za-z0-9-]+")


def _check_alert_id(alert_id):
    """Raise ValueError unless alert_id is safe to use as a file name."""
    if not isinstance(alert_id, str) or not ALERT_ID_RE.fullmatch(alert_id):
        raise ValueError(
            f"Invalid alert id {alert_id!r} — only letters, digits and "
            "hyphens are allowed."
        )


def ensure_dirs(alerts_dir):
    """Create pending/sent/rejected folders if they do not exist yet."""
    alerts_dir = Path(alerts_dir)
    for sub in (PENDING, SENT, REJECTED):
        (alerts_dir / sub).mkdir(parents=True, exist_ok=True)
    return alerts_dir


@contextmanager
def _queue_lock(alerts_dir):
    """Let only one thread or process change the queue at a time.

    The operating system releases the lock if the process dies, so a crash
    can never leave the queue locked. (macOS and Linux only.)
    """
    with (Path(alerts_dir) / LOCK_FILE).open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _write_json(path, data):
    """Write a JSON file in one step: readers see the old file or the new
    one, never a half-written one."""
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _read_alert(path):
    """Read one alert file. Raises ValueError if it is not a valid alert."""
    try:
        with path.open(encoding="utf-8") as f:
            alert = json.load(f)
    except ValueError:
        raise ValueError(f"Alert file {path.name} is not readable JSON.") from None
    if not isinstance(alert, dict) or alert.get("alert_id") != path.stem:
        raise ValueError(f"Alert file {path.name} does not hold alert {path.stem}.")
    gaps = alert.get("gaps", [])
    if not isinstance(gaps, list) or not all(isinstance(g, dict) for g in gaps):
        raise ValueError(f"Alert file {path.name} has a malformed gap list.")
    return alert


def _read_queue(alerts_dir, sub):
    """(alerts, names of unreadable files) for one queue folder."""
    found, unreadable = [], []
    for path in sorted((Path(alerts_dir) / sub).glob("*.json")):
        try:
            found.append(_read_alert(path))
        except FileNotFoundError:
            continue  # decided by someone else a moment ago
        except (ValueError, OSError):
            unreadable.append(path.name)
    return found, unreadable


def write_pending_alert(alerts_dir, alert):
    """Save a draft alert to pending/ and return its file path."""
    _check_alert_id(alert.get("alert_id"))
    alerts_dir = ensure_dirs(alerts_dir)
    path = alerts_dir / PENDING / f"{alert['alert_id']}.json"
    _write_json(path, alert)
    return path


def _same_gaps_key(alert):
    """What makes two alerts duplicates: the same patient and the same gaps
    (explanations may differ). None when the alert cannot be compared."""
    gap_ids = [g.get("id") for g in alert.get("gaps", [])]
    if not alert.get("patient_id") or not gap_ids or not all(gap_ids):
        return None
    return alert["patient_id"], tuple(sorted(str(i) for i in gap_ids))


def _same_content(a, b):
    """True when two drafts would show the reviewer exactly the same thing."""
    return a.get("patient_name") == b.get("patient_name") and a.get("gaps") == b.get("gaps")


def save_draft(alerts_dir, alert):
    """Save a draft so there is never more than one undecided draft for the
    same patient and gaps. Returns the saved alert.

    If an undecided draft already says exactly the same thing, it is kept
    as it is. If the new draft says something different (for example a
    Claude explanation instead of the mock one), the new draft gets its own
    id and replaces the old ones, and the swap goes in the audit log. The
    contents behind an id never change, so a reviewer can only approve the
    text they were shown: approving the old id afterwards is an error.
    """
    _check_alert_id(alert.get("alert_id"))
    alerts_dir = ensure_dirs(alerts_dir)
    with _queue_lock(alerts_dir):
        key = _same_gaps_key(alert)
        older = [a for a in _read_queue(alerts_dir, PENDING)[0]
                 if key and _same_gaps_key(a) == key]
        for existing in older:
            if _same_content(existing, alert):
                return existing
        path = write_pending_alert(alerts_dir, alert)
        try:
            for existing in older:
                _audit(alerts_dir, "REPLACED", existing["alert_id"],
                       alert.get("patient_id", "?"),
                       f"replaced by {alert['alert_id']}")
        except OSError:
            path.unlink()  # no replacement without an audit line
            raise
        for existing in older:
            (alerts_dir / PENDING / f"{existing['alert_id']}.json").unlink(missing_ok=True)
    return alert


def _is_timestamp(value):
    try:
        datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return True


def previously_sent(alerts_dir, alert):
    """When an alert for the same patient and gaps was last approved, or
    None. Lets the reviewer see they may be contacting someone twice.

    The alert itself never counts, so an alert that was just approved
    does not report its own approval. A sent alert with no usable approval
    time shows as "an earlier date", but only when no real time exists.
    """
    key = _same_gaps_key(alert)
    if key is None:
        return None
    matches = [
        a.get("decided_at")
        for a in _read_queue(alerts_dir, SENT)[0]
        if _same_gaps_key(a) == key and a.get("alert_id") != alert.get("alert_id")
    ]
    if not matches:
        return None
    times = [t for t in matches if _is_timestamp(t)]
    return max(times, key=datetime.fromisoformat) if times else "an earlier date"


def list_pending_with_errors(alerts_dir):
    """(pending alerts, names of pending files that could not be read).

    One damaged file never hides the other patients' alerts.
    """
    return _read_queue(ensure_dirs(alerts_dir), PENDING)


def list_pending(alerts_dir):
    """All readable pending alerts, in file-name order, as alert dicts."""
    return list_pending_with_errors(alerts_dir)[0]


def _find_alert(alerts_dir, alert_id):
    """Locate an alert file by id. Decided queues come first, so an alert
    that was already decided is never treated as pending."""
    _check_alert_id(alert_id)
    for sub in (SENT, REJECTED, PENDING):
        path = Path(alerts_dir) / sub / f"{alert_id}.json"
        if path.exists():
            return sub, path
    return None, None


def _audit_field(value):
    """Make a value safe for one audit-log field: whitespace (including
    newlines) collapses to single spaces, so it can't start a forged line,
    and "|" becomes "/", so it can't start a forged field. A rejected
    alert's own file keeps the reason exactly as typed."""
    return " ".join(str(value).replace("|", "/").split())


def _audit(alerts_dir, action, alert_id, patient_id, note):
    """Append one timestamped line to the audit log."""
    stamp = datetime.now().isoformat(timespec="seconds")
    fields = [stamp, action, alert_id, patient_id, note]
    line = " | ".join(_audit_field(f) for f in fields) + "\n"
    with (Path(alerts_dir) / AUDIT_LOG).open("a", encoding="utf-8") as f:
        f.write(line)


def _decide(alerts_dir, alert_id, queue, verb, note, extra=None):
    """Move a pending alert to `queue` and audit it, exactly once.

    The lock makes check, move and audit one step: of two decisions on the
    same alert, the first wins and the second gets a ValueError.
    """
    _check_alert_id(alert_id)
    alerts_dir = ensure_dirs(alerts_dir)
    with _queue_lock(alerts_dir):
        current, path = _find_alert(alerts_dir, alert_id)
        if path is None:
            raise ValueError(f"No alert with id {alert_id} — check --list-pending.")
        if current != PENDING:
            raise ValueError(
                f"Alert {alert_id} is already {current}; only pending alerts "
                f"can be {verb}."
            )
        alert = _read_alert(path)
        alert.update(extra or {})
        alert["status"] = queue
        alert["decided_at"] = datetime.now().isoformat(timespec="seconds")
        destination = alerts_dir / queue / path.name
        _write_json(destination, alert)
        try:
            _audit(alerts_dir, verb.upper(), alert_id,
                   alert.get("patient_id", "?"), note)
        except OSError:
            destination.unlink()  # no decision without an audit line
            raise
        path.unlink()
    return alert


def approve(alerts_dir, alert_id):
    """Approve a pending alert: move it to sent/ and audit the decision.

    Only a pending alert can be approved — approving something already
    sent or rejected is an error, not a silent no-op.
    """
    return _decide(alerts_dir, alert_id, SENT, "approved", "-")


def reject(alerts_dir, alert_id, reason):
    """Reject a pending alert: move it to rejected/ and audit the decision."""
    return _decide(alerts_dir, alert_id, REJECTED, "rejected", reason,
                   extra={"decision_reason": reason})
