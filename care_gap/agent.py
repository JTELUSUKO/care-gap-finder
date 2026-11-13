"""The agent — a LangGraph StateGraph with four nodes.

    load_patient -> detect_gaps -> explain_gaps -> draft_alert

  * load_patient: reads the FHIR bundle for one patient (Data Sources).
  * detect_gaps:  runs the deterministic rules in gap_rules.py (Reason).
  * explain_gaps: asks the explanation adapter (mock or claude) to turn
                  each found gap into plain language. The LLM only ever
                  explains gaps the rules found — it cannot invent one.
  * draft_alert:  writes a draft alert to alerts/pending/ — it does NOT
                  send anything. A human approves it later (see alerts.py).

State is a plain TypedDict carried from node to node: the bundle, the
gaps, the explanations, and the draft alert. Run it with `run_agent()`
or from the command line with `python app.py --patient <file>`.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Optional, TypedDict

from langgraph.graph import END, StateGraph

from . import alerts, fhir_loader, gap_rules
from .llm import explain_gap

DEFAULT_ALERTS_DIR = Path(__file__).resolve().parents[1] / "alerts"


class CareGapState(TypedDict, total=False):
    """Everything the graph carries from node to node."""

    patient_path: str  # input: path to the patient's FHIR bundle JSON
    as_of: str  # input: reference date, ISO 'YYYY-MM-DD'
    llm_mode: str  # input: 'mock' (default) or 'claude'
    alerts_dir: str  # input: where the pending/sent/rejected queues live
    bundle: dict  # loaded by load_patient
    patient_id: str  # e.g. 'patient-p001'
    patient_name: str  # e.g. 'Maria Santos'
    gaps: list  # found by detect_gaps
    alert: Optional[dict]  # written by draft_alert (None when no gaps)


def _node_load_patient(state):
    bundle = fhir_loader.load_bundle(state["patient_path"])
    patient = fhir_loader.get_patient(bundle)
    return {
        "bundle": bundle,
        "patient_id": patient.get("id"),
        "patient_name": fhir_loader.patient_name(patient),
    }


def _node_detect_gaps(state):
    as_of = date.fromisoformat(state["as_of"])
    gaps = gap_rules.detect_gaps(state["bundle"], as_of=as_of)
    return {"gaps": gaps}


def _node_explain_gaps(state):
    gaps = state.get("gaps", [])
    explained = []
    for gap in gaps:
        explained.append(
            {
                **gap,
                "explanation": explain_gap(
                    gap, state["patient_name"], mode=state["llm_mode"]
                ),
            }
        )
    return {"gaps": explained}


def _node_draft_alert(state):
    gaps = state.get("gaps", [])
    if not gaps:
        return {"alert": None}
    patient = fhir_loader.get_patient(state["bundle"])
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    alert_id = f"ALERT-{state['patient_id']}-{stamp}-{suffix}"
    alert = {
        "alert_id": alert_id,
        "status": "pending",
        "patient_id": state["patient_id"],
        "patient_name": state["patient_name"],
        "mrn": fhir_loader.patient_mrn(patient),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "as_of": state["as_of"],
        "synthetic": True,
        "gaps": gaps,
    }
    alerts.write_pending_alert(Path(state["alerts_dir"]), alert)
    return {"alert": alert}


def build_graph():
    """Build and compile the four-node graph."""
    graph = StateGraph(CareGapState)
    graph.add_node("load_patient", _node_load_patient)
    graph.add_node("detect_gaps", _node_detect_gaps)
    graph.add_node("explain_gaps", _node_explain_gaps)
    graph.add_node("draft_alert", _node_draft_alert)
    graph.set_entry_point("load_patient")
    graph.add_edge("load_patient", "detect_gaps")
    graph.add_edge("detect_gaps", "explain_gaps")
    graph.add_edge("explain_gaps", "draft_alert")
    graph.add_edge("draft_alert", END)
    return graph.compile()


_GRAPH = None


def run_agent(patient_path, alerts_dir=None, as_of=None, llm_mode="mock"):
    """Run the agent on one patient bundle.

    Returns the final state dict: patient_name, gaps (each with its
    explanation), and alert (the draft written to pending/, or None when
    the patient has no gaps). `as_of` defaults to the demo reference date
    in gap_rules.DEFAULT_AS_OF so results are stable.
    """
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    if as_of is None:
        as_of = gap_rules.DEFAULT_AS_OF
    if alerts_dir is None:
        alerts_dir = DEFAULT_ALERTS_DIR
    initial_state = {
        "patient_path": str(patient_path),
        "as_of": as_of.isoformat(),
        "llm_mode": llm_mode,
        "alerts_dir": str(alerts_dir),
    }
    return _GRAPH.invoke(initial_state)
