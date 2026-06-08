"""
sovereign_dr_engine.py
======================
Zero-Downtime Sovereign AI Disaster Recovery Engine
Implements the full 5-section strategic framework for the Sovereign AI Stack:

  Section 1 - Architectural Foundations  : Named Volume topology awareness
  Section 2 - Engine-Specific Snapshots  : PostgreSQL (-Fd parallel), SQLite
                                           (VACUUM INTO), Qdrant (REST API),
                                           ClickHouse (BACKUP TO Disk),
                                           MinIO (mc mirror)
  Section 3 - Archival & Compression     : zstd multi-threaded + SHA-256 manifest
  Section 4 - Total Ecosystem Resurrection: Multi-phase hydration protocol
  Section 5 - Immortal Data              : Reed-Solomon 10% parity + .wslconfig

Usage:
    python sovereign_dr_engine.py backup   [--output-dir PATH] [--threads N]
    python sovereign_dr_engine.py restore  --archive PATH [--components all|pg|sqlite|qdrant|ch|minio]
    python sovereign_dr_engine.py verify   --archive PATH
    python sovereign_dr_engine.py harden   [--memory-gb N]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib import request as urllib_request
from urllib.error import URLError
import http.client

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_FORMAT = "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(name)-24s | %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt="%H:%M:%S",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("SovereignDR")

# ---------------------------------------------------------------------------
# Stack topology constants (sourced from langfuse.yml / docker-compose.yml)
# ---------------------------------------------------------------------------
RYZEN_9950X_THREADS: int = 32   # AMD Ryzen 9 9950X logical processor count

# Container names (override via env)
C_POSTGRES  = os.getenv("LANGFUSE_DB_CONTAINER", "langfuse-postgres-1")
C_WEBUI     = os.getenv("WEBUI_CONTAINER",        "open-webui")
C_QDRANT    = os.getenv("QDRANT_CONTAINER",       "qdrant")
C_CLICKHOUSE= os.getenv("CLICKHOUSE_CONTAINER",   "langfuse-clickhouse-1")
C_MINIO     = os.getenv("MINIO_CONTAINER",        "langfuse-minio-1")

# Named volumes (Ext4 inside Docker -- never bind mounts)
VOL_POSTGRES    = "langfuse_postgres_data"
VOL_CLICKHOUSE  = "langfuse_clickhouse_data"
VOL_MINIO       = "langfuse_minio_data"
VOL_WEBUI       = "open-webui"        # Open WebUI named volume
VOL_QDRANT      = "qdrant_storage"    # Qdrant named volume (NOT used directly)

# Service URLs
QDRANT_URL   = os.getenv("QDRANT_URL",   "http://localhost:6333")
PG_USER      = os.getenv("POSTGRES_USER","postgres")
PG_DB        = os.getenv("POSTGRES_DB",  "postgres")
PG_PASSWORD  = os.getenv("POSTGRES_PASSWORD", "postgres")

# Parallelism
DEFAULT_THREADS = int(os.getenv("DR_THREADS", str(RYZEN_9950X_THREADS)))

# Storage paths
DEFAULT_STAGING = os.getenv("DR_STAGING_DIR",  "C:/SovereignDR/staging")
DEFAULT_OUTPUT  = os.getenv("DR_OUTPUT_DIR",   "C:/SovereignDR/archives")
WSLCONFIG_PATH  = Path(os.environ.get("USERPROFILE", "C:/Users/Default")) / ".wslconfig"

# Reed-Solomon parameters (Section 5)
RS_PARITY_RATIO = 0.10   # 10% overhead per framework mandate

# Static manifests to capture
STATIC_FILES = [
    ".env",
    "docker-compose.yml",
    "langfuse.yml",
    "langfuse_compose.yml",
    "extracted_langfuse.yml",
]

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ComponentResult:
    name: str
    success: bool = False
    duration_s: float = 0.0
    size_bytes: int = 0
    error: Optional[str] = None
    notes: List[str] = field(default_factory=list)


@dataclass
class BackupManifest:
    schema_version: str = "2.0"
    timestamp_utc: str = ""
    hostname: str = ""
    threads_used: int = 0
    components: Dict[str, dict] = field(default_factory=dict)
    file_hashes: Dict[str, str] = field(default_factory=dict)   # rel_path -> sha256
    parity_files: List[str] = field(default_factory=list)
    total_bytes_uncompressed: int = 0
    archive_path: str = ""
    compression_algorithm: str = "zstd"
    rs_parity_ratio: float = RS_PARITY_RATIO


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _run(cmd: List[str], timeout: int = 600, env: Optional[dict] = None,
         check: bool = True, stdin_data: Optional[bytes] = None) -> subprocess.CompletedProcess:
    """Execute a subprocess command with unified error handling."""
    flat = " ".join(cmd)
    log.debug("EXEC: %s", flat)
    merged_env = {**os.environ, **(env or {})}
    result = subprocess.run(
        cmd, capture_output=True, input=stdin_data,
        timeout=timeout, env=merged_env
    )
    if check and result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"Command failed (rc={result.returncode}): {flat}\n{stderr}")
    return result


def _get_container_name(env_var_name: str, service_name: str, fallback_default: str) -> str:
    """Resolve container name dynamically using compose labels, falling back to environment or default."""
    val = os.getenv(env_var_name)
    if val:
        return val
    try:
        # Run docker ps with filter for the service name label
        r = _run(["docker", "ps", "--filter", f"label=com.docker.compose.service={service_name}", "--format", "{{.Names}}"], check=False, timeout=10)
        name = r.stdout.decode().strip()
        if name:
            resolved = name.split('\n')[0].strip()
            log.debug("Resolved container for service '%s' via docker ps: '%s'", service_name, resolved)
            return resolved
    except Exception as exc:
        log.debug("Failed to dynamically resolve container for service '%s': %s", service_name, exc)
    return fallback_default


def init_container_names():
    global C_POSTGRES, C_WEBUI, C_QDRANT, C_CLICKHOUSE, C_MINIO
    C_POSTGRES   = _get_container_name("LANGFUSE_DB_CONTAINER", "postgres", "langfuse-postgres-1")
    C_WEBUI      = _get_container_name("WEBUI_CONTAINER", "open-webui", "open-webui")
    C_QDRANT     = _get_container_name("QDRANT_CONTAINER", "qdrant", "qdrant")
    C_CLICKHOUSE = _get_container_name("CLICKHOUSE_CONTAINER", "clickhouse", "langfuse-clickhouse-1")
    C_MINIO      = _get_container_name("MINIO_CONTAINER", "minio", "langfuse-minio-1")



def _docker_exec(container: str, cmd: List[str], timeout: int = 600,
                 env_vars: Optional[Dict[str, str]] = None,
                 check: bool = True) -> subprocess.CompletedProcess:
    """Run a command inside a Docker container."""
    base = ["docker", "exec"]
    if env_vars:
        for k, v in env_vars.items():
            base += ["-e", f"{k}={v}"]
    return _run(base + [container] + cmd, timeout=timeout, check=check)


def _docker_cp(src: str, dst: str, timeout: int = 300) -> None:
    """Copy between container and host using docker cp."""
    _run(["docker", "cp", src, dst], timeout=timeout)


def _container_running(name: str) -> bool:
    """Return True if a Docker container is in a running state."""
    try:
        r = _run(["docker", "inspect", "--format", "{{.State.Running}}", name],
                 check=False, timeout=10)
        return r.stdout.decode().strip() == "true"
    except Exception:
        return False


def _sha256_file(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _dir_size(path: Path) -> int:
    """Recursively compute total byte size of a directory."""
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _http_get(url: str, timeout: int = 15) -> Tuple[int, bytes]:
    """Minimal HTTP GET using stdlib only."""
    try:
        with urllib_request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read()
    except URLError as e:
        return -1, str(e).encode()


def _http_post(url: str, data: Optional[bytes] = None,
               content_type: str = "application/json",
               timeout: int = 60) -> Tuple[int, bytes]:
    """Minimal HTTP POST using stdlib only."""
    parsed = urllib_request.urlparse(url) if hasattr(urllib_request, "urlparse") else None
    req = urllib_request.Request(url, data=data or b"",
                                  method="POST",
                                  headers={"Content-Type": content_type})
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except URLError as e:
        return -1, str(e).encode()


def _http_upload_multipart(url: str, file_path: Path,
                            field_name: str = "snapshot",
                            timeout: int = 120) -> Tuple[int, bytes]:
    """Upload a file via multipart/form-data using stdlib http.client."""
    import uuid
    boundary = uuid.uuid4().hex
    with open(file_path, "rb") as fh:
        file_data = fh.read()

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; filename="{file_path.name}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + file_data + f"\r\n--{boundary}--\r\n".encode()

    # Parse host/path from url
    url_stripped = url.replace("http://", "").replace("https://", "")
    host_part, _, path_part = url_stripped.partition("/")
    path_part = "/" + path_part

    conn = http.client.HTTPConnection(host_part, timeout=timeout)
    try:
        conn.request(
            "POST", path_part, body=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            }
        )
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Section 5: Reed-Solomon erasure coding (pure Python, no external deps)
# ---------------------------------------------------------------------------

class GF256:
    """Galois Field GF(2^8) arithmetic over the AES polynomial x^8+x^4+x^3+x+1."""
    POLY = 0x11B

    @classmethod
    def _build_tables(cls):
        exp = [0] * 512
        log = [0] * 256
        x = 1
        for i in range(255):
            exp[i] = x
            log[x] = i
            x <<= 1
            if x & 0x100:
                x ^= cls.POLY
        for i in range(255, 512):
            exp[i] = exp[i - 255]

        # Precompute 2D multiplication table
        mul_table = [[0] * 256 for _ in range(256)]
        for a in range(256):
            for b in range(256):
                if a == 0 or b == 0:
                    mul_table[a][b] = 0
                else:
                    mul_table[a][b] = exp[(log[a] + log[b]) % 255]

        return exp, log, mul_table

    _EXP, _LOG, _MUL_TABLE = (None, None, None)

    @classmethod
    def mul(cls, a: int, b: int) -> int:
        return cls._MUL_TABLE[a][b]

    @classmethod
    def div(cls, a: int, b: int) -> int:
        if a == 0:
            return 0
        if b == 0:
            raise ZeroDivisionError("GF256 division by zero")
        return cls._EXP[(cls._LOG[a] - cls._LOG[b]) % 255]

    @classmethod
    def pow(cls, x: int, power: int) -> int:
        return cls._EXP[(cls._LOG[x] * power) % 255]


# Force table build at import time
GF256._EXP, GF256._LOG, GF256._MUL_TABLE = GF256._build_tables()


def rs_encode_block(data_block: bytes, gen: list) -> bytes:
    """
    Produce n_parity Reed-Solomon parity bytes for a data block.
    Uses systematic encoding over GF(2^8) with precomputed generator polynomial.
    """
    n_data = len(data_block)
    n_parity = len(gen) - 1

    # Systematic encoding: data * x^n_parity mod gen
    msg_out = bytearray(data_block) + bytearray(n_parity)
    mul_table = GF256._MUL_TABLE
    for i in range(n_data):
        coef = msg_out[i]
        if coef != 0:
            row = mul_table[coef]
            for j in range(1, len(gen)):
                msg_out[i + j] ^= row[gen[j]]
    return bytes(msg_out[n_data:])


def _poly_mul(p: list, q: list) -> list:
    r = [0] * (len(p) + len(q) - 1)
    for i, a in enumerate(p):
        for j, b in enumerate(q):
            r[i + j] ^= GF256.mul(a, b)
    return r


def generate_parity_file(data_path: Path, parity_path: Path,
                          parity_ratio: float = RS_PARITY_RATIO) -> None:
    """
    Apply Reed-Solomon erasure coding to data_path.
    Block size: 223 bytes data + n_parity bytes parity.
    The parity_ratio determines how many parity bytes per block.
    """
    BLOCK_DATA = 223  # standard RS(255, 223) data portion
    n_parity = max(4, math.ceil(BLOCK_DATA * parity_ratio))  # >= 4 parity bytes
    parity_path.parent.mkdir(parents=True, exist_ok=True)

    # Precompute generator polynomial once for the entire file
    gen = [1]
    for i in range(n_parity):
        gen = _poly_mul(gen, [1, GF256.pow(2, i)])

    with open(data_path, "rb") as fin, open(parity_path, "wb") as fout:
        # Header: magic + block config
        fout.write(b"SRS1")                          # magic
        fout.write(struct.pack(">HH", BLOCK_DATA, n_parity))  # block geometry
        fout.write(struct.pack(">Q", data_path.stat().st_size))  # original size

        while True:
            chunk = fin.read(BLOCK_DATA)
            if not chunk:
                break
            padded = chunk.ljust(BLOCK_DATA, b"\x00")
            parity = rs_encode_block(padded, gen)
            fout.write(struct.pack("B", len(chunk)))  # actual data length in block
            fout.write(parity)


def verify_parity_file(data_path: Path, parity_path: Path) -> bool:
    """
    Verify a Reed-Solomon parity sidecar against its data source.
    Returns True if all parity checks pass.
    """
    MAGIC = b"SRS1"
    try:
        with open(parity_path, "rb") as fp:
            magic = fp.read(4)
            if magic != MAGIC:
                return False
            block_data, n_parity = struct.unpack(">HH", fp.read(4))
            orig_size = struct.unpack(">Q", fp.read(8))[0]

            # Precompute generator polynomial once
            gen = [1]
            for i in range(n_parity):
                gen = _poly_mul(gen, [1, GF256.pow(2, i)])

            errors = 0
            with open(data_path, "rb") as fd:
                while True:
                    chunk = fd.read(block_data)
                    if not chunk:
                        break
                    actual_len = struct.unpack("B", fp.read(1))[0]
                    stored_parity = fp.read(n_parity)
                    padded = chunk.ljust(block_data, b"\x00")
                    recomputed = rs_encode_block(padded, gen)
                    if recomputed != stored_parity:
                        errors += 1

            return errors == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Section 2: Engine-specific snapshot handlers
# ---------------------------------------------------------------------------

def backup_postgres(staging_dir: Path, threads: int) -> ComponentResult:
    """
    PostgreSQL Directory Format (-Fd) parallel dump via pg_dump inside the container.
    Streams the directory across the container boundary via docker cp.
    """
    result = ComponentResult(name="postgresql")
    t0 = time.perf_counter()
    pg_staging = "/tmp/pgdump_sovereign"

    if not _container_running(C_POSTGRES):
        result.error = f"Container '{C_POSTGRES}' is not running"
        result.notes.append("Skipped -- container offline")
        result.duration_s = time.perf_counter() - t0
        return result

    try:
        log.info("[PG] Initiating Directory Format (-Fd) parallel dump (j=%d)...", threads)
        # Ensure clean internal staging directory
        _docker_exec(C_POSTGRES, ["sh", "-c", f"rm -rf {pg_staging} && mkdir -p {pg_staging}"])

        # pg_dump with synchronized MVCC snapshot + Directory Format + parallel jobs
        dump_cmd = (
            f"pg_dump -U {PG_USER} -d {PG_DB} "
            f"-Fd -j {min(threads, 8)} "   # cap at 8; pg_dump max practical parallelism
            f"--no-password "
            f"-f {pg_staging}"
        )
        _docker_exec(
            C_POSTGRES,
            ["sh", "-c", dump_cmd],
            timeout=900,
            env_vars={"PGPASSWORD": PG_PASSWORD}
        )
        log.info("[PG] Dump complete. Copying to host staging...")

        host_pg_dir = staging_dir / "postgresql_dump"
        host_pg_dir.mkdir(parents=True, exist_ok=True)
        _docker_cp(f"{C_POSTGRES}:{pg_staging}/", str(host_pg_dir))

        # Cleanup internal temp
        _docker_exec(C_POSTGRES, ["rm", "-rf", pg_staging], check=False)

        result.size_bytes = _dir_size(host_pg_dir)
        result.success = True
        log.info("[PG] Snapshot complete. Size: %.2f MB", result.size_bytes / 1e6)
        result.notes.append(f"Directory format, {threads} parallel workers")
        result.notes.append(f"Restore flags: --clean --if-exists --no-owner -j {threads}")

    except Exception as exc:
        result.error = str(exc)
        log.error("[PG] Snapshot FAILED: %s", exc)
    finally:
        result.duration_s = time.perf_counter() - t0
    return result


def backup_sqlite(staging_dir: Path) -> ComponentResult:
    """
    SQLite VACUUM INTO for WAL-safe point-in-time consistent copy.
    Prohibits raw file copy which misses WAL-trapped transactions.
    """
    result = ComponentResult(name="sqlite_webui")
    t0 = time.perf_counter()
    container_source = "/app/backend/data/webui.db"
    container_vacuumed = "/tmp/webui_sovereign_vacuum.db"

    if not _container_running(C_WEBUI):
        result.error = f"Container '{C_WEBUI}' is not running"
        result.notes.append("Skipped -- container offline")
        result.duration_s = time.perf_counter() - t0
        return result

    try:
        log.info("[SQLite] Issuing VACUUM INTO for WAL-safe atomic copy...")
        # Clear any stale output file from a previous failed run before VACUUM INTO,
        # since sqlite3 raises OperationalError if the destination already exists.
        _docker_exec(C_WEBUI, ["rm", "-f", container_vacuumed], check=False)
        # Use python3 (guaranteed present in the Open WebUI container) to run
        # VACUUM INTO via the stdlib sqlite3 module -- avoids dependency on the
        # optional sqlite3 CLI binary which is not installed in the base image.
        vacuum_py = (
            "import sqlite3; "
            f"c = sqlite3.connect('{container_source}'); "
            f"c.execute(\"VACUUM INTO '{container_vacuumed}'\"); "
            "c.close()"
        )
        _docker_exec(C_WEBUI, ["python3", "-c", vacuum_py], timeout=300)

        host_sqlite = staging_dir / "webui_vacuum.db"
        _docker_cp(f"{C_WEBUI}:{container_vacuumed}", str(host_sqlite))
        _docker_exec(C_WEBUI, ["rm", "-f", container_vacuumed], check=False)

        result.size_bytes = host_sqlite.stat().st_size
        result.success = True
        log.info("[SQLite] VACUUM INTO complete. Size: %.2f MB", result.size_bytes / 1e6)
        result.notes.append("VACUUM INTO -- WAL-resolved, defragmented, pristine output")

    except Exception as exc:
        result.error = str(exc)
        log.error("[SQLite] Snapshot FAILED: %s", exc)
    finally:
        result.duration_s = time.perf_counter() - t0
    return result


def backup_qdrant(staging_dir: Path) -> ComponentResult:
    """
    Qdrant collection-level REST API snapshots.
    Avoids full instance storage which captures Raft metadata triggering
    startup panics on single-node restoration.
    """
    result = ComponentResult(name="qdrant_vectors")
    t0 = time.perf_counter()
    qdrant_dir = staging_dir / "qdrant_snapshots"
    qdrant_dir.mkdir(parents=True, exist_ok=True)

    # Enumerate collections
    status, body = _http_get(f"{QDRANT_URL}/collections", timeout=10)
    if status != 200:
        result.error = f"Qdrant unreachable (HTTP {status})"
        result.notes.append("Skipped -- service offline or not exposed on localhost")
        result.duration_s = time.perf_counter() - t0
        return result

    try:
        collections_data = json.loads(body)
        collections = collections_data.get("result", {}).get("collections", [])
        log.info("[Qdrant] Found %d collection(s) to snapshot.", len(collections))

        failed: List[str] = []
        total_bytes = 0

        for coll in collections:
            cname = coll["name"]
            try:
                log.info("[Qdrant] Creating snapshot for collection: %s", cname)
                snap_status, snap_body = _http_post(
                    f"{QDRANT_URL}/collections/{cname}/snapshots"
                )
                if snap_status not in (200, 201):
                    raise RuntimeError(f"Snapshot creation HTTP {snap_status}")

                snap_info = json.loads(snap_body).get("result", {})
                snap_name = snap_info.get("name")
                if not snap_name:
                    raise RuntimeError("Empty snapshot name in response")

                # Download the snapshot binary
                dl_url = f"{QDRANT_URL}/collections/{cname}/snapshots/{snap_name}"
                log.info("[Qdrant] Downloading: %s", snap_name)
                dl_status, dl_body = _http_get(dl_url, timeout=120)
                if dl_status != 200:
                    raise RuntimeError(f"Download HTTP {dl_status}")

                # Write out with collection namespace prefix
                safe_name = cname.replace("/", "_")
                snap_path = qdrant_dir / f"{safe_name}__{snap_name}"
                snap_path.write_bytes(dl_body)
                total_bytes += snap_path.stat().st_size

                # Record collection-to-file mapping for restoration
                meta_path = qdrant_dir / f"{safe_name}__meta.json"
                meta_path.write_text(json.dumps({
                    "collection": cname,
                    "snapshot_file": snap_path.name,
                    "restore_endpoint": f"/collections/{cname}/snapshots/upload?priority=snapshot"
                }, indent=2))

                log.info("[Qdrant]   OK -- %.2f MB", snap_path.stat().st_size / 1e6)

            except Exception as exc:
                log.error("[Qdrant] Collection '%s' FAILED: %s", cname, exc)
                failed.append(cname)

        result.size_bytes = total_bytes
        result.success = (len(failed) == 0)
        if failed:
            result.error = f"Failed collections: {failed}"
            result.notes.append(f"{len(failed)} collection(s) failed")
        else:
            result.notes.append("All collections snapshotted -- payload/HNSW indices isolated from Raft metadata")

    except Exception as exc:
        result.error = str(exc)
        log.error("[Qdrant] Fatal error: %s", exc)
    finally:
        result.duration_s = time.perf_counter() - t0
    return result


def backup_clickhouse(staging_dir: Path) -> ComponentResult:
    """
    ClickHouse native BACKUP ALL TO File command for atomic consistent export.
    """
    result = ComponentResult(name="clickhouse")
    t0 = time.perf_counter()
    ch_backup_file = "sovereign_backup.zip"
    ch_internal_file = f"/var/lib/clickhouse/backups/{ch_backup_file}"

    if not _container_running(C_CLICKHOUSE):
        result.error = f"Container '{C_CLICKHOUSE}' is not running"
        result.notes.append("Skipped -- container offline")
        result.duration_s = time.perf_counter() - t0
        return result

    try:
        log.info("[ClickHouse] Initiating native BACKUP ALL ...")
        # Ensure clean internal staging backup file
        _docker_exec(C_CLICKHOUSE, ["rm", "-f", ch_internal_file], check=False)
        _docker_exec(C_CLICKHOUSE, ["mkdir", "-p", "/var/lib/clickhouse/backups"])

        backup_sql = (
            f"BACKUP ALL "
            f"TO File('{ch_backup_file}') "
            f"SETTINGS async=0"
        )
        _docker_exec(
            C_CLICKHOUSE,
            ["clickhouse-client", "--query", backup_sql],
            timeout=600
        )

        log.info("[ClickHouse] Copying backup zip to host staging...")
        host_ch_file = staging_dir / "clickhouse_backup.zip"
        _docker_cp(f"{C_CLICKHOUSE}:{ch_internal_file}", str(host_ch_file))

        # Cleanup internal backup file to free disk
        _docker_exec(C_CLICKHOUSE, ["rm", "-f", ch_internal_file], check=False)

        result.size_bytes = host_ch_file.stat().st_size
        result.success = True
        log.info("[ClickHouse] Backup complete. Size: %.2f MB", result.size_bytes / 1e6)
        result.notes.append("Native BACKUP ALL TO File -- MVCC consistent, no cessation required")

    except Exception as exc:
        result.error = str(exc)
        log.error("[ClickHouse] Snapshot FAILED: %s", exc)
    finally:
        result.duration_s = time.perf_counter() - t0
    return result


def backup_minio(staging_dir: Path) -> ComponentResult:
    """
    MinIO object storage mirror using the 'mc' CLI tool.
    Captures Langfuse event payloads, batch exports, and media blobs.
    """
    result = ComponentResult(name="minio_s3")
    t0 = time.perf_counter()

    if not _container_running(C_MINIO):
        result.error = f"Container '{C_MINIO}' is not running"
        result.notes.append("Skipped -- container offline")
        result.duration_s = time.perf_counter() - t0
        return result

    host_minio_dir = staging_dir / "minio_data"
    host_minio_dir.mkdir(parents=True, exist_ok=True)

    try:
        log.info("[MinIO] Mirroring object store via mc mirror...")
        # Use docker exec to run mc inside the minio container or a sidecar
        # The MinIO container includes mc; configure an alias then mirror
        minio_url = "http://localhost:9000"
        mc_alias_cmd = (
            "mc alias set sovereign_local "
            f"http://localhost:9000 "
            "${MINIO_ROOT_USER:-minio} "
            "${MINIO_ROOT_PASSWORD:-miniosecret}"
        )
        _docker_exec(C_MINIO, ["sh", "-c", mc_alias_cmd], timeout=30)

        # Mirror all buckets to /tmp/minio_mirror inside the container
        # Ensure the directory exists first to prevent mc mirror failing
        _docker_exec(C_MINIO, ["mkdir", "-p", "/tmp/minio_mirror"])
        mc_mirror_cmd = "mc mirror --preserve sovereign_local /tmp/minio_mirror"
        _docker_exec(C_MINIO, ["sh", "-c", mc_mirror_cmd], timeout=600)

        _docker_cp(f"{C_MINIO}:/tmp/minio_mirror/", str(host_minio_dir))
        _docker_exec(C_MINIO, ["rm", "-rf", "/tmp/minio_mirror"], check=False)

        result.size_bytes = _dir_size(host_minio_dir)
        result.success = True
        log.info("[MinIO] Mirror complete. Size: %.2f MB", result.size_bytes / 1e6)
        result.notes.append("mc mirror --preserve -- all buckets captured with metadata")

    except Exception as exc:
        result.error = str(exc)
        log.error("[MinIO] Mirror FAILED: %s", exc)
    finally:
        result.duration_s = time.perf_counter() - t0
    return result


# ---------------------------------------------------------------------------
# Section 3: Static manifest + SHA-256 + zstd compression
# ---------------------------------------------------------------------------

def capture_static_manifests(project_root: Path, staging_dir: Path) -> ComponentResult:
    """Copy non-volatile infrastructure configuration files to staging."""
    result = ComponentResult(name="static_manifests")
    t0 = time.perf_counter()
    manifest_dir = staging_dir / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    captured = []

    for filename in STATIC_FILES:
        src = project_root / filename
        if src.exists():
            shutil.copy2(src, manifest_dir / filename)
            captured.append(filename)
            log.info("[Manifests] Captured: %s", filename)
        else:
            log.debug("[Manifests] Not found (skipped): %s", filename)

    result.success = True
    result.size_bytes = _dir_size(manifest_dir)
    result.notes.append(f"Captured: {captured}")
    result.duration_s = time.perf_counter() - t0
    return result


def build_sha256_manifest(staging_dir: Path) -> Dict[str, str]:
    """
    Generate SHA-256 cryptographic hashes for every file in the staging directory.
    Returns a dict mapping relative path to hex digest.
    """
    log.info("[SHA-256] Computing cryptographic hashes for entire staging zone...")
    hashes: Dict[str, str] = {}
    for fpath in sorted(staging_dir.rglob("*")):
        if fpath.is_file():
            rel = str(fpath.relative_to(staging_dir)).replace("\\", "/")
            digest = _sha256_file(fpath)
            hashes[rel] = digest
            log.debug("  %s  %s", digest[:16] + "...", rel)
    log.info("[SHA-256] Hashed %d file(s).", len(hashes))
    return hashes


def compress_zstd(staging_dir: Path, archive_path: Path, threads: int) -> Tuple[Path, int]:
    """
    Compress staging_dir to a .tar.zst archive using zstd multi-threaded.
    Requires zstd CLI. Falls back to Python tarfile + gzip if unavailable.
    Returns (actual_archive_path, compressed_size_bytes) -- the path may
    differ from archive_path if the fallback switches extension to .tar.gz.
    """
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    zstd_available = shutil.which("zstd") is not None
    tar_available  = shutil.which("tar")  is not None

    if zstd_available and tar_available:
        log.info("[Compress] Using zstd with %d threads (Ryzen 9950X optimized)...", threads)
        # Pipe: tar -cf - staging_dir | zstd -T{threads} -19 -o archive.tar.zst
        tar_cmd  = ["tar", "-cf", "-", "-C", str(staging_dir.parent), staging_dir.name]
        zstd_cmd = ["zstd", f"-T{threads}", "-19", "--long", "-o", str(archive_path)]

        tar_proc = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        zstd_proc = subprocess.Popen(zstd_cmd, stdin=tar_proc.stdout,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        tar_proc.stdout.close()
        _, zstd_err = zstd_proc.communicate(timeout=3600)
        tar_proc.wait(timeout=60)

        if zstd_proc.returncode != 0:
            err = zstd_err.decode(errors="replace").strip()
            raise RuntimeError(f"zstd compression failed: {err}")

        log.info("[Compress] zstd archive created successfully.")
        return archive_path, archive_path.stat().st_size
    else:
        log.warning("[Compress] zstd/tar not found -- falling back to Python tarfile+gzip")
        import tarfile as _tarfile
        # Build the .tar.gz path from the archive_path stem (strip .tar.zst if needed)
        stem = archive_path.name
        for ext in (".tar.zst", ".zst", ".tar.gz", ".gz"):
            if stem.endswith(ext):
                stem = stem[: -len(ext)]
                break
        gz_path = archive_path.parent / f"{stem}.tar.gz"
        with _tarfile.open(gz_path, "w:gz") as tar:
            tar.add(staging_dir, arcname=staging_dir.name)
        log.info("[Compress] gzip archive created: %s", gz_path.name)
        return gz_path, gz_path.stat().st_size


# ---------------------------------------------------------------------------
# Section 5: Reed-Solomon parity generation
# ---------------------------------------------------------------------------

def generate_rs_parity(archive_path: Path, parity_dir: Path) -> List[str]:
    """
    Generate Reed-Solomon parity sidecar for the compressed archive.
    Returns list of relative parity file paths.
    """
    parity_dir.mkdir(parents=True, exist_ok=True)
    parity_file = parity_dir / (archive_path.name + ".par2")
    log.info("[RS-Parity] Generating %.0f%% parity sidecar for: %s",
             RS_PARITY_RATIO * 100, archive_path.name)
    generate_parity_file(archive_path, parity_file, RS_PARITY_RATIO)
    size = parity_file.stat().st_size
    log.info("[RS-Parity] Parity file written: %.2f MB", size / 1e6)
    return [str(parity_file.relative_to(parity_dir.parent)).replace("\\", "/")]


# ---------------------------------------------------------------------------
# Section 3: Full backup orchestration
# ---------------------------------------------------------------------------

def cmd_backup(args: argparse.Namespace) -> int:
    """Execute the full zero-downtime backup pipeline."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    # output_dir IS the archive root -- no /archives subdirectory to prevent
    # path doubling when the user already passes e.g. C:\SovereignDR\archives
    output_root = Path(args.output_dir)
    staging_dir = output_root / f"staging_{ts}"
    archive_dir = output_root                          # archives land directly here
    parity_dir  = output_root / "parity"
    threads     = args.threads
    project_root = Path(__file__).parent

    log.info("=" * 72)
    log.info("  SOVEREIGN DR ENGINE -- BACKUP PHASE -- %s UTC", ts)
    log.info("  Project root : %s", project_root)
    log.info("  Staging zone : %s", staging_dir)
    log.info("  Threads      : %d (Ryzen 9950X)", threads)
    log.info("=" * 72)

    staging_dir.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)

    manifest = BackupManifest(
        timestamp_utc=ts,
        hostname=os.environ.get("COMPUTERNAME", "unknown"),
        threads_used=threads,
        rs_parity_ratio=RS_PARITY_RATIO,
    )

    # ---- Section 3 Step 1: Static Manifests ----
    r_static = capture_static_manifests(project_root, staging_dir)
    manifest.components["static_manifests"] = asdict(r_static)

    # ---- Section 2: Engine snapshots (sequential per framework mandate) ----
    r_pg  = backup_postgres(staging_dir, threads)
    manifest.components["postgresql"] = asdict(r_pg)

    r_sql = backup_sqlite(staging_dir)
    manifest.components["sqlite_webui"] = asdict(r_sql)

    r_qd  = backup_qdrant(staging_dir)
    manifest.components["qdrant_vectors"] = asdict(r_qd)

    r_ch  = backup_clickhouse(staging_dir)
    manifest.components["clickhouse"] = asdict(r_ch)

    r_mn  = backup_minio(staging_dir)
    manifest.components["minio_s3"] = asdict(r_mn)

    # ---- Section 3 Step 4: SHA-256 Cryptographic Manifest ----
    manifest.file_hashes = build_sha256_manifest(staging_dir)
    manifest.total_bytes_uncompressed = sum(
        staging_dir.joinpath(p).stat().st_size
        for p in manifest.file_hashes
        if staging_dir.joinpath(p).exists()
    )

    # Write manifest sidecar into staging before compression
    manifest_path = staging_dir / "SOVEREIGN_MANIFEST.json"
    manifest_json = json.dumps(asdict(manifest), indent=2)
    manifest_path.write_text(manifest_json)
    # Rehash the manifest itself
    manifest.file_hashes["SOVEREIGN_MANIFEST.json"] = _sha256_file(manifest_path)
    manifest_path.write_text(json.dumps(asdict(manifest), indent=2))

    # Determine actual suffix (zstd if available, gz fallback)
    zst_suffix = ".tar.zst" if (shutil.which("zstd") and shutil.which("tar")) else ".tar.gz"
    proposed_archive = archive_dir / f"sovereign_backup_{ts}{zst_suffix}"
    try:
        actual_archive, compressed_size = compress_zstd(staging_dir, proposed_archive, threads)
        archive_path = actual_archive   # may differ from proposed if fallback triggered
        manifest.archive_path = str(archive_path)
        log.info("[Archive] Compressed size: %.2f MB", compressed_size / 1e6)
        ratio = manifest.total_bytes_uncompressed / max(compressed_size, 1)
        log.info("[Archive] Compression ratio: %.2fx", ratio)
    except Exception as exc:
        log.error("[Archive] Compression FAILED: %s", exc)
        return 1

    # ---- Section 5: Reed-Solomon Parity ----
    try:
        parity_files = generate_rs_parity(archive_path, parity_dir)
        manifest.parity_files = parity_files
    except Exception as exc:
        log.warning("[RS-Parity] Parity generation failed (non-fatal): %s", exc)

    # Write final manifest alongside archive
    final_manifest_path = archive_dir / f"manifest_{ts}.json"
    final_manifest_path.write_text(json.dumps(asdict(manifest), indent=2))

    # Cleanup staging quarantine zone
    shutil.rmtree(staging_dir, ignore_errors=True)
    log.info("[Cleanup] Staging zone quarantine cleared.")

    # ---- Summary ----
    log.info("")
    log.info("=" * 72)
    log.info("  BACKUP SUMMARY")
    log.info("=" * 72)
    all_ok = True
    for name, comp in manifest.components.items():
        ok = comp.get("success", False)
        status = "OK  " if ok else "FAIL"
        size_mb = comp.get("size_bytes", 0) / 1e6
        dur = comp.get("duration_s", 0.0)
        log.info("  [%s] %-24s  %.2f MB  %.1f s", status, name, size_mb, dur)
        if not ok:
            all_ok = False
            if comp.get("error"):
                log.error("        Error: %s", comp["error"])

    log.info("")
    log.info("  Archive      : %s", archive_path)
    log.info("  Manifest     : %s", final_manifest_path)
    log.info("  Parity files : %s", manifest.parity_files)
    log.info("  Hashed files : %d", len(manifest.file_hashes))
    log.info("=" * 72)

    return 0 if all_ok else 1


