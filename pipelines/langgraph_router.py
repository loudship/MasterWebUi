"""Open WebUI Pipeline providing the context firewall and LangGraph SSE proxy."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import time
import uuid
from collections import OrderedDict
from typing import Any, AsyncIterator, Optional

import aiohttp
from pydantic import BaseModel, Field

MAX_CHARS = 20_000
WARNING_TAG = "[WARNING: Payload truncated to preserve VRAM constraints]"

# SSE reassembly buffer cap: a frame larger than this indicates a broken
# upstream, not a legitimate event — abort instead of growing without bound.
MAX_SSE_BUFFER_CHARS = 1_048_576

# Conversation-state registry bounds (entries map chat -> LangGraph thread).
THREAD_REGISTRY_MAX_ENTRIES = 1_000
THREAD_REGISTRY_TTL_S = 24 * 3600.0

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


class SSEBufferOverflow(RuntimeError):
    """Raised when the upstream emits an SSE frame larger than the cap."""


class ThreadRegistry:
    """Bounded, TTL-evicting map of conversation key -> LangGraph thread_id.

    Replaces the unbounded per-user dict that leaked memory and cross-wired
    two concurrent chats from the same user onto one graph thread.
    """

    def __init__(
        self,
        max_entries: int = THREAD_REGISTRY_MAX_ENTRIES,
        ttl_s: float = THREAD_REGISTRY_TTL_S,
    ) -> None:
        self._entries: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._max_entries = max_entries
        self._ttl_s = ttl_s

    def _prune(self) -> None:
        now = time.monotonic()
        expired = [key for key, (_tid, expires) in self._entries.items() if expires <= now]
        for key in expired:
            del self._entries[key]
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def get(self, key: str) -> Optional[str]:
        self._prune()
        entry = self._entries.get(key)
        if entry is None:
            return None
        self._entries.move_to_end(key)
        return entry[0]

    def set(self, key: str, thread_id: str) -> None:
        self._entries[key] = (thread_id, time.monotonic() + self._ttl_s)
        self._entries.move_to_end(key)
        self._prune()

    def __len__(self) -> int:
        self._prune()
        return len(self._entries)


def conversation_key(body: dict, fallback_user_id: str) -> str:
    """Scope graph threads to a chat, not a user, so concurrent chats from the
    same user never share LangGraph state."""
    metadata = body.get("metadata")
    if isinstance(metadata, dict):
        chat_id = metadata.get("chat_id")
        if chat_id:
            return f"chat:{chat_id}"
    chat_id = body.get("chat_id")
    if chat_id:
        return f"chat:{chat_id}"
    return f"user:{fallback_user_id}"


async def iter_sse_events(content: aiohttp.StreamReader) -> AsyncIterator[tuple[str, Any]]:
    """Parse complete SSE frames from arbitrarily chunked aiohttp content."""
    buffer = ""
    async for chunk in content.iter_any():
        buffer += chunk.decode("utf-8", errors="replace").replace("\r\n", "\n")
        if len(buffer) > MAX_SSE_BUFFER_CHARS:
            raise SSEBufferOverflow(
                f"SSE frame exceeded {MAX_SSE_BUFFER_CHARS} characters without terminating."
            )
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
        # Maximum inter-event silence tolerated on the orchestrator SSE stream.
        STREAM_IDLE_TIMEOUT_S: float = 120.0
        ENABLE_STREAMING: bool = True

    def __init__(self):
        self.valves = self.Valves()
        self._thread_registry = ThreadRegistry()
        self._session: Optional[aiohttp.ClientSession] = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(trust_env=False)
        return self._session

    def pipelines(self) -> list[dict]:
        return [{"id": self.id, "name": self.name}]

    async def on_startup(self):
        return None

    async def on_shutdown(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()
        return None

    async def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        return await sanitize_payload(body)

    async def outlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        return body

    async def _post_json(self, path: str, payload: dict) -> tuple[int, dict]:
        # No global lock: GPU serialization lives in the inference gateway.
        # Serializing here only adds head-of-line blocking across users.
        session = self._get_session()
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

    def _stream(self, payload: dict, conversation: str) -> AsyncIterator[str]:
        async def generate() -> AsyncIterator[str]:
            session = self._get_session()
            try:
                async with session.post(
                    f"{self.valves.LANGGRAPH_BASE_URL}/stream",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(
                        total=None,
                        sock_connect=10,
                        sock_read=self.valves.STREAM_IDLE_TIMEOUT_S,
                    ),
                ) as response:
                    if response.status != 200:
                        detail = await response.text()
                        yield f"Pipeline Error: HTTP {response.status}: {detail[:500]}"
                        return
                    async for event, data in iter_sse_events(response.content):
                        if event == "graph_start" and isinstance(data, dict):
                            thread_id = data.get("thread_id")
                            if thread_id:
                                self._thread_registry.set(conversation, thread_id)
                        elif event == "graph_output" and isinstance(data, dict):
                            yield str(data.get("message", ""))
                        elif event == "fail_safe_termination" and isinstance(data, dict):
                            messages = data.get("message_array", [])
                            if messages:
                                yield str(messages[-1])
                        elif event == "error":
                            detail = data.get("detail", data) if isinstance(data, dict) else data
                            yield f"Pipeline Error: {detail}"
            except SSEBufferOverflow as exc:
                yield f"Pipeline Error: {exc}"
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
        conversation = conversation_key(sanitized_body, user_id)
        thread_id = self._thread_registry.get(conversation)
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
                    self._thread_registry.set(conversation, new_thread_id)
                return data.get("response", "Pipeline Error: Empty interrupt response.")
            return f"Pipeline Interrupt Error: {data.get('error', f'HTTP {status}')}"

        if self.valves.ENABLE_STREAMING:
            return self._stream(payload, conversation)

        status, data = await self._post_json("/invoke", payload)
        if status != 200:
            return f"Pipeline Error: {data.get('error', f'HTTP {status}')}"
        if data.get("thread_id"):
            self._thread_registry.set(conversation, data["thread_id"])
        return data.get("response", "Pipeline Error: Empty orchestrator response.")
