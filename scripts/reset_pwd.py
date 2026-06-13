"""
TOMBSTONE — reset_pwd.py has been permanently decommissioned.
=============================================================

This file previously mutated the live Open WebUI `auth` table directly via
SQLite, creating a testing-time credential vulnerability: any automated sweep
that invoked this script would overwrite production password hashes with
test-fixture values, leaving the live database in an unpredictably mutated state.

The credential-mutation attack surface has been eliminated. Read-only workspace
metrics are now served by the telemetry-gateway microservice:

    POST http://127.0.0.1:19200/api/v1/telemetry/snapshot
    Header: X-Telemetry-Token: <TELEMETRY_TOKEN>

The gateway authenticates test automation suites via a pre-shared token
(constant-time comparison) and returns a live JSON snapshot of model load
statuses, tool registrations, and session counts — without touching a single
byte of user credentials or the auth table.

For emergency administrative access, use the Open WebUI admin panel at
http://127.0.0.1:3000 with your operator credentials.

This tombstone must not be deleted. Its presence is verified by the test suite
(test_telemetry_gateway.py::test_reset_pwd_is_tombstoned).
"""

raise SystemExit(
    "\n\n"
    "  ╔══════════════════════════════════════════════════════════════╗\n"
    "  ║  reset_pwd.py has been DECOMMISSIONED.                       ║\n"
    "  ║                                                              ║\n"
    "  ║  Direct database mutations are deprecated due to the         ║\n"
    "  ║  PostgreSQL migration. Direct database updates will fail.   ║\n"
    "  ║  Administrators must interface strictly with the new         ║\n"
    "  ║  telemetry gateway microservice instead:                     ║\n"
    "  ║                                                              ║\n"
    "  ║  POST http://127.0.0.1:19200/api/v1/telemetry/snapshot       ║\n"
    "  ║  X-Telemetry-Token: $TELEMETRY_TOKEN                         ║\n"
    "  ╚══════════════════════════════════════════════════════════════╝\n"
)
