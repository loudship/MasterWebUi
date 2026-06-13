"""
title: Function - Control - Agentic ReAct Loop Interceptor
author: Workspace Catalog
version: 1.0.0
description: |
  Outlet filter that intercepts clarification halt signals emitted by the
  request_user_clarification pseudo-tool and surfaces the question to the
  chat interface, suspending further background search activity.

  Detection contract
  ------------------
  The filter scans the last assistant message in the outlet body for a JSON
  block containing the key __clarification_request__ = true.  When found:

    1. The assistant bubble content is replaced with a plain-text rendering
       of the targeted question (prefixed with a marker so the inlet filter
       can detect a prior clarification round).
    2. The body is returned immediately, preventing any further tool dispatch.

  The inlet hook additionally injects the clarification marker into the system
  prompt so the model knows to re-enter the ReAct loop after the user replies,
  rather than firing another clarification halt.

  Priority
  --------
  priority = 50 — runs after dynamic_intent_router (10) and
  local_project_context_injector (20) but before brutalist_artifact_formatter (90).
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CLARIFICATION_KEY = "__clarification_request__"
_CLARIFICATION_MARKER = "[agentic-clarification-pending:v1]"

# Matches a JSON block anywhere in the assistant message that contains the
# clarification signal key.
_JSON_BLOCK = re.compile(
    r"\{[^{}]*\"" + re.escape(_CLARIFICATION_KEY) + r"\"[^{}]*\}",
    re.DOTALL,
)


class Valves(BaseModel):
    priority: int = Field(
        default=50,
        description="Run after intent/context filters, before brutalist formatter.",
    )
    max_question_chars: int = Field(
        default=800,
        ge=50,
        le=2000,
        description="Hard cap on the clarifying question surfaced to the user.",
    )


class Filter:
    MARKER = _CLARIFICATION_MARKER

    def __init__(self) -> None:
        self.valves = Valves()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _content_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict) and item.get("type") in {"text", "input_text"}
            )
        return ""

    def _extract_clarification(self, text: str) -> dict | None:
        """
        Scan text for a JSON clarification signal block.

        Returns the parsed dict if found with __clarification_request__ == True,
        otherwise None.
        """
        for match in _JSON_BLOCK.finditer(text):
            try:
                data = json.loads(match.group(0))
                if data.get(_CLARIFICATION_KEY) is True:
                    return data
            except (json.JSONDecodeError, ValueError):
                continue
        return None

    # ------------------------------------------------------------------
    # Inlet: re-entry guard after user replies to a clarification
    # ------------------------------------------------------------------

    def inlet(self, body: dict, __user__: dict | None = None) -> dict:
        """
        Inject a re-entry guard into the system prompt when a prior
        clarification round is detected in the conversation history.

        This tells the model: "the user has answered your clarifying question,
        now execute the ReAct loop fully without halting again."
        """
        try:
            messages = body.get("messages")
            if not isinstance(messages, list):
                return body

            # Check if any prior assistant message contains our clarification marker
            has_prior_clarification = any(
                isinstance(m, dict)
                and m.get("role") == "assistant"
                and self.MARKER in self._content_text(m.get("content"))
                for m in messages
            )
            if not has_prior_clarification:
                return body

            # Inject re-entry notice into the system prompt
            re_entry_note = (
                f"{self.MARKER}-resolved\n"
                "The user has answered the clarifying question above. "
                "Re-enter the ReAct execution loop immediately with the full query "
                "context. Do NOT invoke request_user_clarification again for this thread."
            )
            body["messages"] = [{"role": "system", "content": re_entry_note}, *messages]
        except Exception:
            pass
        return body

    # ------------------------------------------------------------------
    # Outlet: intercept clarification signals
    # ------------------------------------------------------------------

    def outlet(self, body: dict, __user__: dict | None = None) -> dict:
        """
        Detect __clarification_request__ signals in the last assistant message
        and replace the bubble with a clean, user-facing question.

        If no signal is found, the body is returned unmodified.
        """
        try:
            messages = body.get("messages")
            if not isinstance(messages, list):
                return body

            target_id = body.get("id")
            target = next(
                (
                    m
                    for m in messages
                    if isinstance(m, dict)
                    and m.get("role") == "assistant"
                    and (target_id is None or m.get("id") == target_id)
                ),
                None,
            )
            if target is None:
                return body

            content = self._content_text(target.get("content", ""))
            signal = self._extract_clarification(content)
            if signal is None:
                return body

            # ── Clarification signal detected ─────────────────────────────
            question = str(signal.get("question", "Could you clarify your request?")).strip()
            question = question[: self.valves.max_question_chars]
            ambiguity_type = str(signal.get("ambiguity_type", "other"))

            # Build user-facing content with the halting marker embedded
            # (the marker is invisible to the user but readable by the inlet filter)
            clarification_content = (
                f"{self.MARKER}\n\n"
                f"**Before I search**, I need one clarification:\n\n"
                f"> {question}\n\n"
                f"*(Ambiguity detected: {ambiguity_type})*"
            )
            target["content"] = clarification_content

        except Exception:
            pass

        return body
