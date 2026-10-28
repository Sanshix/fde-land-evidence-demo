"""Optional server-enforced local accounts. No generated passwords or external credentials."""

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path


def users():
    file = os.environ.get("FDE_USERS_FILE")
    return json.loads(Path(file).read_text()) if file else None


def verify(authorization):
    accounts = users()
    if accounts is None:
        return None
    try:
        scheme, encoded = authorization.split(" ", 1)
        if scheme.lower() != "basic":
            return None
        name, password = base64.b64decode(encoded, validate=True).decode().split(":", 1)
        item = accounts.get(name)
        if not item:
            return None
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(item["salt"]), n=16384, r=8, p=1).hex()
        if not hmac.compare_digest(actual, item["hash"]) or item["role"] not in {
            "viewer",
            "reviewer",
            "admin",
        }:
            return None
        return {"name": name, "role": item["role"]}
    except (ValueError, KeyError, UnicodeDecodeError):
        return None
