# register_backup_task.ps1
# Automates the execution of the Zero-Downtime Sovereign Backup pipeline via Windows Task Scheduler.

$TaskName = "GhostCommand_SovereignBackup"
$WorkingDirectory = "c:\open-webui-master"
$ScriptPath = "sovereign_backup.py"

Write-Host "Registering Scheduled Task: $TaskName..."

# Define the trigger: Daily at 3:00 AM
$Trigger = New-ScheduledTaskTrigger -Daily -At 3:00AM

# Define the action: Execute python.exe
# Note: Using the WorkingDirectory parameter ensures the script resolves local paths correctly.
$Action = New-ScheduledTaskAction -Execute "python.exe" -Argument $ScriptPath -WorkingDirectory $WorkingDirectory

# Define the principal: Run under the current logged-in user context
# This is critical to ensure Docker Desktop context is correctly mounted and reachable.
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

# Register the task
$Task = Register-ScheduledTask -TaskName $TaskName -Trigger $Trigger -Action $Action -Principal $Principal -Force

if ($Task) {
    Write-Host "Success! Scheduled task '$TaskName' registered." -ForegroundColor Green
    Write-Host "It is configured to run daily at 3:00 AM under the context of $env:USERNAME."
} else {
    Write-Host "Failed to register scheduled task." -ForegroundColor Red
}
