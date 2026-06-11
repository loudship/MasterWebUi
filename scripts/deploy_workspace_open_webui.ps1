param(
    [string]$Image = "open-webui:workspace-catalog",
    [string]$Container = "open-webui",
    [int]$HealthTimeoutSeconds = 180
)

$ErrorActionPreference = "Stop"
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$previous = "$Container-pre-workspace-$stamp"
$tempEnv = Join-Path $env:TEMP "open-webui-workspace-$stamp.env"

try {
    $inspect = docker inspect $Container | ConvertFrom-Json
    if (-not $inspect) {
        throw "Container '$Container' was not found."
    }

    $envLines = [System.Collections.Generic.List[string]]::new()
    foreach ($line in $inspect[0].Config.Env) {
        if ($line -match "^(LANGFUSE_HOST|LANGFUSE_PUBLIC_KEY|LANGFUSE_SECRET_KEY)=") {
            continue
        }
        if ($line -match "^CODE_EVAL_VALVE_OVERRIDE_(AUTO_INSTALL|NETWORKING_ALLOWED|CHECK_FOR_UPDATES)=") {
            continue
        }
        if ($line -match "^ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS=") {
            continue
        }
        $envLines.Add($line)
    }
    $envLines.Add("CODE_EVAL_VALVE_OVERRIDE_AUTO_INSTALL=False")
    $envLines.Add("CODE_EVAL_VALVE_OVERRIDE_NETWORKING_ALLOWED=False")
    $envLines.Add("CODE_EVAL_VALVE_OVERRIDE_CHECK_FOR_UPDATES=False")
    $envLines.Add("ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS=False")
    [System.IO.File]::WriteAllLines($tempEnv, $envLines)

    docker stop $Container | Out-Null
    docker rename $Container $previous

    docker run -d `
        --name $Container `
        --env-file $tempEnv `
        --restart unless-stopped `
        --network open-webui-master_llm-net `
        --cgroupns private `
        -p 127.0.0.1:8080:8080 `
        -v open-webui-live-data:/app/backend/data `
        -v /sys/fs/cgroup:/sys/fs/cgroup:rw `
        $Image | Out-Null

    $deadline = (Get-Date).AddSeconds($HealthTimeoutSeconds)
    $healthy = $false
    while ((Get-Date) -lt $deadline) {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:8080/health" -TimeoutSec 5
            if ($health.status -eq $true) {
                $healthy = $true
                break
            }
        } catch {
            Start-Sleep -Seconds 3
        }
    }

    if (-not $healthy) {
        throw "The upgraded container did not become healthy within $HealthTimeoutSeconds seconds."
    }

    Write-Output "PASS: $Container is healthy on image $Image"
    Write-Output "Rollback container retained as $previous"
} catch {
    Write-Warning $_
    $newExists = docker ps -a --format "{{.Names}}" | Where-Object { $_ -eq $Container }
    if ($newExists) {
        docker rm -f $Container | Out-Null
    }
    $oldExists = docker ps -a --format "{{.Names}}" | Where-Object { $_ -eq $previous }
    if ($oldExists) {
        docker rename $previous $Container
        docker start $Container | Out-Null
    }
    throw
} finally {
    if (Test-Path -LiteralPath $tempEnv) {
        Remove-Item -LiteralPath $tempEnv -Force
    }
}