# ---------------------------------------------------------------------------
# Section 4: Total Ecosystem Resurrection Protocol
# ---------------------------------------------------------------------------

def _ensure_named_volume(volume_name: str) -> None:
    """
    Section 4.1 Infrastructure Scaffolding:
    Create a Docker named volume (Ext4 backend) if it does not exist.
    """
    r = _run(["docker", "volume", "inspect", volume_name], check=False, timeout=10)
    if r.returncode != 0:
        log.info("[Hydration] Creating named volume: %s", volume_name)
        _run(["docker", "volume", "create", volume_name])
    else:
        log.info("[Hydration] Named volume already exists: %s", volume_name)


def restore_postgres(restore_dir: Path, threads: int) -> ComponentResult:
    """
    Section 4.2 PostgreSQL Hydration:
    pg_restore with Directory Format, --clean --if-exists --no-owner, parallel workers.
    """
    result = ComponentResult(name="postgresql_restore")
    t0 = time.perf_counter()
    pg_dir = restore_dir / "postgresql_dump"

    if not pg_dir.exists():
        result.error = "postgresql_dump directory not found in archive"
        result.duration_s = time.perf_counter() - t0
        return result

    if not _container_running(C_POSTGRES):
        result.error = f"Container '{C_POSTGRES}' is not running -- start stack first"
        result.duration_s = time.perf_counter() - t0
        return result

    try:
        log.info("[PG-Restore] Copying Directory Format dump into container...")
        container_dump_path = "/tmp/pgdump_restore"
        _docker_exec(C_POSTGRES, ["sh", "-c", f"rm -rf {container_dump_path}"], check=False)
        _docker_cp(str(pg_dir), f"{C_POSTGRES}:{container_dump_path}")

        log.info("[PG-Restore] Running pg_restore --clean --if-exists --no-owner -j %d ...", threads)
        restore_cmd = (
            f"pg_restore -U {PG_USER} -d {PG_DB} "
            f"-Fd "
            f"--clean --if-exists --no-owner "
            f"-j {min(threads, 8)} "
            f"{container_dump_path}"
        )
        _docker_exec(
            C_POSTGRES, ["sh", "-c", restore_cmd],
            timeout=900, env_vars={"PGPASSWORD": PG_PASSWORD}
        )
        _docker_exec(C_POSTGRES, ["rm", "-rf", container_dump_path], check=False)

        result.success = True
        log.info("[PG-Restore] PostgreSQL hydration complete.")
        result.notes.append("--clean --if-exists strips init schemas; --no-owner prevents role mismatch")

    except Exception as exc:
        result.error = str(exc)
        log.error("[PG-Restore] FAILED: %s", exc)
    finally:
        result.duration_s = time.perf_counter() - t0
    return result


