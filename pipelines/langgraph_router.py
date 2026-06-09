"""Open WebUI Pipeline providing the context firewall and LangGraph SSE proxy."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import uuid
from typing import Any, AsyncIterator, Optional

import aiohttp
from pydantic import BaseModel, Field

MAX_CHARS = 20_000
WARNING_TAG = "[WARNING: Payload truncated to preserve VRAM constraints]"

DATA_URL_BASE64_RE = re.compile(
    r"data:[a-z0-9.+-]+/[a-z0-9.+-]+(?:;[a-z0-9.+-]+=[^;,]+)*;base64,[A-Za-z0-9+/=\s]{1024,}",
    flags=re.IGNORECASE,
)
STANDALONE_BASE64_RE = re.compile(
    r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{4096,}={0,2}(?![A-Za-z0-9+/=])"
)
FENCED_MARKER_RE = re.compile(r"^\s*```[^\n]*$|^\s*~~~[^\n]*$", flags=re.MULTILINE)
MARKDOWN_PREFIX_RE = re.compile(
    r"(?m)^\s{0,12}(?:#{1,6}\s+|>{1,6}\s*|(?:[-+*]|\d+[.)])\s+)"
)
MARKDOWN_RULE_RE = re.compile(r"(?m)^\s*(?:[-*_]\s*){3,}$")
INLINE_MARKDOWN_RE = re.compile(r"(?<!\\)(?:\*\*|__|~~|`)")
TABLE_SEPARATOR_RE = re.compile(r"(?m)^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")


def _flatten_markdown(text: str) -> str:
    text = FENCED_MARKER_RE.sub("", text)
    text = TABLE_SEPARATOR_RE.sub("", text)
    text = MARKDOWN_RULE_RE.sub("", text)
    text = MARKDOWN_PREFIX_RE.sub("", text)
    text = INLINE_MARKDOWN_RE.sub("", text)
    text = re.sub(r"(?m)^\s*\|(.+)\|\s*$", lambda match: match.group(1).replace("|", " "), text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def sanitize_text(text: str) -> tuple[str, bool]:
    """Remove VRAM-heavy payloads, flatten Markdown, and enforce MAX_CHARS."""
    violated = False
    sanitized, count = DATA_URL_BASE64_RE.subn("[BASE64 DATA URL REMOVED]", text)
    violated = violated or count > 0
    sanitized, count = STANDALONE_BASE64_RE.subn("[BASE64 CONTENT REMOVED]", sanitized)
    violated = violated or count > 0
    sanitized = _flatten_markdown(sanitized)
    if len(sanitized) > MAX_CHARS:
        sanitized = sanitized[:MAX_CHARS]
        violated = True
    return sanitized, violated


async def _sanitize_value(value: Any) -> tuple[Any, bool]:
    if isinstance(value, str):
        return await sanitize_text(value)
    if isinstance(value, list):
        output = []
        violated = False
        for item in value:
            clean, item_violated = await _sanitize_value(item)
            output.append(clean)
            violated = violated or item_violated
        return output, violated
    if isinstance(value, dict):
        output = {}
        violated = False
        for key, item in value.items():
            clean, item_violated = await _sanitize_value(item)
            output[key] = clean
            violated = violated or item_violated
        return output, violated
    return value, False


async def sanitize_payload(body: dict) -> dict:
    """Return a sanitized payload and append exactly one warning when required."""
    sanitized, violated = await _sanitize_value(copy.deepcopy(body))
    messages = sanitized.get("messages")
    if not isinstance(messages, list):
        messages = []
        sanitized["messages"] = messages
    if violated and not any(
        message.get("role") == "system" and message.get("content") == WARNING_TAG
        for message in messages
        if isinstance(message, dict)
    ):
        messages.append({"role": "system", "content": WARNING_TAG})
    return sanitized


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif isinstance(block.get("content"), str):
                    parts.append(block["content"])
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content)


def _last_user_text(messages: list[dict], fallback: str = "") -> str:
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if message.get("role") == "user":
            return _message_text(message.get("content", ""))
    return fallback


async def iter_sse_events(content: aiohttp.StreamReader) -> AsyncIterator[tuple[str, Any]]:
    """Parse complete SSE frames from arbitrarily chunked aiohttp content."""
    buffer = ""
    async for chunk in content.iter_any():
        buffer += chunk.decode("utf-8", errors="replace").replace("\r\n", "\n")
        while "\n\n" in buffer:
            frame, buffer = buffer.split("\n\n", 1)
            event_name = "message"
            data_lines: list[str] = []
            for line in frame.splitlines():
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            if not data_lines:
                continue
            data_text = "\n".join(data_lines)
            try:
                data = json.loads(data_text)
            except json.JSONDecodeError:
                data = data_text
            yield event_name, data


class Pipeline:
    type = "manifold"
    id = "hardened_langgraph"
    name = "Hardened LangGraph"

    class Valves(BaseModel):
        LANGGRAPH_BASE_URL: str = Field(
            default=os.environ.get("LANGGRAPH_URL", "http://langgraph-orchestrator:8100")
        )
        REQUEST_TIMEOUT_S: float = 180.0
        ENABLE_STREAMING: bool = True

    def __init__(self):
        self.valves = self.Valves()
        self._thread_registry: dict[str, str] = {}
        self._request_lock = asyncio.Lock()

    def pipelines(self) -> list[dict]:
        return [{"id": self.id, "name": self.name}]

    async def on_startup(self):
        return None

    async def on_shutdown(self):
        return None

    async def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        return await sanitize_payload(body)

    async def outlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        return body

    async def _post_json(self, path: str, payload: dict) -> tuple[int, dict]:
        async with self._request_lock:
            async with aiohttp.ClientSession(trust_env=False) as session:
                try:
                    async with session.post(
                        f"{self.valves.LANGGRAPH_BASE_URL}{path}",
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=self.valves.REQUEST_TIMEOUT_S),
                    ) as response:
                        data = await response.json(content_type=None)
                        return response.status, data
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                    return 0, {"error": f"Orchestrator unavailable: {exc}"}

    def _stream(self, payload: dict, user_id: str) -> AsyncIterator[str]:
        async def generate() -> AsyncIterator[str]:
            async with self._request_lock:
                async with aiohttp.ClientSession(trust_env=False) as session:
                    try:
                        async with session.post(
                            f"{self.valves.LANGGRAPH_BASE_URL}/stream",
                            json=payload,
                            timeout=aiohttp.ClientTimeout(total=None, sock_connect=10),
                        ) as response:
                            if response.status != 200:
                                detail = await response.text()
                                yield f"Pipeline Error: HTTP {response.status}: {detail[:500]}"
                                return
                            async for event, data in iter_sse_events(response.content):
                                if event == "graph_start" and isinstance(data, dict):
                                    thread_id = data.get("thread_id")
                                    if thread_id:
                                        self._thread_registry[user_id] = thread_id
                                elif event == "graph_output" and isinstance(data, dict):
                                    yield str(data.get("message", ""))
                                elif event == "fail_safe_termination" and isinstance(data, dict):
                                    messages = data.get("message_array", [])
                                    if messages:
                                        yield str(messages[-1])
                                elif event == "error":
                                    detail = data.get("detail", data) if isinstance(data, dict) else data
                                    yield f"Pipeline Error: {detail}"
                    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                        yield f"Pipeline Connection Error: {exc}"

        return generate()

    async def pipe(
        self,
        user_message: str,
        model_id: str,
        messages: list[dict],
        body: dict,
    ) -> Any:
        sanitized_body = await sanitize_payload({**body, "messages": messages})
        sanitized_messages = sanitized_body.get("messages", [])
        sanitized_input, input_violated = await sanitize_text(user_message)
        if input_violated and not any(
            message.get("role") == "system" and message.get("content") == WARNING_TAG
            for message in sanitized_messages
            if isinstance(message, dict)
        ):
            sanitized_messages.append({"role": "system", "content": WARNING_TAG})
        sanitized_input = _last_user_text(sanitized_messages, sanitized_input)

        user = sanitized_body.get("user", {})
        user_id = user.get("id", "anonymous") if isinstance(user, dict) else "anonymous"
        thread_id = self._thread_registry.get(user_id)
        trace_id = str(uuid.uuid4())
        last_committed = sanitized_body.get("__last_committed_input__")
        is_retroactive_edit = (
            thread_id is not None
            and last_committed is not None
            and last_committed != sanitized_input
        )

        payload = {
            "input": sanitized_input,
            "messages": sanitized_messages,
            "thread_id": thread_id,
            "trace_id": trace_id,
        }

        if is_retroactive_edit:
            status, data = await self._post_json(
                "/interrupt",
                {
                    "thread_id": thread_id,
                    "new_input": sanitized_input,
                    "messages": sanitized_messages,
                    "trace_id": trace_id,
                    "node_index": 0,
                },
            )
            if status == 200:
                new_thread_id = data.get("new_thread_id")
                if new_thread_id:
                    self._thread_registry[user_id] = new_thread_id
                return data.get("response", "Pipeline Error: Empty interrupt response.")
            return f"Pipeline Interrupt Error: {data.get('error', f'HTTP {status}')}"

        if self.valves.ENABLE_STREAMING:
            return self._stream(payload, user_id)

        status, data = await self._post_json("/invoke", payload)
        if status != 200:
            return f"Pipeline Error: {data.get('error', f'HTTP {status}')}"
        if data.get("thread_id"):
            self._thread_registry[user_id] = data["thread_id"]
        return data.get("response", "Pipeline Error: Empty orchestrator response.")
