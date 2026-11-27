"""Care-Gap Finder — web UI backend (FastAPI).

Run it:
    python server.py
Then open http://127.0.0.1:8000 in a browser.

What it does: the same agent as app.py (load -> detect gaps -> explain
-> draft alert), behind a small web API, with a single-page UI served
from static/index.html. This file only wraps the same run_agent / alerts
functions that app.py uses in HTTP endpoints.

Endpoints:
    GET  /                              the UI page
    GET  /api/patients                  the 5 synthetic patients (id + name)
    POST /api/run/{patient_id}?llm=     run the agent (llm=mock default,
                                         llm=claude shells to Claude Code)
    GET  /api/alerts/pending            alerts waiting for a human decision
    POST /api/alerts/{alert_id}/approve approve a pending alert
    POST /api/alerts/{alert_id}/reject  reject a pending alert {"reason": ...}

Python 3.9 compatible (no 3.10+ syntax). Local use only — no auth, no
real patient data (the 5 patients are synthetic FHIR fixtures).

Browser safety (there is no login, so these stop other websites from
using this server through your browser):
  * Only Host headers of 127.0.0.1 / localhost are accepted, which blocks
    DNS-rebinding pages from reading the API.
  * Every POST must carry the header  X-Requested-With: care-gap-finder.
    A web page on another site cannot add a custom header to a request
    here without the server's permission (which this server never gives),
    so it cannot trigger agent runs or decisions.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

# Make the project root importable no matter where the script is run from.
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from care_gap import agent, alerts, fhir_loader, gap_rules  # noqa: E402

PATIENTS_DIR = ROOT / "data" / "patients"
ALERTS_DIR = ROOT / "alerts"
INDEX_HTML = ROOT / "static" / "index.html"

# Patient ids are file stems like "p001" — nothing else is accepted,
# so a crafted id can never escape the patients directory. Use fullmatch.
PATIENT_ID_RE = re.compile(r"p[0-9]+")

ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "care-gap-finder"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

app = FastAPI(title="Care-Gap Finder")


@app.middleware("http")
async def require_ui_header(request: Request, call_next):
    """Refuse state-changing requests that lack the UI's custom header."""
    if (request.method not in SAFE_METHODS
            and request.headers.get(CSRF_HEADER) != CSRF_VALUE):
        return JSONResponse(
            status_code=403,
            content={"detail": f"Missing {CSRF_HEADER}: {CSRF_VALUE} header."},
        )
    return await call_next(request)


# Added last so it runs first: a bad Host header is refused before anything.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)


class RejectBody(BaseModel):
    reason: str = ""


def _patient_path(patient_id: str) -> Path:
    """Resolve a patient id to its bundle file, or raise 404."""
    if not PATIENT_ID_RE.fullmatch(patient_id):
        raise HTTPException(status_code=404, detail=f"No patient {patient_id}.")
    path = PATIENTS_DIR / f"{patient_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"No patient {patient_id}.")
    return path


def _alert_view(alert):
    """The alert fields the UI shows. Leaves out everything else, such as
    the MRN and the evidence, which stay in the alert file."""
    return {
        "alert_id": alert.get("alert_id"),
        "patient_id": alert.get("patient_id"),
        "patient_name": alert.get("patient_name"),
        "created_at": alert.get("created_at"),
        "gaps": [
            {"severity": g.get("severity"), "title": g.get("title")}
            for g in alert.get("gaps", [])
        ],
        "previously_sent": alerts.previously_sent(ALERTS_DIR, alert),
    }


@app.get("/")
def serve_ui():
    return FileResponse(INDEX_HTML)


@app.get("/api/patients")
def list_patients():
    """The 5 synthetic patients, with names read from their bundles."""
    patients = []
    for path in sorted(PATIENTS_DIR.glob("p*.json")):
        patient_id = path.stem
        try:
            bundle = fhir_loader.load_bundle(path)
            name = fhir_loader.patient_name(fhir_loader.get_patient(bundle))
        except Exception:
            name = patient_id  # a broken fixture still shows up in the list
        patients.append({"id": patient_id, "name": name})
    return {"patients": patients}


@app.post("/api/run/{patient_id}")
def run_patient(patient_id: str, llm: str = "mock"):
    """Run the agent on one patient. llm=mock (default) or llm=claude."""
    mode = llm.lower()
    if mode not in ("mock", "claude"):
        raise HTTPException(
            status_code=400, detail='llm must be "mock" or "claude".'
        )
    path = _patient_path(patient_id)
    try:
        result = agent.run_agent(
            path, alerts_dir=ALERTS_DIR, as_of=gap_rules.DEFAULT_AS_OF,
            llm_mode=mode,
        )
    except (ValueError, RuntimeError) as exc:
        # Missing `claude` CLI, bad bundle, etc. — a plain message, not a
        # traceback, so the UI can show it as-is.
        raise HTTPException(status_code=400, detail=str(exc))
    except OSError as exc:
        # A file that can't be read or written (permissions, full disk) is
        # the server's problem, not the request's — but the UI still gets
        # a plain message it can show.
        name = Path(exc.filename).name if exc.filename else "a file"
        raise HTTPException(
            status_code=500,
            detail=f"The server could not read or write {name}: "
                   f"{exc.strerror or exc}.",
        )
    # Return only what the UI needs — never the full FHIR bundle or the MRN.
    alert = result.get("alert")
    return {
        "patient_id": result["patient_id"],
        "patient_name": result["patient_name"],
        "gaps": result.get("gaps", []),
        "alert": _alert_view(alert) if alert else None,
    }


@app.get("/api/alerts/pending")
def pending_alerts():
    """Readable alerts, plus the names of any files that could not be read."""
    pending, unreadable = alerts.list_pending_with_errors(ALERTS_DIR)
    return {"pending": [_alert_view(a) for a in pending], "unreadable": unreadable}


@app.post("/api/alerts/{alert_id}/approve")
def approve_alert(alert_id: str):
    try:
        alert = alerts.approve(ALERTS_DIR, alert_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"approved": _alert_view(alert)}


@app.post("/api/alerts/{alert_id}/reject")
def reject_alert(alert_id: str, body: Optional[RejectBody] = None):
    reason = (body.reason if body and body.reason else "No reason given")
    try:
        alert = alerts.reject(ALERTS_DIR, alert_id, reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"rejected": _alert_view(alert)}


if __name__ == "__main__":
    import uvicorn

    print("Care-Gap Finder UI — open http://127.0.0.1:8000 in your browser.")
    print("Stop the server with Ctrl+C.")
    uvicorn.run(app, host="127.0.0.1", port=8000)
