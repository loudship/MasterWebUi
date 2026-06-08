# verify_relocation_diagnostics.py
# Diagnostic Verification Script for Open WebUI Stack after File Relocation
# Evaluates host paths, SQLite integrity, Docker microservices, RAG pipeline endpoints, and host connections.

import os
import sys
import sqlite3
import json
import socket
import urllib.request
import urllib.error
import subprocess
from pathlib import Path

# Enable ANSI colors on Windows Console
if sys.platform == "win32":
    os.system("color")

# Colors
C_GREEN = "\033[92m"
C_YELLOW = "\033[93m"
C_RED = "\033[91m"
C_CYAN = "\033[96m"
C_RESET = "\033[0m"

def print_section(title):
    print(f"\n{C_CYAN}=== {title} ==={C_RESET}")

def print_result(item_name, success, info="", warning=False):
    if success:
        print(f" [{C_GREEN}OK{C_RESET}] {item_name} {f'- {info}' if info else ''}")
    elif warning:
        print(f" [{C_YELLOW}WARN{C_RESET}] {item_name} {f'- {info}' if info else ''}")
    else:
        print(f" [{C_RED}FAIL{C_RESET}] {item_name} {f'- {info}' if info else ''}")

def check_port_open(host, port, timeout=2):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False

def check_http_status(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False

def run_command(cmd):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, shell=True, timeout=10)
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
    except Exception as e:
        return False, "", str(e)

