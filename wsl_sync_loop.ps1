<#
.SYNOPSIS
WSL2 Infrastructure Synchronization Suite - Boot Loop

.DESCRIPTION
This script hooks into Windows system startup (via Task Scheduler or Startup folder)
to verify that the WSL2 subsystem is running and the Mirrored Networking bridge is
fully operational. It pings the Open WebUI and Pipelines containers to confirm seamless
localhost bridging.

.NOTES
Ensure WSL is configured with networkingMode=mirrored in %USERPROFILE%\.wslconfig
#>

$VerbosePreference = "Continue"

function Write-Output-Verbose {
    param([string]$Message)
    Write-Verbose "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Message"
}

Write-Output-Verbose "Starting Windows Boot Synchronization Automation Loop for WSL2..."

# 1. Verify WSL Status
Write-Output-Verbose "Checking if WSL2 subsystem is running..."
$wslStatus = wsl.exe --status *>&1

if ($LASTEXITCODE -ne 0 -or $wslStatus -match "The Windows Subsystem for Linux has not been enabled") {
    Write-Output-Verbose "[ERROR] WSL2 is not available or not installed. Exiting."
    Exit 1
}

# Ensure the default WSL distribution is actually running
$wslList = wsl.exe --list --running *>&1
if (-not ($wslList -match "Ubuntu" -or $wslList -match "Debian" -or $wslList -match "docker-desktop" -or ($wslList.Length -gt 10))) {
    Write-Output-Verbose "[INFO] Starting WSL subsystem..."
    # A quick dummy command to spin up the default distro
    wsl.exe --exec echo "WSL Initialization Check" > $null
    Start-Sleep -Seconds 5
} else {
    Write-Output-Verbose "[SUCCESS] WSL Subsystem is currently running."
}

# 2. Verify Open WebUI and Pipelines via Mirrored Loopback
$MaxRetries = 12
$RetryIntervalSeconds = 5

$PortsToCheck = @(
    @{ Name = "Open WebUI"; Port = 8080 },
    @{ Name = "Pipelines"; Port = 9099 }
)

foreach ($target in $PortsToCheck) {
    $port = $target.Port
    $name = $target.Name
    $success = $false

    Write-Output-Verbose "Testing loopback connection to $name on 127.0.0.1:$port..."

    for ($i = 1; $i -le $MaxRetries; $i++) {
        $connection = Test-NetConnection -ComputerName 127.0.0.1 -Port $port -InformationLevel Quiet -WarningAction SilentlyContinue

        if ($connection) {
            Write-Output-Verbose "[SUCCESS] Established mirrored bridge to $name (Port $port)."
            $success = $true
            break
        } else {
            Write-Output-Verbose "[WAIT] $name (Port $port) is not reachable yet. Attempt $i of $MaxRetries. Retrying in $RetryIntervalSeconds seconds..."
            Start-Sleep -Seconds $RetryIntervalSeconds
        }
    }

    if (-not $success) {
        Write-Output-Verbose "[FAILED] Could not connect to $name on port $port. Check if the container is running inside WSL."
    }
}

Write-Output-Verbose "WSL2 Boot Synchronization Automation Loop completed."
