"""
title: Request User Clarification
author: Local Operations
version: 1.0.0
description: |
  Native clarification tool for the web-search workflow. It returns a
  user-facing question directly and does not depend on an outlet filter,
  marker string, or model-output scraping.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Trigger conditions (reference for model system-prompt documentation)
# ---------------------------------------------------------------------------

CLARIFICATION_TRIGGERS = [
    "Completely missing technical scope — cannot determine what domain or product the query refers to.",
    "Undefined comparative baseline — 'compare X' without a defined second entity or dimension.",
    "Unresolvable noun reference — pronoun or abbreviation with no prior context to resolve it.",
]


class Tools:
    class Valves(BaseModel):
        max_question_chars: int = Field(
            default=500,
            ge=50,
            le=2000,
            description="Hard character limit on the clarifying question.",
        )

    def __init__(self) -> None:
        self.valves = self.Valves()

    def request_user_clarification(
        self,
        question: str,
        ambiguity_type: str = "unresolvable_scope",
    ) -> str:
        """
        Return a targeted user-facing clarification question.

        Invoke this tool when — and ONLY when — the user's query cannot be
        resolved into a concrete research target without a direct answer to
        a specific clarifying question.

        Parameters
        ----------
        question : str
            The exact, targeted clarifying question to surface to the user.
            Must be a single specific question, not a list of options.

        ambiguity_type : str
            Coarse category of ambiguity detected.  One of:
              - missing_scope      : no technical domain or product identified
              - undefined_baseline : comparative query missing second entity
              - unresolvable_noun  : pronoun or abbreviation with no context
              - other              : any structural ambiguity not covered above

        Returns
        -------
        str
            Markdown rendered directly by the assistant.
        """
        question = question.strip()[: self.valves.max_question_chars]
        if not question:
            question = "Could you clarify what you are asking about?"

        category = ambiguity_type.strip() or "other"
        return f"**Before I search, I need one clarification:**\n\n> {question}\n\nAmbiguity: `{category}`"
