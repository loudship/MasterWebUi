# register_backup_task.ps1
# Registers the Sovereign DR Engine v2.0 backup pipeline with Windows Task Scheduler.
# Runs daily at 3:00 AM under the current user context so Docker Desktop is reachable.
#
# Usage:
#   .\register_backup_task.ps1
#   .\register_backup_task.ps1 -MemoryGB 64 -OutputDir "D:\SovereignDR\archives"

param(
    [int]$MemoryGB  = 48,
    [string]$OutputDir = "C:\SovereignDR\archives",
    [int]$Threads   = 32
)

$TaskName        = "GhostCommand_SovereignDR_v2"
$WorkingDirectory = "C:\open-webui-master"
$ScriptPath      = "sovereign_dr_engine.py"
$BackupArgs      = "backup --output-dir `"$OutputDir`" --threads $Threads"

Write-Host "=== Sovereign DR Engine -- Task Scheduler Registration ===" -ForegroundColor Cyan
Write-Host "Task Name   : $TaskName"
Write-Host "Script      : $ScriptPath"
Write-Host "Output Dir  : $OutputDir"
Write-Host "Threads     : $Threads (Ryzen 9950X)"
Write-Host ""

# Step 1: Apply WSL2 host hardening before registering the scheduled task
Write-Host "Applying WSL2 host hardening (.wslconfig)..." -ForegroundColor Yellow
python.exe $ScriptPath harden --memory-gb $MemoryGB
if ($LASTEXITCODE -ne 0) {
    Write-Host "WARNING: Harden step returned non-zero exit. Continuing..." -ForegroundColor Yellow
}

# Step 2: Ensure output directory exists
if (-not (Test-Path $OutputDir)) {
    New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
    Write-Host "Created output directory: $OutputDir"
}

# Step 3: Define scheduled task components
$Trigger = New-ScheduledTaskTrigger -Daily -At 3:00AM

$Action = New-ScheduledTaskAction `
    -Execute "python.exe" `
    -Argument "$ScriptPath $BackupArgs" `
    -WorkingDirectory $WorkingDirectory

# Run under current user with interactive logon (required for Docker Desktop context)
$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Highest

# Settings: allow task to run for up to 4 hours, restart on failure
$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -RestartCount 1 `
    -RestartInterval (New-TimeSpan -Minutes 30) `
    -StartWhenAvailable

# Step 4: Register (or update) the task
$Task = Register-ScheduledTask `
    -TaskName $TaskName `
    -Trigger $Trigger `
    -Action $Action `
    -Principal $Principal `
    -Settings $Settings `
    -Description "Sovereign AI Stack Zero-Downtime DR Backup -- sovereign_dr_engine.py v2.0" `
    -Force

if ($Task) {
    Write-Host "" 
    Write-Host "SUCCESS: Scheduled task '$TaskName' registered." -ForegroundColor Green
    Write-Host "  Schedule     : Daily at 03:00 AM"
    Write-Host "  User context : $env:USERNAME (Interactive -- Docker Desktop accessible)"
    Write-Host "  Max runtime  : 4 hours"
    Write-Host "  Restart      : 1x after 30 min on failure"
    Write-Host ""
    Write-Host "Manual trigger:"
    Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
    Write-Host ""
    Write-Host "View last run result:"
    Write-Host "  Get-ScheduledTaskInfo -TaskName '$TaskName' | Select LastRunTime,LastTaskResult"
} else {
    Write-Host "FAILED to register scheduled task." -ForegroundColor Red
    exit 1
}
