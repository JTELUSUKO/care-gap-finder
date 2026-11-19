"""The care-gap multi-agent workflow implemented as a LangGraph StateGraph.

    load_patient -> detect_gaps -> four parallel specialists
                 -> coordinate_explanations -> verify_explanations
                 -> draft_alert

  * load_patient: reads the FHIR bundle for one patient (Data Sources).
  * detect_gaps:  runs the deterministic rules in gap_rules.py (Reason).
  * specialists:  each handles only its assigned gap type and drafts
                  explanations for findings already returned by the rules.
  * coordinator:  joins specialist outputs and restores rule-result order.
  * verifier:     checks each result's gap, specialist, and evidence linkage.
  * draft_alert:  writes a draft alert to alerts/pending/ — it does NOT
                  send anything. A human approves it later (see alerts.py).

State is a plain TypedDict carried from node to node: the bundle, the
gaps, the explanations, and the draft alert. Run it with `run_agent()`
or from the command line with `python app.py --patient <file>`.
"""
from __future__ import annotations

import operator
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Optional, TypedDict

from langgraph.graph import END, StateGraph

from . import alerts, fhir_loader, gap_rules
from .llm import explain_gap

DEFAULT_ALERTS_DIR = Path(__file__).resolve().parents[1] / "alerts"

SPECIALIST_AGENT_NAMES = {
    "blood_pressure": "blood_pressure_agent",
    "hba1c": "hba1c_agent",
    "colorectal_screening": "colorectal_screening_agent",
    "open_referral": "open_referral_agent",
}


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
    agent_outputs: Annotated[list[dict], operator.add]
    coordinated_explanations: list[dict]
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


def _node_specialist(state, gap_type):
    outputs = []
    for gap in state.get("gaps", []):
        if gap["type"] != gap_type:
            continue
        outputs.append(
            {
                "gap_id": gap["id"],
                "gap_type": gap_type,
                "agent": SPECIALIST_AGENT_NAMES[gap_type],
                "evidence_refs": [
                    f"{evidence['resourceType']}/{evidence['id']}"
                    for evidence in gap["evidence"]
                ],
                "explanation": explain_gap(
                    gap, state["patient_name"], mode=state["llm_mode"]
                ),
            }
        )
    return {"agent_outputs": outputs}


def _node_blood_pressure_agent(state):
    return _node_specialist(state, "blood_pressure")


def _node_hba1c_agent(state):
    return _node_specialist(state, "hba1c")


def _node_colorectal_screening_agent(state):
    return _node_specialist(state, "colorectal_screening")


def _node_open_referral_agent(state):
    return _node_specialist(state, "open_referral")


def _node_coordinate_explanations(state):
    gaps = state.get("gaps", [])
    gap_ids = [gap["id"] for gap in gaps]
    if len(gap_ids) != len(set(gap_ids)):
        raise ValueError("The deterministic rules returned duplicate gap IDs.")

    outputs_by_id = {}
    for output in state.get("agent_outputs", []):
        gap_id = output.get("gap_id")
        if gap_id not in gap_ids:
            raise ValueError(f"An explanation agent returned unknown gap ID {gap_id}.")
        if gap_id in outputs_by_id:
            raise ValueError(f"More than one explanation agent handled gap {gap_id}.")
        outputs_by_id[gap_id] = output

    missing = [gap_id for gap_id in gap_ids if gap_id not in outputs_by_id]
    if missing:
        raise ValueError(f"No explanation agent handled gaps: {', '.join(missing)}.")
    return {
        "coordinated_explanations": [
            outputs_by_id[gap_id] for gap_id in gap_ids
        ]
    }


def _node_verify_explanations(state):
    explained_gaps = []
    for gap, output in zip(
        state.get("gaps", []), state.get("coordinated_explanations", [])
    ):
        expected_agent = SPECIALIST_AGENT_NAMES.get(gap["type"])
        expected_evidence = [
            f"{evidence['resourceType']}/{evidence['id']}"
            for evidence in gap["evidence"]
        ]
        if (
            output.get("gap_type") != gap["type"]
            or output.get("agent") != expected_agent
        ):
            raise ValueError(f"Explanation agent mismatch for gap {gap['id']}.")
        if output.get("evidence_refs") != expected_evidence:
            raise ValueError(f"Explanation evidence mismatch for gap {gap['id']}.")
        explanation = output.get("explanation")
        if not isinstance(explanation, str) or not explanation.strip():
            raise ValueError(f"Explanation agent returned no text for gap {gap['id']}.")
        explained_gaps.append(
            {
                **gap,
                "explanation": explanation,
                "explanation_agent": expected_agent,
            }
        )
    return {"gaps": explained_gaps}


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
    """Build the graph with parallel care-gap specialists and a coordinator."""
    graph = StateGraph(CareGapState)
    graph.add_node("load_patient", _node_load_patient)
    graph.add_node("detect_gaps", _node_detect_gaps)
    specialist_nodes = {
        "blood_pressure_agent": _node_blood_pressure_agent,
        "hba1c_agent": _node_hba1c_agent,
        "colorectal_screening_agent": _node_colorectal_screening_agent,
        "open_referral_agent": _node_open_referral_agent,
    }
    for name, node in specialist_nodes.items():
        graph.add_node(name, node)
    graph.add_node("coordinate_explanations", _node_coordinate_explanations)
    graph.add_node("verify_explanations", _node_verify_explanations)
    graph.add_node("draft_alert", _node_draft_alert)
    graph.set_entry_point("load_patient")
    graph.add_edge("load_patient", "detect_gaps")
    for name in specialist_nodes:
        graph.add_edge("detect_gaps", name)
    graph.add_edge(list(specialist_nodes), "coordinate_explanations")
    graph.add_edge("coordinate_explanations", "verify_explanations")
    graph.add_edge("verify_explanations", "draft_alert")
    graph.add_edge("draft_alert", END)
    return graph.compile()


_GRAPH = None


def run_agent(patient_path, alerts_dir=None, as_of=None, llm_mode="mock"):
    """Run the agent on one patient bundle.

    Returns the final state dict: patient_name, gaps (each with its
    specialist explanation), and alert (the draft written to pending/,
    or None when the patient has no gaps). `as_of` defaults to the demo
    reference date in gap_rules.DEFAULT_AS_OF so results are stable.
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