def restore_qdrant(restore_dir: Path) -> ComponentResult:
    """
    Section 4.3 Qdrant Injection:
    Iterative collection upload via multipart/form-data REST API.
    Uses priority=snapshot to replace default initialization state.
    """
    result = ComponentResult(name="qdrant_restore")
    t0 = time.perf_counter()
    qdrant_dir = restore_dir / "qdrant_snapshots"

    if not qdrant_dir.exists():
        result.error = "qdrant_snapshots directory not found in archive"
        result.duration_s = time.perf_counter() - t0
        return result

    # Check Qdrant is alive
    status, _ = _http_get(f"{QDRANT_URL}/collections", timeout=10)
    if status != 200:
        result.error = f"Qdrant not reachable (HTTP {status})"
        result.duration_s = time.perf_counter() - t0
        return result

    meta_files = sorted(qdrant_dir.glob("*__meta.json"))
    if not meta_files:
        result.error = "No Qdrant snapshot meta files found"
        result.duration_s = time.perf_counter() - t0
        return result

    failed: List[str] = []
    for meta_path in meta_files:
        meta = json.loads(meta_path.read_text())
        cname = meta["collection"]
        snap_file = qdrant_dir / meta["snapshot_file"]
        upload_endpoint = meta.get("restore_endpoint",
                                    f"/collections/{cname}/snapshots/upload?priority=snapshot")

        if not snap_file.exists():
            log.error("[Qdrant-Restore] Snapshot file missing: %s", snap_file)
            failed.append(cname)
            continue

        try:
            log.info("[Qdrant-Restore] Uploading collection '%s' (%.2f MB)...",
                     cname, snap_file.stat().st_size / 1e6)
            url = f"{QDRANT_URL}{upload_endpoint}"
            up_status, up_body = _http_upload_multipart(url, snap_file)

            if up_status not in (200, 201):
                raise RuntimeError(f"Upload returned HTTP {up_status}: {up_body[:200]}")

            log.info("[Qdrant-Restore] Collection '%s' injected successfully.", cname)
        except Exception as exc:
            log.error("[Qdrant-Restore] Failed '%s': %s", cname, exc)
            failed.append(cname)

    result.success = (len(failed) == 0)
    result.duration_s = time.perf_counter() - t0
    if failed:
        result.error = f"Failed collections: {failed}"
    else:
        result.notes.append("All collections injected via multipart/form-data with priority=snapshot")
    return result


