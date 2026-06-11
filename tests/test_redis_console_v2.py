import asyncio
import json
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "services" / "redis-ui"
sys.path.insert(0, str(SERVICE))

from redis_console.diagnostics import run_full_debug
from redis_console.store import ReportStore, markdown_report, sanitize


class FakePipeline:
    def __init__(self, owner):
        self.owner = owner
        self.pending = []

    def type(self, key):
        self.pending.append(("type", key))

    def ttl(self, key):
        self.pending.append(("ttl", key))

    def memory_usage(self, key):
        self.pending.append(("memory_usage", key))

    async def execute(self):
        self.owner.calls.extend(name for name, _ in self.pending)
        values = {"type": "string", "ttl": 60, "memory_usage": 128}
        return [values[name] for name, _ in self.pending]


class FakeRedis:
    def __init__(self, db, calls):
        self.db = db
        self.calls = calls

    def _called(self, name):
        self.calls.append(name)

    async def ping(self):
        self._called("ping")
        return True

    async def info(self, section):
        self._called("info")
        return {
            "server": {"redis_version": "7.4.0", "redis_mode": "standalone", "uptime_in_seconds": 123},
            "memory": {
                "used_memory_human": "1M",
                "used_memory": 1_000_000,
                "used_memory_peak": 1_100_000,
                "mem_fragmentation_ratio": 1.2,
                "maxmemory": 0,
                "maxmemory_policy": "noeviction",
            },
            "stats": {
                "rejected_connections": 0,
                "instantaneous_input_kbps": 1,
                "instantaneous_output_kbps": 1,
                "total_error_replies": 0,
                "instantaneous_ops_per_sec": 1,
                "total_commands_processed": 50,
            },
            "clients": {"connected_clients": 1, "blocked_clients": 0},
            "persistence": {"rdb_last_bgsave_status": "ok", "aof_enabled": 0},
            "keyspace": {},
            "commandstats": {"cmdstat_get": {"calls": 4, "usec_per_call": 1, "failed_calls": 0}},
        }[section]

    async def module_list(self):
        self._called("module_list")
        return []

    async def config_get(self, pattern):
        self._called("config_get")
        return {"protected-mode": "no", "bind": "* -::*", "requirepass": "", "aclfile": ""}

    async def acl_list(self):
        self._called("acl_list")
        return ["user default on nopass ~* &* +@all"]

    async def slowlog_get(self, count):
        self._called("slowlog_get")
        return []

    async def scan(self, cursor=0, count=250):
        self._called("scan")
        return (0, ["private:key:name"]) if self.db == 0 else (0, [])

    def pipeline(self, transaction=False):
        self._called("pipeline")
        return FakePipeline(self)

    async def aclose(self):
        self._called("aclose")


def test_full_debug_is_read_only_metadata_only_and_classifies_security_warning():
    calls = []
    run = asyncio.run(run_full_debug(lambda db=0: FakeRedis(db, calls)))
    forbidden = {"set", "delete", "flushall", "flushdb", "expire", "persist", "config_set"}
    assert not forbidden.intersection(calls)
    encoded = json.dumps(run)
    assert "private:key:name" not in encoded
    assert run["status"] == "warning"
    security = next(item for item in run["checks"] if item["category"] == "Security")
    assert security["status"] == "warning"
    assert security["recommendation"]


def test_report_store_persists_prunes_redacts_and_exports(tmp_path):
    path = tmp_path / "reports.db"
    store = ReportStore(str(path), retention_days=30, max_reports=2)
    for index in range(3):
        run = {
            "run_id": f"run-{index}",
            "status": "pass",
            "started_at": time.time() + index,
            "completed_at": time.time() + index,
            "summary": {"pass": 1, "warning": 0, "fail": 0, "total": 1},
            "checks": [{
                "category": "Security",
                "name": "Redaction",
                "status": "pass",
                "summary": "Done",
                "recommendation": "",
                "evidence": {"password": "secret-value", "authentication_configured": True},
            }],
        }
        store.save(run)

    reopened = ReportStore(str(path), retention_days=30, max_reports=2)
    assert [item["run_id"] for item in reopened.list()] == ["run-2", "run-1"]
    saved = reopened.get("run-2")
    assert saved["checks"][0]["evidence"]["password"] == "[REDACTED]"
    assert saved["checks"][0]["evidence"]["authentication_configured"] is True
    report = markdown_report(saved)
    assert "# Redis Full Debug Report run-2" in report
    assert "secret-value" not in report


def test_payload_cap_and_modular_ui_contracts():
    capped = sanitize({"data": "x" * 250_000})
    assert capped["truncated"] is True

    html = (SERVICE / "redis_ui" / "index.html").read_text(encoding="utf-8")
    js = (SERVICE / "redis_ui" / "app.js").read_text(encoding="utf-8")
    dockerfile = (SERVICE / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for label in ("Dashboard", "Browse Keys", "Manage Keys", "Health & Diagnostics", "Report History", "Run Full Debug"):
        assert label in html
    assert 'role="tooltip"' in html
    assert "data-tip=" in html
    assert "EventSource" in js
    assert "60000" in js
    assert "COPY services/redis-ui/redis_console ./redis_console" in dockerfile
    assert "redis-ui-data:/app/data" in compose
