import os
from datetime import datetime
from sqlalchemy import create_engine, Column, String, LargeBinary, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker
from cryptography.fernet import Fernet
import json

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/auth_vault.db")
AES_SECRET_KEY = os.getenv("AES_SECRET_KEY")

if AES_SECRET_KEY:
    # Ensure key is 32 url-safe base64-encoded bytes for Fernet
    # If the key provided isn't properly formatted, we can derive one or pad it.
    # For simplicity, if it's not standard length, we'll just hash it and base64 encode to fit Fernet.
    import base64
    import hashlib
    key_hash = hashlib.sha256(AES_SECRET_KEY.encode()).digest()
    fernet_key = base64.urlsafe_b64encode(key_hash)
    fernet = Fernet(fernet_key)
else:
    # Generate an ephemeral key if none provided (not recommended for persistent sessions)
    fernet = Fernet(Fernet.generate_key())

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class CredentialVault(Base):
    __tablename__ = "credential_vault"

    domain_id = Column(String, primary_key=True, index=True)
    auth_payload = Column(LargeBinary, nullable=False) # Encrypted JSON array of cookies/JWTs
    last_rotated = Column(DateTime, default=datetime.utcnow)
    proxy_node = Column(String, nullable=True) # Tracks specific Tor exit node/proxy used

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

def save_credentials(domain_id: str, payload: list | dict, proxy_node: str = None):
    db = SessionLocal()
    encrypted_payload = encrypt_payload(payload)
    
    cred = db.query(CredentialVault).filter(CredentialVault.domain_id == domain_id).first()
    if cred:
        cred.auth_payload = encrypted_payload
        cred.last_rotated = datetime.utcnow()
        if proxy_node:
            cred.proxy_node = proxy_node
    else:
        cred = CredentialVault(
            domain_id=domain_id,
            auth_payload=encrypted_payload,
            proxy_node=proxy_node
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
            "last_rotated": cred.last_rotated,
            "proxy_node": cred.proxy_node
        }
    return None