def restore_sqlite(restore_dir: Path) -> ComponentResult:
    """
    Section 4.4 SQLite Replacement:
    Suspend Open WebUI, perform binary volume replacement, resume.
    """
    result = ComponentResult(name="sqlite_restore")
    t0 = time.perf_counter()
    vacuum_db = restore_dir / "webui_vacuum.db"

    if not vacuum_db.exists():
        result.error = "webui_vacuum.db not found in archive"
        result.duration_s = time.perf_counter() - t0
        return result

    try:
        log.info("[SQLite-Restore] Suspending Open WebUI container...")
        _run(["docker", "stop", C_WEBUI], timeout=60)

        log.info("[SQLite-Restore] Performing binary file replacement in named volume...")
        container_db_path = "/app/backend/data/webui.db"
        _docker_cp(str(vacuum_db), f"{C_WEBUI}:{container_db_path}")

        log.info("[SQLite-Restore] Resuming Open WebUI container...")
        _run(["docker", "start", C_WEBUI], timeout=30)

        result.success = True
        result.notes.append("Stop -> binary replace -> start sequence preserves volume Ext4 integrity")
        log.info("[SQLite-Restore] SQLite hydration complete.")

    except Exception as exc:
        result.error = str(exc)
        log.error("[SQLite-Restore] FAILED: %s", exc)
        # Always attempt to resume container
        try:
            _run(["docker", "start", C_WEBUI], timeout=30, check=False)
        except Exception:
            pass
    finally:
        result.duration_s = time.perf_counter() - t0
    return result