def main():
    print(f"{C_CYAN}======================================================================{C_RESET}")
    print(f"{C_CYAN}         SOVEREIGN AI ECOSYSTEM - POST-RELOCATION DIAGNOSTICS         {C_RESET}")
    print(f"{C_CYAN}======================================================================{C_RESET}")
    
    current_dir = Path(__file__).parent.resolve()
    print(f"Current project path: {current_dir}")
    
    # ---------------------------------------------------------------------------
    # Section 1: Host-level Path and Workspace Validation
    # ---------------------------------------------------------------------------
    print_section("1. Host-level Path & Workspace Integrity")
    
    # Check if .env exists
    env_path = current_dir / ".env"
    env_exists = env_path.exists()
    print_result(".env File Presence", env_exists, f"Path: {env_path}")
    
    # Load .env variables
    env_vars = {}
    if env_exists:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env_vars[k.strip()] = v.strip()
        print_result(".env Load Success", True, f"Loaded {len(env_vars)} variables")
    else:
        print_result(".env Load Success", False, "Missing .env configuration file")

    # Verify WSL2 resource cap config (.wslconfig)
    user_profile = os.environ.get("USERPROFILE", "C:/Users/Default")
    wslconfig_path = Path(user_profile) / ".wslconfig"
    wslconfig_exists = wslconfig_path.exists()
    
    if wslconfig_exists:
        # Check settings
        has_memory = False
        has_swap = False
        with open(wslconfig_path, "r") as f:
            content = f.read()
            if "memory=" in content:
                has_memory = True
            if "swap=0" in content:
                has_swap = True
        
        info = f"Path: {wslconfig_path}"
        if not has_memory or not has_swap:
            info += " (Warning: memory limit or swap=0 missing)"
            print_result("WSL2 Hardware Hardening (.wslconfig)", False, info, warning=True)
        else:
            print_result("WSL2 Hardware Hardening (.wslconfig)", True, info)
    else:
        print_result("WSL2 Hardware Hardening (.wslconfig)", False, f"Not found at {wslconfig_path}", warning=True)

    # ---------------------------------------------------------------------------
    # Section 2: Windows Task Scheduler Path Check
    # ---------------------------------------------------------------------------
    print_section("2. Windows Task Scheduler & Backup Registration")
    
    has_scheduler, sched_out, sched_err = run_command(
        'powershell -Command "Get-ScheduledTask -TaskName GhostCommand_SovereignDR_v2 -ErrorAction Stop | Select-Object -ExpandProperty Actions"'
    )
    
    if has_scheduler and "WorkingDirectory" in sched_out:
        # Extract registered working directory
        work_dir_line = [line for line in sched_out.splitlines() if "WorkingDirectory" in line]
        if work_dir_line:
            registered_dir = work_dir_line[0].split(":", 1)[1].strip()
            dirs_match = Path(registered_dir).resolve() == current_dir.resolve()
            
            info = f"Registered: '{registered_dir}' | Local: '{current_dir}'"
            if dirs_match:
                print_result("Sovereign DR Task Working Directory", True, "Task matches current relocated workspace path")
            else:
                print_result("Sovereign DR Task Working Directory", False, f"Mismatch detected! {info}. Please run '.\\register_backup_task.ps1' to update.")
        else:
            print_result("Sovereign DR Task Actions Analysis", False, "Could not extract WorkingDirectory field")
    else:
        print_result("Sovereign DR Task Scheduler Presence", False, "Task 'GhostCommand_SovereignDR_v2' not found. Run '.\\register_backup_task.ps1' to register.")

    # ---------------------------------------------------------------------------
    # Section 3: Data Volume Writable & SQLite Integrity
    # ---------------------------------------------------------------------------
    print_section("3. Database Health & Host Directory Permissions")
    
    # Check data directories
    data_dir = current_dir / "data"
    data_exists = data_dir.exists()
    print_result("Host 'data/' Directory Presence", data_exists, f"Path: {data_dir}")
    
    if data_exists:
        # Check permissions by writing a temp file to folders
        subdirs_to_check = [
            ("Open WebUI Data", data_dir / "open-webui"),
            ("Qdrant Vector Storage", data_dir / "qdrant"),
            ("SearXNG Configuration", data_dir / "searxng"),
            ("Deep Web MCP Data", data_dir / "deep-web-mcp")
        ]
        
        for name, path in subdirs_to_check:
            if not path.exists():
                print_result(f"Directory: {name}", False, f"Folder does not exist: {path}")
                continue
            
            # Try write permissions
            try:
                temp_file = path / ".relocation_test_write"
                temp_file.write_text("test")
                temp_file.unlink()
                print_result(f"Permissions: {name}", True, "Read/Write permissions verified")
            except Exception as e:
                print_result(f"Permissions: {name}", False, f"Write permission failed: {e}")
                
        # SQLite Integrity Checks
        webui_db_path = data_dir / "open-webui" / "webui.db"
        if webui_db_path.exists():
            try:
                conn = sqlite3.connect(webui_db_path)
                cursor = conn.cursor()
                cursor.execute("PRAGMA integrity_check")
                res = cursor.fetchone()[0]
                cursor.execute("PRAGMA journal_mode")
                j_mode = cursor.fetchone()[0]
                conn.close()
                
                if res == "ok":
                    print_result("SQLite: Open WebUI Database", True, f"Integrity check OK | Mode: {j_mode}")
                else:
                    print_result("SQLite: Open WebUI Database", False, f"Corrupted! Integrity output: {res}")
            except Exception as e:
                print_result("SQLite: Open WebUI Database", False, f"Failed to connect: {e}")
        else:
            print_result("SQLite: Open WebUI Database", False, f"Database file not found at {webui_db_path}")

        mcp_db_path = data_dir / "deep-web-mcp" / "auth_vault.db"
        if mcp_db_path.exists():
            try:
                conn = sqlite3.connect(mcp_db_path)
                cursor = conn.cursor()
                cursor.execute("PRAGMA integrity_check")
                res = cursor.fetchone()[0]
                conn.close()
                if res == "ok":
                    print_result("SQLite: Deep Web MCP Vault Database", True, "Integrity check OK")
                else:
                    print_result("SQLite: Deep Web MCP Vault Database", False, f"Corrupted! Integrity output: {res}")
            except Exception as e:
                print_result("SQLite: Deep Web MCP Vault Database", False, f"Failed to connect: {e}")
        else:
            print_result("SQLite: Deep Web MCP Vault Database", False, f"Database file not found at {mcp_db_path}")

    # ---------------------------------------------------------------------------
    # Section 4: Docker Compose Microservice Status
    # ---------------------------------------------------------------------------
    print_section("4. Docker Microservices Health & Status")
    
    docker_ok, docker_out, docker_err = run_command("docker ps --format \"{{.Names}}|{{.Status}}\"")
    if docker_ok:
        running_containers = {}
        for line in docker_out.splitlines():
            if "|" in line:
                name, status = line.split("|", 1)
                running_containers[name] = status
                
        expected_services = [
            ("open-webui", "Open WebUI Core Portal"),
            ("qdrant", "Qdrant Vector Database"),
            ("pipelines", "Open WebUI Pipelines Engine"),
            ("searxng", "SearXNG Search Engine"),
            ("docling-serve", "Docling Parse Server"),
            ("crawl4ai-proxy", "Crawl4AI Web Crawl Proxy"),
            ("crawl4ai", "Crawl4AI Execution Agent"),
            ("tor-gateway", "Tor Proxy Gateway"),
            ("browserless", "Headless Chrome Engine"),
            ("redis-cache", "Redis Memory Cache"),
            ("deep-web-mcp", "Deep Web Authentication MCP"),
            ("ha-mcp", "Home Assistant MCP"),
            ("calendar-mcp", "Calendar Database MCP"),
            ("calendar-db", "Calendar Postgres Database"),
            ("vram-arbiter", "VRAM Ceiling Control Daemon"),
            ("monitor-daemon", "Ecosystem Drift Monitor"),
            ("kokoro-tts", "Kokoro Text-to-Speech Engine"),
        ]
        
        for container_name, description in expected_services:
            if container_name in running_containers:
                status = running_containers[container_name]
                is_paused = "paused" in status.lower()
                is_unhealthy = "unhealthy" in status.lower()
                
                if is_paused:
                    print_result(f"Service: {description} ({container_name})", False, f"PAUSED ({status}) - Please run 'docker compose unpause'", warning=True)
                elif is_unhealthy:
                    print_result(f"Service: {description} ({container_name})", False, f"UNHEALTHY ({status}) - Inspect container logs")
                else:
                    print_result(f"Service: {description} ({container_name})", True, f"Running ({status})")
            else:
                print_result(f"Service: {description} ({container_name})", False, "OFFLINE (Container not running)")
    else:
        print_result("Docker Daemon Connectivity", False, f"Could not list containers. Error: {docker_err}")

    # ---------------------------------------------------------------------------
    # Section 5: RAG Pipelines and MCP Source Connections
    # ---------------------------------------------------------------------------
    print_section("5. Connection Routing & RAG API Diagnostics")
    
    # 1. Qdrant HTTP REST API
    qdrant_url = env_vars.get("QDRANT_URI", "http://localhost:6333")
    # Inside docker compose it's http://qdrant:6333, but from host it is http://localhost:6333
    qdrant_host_url = qdrant_url.replace("qdrant", "localhost")
    qdrant_api_ok = check_http_status(f"{qdrant_host_url}/collections")
    print_result("Connection: Qdrant Vector REST API", qdrant_api_ok, f"URL: {qdrant_host_url}")
    
    # 2. Docling Parser Server
    docling_url = env_vars.get("DOCLING_ENGINE_URL", "http://localhost:5001")
    docling_host_url = docling_url.replace("docling-serve", "localhost")
    docling_api_ok = check_http_status(f"{docling_host_url}/")
    # Docling serve might return 404/405 on root, check if port is open instead
    docling_port_ok = check_port_open("localhost", 5001)
    print_result("Connection: Docling Document Parser", docling_port_ok, f"URL: {docling_host_url}")
    
    # 3. Crawl4AI Crawl Proxy
    crawl_url = env_vars.get("EXTERNAL_WEB_LOADER_URL", "http://localhost:8000/crawl")
    crawl_host_url = crawl_url.replace("crawl4ai-proxy", "localhost").replace("/crawl", "")
    crawl_port_ok = check_port_open("localhost", 8000)
    print_result("Connection: Crawl4AI Crawl Proxy", crawl_port_ok, f"URL: {crawl_host_url}")
    
    # 4. Kokoro TTS Engine
    tts_url = env_vars.get("AUDIO_TTS_OPENAI_API_BASE_URL", "http://localhost:8880/v1")
    tts_host_url = tts_url.replace("kokoro-tts", "localhost")
    tts_port_ok = check_port_open("localhost", 8880)
    print_result("Connection: Kokoro Text-To-Speech Engine", tts_port_ok, f"URL: {tts_host_url}")

    # 5. MCP Deep Web and HA Servers
    deep_web_mcp_ok = check_port_open("localhost", 8000)  # Verify Deep Web MCP internal socket port
    print_result("Connection: Deep Web Authentication MCP", deep_web_mcp_ok, "Local port 8000 (sse route)")

    # ---------------------------------------------------------------------------
    # Section 6: Host Inference Connection (LM Studio) & VRAM Eviction CLI
    # ---------------------------------------------------------------------------
    print_section("6. Host Inference Integration (LM Studio)")
    
    # Check if host LM Studio is listening
    lms_api_url = env_vars.get("OPENAI_API_BASE_URL", "http://localhost:4321/v1")
    # Resolve host.docker.internal to localhost for host check
    lms_host_url = lms_api_url.replace("host.docker.internal", "localhost")
    
    lms_online = check_http_status(f"{lms_host_url}/models")
    print_result("Connection: LM Studio Inference Server", lms_online, f"URL: {lms_host_url} (Must be running on host)")
    
    # Verify lms.exe binary resolution on host
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    user_profile = os.environ.get("USERPROFILE", "")
    
    lms_candidates = [
        Path(local_appdata) / "Programs" / "lm-studio" / "resources" / "app" / "resources" / "cli" / "bin" / "lms.exe",
        Path(user_profile) / ".cache" / "lm-studio" / "bin" / "lms.exe"
    ]
    
    resolved_lms = None
    for cand in lms_candidates:
        if cand.is_file():
            resolved_lms = cand
            break
            
    if resolved_lms:
        print_result("Inference: lms CLI Host Binary Location", True, f"Found at: {resolved_lms}")
    else:
        print_result("Inference: lms CLI Host Binary Location", False, "lms.exe CLI not found in standard user paths! Evictions will fail.")

    print(f"\n{C_CYAN}======================================================================{C_RESET}")
    print(f"Diagnostics complete. Review any failed [FAIL] or warning [WARN] flags.")
    print(f"For service recoveries, refer to the printed guidance details.")
    print(f"{C_CYAN}======================================================================{C_RESET}")

if __name__ == "__main__":
    main()
