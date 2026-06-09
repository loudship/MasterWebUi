param(
    [string]$PythonImage = "python:3.11-slim@sha256:a3ab0b966bc4e91546a033e22093cb840908979487a9fc0e6e38295747e49ac0"
)

$ErrorActionPreference = "Stop"
$root = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$targets = @{
    "inference-gateway" = "services/inference-gateway/requirements.txt"
    "langgraph-orchestrator" = "services/langgraph-orchestrator/requirements.txt"
    "deep-web-mcp" = "deep-web-mcp/requirements.offline.txt"
}

foreach ($name in $targets.Keys) {
    $destination = Join-Path $root "wheelhouse/$name"
    New-Item -ItemType Directory -Path $destination -Force | Out-Null
    $requirements = $targets[$name]
    docker run --rm `
        -v "${root}:/workspace" `
        -w /workspace `
        $PythonImage `
        python -m pip download --dest "/workspace/wheelhouse/$name" -r $requirements
}

Write-Host "Wheelhouses populated. Rebuild the local images before disconnecting external network access."
