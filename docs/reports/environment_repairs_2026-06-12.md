# Sandbox and Calendar Environment Repairs - 2026-06-12

## Verdict

- **Code Sandbox: PASS.** Namespace creation and real Python execution are restored.
- **Calendar database and transport: PASS.** Password synchronization, Streamable HTTP, identity propagation, and database initialization are restored.
- **Calendar event retrieval: BLOCKED by external credentials.** Calendar MCP reaches Google Calendar actions but no Google Application Default Credentials exist on the host, repository, volume, or container.

## Sandbox Repair

The sandbox is embedded in the live `open-webui` container. The live container now uses:

- `CAP_SYS_ADMIN` as the only added capability.
- `seccomp=unconfined` to permit the required namespace syscalls.
- `no-new-privileges:true`.
- private cgroup namespace and writable `/sys/fs/cgroup` mount.
- `Privileged=false`.

Validation:

- `scripts/validate_sandbox_isolation.ps1`: PASS; distinct user, mount, and PID namespaces were created.
- Real `run_code_py` call: PASS; `print(2 + 2)` returned `status: OK`, output `4`.
- Additional real call returned UID `0` inside gVisor and output `42`.

## Calendar Repair

Root causes:

1. Live `calendar-mcp` used a 10-character literal password while the persisted PostgreSQL role used the 20-character configured database password.
2. The Calendar Workspace tool used the obsolete SSE client against a Streamable-HTTP MCP endpoint.
3. The wrapper omitted the required `X-User-ID` header and no Calendar user mapping existed.

Repairs:

- Added `docker-compose.calendar.yml` using one required `CALENDAR_DB_PASSWORD` for both services.
- Recreated only `calendar-mcp`; existing external Calendar volumes and network were retained.
- Added `workspace/catalog-tools/calendar_readonly.py` using bounded Streamable HTTP.
- Preserved read-only behavior.
- Added `scripts/sync_calendar_user.ps1` and synchronized the current Open WebUI admin to timezone `America/Toronto`, calendar `primary`.

Validation:

- Calendar MCP database initialization: PASS.
- No new PostgreSQL password-authentication failures: PASS.
- Streamable-HTTP initialize and tool call: PASS.
- User identity and timezone middleware: PASS.
- Google Calendar action reached: PASS.
- Google Calendar data fetch: BLOCKED because Application Default Credentials are absent.

## Required External Credential Step

To enable actual Google Calendar event data, provision Google Application Default Credentials outside the repository, set `GOOGLE_APPLICATION_CREDENTIALS_HOST_PATH`, then deploy the operator-gated read-only overlay:

`docker compose -f docker-compose.calendar.yml -f docker-compose.calendar.google.yml up -d --no-deps --force-recreate calendar-mcp`

Credentials must not be committed or embedded in Workspace tool source.

## Regression Evidence

- Repository suite: **101 passed, 3 skipped**.
- Catalog reconciliation dry-run: clean after apply.
- Open WebUI remained healthy on host port `3000`.
- Rollback container retained: `open-webui-pre-workspace-20260612_114259`.
- Catalog rollback: `backups/environment_repairs_20260612`.
