"""
title: Function - Routing - Dynamic Intent Router
author: Workspace Catalog
version: 1.0.0
description: Routes attachment and development requests to specialized local presets.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field


class Valves(BaseModel):
    priority: int = Field(default=10, description="Run before other inlet filters.")
    max_prompt_chars: int = Field(default=32768, ge=1024, le=131072)


class Filter:
    def __init__(self) -> None:
        self.valves = Valves()
        self._development_pattern = re.compile(
            r"\b(?:code|coding|script|debug|debugging|program|programming|"
            r"python|javascript|typescript|sql|csv|spreadsheet|data\s+analysis|analy[sz]e\s+data)\b",
            re.IGNORECASE,
        )

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

    @staticmethod
    def _has_attachments(body: dict[str, Any], messages: list[Any]) -> bool:
        if body.get("files"):
            return True
        for message in messages:
            if not isinstance(message, dict):
                continue
            if message.get("files"):
                return True
            content = message.get("content")
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    item_type = str(item.get("type", "")).lower()
                    if item_type in {"image", "image_url", "input_image", "file", "input_file"}:
                        return True
        return False

    def inlet(self, body: dict, __user__: dict | None = None) -> dict:
        try:
            messages = body.get("messages")
            if not isinstance(messages, list):
                return body

            target_model = None
            if self._has_attachments(body, messages):
                target_model = "qwen257b"
            else:
                latest_prompt = ""
                for message in reversed(messages):
                    if isinstance(message, dict) and message.get("role") == "user":
                        latest_prompt = self._content_text(message.get("content"))
                        break
                if self._development_pattern.search(latest_prompt[: self.valves.max_prompt_chars]):
                    target_model = "-data-analyst--developer"

            if target_model:
                body["model"] = target_model
            return body
        except Exception:
            return body
