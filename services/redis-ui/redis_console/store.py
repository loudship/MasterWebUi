from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


SENSITIVE = re.compile(
    r"(password|passwd|passphrase|requirepass|secret|token|credential|api.?key|authorization)",
    re.IGNORECASE,
)
MAX_ARTIFACT_CHARS = 200_000


def sanitize(value: Any, max_chars: int = MAX_ARTIFACT_CHARS) -> Any:
    def clean(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                str(key): "[REDACTED]" if SENSITIVE.search(str(key)) else clean(child)
                for key, child in item.items()
            }
        if isinstance(item, (list, tuple, set)):
            return [clean(child) for child in item]
        return item

    result = clean(value)
    encoded = json.dumps(result, ensure_ascii=True, default=str)
    if len(encoded) <= max_chars:
        return result
    return {"truncated": True, "original_chars": len(encoded), "preview": encoded[:max_chars]}


class ReportStore:
    def __init__(self, path: str, retention_days: int = 30, max_reports: int = 200):
        self.path = Path(path)
        self.retention_days = max(1, retention_days)
        self.max_reports = max(1, max_reports)
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS debug_runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    completed_at REAL,
                    summary_json TEXT NOT NULL,
                    checks_json TEXT NOT NULL
                )
                """
            )

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(sanitize(value), ensure_ascii=True, default=str)

    def save(self, run: dict[str, Any]) -> None:
        with self.lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO debug_runs
                (run_id, status, started_at, completed_at, summary_json, checks_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run["run_id"],
                    run["status"],
                    run["started_at"],
                    run.get("completed_at"),
                    self._dump(run.get("summary", {})),
                    self._dump(run.get("checks", [])),
                ),
            )
        self.prune()

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT run_id,status,started_at,completed_at,summary_json FROM debug_runs ORDER BY started_at DESC LIMIT ?",
                (min(max(1, limit), 200),),
            ).fetchall()
        return [
            {
                "run_id": row["run_id"],
                "status": row["status"],
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "summary": json.loads(row["summary_json"]),
            }
            for row in rows
        ]

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self.lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM debug_runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            return None
        return {
            "run_id": row["run_id"],
            "status": row["status"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "summary": json.loads(row["summary_json"]),
            "checks": json.loads(row["checks_json"]),
        }

    def prune(self) -> None:
        cutoff = time.time() - self.retention_days * 86400
        with self.lock, self._connect() as connection:
            connection.execute("DELETE FROM debug_runs WHERE started_at < ?", (cutoff,))
            stale = connection.execute(
                "SELECT run_id FROM debug_runs ORDER BY started_at DESC LIMIT -1 OFFSET ?",
                (self.max_reports,),
            ).fetchall()
            connection.executemany("DELETE FROM debug_runs WHERE run_id=?", [(row["run_id"],) for row in stale])


def markdown_report(run: dict[str, Any]) -> str:
    lines = [
        f"# Redis Full Debug Report {run['run_id']}",
        "",
        f"- Status: **{run['status'].upper()}**",
        f"- Started: `{run['started_at']}`",
        f"- Completed: `{run.get('completed_at')}`",
        "",
        "## Summary",
        "",
        "```json",
        json.dumps(run["summary"], indent=2, ensure_ascii=True),
        "```",
        "",
    ]
    for check in run["checks"]:
        lines.extend(
            [
                f"## {check['category']} - {check['name']}",
                "",
                f"- Status: **{check['status'].upper()}**",
                f"- Summary: {check['summary']}",
                f"- Recommendation: {check.get('recommendation') or 'None'}",
                "",
                "```json",
                json.dumps(check.get("evidence", {}), indent=2, ensure_ascii=True),
                "```",
                "",
            ]
        )
    return "\n".join(lines)
