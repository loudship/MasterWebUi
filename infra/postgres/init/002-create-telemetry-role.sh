#!/bin/sh
# 002-create-telemetry-role.sh
# Creates the telemetry_ro read-only PostgreSQL role used by the telemetry-gateway
# service. This role is granted SELECT-only access on the ops and open_webui schemas.
# It cannot INSERT, UPDATE, DELETE, or ALTER any table.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<'SQL'
DO $$
BEGIN
  -- Create role only if it does not already exist. The placeholder password
  -- is replaced immediately below from TELEMETRY_RO_PASSWORD.
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'telemetry_ro') THEN
    CREATE ROLE telemetry_ro LOGIN PASSWORD 'telemetry_readonly_changeme';
    RAISE NOTICE 'Role telemetry_ro created.';
  ELSE
    RAISE NOTICE 'Role telemetry_ro already exists — skipping create.';
  END IF;
END $$;

-- Grant connection permission
GRANT CONNECT ON DATABASE ops TO telemetry_ro;
GRANT CONNECT ON DATABASE open_webui TO telemetry_ro;

-- Grant SELECT on all current and future tables in ops
\c ops
GRANT USAGE ON SCHEMA public TO telemetry_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO telemetry_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO telemetry_ro;

-- Grant SELECT on open_webui tables needed for workspace metrics
\c open_webui
GRANT USAGE ON SCHEMA public TO telemetry_ro;
GRANT SELECT ON TABLE auth, "user", model, tool, "function", session TO telemetry_ro;
SQL

# Apply the operator-supplied password (idempotent; avoids committing a real
# credential to this script). Must not contain single quotes.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -c "ALTER ROLE telemetry_ro PASSWORD '${TELEMETRY_RO_PASSWORD:?Set TELEMETRY_RO_PASSWORD in the operator environment}'"

echo "[002] telemetry_ro role provisioned."
