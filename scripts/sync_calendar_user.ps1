param(
    [string]$OpenWebUIContainer = "open-webui",
    [string]$CalendarDbContainer = "calendar-db",
    [string]$Timezone = "America/Toronto",
    [string]$CalendarId = "primary"
)

$ErrorActionPreference = "Stop"
$python = 'import sqlite3; c=sqlite3.connect("/app/backend/data/webui.db"); print(c.execute("select id from user where role=''admin'' limit 1").fetchone()[0])'
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($python))
$userId = docker exec $OpenWebUIContainer python -c "import base64; exec(base64.b64decode('$encoded'))"
if (-not $userId) {
    throw "No Open WebUI admin user was found."
}

$sql = @"
INSERT INTO users (user_id, role, calendar_id, timezone, created_at, updated_at)
VALUES ('$userId', 'admin', '$CalendarId', '$Timezone', now(), now())
ON CONFLICT (user_id) DO UPDATE
SET role = EXCLUDED.role,
    calendar_id = EXCLUDED.calendar_id,
    timezone = EXCLUDED.timezone,
    updated_at = now();
"@
$sql | docker exec -i $CalendarDbContainer psql -v ON_ERROR_STOP=1 -U calendar -d calendar_db | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Calendar user synchronization failed."
}
Write-Output "PASS: synchronized Open WebUI admin identity into Calendar MCP."
