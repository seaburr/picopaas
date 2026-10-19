"""Generates `<adjective>-<animal>-<5 char suffix>` hostnames, e.g. clever-robin-a1b2c.

Matches ARCHITECTURE.md: "<adjective>-<animal>-<5 chars of [a-z0-9]>.raisehellpraisedale.xyz", with a
uniqueness check in the DB. This module only builds the label; the caller (apps router)
appends the domain and checks the `apps.hostname` column for collisions.
"""
import secrets
import string

from sqlalchemy.orm import Session

from app import models

ADJECTIVES = [
    "clever", "quiet", "bold", "lucky", "swift", "gentle", "bright", "cosmic",
    "lazy", "brave", "calm", "eager", "fuzzy", "jolly", "mighty", "nimble",
    "proud", "silly", "spry", "witty", "zesty", "amber", "coral", "golden",
    "little", "shiny", "sleepy", "sneaky", "sunny", "tiny",
]

ANIMALS = [
    "robin", "otter", "panda", "falcon", "koala", "badger", "heron", "lynx",
    "mole", "newt", "orca", "puffin", "quail", "raven", "seal", "shrew",
    "tapir", "vole", "walrus", "yak", "zebra", "crane", "finch", "gecko",
    "ibis", "jackal", "kiwi", "lemur", "moth", "wren",
]

_SUFFIX_ALPHABET = string.ascii_lowercase + string.digits
_SUFFIX_LEN = 5
_MAX_ATTEMPTS = 50


def _random_hostname() -> str:
    adjective = secrets.choice(ADJECTIVES)
    animal = secrets.choice(ANIMALS)
    suffix = "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(_SUFFIX_LEN))
    return f"{adjective}-{animal}-{suffix}"


def generate_unique_hostname(db: Session) -> str:
    """Generate a hostname label, retrying on collision against apps.hostname."""
    for _ in range(_MAX_ATTEMPTS):
        candidate = _random_hostname()
        exists = db.query(models.App.id).filter(models.App.hostname == candidate).first()
        if not exists:
            return candidate
    raise RuntimeError("could not generate a unique hostname after many attempts")
