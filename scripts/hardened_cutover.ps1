param(
    [switch]$ConfirmCutover
)

$ErrorActionPreference = "Stop"
if (-not $ConfirmCutover) {
    throw "Cutover is destructive to legacy containers. Re-run with -ConfirmCutover."
}

$configJson = docker compose config --format json
if ($LASTEXITCODE -ne 0) {
    throw "Compose validation failed. Supply every required secret and MODEL_ALLOWLIST."
}
$config = $configJson | ConvertFrom-Json

try {
    $inventory = Invoke-RestMethod -Uri "http://127.0.0.1:1234/v1/models" -TimeoutSec 5
    if (-not $inventory.data) {
        throw "LM Studio returned no models."
    }
} catch {
    throw "Host LM Studio is not ready at http://127.0.0.1:1234/v1/models: $_"
}

$allowlist = @(
    $config.services."inference-gateway".environment.MODEL_ALLOWLIST -split "," |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ }
)
$available = @($inventory.data | ForEach-Object { $_.id })
if (-not ($allowlist | Where-Object { $_ -in $available })) {
    throw "MODEL_ALLOWLIST does not contain a model currently exposed by LM Studio."
}

& (Join-Path $PSScriptRoot "maintenance_migrate.ps1")

$legacyContainers = @(
    docker ps -a `
        --filter "label=com.docker.compose.project=open-webui-master" `
        --format "{{.Names}}"
)
foreach ($container in $legacyContainers) {
    if ($container) {
        docker rm -f $container | Out-Null
    }
}

$legacyNetwork = "open-webui-master_llm-net"
if (docker network ls --filter "name=^${legacyNetwork}$" --format "{{.Name}}") {
    docker network rm $legacyNetwork | Out-Null
}

docker compose up --wait
if ($LASTEXITCODE -ne 0) {
    throw "Hardened stack failed to become healthy."
}

& (Join-Path $PSScriptRoot "airgap_check.ps1")
