"""Explanation adapter — turns a found gap into plain language.

GROUNDING RULE (the same discipline as the agent-evaluation harness): the
LLM NEVER invents gaps. It only explains gaps the deterministic rules in
gap_rules.py already found, using only the evidence those rules attached.
If the rules found nothing, there is nothing to explain.

Two modes, chosen by the CARE_GAP_LLM environment variable:
  * "mock"   (default): template-based explanations built from the gap's
             own evidence. No network, no API keys, no model calls.
  * "claude": shells out to the `claude` CLI (headless `claude -p`) to draft
             the explanation. Needs Claude Code installed and signed in on
             your Mac — no API key is stored anywhere in this project. If
             the CLI is missing, you get a clear error instead of a crash.
"""
from __future__ import annotations

import subprocess

MOCK_TEMPLATES = {
    "blood_pressure": (
        "{name}'s blood pressure is overdue for measurement. {detail} "
        "A blood pressure check at the next visit would close this gap."
    ),
    "hba1c": (
        "{name} has type 2 diabetes and the HbA1c check is overdue. "
        "{detail} An HbA1c lab order would close this gap."
    ),
    "colorectal_screening": (
        "{name} is due for colorectal cancer screening. {detail} "
        "Scheduling a screening (colonoscopy or stool FIT test) would "
        "close this gap."
    ),
    "open_referral": (
        "{name} has a referral that is still open. {detail} Following up "
        "with the referred service — or confirming the visit happened "
        "elsewhere — would close this gap."
    ),
}


def explain_gap(gap, patient_name, mode="mock"):
    """Return a plain-language explanation for one gap dict."""
    if mode == "claude":
        return _explain_with_claude(gap, patient_name)
    if mode == "mock":
        template = MOCK_TEMPLATES.get(
            gap["type"],
            "{name} has an open care gap: {detail}",
        )
        return template.format(name=patient_name, detail=gap["detail"])
    raise ValueError(f'Unknown LLM mode "{mode}" — use "mock" or "claude".')


def _explain_with_claude(gap, patient_name):
    """Draft the explanation with the Claude Code CLI.

    The prompt hands the model ONLY the gap the rules found, with its
    evidence, and instructs it to explain that gap and nothing else.
    """
    prompt = (
        "You are drafting a short note for a care coordinator about one "
        "care gap found by a rules engine. Explain the gap in plain "
        "language in two or three sentences. Use ONLY the facts below. "
        "Do not invent, add, or suggest any other gap, condition, or test.\n\n"
        f"Patient: {patient_name} (fictional synthetic record)\n"
        f"Gap: {gap['title']}\n"
        f"Severity: {gap['severity']}\n"
        f"Facts: {gap['detail']}\n\n"
        "Reply with the note only — no headings, no bullet points."
    )
    try:
        completed = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "The `claude` CLI is not installed or not on PATH. Install "
            "Claude Code (https://code.claude.com) and make sure "
            "`claude --version` works in Terminal, or run in mock mode "
            "(the default — just unset CARE_GAP_LLM)."
        ) from None
    except subprocess.TimeoutExpired:
        raise RuntimeError("The `claude` run timed out after 120 seconds.") from None
    if completed.returncode != 0:
        raise RuntimeError(
            f"`claude -p` exited with code {completed.returncode}: "
            f"{completed.stderr.strip()}"
        )
    return completed.stdout.strip()
