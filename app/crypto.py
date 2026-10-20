"""Fernet encryption for secrets at rest, keyed by the platform FERNET_KEY.

MultiFernet: the current FERNET_KEY encrypts; FERNET_KEYS_PREVIOUS (older keys) are only tried for decryption,
so rotating the key does not lose data. `rotate_all` re-encrypts every stored ciphertext to the current key.
"""
import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app import config
from app.config import FERNET_KEY

log = logging.getLogger(__name__)


def _b(key: str | bytes) -> bytes:
    return key.encode() if isinstance(key, str) else key


def build(current: str, previous: tuple[str, ...] | list[str] = ()) -> MultiFernet:
    keys = [current, *[k for k in previous if k != current]]
    return MultiFernet([Fernet(_b(k)) for k in keys])


_fernet = build(FERNET_KEY, config.FERNET_KEYS_PREVIOUS)


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


def _encrypted_columns():
    from app import models

    return [
        (models.AppEnvVar, "value_encrypted"),
        (models.DbServer, "admin_password_encrypted"),
        (models.AppDatabase, "password_encrypted"),
        (models.AppStorage, "secret_key_encrypted"),
    ]


def rotate_all(session, fernet: MultiFernet | None = None) -> dict[str, int]:
    """Re-encrypt every encrypted column to the current key. Idempotent; undecryptable rows are skipped.
    Logs and returns counts only (never values)."""
    mf = fernet or _fernet
    current = mf._fernets[0]
    rotated = skipped = unchanged = 0
    for model, col in _encrypted_columns():
        for row in session.query(model).all():
            token = getattr(row, col)
            try:
                current.decrypt(token.encode())
                unchanged += 1
                continue
            except InvalidToken:
                pass
            try:
                new = mf.rotate(token.encode()).decode()
            except InvalidToken:
                skipped += 1
                continue
            setattr(row, col, new)
            rotated += 1
    session.commit()
    # WARNING level on purpose: prod logs only show WARNING and above, and this is the audit trail of a key rotation.
    log.warning("fernet rotation: rotated=%d unchanged=%d skipped_undecryptable=%d", rotated, unchanged, skipped)
    if skipped:
        log.warning("fernet rotation: %d rows could not be decrypted by any key and were left as-is", skipped)
    return {"rotated": rotated, "unchanged": unchanged, "skipped": skipped}
