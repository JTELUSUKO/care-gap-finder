"""Care-Gap Finder Agent — the `care_gap` package.

Modules:
  fhir_loader — read and query a synthetic FHIR R4 patient bundle
  gap_rules   — deterministic care-gap rules (the agent's "reason" core)
  llm         — explanation adapter (mock by default; `claude` CLI optional)
  agent       — LangGraph graph: load -> detect -> explain -> draft alert
  alerts      — the human-in-the-loop approval queue
"""
from __future__ import annotations
