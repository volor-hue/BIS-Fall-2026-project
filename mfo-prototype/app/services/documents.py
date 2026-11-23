# app/services/documents.py
# ------------------------------------------------------------
# Saving uploaded client documents (passport photo...).
# ------------------------------------------------------------
import hashlib
import os

from app.core import config
from app.core.logs import audit
from app.db import database

# Uploaded documents are saved into this folder (setting MFO_UPLOAD_DIR, tests use a temp folder).
UPLOAD_DIR = config.UPLOAD_DIR


def save_document(client_id, doc_type, upload):
    # 1) read the file bytes, 2) compute SHA-256 of the content,
    # 3) save the file into uploads/, 4) write a row into "documents".
    content = upload.file.read()
    file_hash = hashlib.sha256(content).hexdigest()
    # Keep only safe characters of the original name (no "../" tricks).
    original = os.path.basename(upload.filename or "file")
    safe_name = "".join(ch for ch in original if ch.isalnum() or ch in "._-") or "file"
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    file_path = os.path.join(UPLOAD_DIR, f"client{client_id}_{doc_type}_{file_hash[:8]}_{safe_name}")
    with open(file_path, "wb") as f:
        f.write(content)
    document_id = database.create_document(client_id, doc_type, file_path, file_hash)
    audit("document_uploaded", client_id=client_id, doc_type=doc_type,
          document_id=document_id, sha256=file_hash[:12])
    return {"document_id": document_id, "client_id": client_id, "doc_type": doc_type,
            "file_path": file_path, "file_hash": file_hash}