def restore_clickhouse(restore_dir: Path) -> ComponentResult:
    """
    Restore ClickHouse database from zip file native backup.
    """
    result = ComponentResult(name="clickhouse_restore")
    t0 = time.perf_counter()
    ch_backup_zip = restore_dir / "clickhouse_backup.zip"

    if not ch_backup_zip.exists():
        result.error = "clickhouse_backup.zip file not found in archive"
        result.duration_s = time.perf_counter() - t0
        return result

    if not _container_running(C_CLICKHOUSE):
        result.error = f"Container '{C_CLICKHOUSE}' is not running"
        result.duration_s = time.perf_counter() - t0
        return result

    try:
        log.info("[ClickHouse-Restore] Copying backup file into container...")
        container_backup_zip = "/var/lib/clickhouse/backups/sovereign_restore.zip"
        _docker_exec(C_CLICKHOUSE, ["rm", "-f", container_backup_zip], check=False)
        _docker_exec(C_CLICKHOUSE, ["mkdir", "-p", "/var/lib/clickhouse/backups"])
        _docker_cp(str(ch_backup_zip), f"{C_CLICKHOUSE}:{container_backup_zip}")

        log.info("[ClickHouse-Restore] Executing native RESTORE ALL ...")
        # ClickHouse RESTORE command
        restore_sql = "RESTORE ALL FROM File('sovereign_restore.zip') SETTINGS async=0"
        _docker_exec(
            C_CLICKHOUSE,
            ["clickhouse-client", "--query", restore_sql],
            timeout=600
        )

        _docker_exec(C_CLICKHOUSE, ["rm", "-f", container_backup_zip], check=False)
        result.success = True
        log.info("[ClickHouse-Restore] ClickHouse restore complete.")
    except Exception as exc:
        result.error = str(exc)
        log.error("[ClickHouse-Restore] FAILED: %s", exc)
    finally:
        result.duration_s = time.perf_counter() - t0
    return result


