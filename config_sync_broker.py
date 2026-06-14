"""
config_sync_broker.py — One-off "golden config" enforcement tool.

Applies the values from master_webui_config.yaml (RAG, search, models, audio/tts)
into a running Open WebUI instance via its internal REST APIs.

NOTE: In the current hardened air-gap setup, most of these settings are already
baked at container startup via docker-compose.yml environment and the
inference/pipelines/langgraph services. This script is retained for:
- Initial bootstrap / drift repair on an existing stack
- Operators who prefer "config as code" POST after the fact

It is intentionally a standalone CLI (if __name__ main) like other ops tools.
See also: services/config-drift-monitor/ and config/config-drift-baseline.yaml.
"""
import os
import sys
import yaml
import json
import time
import glob
import logging
import requests
import subprocess
from pathlib import Path
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

WEBUI_BASE_URL = os.getenv("WEBUI_BASE_URL", "http://localhost:3000").rstrip("/")
WEBUI_API_KEY = os.getenv("WEBUI_API_KEY")

if not WEBUI_API_KEY:
    logger.warning("WEBUI_API_KEY is not set in environment. Continuing without authentication.")

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json"
}

if WEBUI_API_KEY:
    HEADERS["Authorization"] = f"Bearer {WEBUI_API_KEY}"

def write_error_log(payload, endpoint, status_code, message):
    error_log_path = "sync_error_log.json"
    error_data = {
        "endpoint": endpoint,
        "status_code": status_code,
        "message": message,
        "expected_payload": payload,
        "timestamp": time.time()
    }
    with open(error_log_path, "w") as f:
        json.dump(error_data, f, indent=4)
    logger.error(f"Dumped error payload to {error_log_path}")

def api_get(endpoint):
    url = f"{WEBUI_BASE_URL}{endpoint}"
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"GET {endpoint} failed: {e}")
        print("[FAILED - ABORTING]")
        sys.exit(1)

def api_post(endpoint, payload):
    url = f"{WEBUI_BASE_URL}{endpoint}"
    try:
        response = requests.post(url, headers=HEADERS, json=payload, timeout=10)
        
        if response.status_code in [404, 422]:
            write_error_log(payload, endpoint, response.status_code, response.text)
            print("[FAILED - ABORTING]")
            sys.exit(1)
            
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        if e.response is not None and e.response.status_code in [404, 422]:
            write_error_log(payload, endpoint, e.response.status_code, e.response.text)
        else:
            logger.error(f"POST {endpoint} failed: {e}")
        print("[FAILED - ABORTING]")
        sys.exit(1)

def check_recent_backup(backup_dir="./backups", threshold_seconds=60):
    files = glob.glob(os.path.join(backup_dir, "*.tar.gz"))
    if not files:
        return False
    # Get latest file
    latest_file = max(files, key=os.path.getmtime)
    file_age = time.time() - os.path.getmtime(latest_file)
    return file_age <= threshold_seconds

