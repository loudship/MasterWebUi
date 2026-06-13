"""
title: Request User Clarification
author: Local Operations
version: 1.0.0
description: |
  Semantic clarification pseudo-tool for the ReAct agentic web-search loop.

  The model invokes this tool ONLY when the incoming user query contains
  severe semantic ambiguity: a completely missing technical scope, an
  undefined comparative baseline, or an unresolvable noun reference.

  Invoking this tool emits a structured JSON clarification signal that is
  intercepted by the agentic_react_loop outlet filter, which halts further
  background search activity and surfaces the targeted question to the user.

  Schema contract
  ---------------
  The tool emits a top-level JSON object with the key __clarification_request__
  set to true and a human-readable question in the question field.  The outlet
  filter pattern-matches on this key to distinguish clarification halts from
  normal research results.

  IMPORTANT: This tool must never be invoked for well-formed queries, even
  vague or broad ones.  It is reserved for structurally unresolvable ambiguity.
"""

from __future__ import annotations

import json

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
        Emit a structured clarification halt signal.

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
            JSON-serialized clarification signal consumed by the
            agentic_react_loop outlet filter.
        """
        question = question.strip()[: self.valves.max_question_chars]
        if not question:
            question = "Could you clarify what you are asking about?"

        return json.dumps(
            {
                "__clarification_request__": True,
                "question": question,
                "ambiguity_type": ambiguity_type,
            },
            ensure_ascii=False,
        )
