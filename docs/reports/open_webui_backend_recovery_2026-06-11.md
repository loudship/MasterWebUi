# Open WebUI Backend Recovery

**Date:** 2026-06-11  
**Decision:** **PASS - RECOVERED**

## Symptom

Opening `http://localhost:8080/` redirected to `/error` and displayed:

> Open WebUI Backend Required

The static frontend was reachable, but backend configuration requests were
failing.

## Root Cause

The live Open WebUI container stored SQLite data on the Windows bind mount:

`C:\open-webui-master\data\open-webui:/app/backend/data`

Open WebUI forces SQLite WAL mode. Malformed/stale `webui.db-wal` and
`webui.db-shm` sidecars prevented fresh SQLite connections inside the
container, producing repeated:

`sqlite3.OperationalError: unable to open database file`

The primary `webui.db` database was not corrupt:

- Host `PRAGMA quick_check`: `ok`
- Container-local copied DB `PRAGMA quick_check`: `ok`
- User count remained `1`

## Recovery

- Stopped only the `open-webui` container.
- Backed up the healthy database to:
  `C:\open-webui-master\backups\open_webui_sqlite_recovery_20260611_113802`
- Removed the malformed disposable WAL/SHM sidecars.
- Migrated the full Open WebUI data directory to Docker-managed volume:
  `open-webui-live-data`
- Recreated Open WebUI with its existing image, environment, network, auth
  secret, tools, model settings, and port.
- Updated `docker-compose.yml` to reuse `open-webui-live-data` so future
  Compose recreations do not restore the Windows SQLite bind mount.

## Verification

- Open WebUI container: `healthy`
- `http://localhost:8080/api/config`: HTTP 200
- `http://localhost:8080/health`: HTTP 200
- `http://localhost:8080/`: HTTP 200
- Web Tools Control Center: `7/7` connected, `100%` health
- SQLite `PRAGMA quick_check`: `ok`
- SQLite journal mode: `wal`
- Multiple simultaneous/fresh SQLite connections: passed
- Full Open WebUI restart durability test: passed
- Post-restart database error scan: clean
- `docker compose --profile offline-tools config --quiet`: passed
- `git diff --check`: passed, with only existing Windows line-ending notices

## Final Decision

**PASS - RECOVERED.** The backend-required error was caused by SQLite WAL on a
Windows bind mount. Open WebUI now uses a Docker-managed volume and remains
healthy after restart.
