import os
from pathlib import Path
from datetime import datetime
from sqlalchemy import create_engine, Column, String, LargeBinary, DateTime
from sqlalchemy.engine import make_url
from sqlalchemy.orm import declarative_base, sessionmaker
from cryptography.fernet import Fernet
import json

DATABASE_URL = os.environ["DATABASE_URL"]
AES_SECRET_KEY = os.environ["AES_SECRET_KEY"]

database_target = make_url(DATABASE_URL)
if database_target.drivername.startswith("sqlite") and database_target.database not in (None, ":memory:"):
    Path(database_target.database).parent.mkdir(parents=True, exist_ok=True)

import base64
import hashlib

key_hash = hashlib.sha256(AES_SECRET_KEY.encode()).digest()
fernet_key = base64.urlsafe_b64encode(key_hash)
fernet = Fernet(fernet_key)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class CredentialVault(Base):
    __tablename__ = "credential_vault"

    domain_id = Column(String, primary_key=True, index=True)
    auth_payload = Column(LargeBinary, nullable=False) # Encrypted JSON array of cookies/JWTs
    last_rotated = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

def encrypt_payload(payload_dict: list | dict) -> bytes:
    """Encrypts a Python dict/list into an AES-256 encrypted blob."""
    json_str = json.dumps(payload_dict)
    return fernet.encrypt(json_str.encode("utf-8"))

def decrypt_payload(encrypted_blob: bytes) -> list | dict:
    """Decrypts an AES-256 encrypted blob back into a Python dict/list."""
    decrypted_str = fernet.decrypt(encrypted_blob).decode("utf-8")
    return json.loads(decrypted_str)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def save_credentials(domain_id: str, payload: list | dict):
    db = SessionLocal()
    encrypted_payload = encrypt_payload(payload)
    
    cred = db.query(CredentialVault).filter(CredentialVault.domain_id == domain_id).first()
    if cred:
        cred.auth_payload = encrypted_payload
        cred.last_rotated = datetime.utcnow()
    else:
        cred = CredentialVault(
            domain_id=domain_id,
            auth_payload=encrypted_payload
        )
        db.add(cred)
    
    db.commit()
    db.refresh(cred)
    db.close()
    return cred

def get_credentials(domain_id: str):
    db = SessionLocal()
    cred = db.query(CredentialVault).filter(CredentialVault.domain_id == domain_id).first()
    db.close()
    if cred:
        return {
            "domain_id": cred.domain_id,
            "payload": decrypt_payload(cred.auth_payload),
            "last_rotated": cred.last_rotated
        }
    return None
