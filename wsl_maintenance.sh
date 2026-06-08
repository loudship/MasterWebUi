#!/bin/bash
set -e

echo "==================================================="
echo " WSL2 DevSecOps Maintenance & Cleanup Routine "
echo "==================================================="

echo ""
echo "[1/3] Synchronizing WSL2 System Clock..."
# WSL2 clocks frequently drift when the Windows host sleeps/hibernates.
# This prevents OAuth tokens from instantly expiring and Redis TTL desyncs.
sudo hwclock -s || echo "Warning: 'hwclock -s' failed. Ensure you have passwordless sudo or run this script as root."

echo ""
echo "[2/3] Purging Orphaned Containers on 'llm-net'..."
# Check if llm-net exists
if docker network ls | grep -q llm-net; then
    # 1. Clean up any containers on llm-net that have crashed/exited
    STOPPED=$(docker ps -a --filter network=llm-net --filter status=exited --filter status=dead -q)
    if [ ! -z "$STOPPED" ]; then
        echo " -> Removing stopped/dead containers on llm-net..."
        docker rm -f $STOPPED
    fi
    
    # 2. Specifically target ghost langfuse/postgres instances left over from rapid iterations
    # We grep the names to ensure old workers don't sit in the background consuming RAM
    GHOSTS=$(docker ps -a --format '{{.ID}} {{.Names}}' | grep -iE "langfuse|postgres" | awk '{print $1}')
    if [ ! -z "$GHOSTS" ]; then
        echo " -> Force-removing ghost Langfuse/Postgres containers..."
        docker rm -f $GHOSTS
    fi
else
    echo " -> Network 'llm-net' not found. Skipping."
fi

echo ""
echo "[3/3] Executing Deep System Prune..."
# Removes stopped containers, unused networks, dangling images, and build cache
# We omit the '--volumes' flag to protect persistent data like Qdrant and Calendar-DB
docker system prune -f

echo ""
echo "==================================================="
echo " Maintenance Routine Completed Successfully.       "
echo "==================================================="
