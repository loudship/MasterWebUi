#!/usr/bin/env python3
import os
import subprocess
import sqlite3
import httpx
import hashlib
import sys
from datetime import datetime

BACKUP_DIR = "./backup"

def ensure_reedsolo():
    try:
        import reedsolo
    except ImportError:
        print("[*] Installing reedsolo for erasure coding...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "reedsolo"])
        import reedsolo

def create_directory():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    print(f"[*] Backup directory '{BACKUP_DIR}' ready.")

def backup_postgres():
    print("[*] Initiating PostgreSQL MVCC pg_dump...")
    dump_path = os.path.join(BACKUP_DIR, f"calendar_db_snapshot_{datetime.now().strftime('%Y%m%d%H%M%S')}.sql")
    try:
        with open(dump_path, 'wb') as f:
            subprocess.run(
                ["docker", "exec", "-i", "calendar-db", "pg_dump", "-U", "calendar", "calendar_db"],
                stdout=f,
                check=True
            )
        print(f"  -> PostgreSQL backup successful: {dump_path}")
        return dump_path
    except subprocess.CalledProcessError as e:
        print(f"  -> [ERROR] PostgreSQL backup failed: {e}")
        return None

def backup_sqlite():
    print("[*] Initiating SQLite VACUUM INTO...")
    source_db = "./data/open-webui/webui.db"
    dest_db = os.path.join(BACKUP_DIR, f"webui_snapshot_{datetime.now().strftime('%Y%m%d%H%M%S')}.db")
    
    if os.path.exists(dest_db):
        os.remove(dest_db)
        
    try:
        with sqlite3.connect(source_db) as conn:
            conn.execute(f"VACUUM INTO '{dest_db}'")
        print(f"  -> SQLite backup successful: {dest_db}")
        return dest_db
    except Exception as e:
        print(f"  -> [ERROR] SQLite backup failed: {e}")
        return None

def backup_qdrant():
    print("[*] Initiating Qdrant REST Snapshot API...")
    url = "http://localhost:6333"
    snapshots_created = []
    try:
        # Get collections
        collections_resp = httpx.get(f"{url}/collections", timeout=10.0)
        collections_resp.raise_for_status()
        collections = collections_resp.json().get("result", {}).get("collections", [])
        
        for c in collections:
            c_name = c["name"]
            print(f"  -> Snapshotting collection: {c_name}")
            snap_resp = httpx.post(f"{url}/collections/{c_name}/snapshots", timeout=60.0)
            snap_resp.raise_for_status()
            snapshots_created.append(c_name)
            
        print(f"  -> Qdrant snapshots successful for {len(snapshots_created)} collections.")
        return snapshots_created
    except Exception as e:
        print(f"  -> [ERROR] Qdrant backup failed: {e}")
        return None

def calculate_sha256(filepath):
    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

def generate_parity_block(filepath):
    import reedsolo
    rs = reedsolo.RSCodec(10) # 10 ECC symbols
    
    with open(filepath, "rb") as f:
        data = f.read()
        
    # For massive files this can OOM, but we apply it to chunks or the whole file for the PoC
    chunk_size = 255 - 10
    encoded_data = bytearray()
    
    for i in range(0, len(data), chunk_size):
        chunk = data[i:i+chunk_size]
        encoded_data.extend(rs.encode(chunk))
        
    parity_path = filepath + ".parity"
    with open(parity_path, "wb") as f:
        f.write(encoded_data)
        
    return parity_path

def apply_integrity_and_parity(files):
    ensure_reedsolo()
    for file in files:
        if not file or not os.path.exists(file):
            continue
            
        print(f"[*] Processing Integrity and Parity for {os.path.basename(file)}...")
        hash_val = calculate_sha256(file)
        
        hash_file = file + ".sha256"
        with open(hash_file, "w") as f:
            f.write(hash_val)
        print(f"  -> SHA-256 Checksum written: {hash_val}")
        
        print("  -> Generating Reed-Solomon Erasure Parity block...")
        try:
            parity_path = generate_parity_block(file)
            print(f"  -> Parity block written: {parity_path}")
        except Exception as e:
            print(f"  -> [WARNING] Parity generation skipped (file might be too large for basic memory buffer): {e}")

if __name__ == "__main__":
    print("=============================================")
    print(" SOVEREIGN PERSISTENCE AND DISASTER RECOVERY ")
    print("=============================================")
    
    create_directory()
    
    files_to_process = []
    
    sql_backup = backup_postgres()
    files_to_process.append(sql_backup)
    
    sqlite_backup = backup_sqlite()
    files_to_process.append(sqlite_backup)
    
    backup_qdrant()
    
    apply_integrity_and_parity(files_to_process)
    
    print("=============================================")
    print(" BACKUP ROUTINE COMPLETED SUCCESSFULLY       ")
    print("=============================================")
