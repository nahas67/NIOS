"""Shared prompt strings for the Trading-R1 verification loop."""

from __future__ import annotations

__all__ = ["GROUNDING_REVIEW_PROMPT", "VERIFY_PROMPT", "WRAP_UP_PROMPT"]

VERIFY_PROMPT = (
    "Recompute every material number from the evidence records before citing it. "
    "If a figure cannot be traced to a record, remove it."
)

GROUNDING_REVIEW_PROMPT = (
    "Review the draft against the evidence ledger. Every stated figure must carry "
    "a resolvable record id and match its recorded value within tolerance."
)

WRAP_UP_PROMPT = (
    "The research budget is exhausted. Write the answer from the evidence already "
    "gathered; state explicitly what could not be verified."
)
