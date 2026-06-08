#!/usr/bin/env python3
"""Fail-fast standalone LangGraph orchestrator diagnostic.

Builds a temporary candidate image from the current backend modules, runs it on
the compose bridge network, probes it from open-webui, and verifies that a
narrative invoke writes at least one point to Qdrant.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent
IMAGE = "langgraph-orchestrator:diag"
CONTAINER = "langgraph-orchestrator-diag"
NETWORK = "open-webui-master_llm-net"
OPEN_WEBUI_CONTAINER = "open-webui"


def run(cmd: list[str], *, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    print("+ " + " ".join(cmd), flush=True)
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    if proc.stdout:
        print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n")
    if check and proc.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {proc.returncode}: {' '.join(cmd)}")
    return proc


def docker_exec_python(code: str, *, timeout: int = 120) -> str:
    proc = run(
        ["docker", "exec", "-i", OPEN_WEBUI_CONTAINER, "python", "-c", code],
        timeout=timeout,
    )
    return proc.stdout.strip()


def qdrant_count() -> int:
    code = r"""
import json
import urllib.request

payload = json.dumps({"exact": True}).encode()
req = urllib.request.Request(
    "http://qdrant:6333/collections/primary_narrative/points/count",
    data=payload,
    headers={"Content-Type": "application/json"},
)
try:
    data = json.loads(urllib.request.urlopen(req, timeout=10).read())
    print(int(data.get("result", {}).get("count", 0)))
except Exception:
    print(0)
"""
    return int(docker_exec_python(code, timeout=30).splitlines()[-1])


def invoke_candidate(requests: int) -> list[dict]:
    code = f"""
import concurrent.futures
import json
import time
import urllib.request

def call(i):
    payload = json.dumps({{
        "input": "story diagnostic: continue the world canon safely",
        "thread_id": f"diag-{{int(time.time())}}-{{i}}",
    }}).encode()
    req = urllib.request.Request(
        "http://{CONTAINER}:8100/invoke",
        data=payload,
        headers={{"Content-Type": "application/json"}},
    )
    with urllib.request.urlopen(req, timeout=150) as resp:
        body = json.loads(resp.read())
        return {{"status": resp.status, "body": body}}

with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    results = list(pool.map(call, range({requests})))

print(json.dumps(results))
"""
    out = docker_exec_python(code, timeout=180)
    return json.loads(out.splitlines()[-1])


def build_context(tmp: Path, base_image: str) -> None:
    shutil.copy2(ROOT / "backend" / "langgraph_orchestrator.py", tmp / "langgraph_orchestrator.py")
    shutil.copy2(ROOT / "backend" / "hitl_broker.py", tmp / "hitl_broker.py")
    (tmp / "Dockerfile").write_text(
        textwrap.dedent(
            f"""
            FROM {base_image}
            WORKDIR /app/orchestrator
            COPY langgraph_orchestrator.py hitl_broker.py ./
            ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
            EXPOSE 8100
            CMD ["python", "-m", "uvicorn", "langgraph_orchestrator:app", "--host", "0.0.0.0", "--port", "8100", "--workers", "1", "--log-level", "info"]
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


def wait_for_health(timeout_s: int) -> None:
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        proc = run(
            [
                "docker",
                "exec",
                OPEN_WEBUI_CONTAINER,
                "sh",
                "-lc",
                f"curl -fsS http://{CONTAINER}:8100/health",
            ],
            check=False,
            timeout=15,
        )
        last = proc.stdout.strip()
        if proc.returncode == 0:
            print(f"health_ok: {last}")
            return
        time.sleep(2)
    raise RuntimeError(f"Candidate did not become healthy within {timeout_s}s. Last output: {last}")


def assert_log_purity() -> None:
    logs = run(["docker", "logs", CONTAINER], timeout=30).stdout
    bad_tokens = ("Traceback", "ModuleNotFoundError", "WARNING", "ERROR")
    matches = [line for line in logs.splitlines() if any(token in line for token in bad_tokens)]
    if matches:
        sample = "\n".join(matches[:50])
        raise RuntimeError(f"Log purity failed; matched forbidden tokens:\n{sample}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", action="store_true", help="Run the candidate diagnostic.")
    parser.add_argument("--base-image", default="open-webui:0.9.6")
    parser.add_argument("--requests", type=int, default=2)
    parser.add_argument("--health-timeout", type=int, default=90)
    parser.add_argument("--keep", action="store_true", help="Keep the diagnostic container after the run.")
    args = parser.parse_args()

    if not args.candidate:
        parser.error("pass --candidate to run the diagnostic")
    if args.requests < 1:
        parser.error("--requests must be >= 1")

    with tempfile.TemporaryDirectory(prefix="langgraph-orchestrator-diag-") as raw_tmp:
        tmp = Path(raw_tmp)
        build_context(tmp, args.base_image)

        run(["docker", "rm", "-f", CONTAINER], check=False, timeout=30)
        try:
            run(["docker", "build", "-t", IMAGE, str(tmp)], timeout=600)
            run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    CONTAINER,
                    "--network",
                    NETWORK,
                    "--add-host",
                    "host.docker.internal:host-gateway",
                    "-e",
                    "QDRANT_URI=http://qdrant:6333",
                    "-e",
                    "REDIS_URL=redis://redis-cache:6379/0",
                    "-e",
                    "LM_STUDIO_BASE_URL=http://host.docker.internal:4321",
                    IMAGE,
                ],
                timeout=60,
            )
            wait_for_health(args.health_timeout)

            before = qdrant_count()
            results = invoke_candidate(args.requests)
            after = qdrant_count()
            print(json.dumps({"before": before, "after": after, "results": results}, indent=2))

            if not all(item.get("status") == 200 for item in results):
                raise RuntimeError(f"Invoke failed: {results}")
            if after < before + 1:
                raise RuntimeError(f"Qdrant persistence failed: before={before}, after={after}")

            assert_log_purity()
            print("diagnostic_ok")
            return 0
        finally:
            if not args.keep:
                run(["docker", "rm", "-f", CONTAINER], check=False, timeout=30)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"diagnostic_failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
