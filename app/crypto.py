"""Fernet encryption for env var values at rest, keyed by the platform FERNET_KEY."""
from cryptography.fernet import Fernet, InvalidToken

from app.config import FERNET_KEY

_fernet = Fernet(FERNET_KEY.encode() if isinstance(FERNET_KEY, str) else FERNET_KEY)


def encrypt(value: str) -> str:
    return _fernet.encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet.decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("could not decrypt value") from exc


def try_decrypt(token: str) -> str | None:
    """Like decrypt, but returns None when the ciphertext is unreadable (e.g. platform key changed)."""
    try:
        return decrypt(token)
    except ValueError:
        return None