def restore_minio(restore_dir: Path) -> ComponentResult:
    """
    Restore MinIO objects from staging directory.
    Uses 'mc mirror' inside the container.
    """
    result = ComponentResult(name="minio_restore")
    t0 = time.perf_counter()
    minio_dir = restore_dir / "minio_data"

    if not minio_dir.exists():
        result.error = "minio_data directory not found in archive"
        result.duration_s = time.perf_counter() - t0
        return result

    if not _container_running(C_MINIO):
        result.error = f"Container '{C_MINIO}' is not running"
        result.duration_s = time.perf_counter() - t0
        return result

    try:
        log.info("[MinIO-Restore] Copying mirror data into container staging...")
        _docker_exec(C_MINIO, ["rm", "-rf", "/tmp/minio_restore_staging"], check=False)
        _docker_exec(C_MINIO, ["mkdir", "-p", "/tmp/minio_restore_staging"])
        _docker_cp(str(minio_dir) + "/.", f"{C_MINIO}:/tmp/minio_restore_staging/")

        log.info("[MinIO-Restore] Restoring buckets using mc mirror...")
        mc_alias_cmd = (
            "mc alias set sovereign_local "
            "http://localhost:9000 "
            "${MINIO_ROOT_USER:-minio} "
            "${MINIO_ROOT_PASSWORD:-miniosecret}"
        )
        _docker_exec(C_MINIO, ["sh", "-c", mc_alias_cmd], timeout=30)

        # Mirror back from staging directory to local minio
        mc_mirror_cmd = "mc mirror --preserve /tmp/minio_restore_staging sovereign_local"
        _docker_exec(C_MINIO, ["sh", "-c", mc_mirror_cmd], timeout=600)

        _docker_exec(C_MINIO, ["rm", "-rf", "/tmp/minio_restore_staging"], check=False)

        result.success = True
        log.info("[MinIO-Restore] MinIO restore complete.")
    except Exception as exc:
        result.error = str(exc)
        log.error("[MinIO-Restore] FAILED: %s", exc)
    finally:
        result.duration_s = time.perf_counter() - t0
    return result


