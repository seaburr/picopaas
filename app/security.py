"""Password hashing, JWT session tokens, and API key hashing."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt

from app.config import API_KEY_PREFIX, JWT_ALGORITHM, JWT_EXPIRE_DAYS, SECRET_KEY


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except ValueError:
        return False


def create_access_token(user_id: int) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRE_DAYS)
    return jwt.encode({"sub": str(user_id), "exp": expire}, SECRET_KEY, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> Optional[int]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return int(payload["sub"])
    except Exception:
        return None


def generate_api_key() -> str:
    """Return a new plaintext API key. Store only hash_api_key(key), never this value."""
    return API_KEY_PREFIX + secrets.token_hex(32)


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def api_key_prefix_for_display(raw_key: str) -> str:
    """A short, non-secret prefix safe to store/display so a user can tell keys apart."""
    return raw_key[: len(API_KEY_PREFIX) + 8]
