"""
title: Function - Context - Local Project Context Injector
author: Workspace Catalog
version: 1.0.0
description: Injects concise offline operating context for named local projects.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field


class Valves(BaseModel):
    priority: int = Field(default=20, description="Run after intent routing.")
    max_prompt_chars: int = Field(default=32768, ge=1024, le=131072)


class Filter:
    MARKER = "[workspace-local-project-context:v1]"
    PROJECTS = {
        "Synthetic Sunrise": (
            "Synthetic Sunrise is the localized narrative-simulation project for an approximately "
            "80,000-word branching fictional universe. Preserve faction dynamics, timelines, entity "
            "state, lore continuity, and the logical meaning of maps, charts, and demographic tables."
        ),
        "Ghost Command": (
            "Ghost Command is the offline workspace operations and visual-cortex project. Favor "
            "read-only observation, local service health, CPU/GPU resource awareness, reversible "
            "actions, and explicit operator confirmation before state changes."
        ),
        "Jarvis": (
            "Jarvis is the portable, self-contained local AI ecosystem rooted under C:\\Jarvis. "
            "Prefer isolated dependencies, version checks, minimal host-profile or registry changes, "
            "and designs that remain easy to back up, migrate, and archive."
        ),
    }

    def __init__(self) -> None:
        self.valves = Valves()
        self._patterns = {
            name: re.compile(rf"(?<!\w){re.escape(name)}(?!\w)", re.IGNORECASE)
            for name in self.PROJECTS
        }

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

    def inlet(self, body: dict, __user__: dict | None = None) -> dict:
        try:
            messages = body.get("messages")
            if not isinstance(messages, list):
                return body
            if any(
                isinstance(message, dict)
                and message.get("role") == "system"
                and self.MARKER in self._content_text(message.get("content"))
                for message in messages
            ):
                return body

            latest_prompt = ""
            for message in reversed(messages):
                if isinstance(message, dict) and message.get("role") == "user":
                    latest_prompt = self._content_text(message.get("content"))
                    break
            latest_prompt = latest_prompt[: self.valves.max_prompt_chars]
            matches = [name for name, pattern in self._patterns.items() if pattern.search(latest_prompt)]
            if not matches:
                return body

            context = self.MARKER + "\n" + "\n".join(
                f"{name}: {self.PROJECTS[name]}" for name in matches
            )
            body["messages"] = [{"role": "system", "content": context}, *messages]
            return body
        except Exception:
            return body