def cmd_restore(args: argparse.Namespace) -> int:
    """
    Section 4: Total Ecosystem Resurrection Protocol.
    Extract archive -> scaffold named volumes -> multi-phase hydration.
    """
    archive_path = Path(args.archive)
    if not archive_path.exists():
        log.error("Archive not found: %s", archive_path)
        return 1

    threads = args.threads

    log.info("=" * 72)
    log.info("  SOVEREIGN DR ENGINE -- RESTORATION PHASE")
    log.info("  Archive: %s", archive_path)
    log.info("=" * 72)

    # Decompress into a temp restore staging area
    restore_staging = Path(tempfile.mkdtemp(prefix="sovereign_restore_"))
    log.info("[Phase 0] Decompressing archive to: %s", restore_staging)

    try:
        if str(archive_path).endswith(".tar.zst"):
            if not shutil.which("zstd") or not shutil.which("tar"):
                log.error("zstd + tar required to decompress .tar.zst archives")
                return 1
            decomp = subprocess.run(
                ["sh", "-c",
                 f"zstd -d --stdout '{archive_path}' | tar -xf - -C '{restore_staging}'"],
                timeout=1800, capture_output=True
            )
            if decomp.returncode != 0:
                raise RuntimeError(decomp.stderr.decode(errors="replace"))
        elif str(archive_path).endswith(".tar.gz"):
            import tarfile
            with tarfile.open(archive_path, "r:gz") as tar:
                tar.extractall(restore_staging)
        else:
            log.error("Unsupported archive format: %s", archive_path.suffix)
            return 1

        # Locate the inner staging directory
        inner_dirs = [d for d in restore_staging.iterdir() if d.is_dir()]
        if len(inner_dirs) == 1:
            restore_dir = inner_dirs[0]
        else:
            restore_dir = restore_staging

        log.info("[Phase 0] Archive extracted. Restore root: %s", restore_dir)

        # ---- Phase 1: Infrastructure Scaffolding ----
        log.info("")
        log.info("[Phase 1] Infrastructure Scaffolding -- Named Volume creation")
        for vol in [VOL_POSTGRES, VOL_CLICKHOUSE, VOL_MINIO, VOL_WEBUI]:
            try:
                _ensure_named_volume(vol)
            except Exception as exc:
                log.warning("  Volume '%s': %s", vol, exc)

        components = [c.strip().lower() for c in args.components.split(",")]
        run_all = "all" in components

        results = []

        # ---- Phase 2: PostgreSQL Hydration ----
        if run_all or "pg" in components or "postgresql" in components:
            log.info("")
            log.info("[Phase 2] PostgreSQL Hydration")
            r_pg = restore_postgres(restore_dir, threads)
            results.append(r_pg)

        # ---- Phase 3: Qdrant Injection ----
        if run_all or "qdrant" in components:
            log.info("")
            log.info("[Phase 3] Qdrant Vector Memory Injection")
            r_qd = restore_qdrant(restore_dir)
            results.append(r_qd)

        # ---- Phase 4: SQLite Replacement ----
        if run_all or "sqlite" in components or "webui" in components:
            log.info("")
            log.info("[Phase 4] SQLite Binary Replacement (Open WebUI)")
            r_sql = restore_sqlite(restore_dir)
            results.append(r_sql)

        # ---- ClickHouse Restoration ----
        if run_all or "ch" in components or "clickhouse" in components:
            log.info("")
            log.info("[ClickHouse] Restoration")
            r_ch = restore_clickhouse(restore_dir)
            results.append(r_ch)

        # ---- MinIO Restoration ----
        if run_all or "minio" in components:
            log.info("")
            log.info("[MinIO] Restoration")
            r_mn = restore_minio(restore_dir)
            results.append(r_mn)

        # ---- Summary ----
        log.info("")
        log.info("=" * 72)
        log.info("  RESTORATION SUMMARY")
        log.info("=" * 72)
        all_ok = True
        for r in results:
            ok = r.success
            status = "OK  " if ok else "FAIL"
            log.info("  [%s] %-28s  %.1f s", status, r.name, r.duration_s)
            if not ok and r.error:
                log.error("        Error: %s", r.error)
                all_ok = False
        log.info("=" * 72)
        return 0 if all_ok else 1

    except Exception as exc:
        log.error("Restoration FATAL: %s", exc)
        return 1
    finally:
        shutil.rmtree(restore_staging, ignore_errors=True)


# ---------------------------------------------------------------------------
# Archive verification
# ---------------------------------------------------------------------------

