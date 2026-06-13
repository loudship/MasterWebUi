param(
    [string]$Container = "open-webui",
    [int]$TimeoutSeconds = 20
)

$ErrorActionPreference = "Stop"
$probe = @'
set -eu
before="$(readlink /proc/self/ns/user):$(readlink /proc/self/ns/mnt):$(readlink /proc/self/ns/pid)"
result="$(timeout 15 unshare --user --mount --pid --fork sh -c 'printf "%s:%s:%s" "$(readlink /proc/self/ns/user)" "$(readlink /proc/self/ns/mnt)" "$(readlink /proc/self/ns/pid)"')"
test "$before" != "$result"
printf "PASS: unshare created isolated user, mount, and PID namespaces\n"
'@

$job = Start-Job -ScriptBlock {
    param($ContainerName, $Script)
    $Script | docker exec -i $ContainerName sh
    if ($LASTEXITCODE -ne 0) {
        throw "docker exec returned exit code $LASTEXITCODE"
    }
} -ArgumentList $Container, $probe

if (-not (Wait-Job $job -Timeout $TimeoutSeconds)) {
    Stop-Job $job
    Remove-Job $job -Force
    throw "FAIL: namespace probe exceeded $TimeoutSeconds seconds."
}

$output = Receive-Job $job
$failed = $job.State -ne "Completed"
Remove-Job $job -Force
if ($failed -or $output -notmatch "^PASS:") {
    throw "FAIL: isolated unshare operation did not complete. $output"
}
$output
