"""Run inside inference-gateway:local to validate its runtime-only contracts."""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

import aiohttp

sys.path.insert(0, "/app/gateway")
os.environ.setdefault("POSTGRES_OPS_URL", "postgresql://unused/unused")
os.environ.setdefault("MODEL_ALLOWLIST", "approved-model")

import inference_gateway as gateway


class Tracker:
    def __init__(self):
        self.active = 0
        self.max_active = 0


class Content:
    async def iter_any(self):
        yield b"data: one\n\n"
        yield b"data: two\n\n"


class Upstream:
    status = 200
    headers = {"Content-Type": "application/json"}
    content = Content()

    def __init__(self, tracker: Tracker, fail: bool = False):
        self.tracker = tracker
        self.fail = fail

    async def __aenter__(self):
        if self.fail:
            raise aiohttp.ClientConnectionError("offline")
        self.tracker.active += 1
        self.tracker.max_active = max(self.tracker.max_active, self.tracker.active)
        await asyncio.sleep(0.03)
        return self

    async def __aexit__(self, *_):
        self.tracker.active -= 1

    async def read(self):
        return b'{"ok":true}'

    async def text(self):
        return "failure"


class Http:
    def __init__(self, tracker: Tracker, fail: bool = False):
        self.tracker = tracker
        self.fail = fail

    def post(self, *_args, **_kwargs):
        return Upstream(self.tracker, self.fail)


class OpsPool:
    def __init__(self):
        self.records = []

    async def execute(self, _sql, *args):
        self.records.append(args)


class Request:
    def __init__(self, http: Http, ops: OpsPool, *, stream: bool = False):
        self.app = SimpleNamespace(state=SimpleNamespace(http=http, ops_pool=ops))
        self.headers = {"X-Trace-Id": "trace-contract"}
        self.payload = {"model": "approved-model", "stream": stream}

    async def json(self):
        return self.payload


async def main() -> None:
    gateway.MODEL_ALLOWLIST = {"approved-model"}
    inventory = gateway._filter_model_inventory(
        {"data": [{"id": "approved-model"}, {"id": "blocked-model"}]}
    )
    assert inventory["data"] == [{"id": "approved-model"}]

    tracker = Tracker()
    ops = OpsPool()
    requests = [Request(Http(tracker), ops), Request(Http(tracker), ops)]
    await asyncio.gather(
        *(gateway._proxy_json(request, "/v1/chat/completions") for request in requests)
    )
    assert tracker.max_active == 1
    assert len(ops.records) == 2
    assert all(record[0] == "trace-contract" for record in ops.records)

    stream_tracker = Tracker()
    stream_ops = OpsPool()
    stream_request = Request(Http(stream_tracker), stream_ops, stream=True)
    stream_response = await gateway._proxy_stream(
        stream_request,
        "/v1/chat/completions",
        stream_request.payload,
    )
    chunks = [chunk async for chunk in stream_response.body_iterator]
    assert chunks == [b"data: one\n\n", b"data: two\n\n"]
    assert len(stream_ops.records) == 1

    failure_ops = OpsPool()
    failure_request = Request(Http(Tracker(), fail=True), failure_ops)
    try:
        await gateway._proxy_json(failure_request, "/v1/chat/completions")
    except gateway.HTTPException as exc:
        assert exc.status_code == 502
    else:
        raise AssertionError("Upstream failure did not produce HTTP 502.")
    assert len(failure_ops.records) == 1
    assert failure_ops.records[0][-1] == "offline"

    print("gateway-container-contract-ok")


if __name__ == "__main__":
    asyncio.run(main())
