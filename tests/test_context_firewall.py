import asyncio

import pytest

from pipelines.langgraph_router import (
    MAX_CHARS,
    WARNING_TAG,
    iter_sse_events,
    sanitize_payload,
    sanitize_text,
)


@pytest.mark.asyncio
async def test_data_url_base64_is_removed_and_warned():
    body = {
        "messages": [
            {"role": "user", "content": "data:image/png;base64," + ("A" * 1024)}
        ]
    }
    sanitized = await sanitize_payload(body)
    assert "BASE64 DATA URL REMOVED" in sanitized["messages"][0]["content"]
    assert sanitized["messages"].count({"role": "system", "content": WARNING_TAG}) == 1


@pytest.mark.asyncio
async def test_standalone_base64_is_removed():
    text, violated = await sanitize_text("before " + ("A" * 4096) + " after")
    assert violated is True
    assert text == "before [BASE64 CONTENT REMOVED] after"


@pytest.mark.asyncio
async def test_exact_boundary_is_preserved_without_warning():
    exact_prose = ("a " * 9_999) + "ab"
    body = {"messages": [{"role": "user", "content": exact_prose}]}
    sanitized = await sanitize_payload(body)
    assert len(sanitized["messages"][0]["content"]) == MAX_CHARS
    assert all(message.get("content") != WARNING_TAG for message in sanitized["messages"])


@pytest.mark.asyncio
async def test_over_boundary_is_truncated_and_warning_is_idempotent():
    over_limit_prose = ("a " * 9_999) + "abc"
    body = {
        "messages": [
            {"role": "user", "content": over_limit_prose},
            {"role": "system", "content": WARNING_TAG},
        ]
    }
    sanitized = await sanitize_payload(await sanitize_payload(body))
    assert len(sanitized["messages"][0]["content"]) == MAX_CHARS
    assert sanitized["messages"].count({"role": "system", "content": WARNING_TAG}) == 1


@pytest.mark.asyncio
async def test_nested_multimodal_strings_are_sanitized():
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "# Heading\n> quoted"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + ("A" * 1024)}},
                ],
            }
        ]
    }
    sanitized = await sanitize_payload(body)
    assert sanitized["messages"][0]["content"][0]["text"] == "Heading\nquoted"
    assert "BASE64 DATA URL REMOVED" in sanitized["messages"][0]["content"][1]["image_url"]["url"]


@pytest.mark.asyncio
async def test_non_list_messages_fail_closed_to_a_warning_list():
    sanitized = await sanitize_payload({"messages": "A" * 4096})
    assert sanitized["messages"] == [{"role": "system", "content": WARNING_TAG}]


class _FakeSSEContent:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_any(self):
        for chunk in self.chunks:
            await asyncio.sleep(0)
            yield chunk


@pytest.mark.asyncio
async def test_sse_parser_waits_for_complete_frames():
    content = _FakeSSEContent(
        [
            b"event: graph_out",
            b"put\ndata: {\"message\": \"ok\"}\n\n",
            b"data: [DONE]\n\n",
        ]
    )
    events = [event async for event in iter_sse_events(content)]
    assert events == [
        ("graph_output", {"message": "ok"}),
        ("message", "[DONE]"),
    ]
