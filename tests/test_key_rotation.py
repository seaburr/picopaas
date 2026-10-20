"""Single-key setup: derived SECRET_KEY, MultiFernet previous keys, and the re-encrypt pass."""
from cryptography.fernet import Fernet

from app import config, crypto, models
from tests.conftest import register_and_login


def _key() -> str:
    return Fernet.generate_key().decode()


def test_derived_secret_key_is_stable_and_distinct():
    k = _key()
    a, b = config.derive_secret_key(k), config.derive_secret_key(k)
    assert a == b and a != k and len(a) == 64
    assert config.derive_secret_key(_key()) != a


def test_multifernet_decrypts_old_key_ciphertext():
    old, new = _key(), _key()
    token = Fernet(old.encode()).encrypt(b"hello")
    assert crypto.build(new, [old]).decrypt(token) == b"hello"
    assert crypto.build(new).decrypt(Fernet(new.encode()).encrypt(b"x")) == b"x"


def test_rotate_all_rotates_and_skips_undecryptable(make_client, db_session):
    c = make_client()
    register_and_login(c, "o@example.com")
    r = c.post("/apps", json={"name": "demo", "image": "example/demo:latest", "port": 8080,
                              "env": [{"key": "A", "value": "one"}, {"key": "B", "value": "two"}]})
    assert r.status_code == 201, r.text
    vars_ = db_session.get(models.App, r.json()["id"]).env_vars
    old, other = _key(), _key()
    vars_[0].value_encrypted = Fernet(old.encode()).encrypt(b"one").decode()
    vars_[1].value_encrypted = Fernet(other.encode()).encrypt(b"two").decode()  # key nobody has
    db_session.commit()
    mf = crypto.build(config.FERNET_KEY, [old])
    stats = crypto.rotate_all(db_session, mf)
    assert stats["rotated"] == 1 and stats["skipped"] == 1
    db_session.expire_all()
    a, b = sorted(db_session.get(models.App, r.json()["id"]).env_vars, key=lambda v: v.key)
    assert crypto.decrypt(a.value_encrypted) == "one"
    assert crypto.try_decrypt(b.value_encrypted) is None  # left untouched
    again = crypto.rotate_all(db_session, mf)
    assert again["rotated"] == 0