def cmd_verify(args: argparse.Namespace) -> int:
    """
    Verify a sovereign archive by:
      1. Decompressing to temp dir
      2. Re-hashing every file and comparing against SOVEREIGN_MANIFEST.json
      3. Running RS parity check if sidecar present
    """
    archive_path = Path(args.archive)
    if not archive_path.exists():
        log.error("Archive not found: %s", archive_path)
        return 1

    log.info("=" * 72)
    log.info("  SOVEREIGN DR ENGINE -- VERIFICATION PHASE")
    log.info("  Archive: %s", archive_path)
    log.info("=" * 72)

    restore_staging = Path(tempfile.mkdtemp(prefix="sovereign_verify_"))
    try:
        # Decompress
        if str(archive_path).endswith(".tar.zst"):
            subprocess.run(
                ["sh", "-c",
                 f"zstd -d --stdout '{archive_path}' | tar -xf - -C '{restore_staging}'"],
                timeout=1800, check=True, capture_output=True
            )
        else:
            import tarfile
            with tarfile.open(archive_path, "r:gz") as tar:
                tar.extractall(restore_staging)

        inner_dirs = [d for d in restore_staging.iterdir() if d.is_dir()]
        restore_dir = inner_dirs[0] if len(inner_dirs) == 1 else restore_staging

        # Load manifest
        manifest_path = restore_dir / "SOVEREIGN_MANIFEST.json"
        if not manifest_path.exists():
            log.error("SOVEREIGN_MANIFEST.json not found in archive -- cannot verify")
            return 1

        stored_manifest = json.loads(manifest_path.read_text())
        stored_hashes: Dict[str, str] = stored_manifest.get("file_hashes", {})
        log.info("[Verify] Manifest loaded. %d file hashes to verify.", len(stored_hashes))

        # Recompute hashes
        mismatches: List[str] = []
        missing: List[str] = []
        for rel_path, expected in stored_hashes.items():
            if rel_path == "SOVEREIGN_MANIFEST.json":
                continue
            fpath = restore_dir / rel_path
            if not fpath.exists():
                missing.append(rel_path)
                log.warning("  MISSING : %s", rel_path)
                continue
            actual = _sha256_file(fpath)
            if actual != expected:
                mismatches.append(rel_path)
                log.error("  MISMATCH: %s", rel_path)
                log.error("    Expected : %s", expected)
                log.error("    Actual   : %s", actual)
            else:
                log.debug("  OK      : %s", rel_path)

        log.info("[Verify] SHA-256 check complete.")
        log.info("  Files verified : %d", len(stored_hashes))
        log.info("  Mismatches     : %d", len(mismatches))
        log.info("  Missing        : %d", len(missing))

        # RS parity check
        parity_dir = archive_path.parent / "parity"
        parity_file = parity_dir / (archive_path.name + ".par2")
        if parity_file.exists():
            log.info("[RS-Parity] Verifying Reed-Solomon parity sidecar...")
            ok = verify_parity_file(archive_path, parity_file)
            if ok:
                log.info("[RS-Parity] Parity check PASSED -- archive bit-integrity confirmed")
            else:
                log.error("[RS-Parity] Parity check FAILED -- archive may be corrupted")
                mismatches.append("__rs_parity__")
        else:
            log.info("[RS-Parity] No parity sidecar found at %s -- skipping", parity_file)

        integrity_ok = (len(mismatches) == 0 and len(missing) == 0)
        if integrity_ok:
            log.info("VERIFICATION RESULT: PASSED -- archive integrity confirmed")
        else:
            log.error("VERIFICATION RESULT: FAILED -- %d mismatch(es), %d missing file(s)",
                      len(mismatches), len(missing))
        return 0 if integrity_ok else 1

    except Exception as exc:
        log.error("Verification FATAL: %s", exc)
        return 1
    finally:
        shutil.rmtree(restore_staging, ignore_errors=True)


# ---------------------------------------------------------------------------
# Section 5: WSL2 host hardening (.wslconfig)
# ---------------------------------------------------------------------------

WSLCONFIG_TEMPLATE = """\
# Sovereign AI Stack -- WSL2 Resource Caps
# Applied by sovereign_dr_engine.py harden command
# Reference: Section 4 -- Host Environment Hardening

[wsl2]
memory={memory_gb}GB
swap=0
processors={processors}
kernelCommandLine=vsyscall=emulate

[experimental]
autoMemoryReclaim=gradual
sparseVhd=true
"""


def cmd_harden(args: argparse.Namespace) -> int:
    """
    Section 4 / Section 5: Apply WSL2 resource caps to `%UserProfile%\\.wslconfig`.
    Enforces memory ceiling and disables swap to prevent host instability
    during high-throughput AI agent operations.
    """
    memory_gb = args.memory_gb
    processors = RYZEN_9950X_THREADS

    log.info("=" * 72)
    log.info("  SOVEREIGN DR ENGINE -- HOST HARDENING")
    log.info("  Target : %s", WSLCONFIG_PATH)
    log.info("  Memory : %dGB", memory_gb)
    log.info("  Procs  : %d (Ryzen 9950X logical threads)", processors)
    log.info("=" * 72)

    config_content = WSLCONFIG_TEMPLATE.format(
        memory_gb=memory_gb, processors=processors
    )

    if WSLCONFIG_PATH.exists():
        backup = WSLCONFIG_PATH.with_suffix(".wslconfig.bak")
        shutil.copy2(WSLCONFIG_PATH, backup)
        log.info("[Harden] Existing .wslconfig backed up to: %s", backup)

    WSLCONFIG_PATH.write_text(config_content)
    log.info("[Harden] .wslconfig written successfully.")
    log.info("")
    log.info("  IMPORTANT: Restart WSL2 for settings to take effect:")
    log.info("    wsl --shutdown")
    log.info("    wsl")
    log.info("")
    log.info("[Harden] Named Volume usage mandate:")
    log.info("  All persistent services must use Docker Named Volumes (Ext4)")
    log.info("  not bind mounts (NTFS/9P), as mandated by the architectural framework.")
    log.info("  Verify with: docker inspect <container> | grep -A5 Mounts")

    return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sovereign_dr_engine",
        description="Zero-Downtime Sovereign AI Disaster Recovery Engine v2.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--threads", type=int, default=DEFAULT_THREADS,
                        help=f"Parallel threads (default: {DEFAULT_THREADS} -- Ryzen 9950X)")

    sub = parser.add_subparsers(dest="command", required=True)

    # backup
    p_backup = sub.add_parser("backup", help="Execute full zero-downtime backup pipeline")
    p_backup.add_argument("--output-dir", default=DEFAULT_OUTPUT,
                          help="Directory for archives and manifests")

    # restore
    p_restore = sub.add_parser("restore", help="Execute total ecosystem resurrection protocol")
    p_restore.add_argument("--archive", required=True,
                            help="Path to .tar.zst or .tar.gz sovereign archive")
    p_restore.add_argument("--components", default="all",
                            help="Comma-separated: all|pg|sqlite|qdrant|ch|minio")

    # verify
    p_verify = sub.add_parser("verify", help="Cryptographically verify archive integrity")
    p_verify.add_argument("--archive", required=True, help="Path to archive file")

    # harden
    p_harden = sub.add_parser("harden", help="Apply WSL2 host hardening (.wslconfig)")
    p_harden.add_argument("--memory-gb", type=int, default=48,
                           help="WSL2 memory ceiling in GB (default: 48)")

    return parser


def main() -> int:
    init_container_names()
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "backup":  cmd_backup,
        "restore": cmd_restore,
        "verify":  cmd_verify,
        "harden":  cmd_harden,
    }
    return dispatch[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
