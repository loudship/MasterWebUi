#!/usr/bin/env bash
# dev.sh - Gigascrape Setup & Boot Script

set -e

echo "[1/4] Starting backend data and parsing services..."
docker compose up -d searxng crawl4ai crawl4ai-proxy docling-serve redis-cache qdrant

echo "Waiting 15 seconds for backend initialization to prevent lock conflicts..."
sleep 15

echo "[2/4] Booting the Open WebUI orchestrator and other services..."
docker compose up -d open-webui

echo "Waiting 10 seconds for open-webui container to start..."
sleep 10

echo "[3/4] Injecting root-level dependencies for chronological video extraction..."
docker exec -u 0 open-webui pip install langchain-yt-dlp youtube_transcript_api

echo "[4/4] Validating inter-container routing and sandboxing..."
echo "Testing Crawl4AI Proxy endpoint..."
docker exec open-webui curl -s -o /dev/null -w "%{http_code}" http://crawl4ai-proxy:8000 || echo "Crawl4AI Proxy failed"

echo "Testing Docling Serve endpoint..."
docker exec open-webui curl -s -o /dev/null -w "%{http_code}" http://docling-serve:5001 || echo "Docling Serve failed"

echo "Testing SearXNG endpoint..."
docker exec open-webui curl -s -o /dev/null -w "%{http_code}" http://searxng:8080 || echo "SearXNG failed"

echo "Verifying capabilities (CapEff) on /proc/self/status inside open-webui..."
docker exec open-webui grep CapEff /proc/self/status

echo "Gigascrape Architecture Initialization Complete."
