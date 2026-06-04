import os
import sys
import time
import shutil
import tarfile
import subprocess
import requests
from datetime import datetime
from pathlib import Path

# --- Configuration ---
STAGING_BASE = "c:/open-webui-master/backup_staging"
OUTPUT_DIR = "c:/open-webui-master/backups"
OPEN_WEBUI_DIR = "c:/open-webui-master"
LANGFUSE_DB_CONTAINER = "open-webui-master-postgres-1"
WEBUI_CONTAINER = "open-webui"
QDRANT_CONTAINER = "qdrant"
QDRANT_URL = "http://localhost:6333"

def run_cmd(cmd, check=True):
    print(f"Executing: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        print(f"Error executing command: {' '.join(cmd)}")
        print(f"STDOUT: {result.stdout}")
        print(f"STDERR: {result.stderr}")
        sys.exit(1)
    return result

def main():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    staging_dir = Path(f"{STAGING_BASE}_{timestamp}")
    
    print(f"=== Zero-Downtime Sovereign Backup ===")
    print(f"Timestamp: {timestamp}")
    
    # 1. Initialize Staging
    os.makedirs(staging_dir, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    env_file = Path(OPEN_WEBUI_DIR) / ".env"
    compose_file = Path(OPEN_WEBUI_DIR) / "docker-compose.yml"
    langfuse_yml = Path(OPEN_WEBUI_DIR) / "langfuse.yml"
    
    if env_file.exists():
        shutil.copy2(env_file, staging_dir / ".env")
    if compose_file.exists():
        shutil.copy2(compose_file, staging_dir / "docker-compose.yml")
    if langfuse_yml.exists():
        shutil.copy2(langfuse_yml, staging_dir / "langfuse.yml")

    # 2. Postgres Extraction (Langfuse)
    print("\n--- Extracting Langfuse Postgres DB ---")
    run_cmd([
        "docker", "exec", LANGFUSE_DB_CONTAINER, 
        "sh", "-c", "pg_dump -U postgres -d postgres -Fc > /tmp/langfuse.dump"
    ])
    run_cmd([
        "docker", "cp", 
        f"{LANGFUSE_DB_CONTAINER}:/tmp/langfuse.dump", 
        str(staging_dir / "langfuse.dump")
    ])
    run_cmd(["docker", "exec", LANGFUSE_DB_CONTAINER, "rm", "/tmp/langfuse.dump"], check=False)

    # 3. SQLite Extraction (Open WebUI)
    print("\n--- Extracting Open WebUI SQLite DB ---")
    # Using VACUUM INTO for zero-downtime lock-free extraction
    run_cmd([
        "docker", "exec", WEBUI_CONTAINER,
        "sh", "-c", "sqlite3 /app/backend/data/webui.db \"VACUUM INTO '/app/backend/data/webui_backup.db'\""
    ])
    run_cmd([
        "docker", "cp", 
        f"{WEBUI_CONTAINER}:/app/backend/data/webui_backup.db", 
        str(staging_dir / "webui_backup.db")
    ])
    run_cmd(["docker", "exec", WEBUI_CONTAINER, "rm", "/app/backend/data/webui_backup.db"], check=False)

    # 4. Qdrant Extraction
    print("\n--- Extracting Qdrant Vector Snapshots ---")
    try:
        collections_resp = requests.get(f"{QDRANT_URL}/collections")
        collections_resp.raise_for_status()
        collections = collections_resp.json().get("result", {}).get("collections", [])
        
        qdrant_staging = staging_dir / "qdrant_snapshots"
        os.makedirs(qdrant_staging, exist_ok=True)

        for coll in collections:
            cname = coll["name"]
            print(f"Snapshotting Qdrant collection: {cname}")
            snap_resp = requests.post(f"{QDRANT_URL}/collections/{cname}/snapshots")
            snap_resp.raise_for_status()
            snap_data = snap_resp.json().get("result", {})
            snap_name = snap_data.get("name")
            
            if snap_name:
                print(f"Transporting snapshot: {snap_name}")
                # Inside the container, snapshots are stored at /qdrant/snapshots/<collection_name>/<snapshot_name>
                # Qdrant docker image uses /qdrant/storage/snapshots? Wait, API returns name. Let's just download it via REST!
                # Actually, Qdrant exposes a REST endpoint to download snapshots directly without docker cp!
                # Endpoint: /collections/{collection_name}/snapshots/{snapshot_name}
                download_url = f"{QDRANT_URL}/collections/{cname}/snapshots/{snap_name}"
                dl_resp = requests.get(download_url, stream=True)
                dl_resp.raise_for_status()
                with open(qdrant_staging / snap_name, 'wb') as f:
                    for chunk in dl_resp.iter_content(chunk_size=8192):
                        f.write(chunk)
            else:
                print(f"Failed to parse snapshot name for {cname}")
    except requests.exceptions.RequestException as e:
        print(f"Failed to communicate with Qdrant API: {e}")
        # Proceeding without Qdrant backup rather than crashing

    # 5. Compression & Cleanup
    print("\n--- Compressing Archive ---")
    archive_name = Path(OUTPUT_DIR) / f"sovereign_backup_{timestamp}.tar.gz"
    
    with tarfile.open(archive_name, "w:gz") as tar:
        tar.add(staging_dir, arcname=f"sovereign_backup_{timestamp}")
        
    print(f"Archive created at: {archive_name}")

    # Cleanup staging
    shutil.rmtree(staging_dir)
    print("Staging directory cleaned up.")
    print("=== Backup Complete ===")

if __name__ == "__main__":
    main()
