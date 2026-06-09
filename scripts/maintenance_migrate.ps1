param(
    [string]$BackupRoot = ".\backups\hardened-cutover-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
)

$ErrorActionPreference = "Stop"
$resolvedBackup = [System.IO.Path]::GetFullPath((Join-Path $PWD $BackupRoot))
New-Item -ItemType Directory -Path $resolvedBackup -Force | Out-Null

$writeContainers = @(
    "open-webui",
    "deep-web-mcp",
    "langgraph-orchestrator",
    "monitor-daemon",
    "qdrant"
)
foreach ($container in $writeContainers) {
    if (docker ps --format "{{.Names}}" | Select-String -SimpleMatch $container) {
        docker stop $container | Out-Null
    }
}

Copy-Item -LiteralPath ".\data\open-webui\webui.db" -Destination $resolvedBackup -Force
Get-ChildItem -LiteralPath ".\data\open-webui" -Filter "webui.db-*" -ErrorAction SilentlyContinue |
    Copy-Item -Destination $resolvedBackup -Force
Copy-Item -LiteralPath ".\data\deep-web-mcp\auth_vault.db" -Destination $resolvedBackup -Force

if (docker ps -a --format "{{.Names}}" | Select-String -SimpleMatch "calendar-db") {
    docker exec calendar-db pg_dump -U calendar -d calendar_db -Fc -f /tmp/calendar_db.dump
    docker cp calendar-db:/tmp/calendar_db.dump (Join-Path $resolvedBackup "calendar_db.dump")
}

docker compose --profile migration run --rm state-migration

$qdrantRoot = [System.IO.Path]::GetFullPath((Join-Path $PWD ".\data\qdrant"))
$qdrantBackup = Join-Path $resolvedBackup "qdrant-snapshots"
Get-ChildItem -LiteralPath $qdrantRoot -Recurse -File -Filter "*.snapshot" -ErrorAction SilentlyContinue |
    ForEach-Object {
        $relative = $_.FullName.Substring($qdrantRoot.Length).TrimStart([char]"\", [char]"/")
        $destination = Join-Path $qdrantBackup $relative
        New-Item -ItemType Directory -Path (Split-Path $destination) -Force | Out-Null
        Copy-Item -LiteralPath $_.FullName -Destination $destination -Force
    }

Write-Host "Maintenance migration complete. Backup set: $resolvedBackup"
