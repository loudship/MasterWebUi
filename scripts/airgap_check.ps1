param(
    [string[]]$ProbeContainers = @("inference-gateway", "langgraph-orchestrator")
)

$ErrorActionPreference = "Stop"
$network = (docker network inspect llm-net | ConvertFrom-Json)[0]
$masquerade = $network.Options."com.docker.network.bridge.enable_ip_masquerade"
if ($masquerade -ne "false") {
    throw "llm-net IP masquerading is not disabled."
}

$probe = @'
import json
import socket

result = {"internal_dns": {}, "public_dns_blocked": False, "public_ip_blocked": False}
for host in ("postgres", "qdrant", "inference-gateway", "langgraph-orchestrator"):
    result["internal_dns"][host] = socket.gethostbyname(host)

try:
    socket.getaddrinfo("example.com", 443)
except OSError:
    result["public_dns_blocked"] = True

try:
    socket.create_connection(("1.1.1.1", 443), timeout=3).close()
except OSError:
    result["public_ip_blocked"] = True

print(json.dumps(result, sort_keys=True))
if not result["public_dns_blocked"] or not result["public_ip_blocked"]:
    raise SystemExit(1)
'@

$results = @()
foreach ($container in $ProbeContainers) {
    $raw = docker exec $container python -c $probe
    if ($LASTEXITCODE -ne 0) {
        throw "Air-gap probe failed in container $container."
    }
    $results += [pscustomobject]@{
        container = $container
        result = $raw | ConvertFrom-Json
    }
}

[pscustomobject]@{
    network = "llm-net"
    ip_masquerade = $masquerade
    probes = $results
} | ConvertTo-Json -Depth 6
