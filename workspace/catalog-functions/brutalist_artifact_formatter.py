"""
title: Function - Formatting - Brutalist Artifact Formatter
author: Workspace Catalog
version: 1.0.0
description: Marks code, tables, and blockquotes for scoped OLED-black and cyan rendering.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field


class Valves(BaseModel):
    priority: int = Field(default=90, description="Run late in the outlet filter chain.")
    max_response_chars: int = Field(default=200000, ge=4096, le=1000000)


class Filter:
    MARKER_TEMPLATE = '<div class="brutalist-artifact-marker" data-artifact="{kind}"></div>\n'
    MARKER_CHECK = "brutalist-artifact-marker"
    FENCED_CODE = re.compile(r"(?ms)^```[^\n]*\n.*?^```[ \t]*$")
    BLOCKQUOTE = re.compile(r"(?m)^(?:>[^\n]*(?:\n|$))+")
    TABLE = re.compile(
        r"(?m)^(?P<header>[^\n|]*\|[^\n]*\n)"
        r"(?P<separator>\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*\n)"
        r"(?P<rows>(?:[^\n|]*\|[^\n]*(?:\n|$))*)"
    )

    def __init__(self) -> None:
        self.valves = Valves()

    def _mark(self, content: str) -> str:
        if self.MARKER_CHECK in content or len(content) > self.valves.max_response_chars:
            return content
        content = self.FENCED_CODE.sub(
            lambda match: self.MARKER_TEMPLATE.format(kind="code") + match.group(0),
            content,
        )
        content = self.TABLE.sub(
            lambda match: self.MARKER_TEMPLATE.format(kind="table") + match.group(0),
            content,
        )
        return self.BLOCKQUOTE.sub(
            lambda match: self.MARKER_TEMPLATE.format(kind="blockquote") + match.group(0),
            content,
        )

    def outlet(self, body: dict, __user__: dict | None = None) -> dict:
        try:
            messages = body.get("messages")
            if not isinstance(messages, list):
                return body
            target_id = body.get("id")
            target = next(
                (
                    message
                    for message in messages
                    if isinstance(message, dict)
                    and message.get("role") == "assistant"
                    and (target_id is None or message.get("id") == target_id)
                ),
                None,
            )
            if target is None:
                return body
            content = target.get("content")
            if isinstance(content, str):
                target["content"] = self._mark(content)
            return body
        except Exception:
            return body