def run_pre_flight_backup():
    logger.info("Running pre-flight database backup via sovereign_backup.py...")
    try:
        result = subprocess.run([sys.executable, "sovereign_backup.py"], capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            logger.error(f"Backup script failed with return code {result.returncode}")
            logger.error(f"Output:\n{result.stdout}\n{result.stderr}")
            print("[FAILED - ABORTING]")
            sys.exit(1)
            
        # Verify backup file creation
        if not check_recent_backup():
            logger.error("Backup script succeeded, but no new .tar.gz archive was found within the last 60 seconds.")
            print("[FAILED - ABORTING]")
            sys.exit(1)
            
        logger.info("[SUCCESS] Pre-flight backup completed and validated.")
    except FileNotFoundError:
        logger.error("sovereign_backup.py not found in the current directory.")
        print("[FAILED - ABORTING]")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Exception during pre-flight backup: {e}")
        print("[FAILED - ABORTING]")
        sys.exit(1)

def load_yaml_config(filepath="master_webui_config.yaml"):
    logger.info(f"Loading configuration from {filepath}...")
    try:
        with open(filepath, "r") as f:
            config = yaml.safe_load(f)
        return config
    except Exception as e:
        logger.error(f"Failed to load YAML config: {e}")
        print("[FAILED - ABORTING]")
        sys.exit(1)

def check_dict_diff(current, target):
    """Recursively check if target fields differ from current state."""
    if current is None:
        return True
    for k, v in target.items():
        if isinstance(v, dict):
            if k not in current or not isinstance(current[k], dict):
                return True
            if check_dict_diff(current[k], v):
                return True
        else:
            if current.get(k) != v:
                return True
    return False

def sync_rag_and_search(config):
    if not config:
        logger.info("No configuration found, skipping RAG/Search sync.")
        return
    logger.info("Checking RAG and Web Search settings...")
    existing = api_get("/api/v1/retrieval/config") or {}
    
    payload = {}
    if "rag" in config:
        payload.update(config["rag"])
    if "search" in config:
        payload["web"] = config["search"]

    # Idempotency check
    if check_dict_diff(existing, payload):
        logger.info("Differences detected in RAG/Search config. Pushing updates...")
        api_post("/api/v1/retrieval/config/update", payload)
        
        # Verify
        updated = api_get("/api/v1/retrieval/config")
        if check_dict_diff(updated, payload):
            logger.error("Verification failed for RAG/Search settings after POST.")
            print("[FAILED - ABORTING]")
            sys.exit(1)
        logger.info("[SUCCESS] RAG and Search settings synced and verified.")
    else:
        logger.info("[SUCCESS] RAG and Search settings are already up-to-date.")

def sync_models(config):
    if not config:
        logger.info("No configuration found, skipping Models sync.")
        return
    logger.info("Checking Model settings (Context length, system prompts)...")
    if "models" not in config:
        logger.info("No models configuration found, skipping.")
        return
        
    existing = api_get("/api/v1/configs/models") or {}
    
    payload = {
        "DEFAULT_MODELS": existing.get("DEFAULT_MODELS"),
        "DEFAULT_PINNED_MODELS": existing.get("DEFAULT_PINNED_MODELS"),
        "MODEL_ORDER_LIST": existing.get("MODEL_ORDER_LIST", []),
        "DEFAULT_MODEL_METADATA": existing.get("DEFAULT_MODEL_METADATA", {}),
        "DEFAULT_MODEL_PARAMS": existing.get("DEFAULT_MODEL_PARAMS", {})
    }
    
    # Merge DEFAULT_MODEL_PARAMS
    target_params = config["models"].get("DEFAULT_MODEL_PARAMS", {})
    for k, v in target_params.items():
        payload["DEFAULT_MODEL_PARAMS"][k] = v

    if check_dict_diff(existing.get("DEFAULT_MODEL_PARAMS", {}), target_params):
        logger.info("Differences detected in Models config. Pushing updates...")
        api_post("/api/v1/configs/models", payload)
        
        # Verify
        updated = api_get("/api/v1/configs/models")
        if check_dict_diff(updated.get("DEFAULT_MODEL_PARAMS", {}), target_params):
            logger.error("Verification failed for Models settings after POST.")
            print("[FAILED - ABORTING]")
            sys.exit(1)
        logger.info("[SUCCESS] Model settings synced and verified.")
    else:
        logger.info("[SUCCESS] Model settings are already up-to-date.")

def sync_audio(config):
    if not config:
        logger.info("No configuration found, skipping Audio sync.")
        return
    logger.info("Checking Audio/TTS settings...")
    if "audio" not in config:
        logger.info("No audio configuration found, skipping.")
        return
        
    existing = api_get("/api/v1/audio/config") or {}
    
    payload = {
        "tts": existing.get("tts", {}),
        "stt": existing.get("stt", {})
    }
    
    target_tts = config["audio"].get("tts", {})
    for k, v in target_tts.items():
        payload["tts"][k] = v

    if check_dict_diff(existing.get("tts", {}), target_tts):
        logger.info("Differences detected in Audio config. Pushing updates...")
        api_post("/api/v1/audio/config/update", payload)
        
        # Verify
        updated = api_get("/api/v1/audio/config")
        if check_dict_diff(updated.get("tts", {}), target_tts):
            logger.error("Verification failed for Audio settings after POST.")
            print("[FAILED - ABORTING]")
            sys.exit(1)
        logger.info("[SUCCESS] Audio settings synced and verified.")
    else:
        logger.info("[SUCCESS] Audio settings are already up-to-date.")

def main():
    print("=== Open WebUI Configuration-as-Code Sync Pipeline ===")
    run_pre_flight_backup()
    
    config = load_yaml_config()
    
    sync_rag_and_search(config)
    sync_models(config)
    sync_audio(config)
    
    print("\n[SUCCESS] Synchronization complete. All golden settings applied to Open WebUI.")

if __name__ == "__main__":
    main()
