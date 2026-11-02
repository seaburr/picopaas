"""app/providers/cloud.py: the single switch for cloud-specific providers (CLOUD_PROVIDER)."""
import pytest

from app import config
from app.providers import cloud
from app.providers.base import BackupTarget, DbHostMetrics, ObjectStoreError
from app.providers.digitalocean.db_metrics import DoDbHostMetrics
from app.providers.digitalocean.spaces import SpacesStore

STORAGE_ENV = ("DIGITALOCEAN_TOKEN", "SPACES_ACCESS_KEY_ID", "SPACES_SECRET_ACCESS_KEY",
               "OBJECT_STORE_ACCESS_KEY_ID", "OBJECT_STORE_SECRET_ACCESS_KEY")


@pytest.fixture
def clean_env(monkeypatch):
    for k in STORAGE_ENV:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_default_provider_is_digitalocean():
    assert config.CLOUD_PROVIDER == "digitalocean"


def test_validate_cloud_provider_accepts_known_names():
    for name in ("digitalocean", "aws", "gcp", "generic", " AWS "):
        assert config.validate_cloud_provider(name) in config.CLOUD_PROVIDERS


def test_validate_cloud_provider_rejects_unknown_value():
    with pytest.raises(ValueError, match="CLOUD_PROVIDER must be one of"):
        config.validate_cloud_provider("azure")


@pytest.mark.parametrize("provider", ["aws", "gcp"])
def test_planned_providers_raise_not_implemented(monkeypatch, provider):
    monkeypatch.setattr(config, "CLOUD_PROVIDER", provider)
    for build in (cloud.object_store, cloud.storage_configured, cloud.backup_target, cloud.db_host_metrics):
        with pytest.raises(NotImplementedError, match="docs/PORTABILITY_PLAN.md"):
            build()


def test_object_store_is_spaces_when_configured(clean_env):
    clean_env.setenv("DIGITALOCEAN_TOKEN", "t")
    clean_env.setenv("SPACES_ACCESS_KEY_ID", "a")
    clean_env.setenv("SPACES_SECRET_ACCESS_KEY", "b")
    assert isinstance(cloud.object_store(), SpacesStore)


def test_object_store_unconfigured_raises_object_store_error(clean_env):
    with pytest.raises(ObjectStoreError, match="DIGITALOCEAN_TOKEN"):
        cloud.object_store()


@pytest.mark.parametrize("env,expected", [
    ({}, False),
    ({"DIGITALOCEAN_TOKEN": "t"}, False),
    ({"DIGITALOCEAN_TOKEN": "t", "SPACES_ACCESS_KEY_ID": "a"}, False),
    ({"SPACES_ACCESS_KEY_ID": "a", "SPACES_SECRET_ACCESS_KEY": "b"}, False),
    ({"DIGITALOCEAN_TOKEN": "t", "SPACES_ACCESS_KEY_ID": "a", "SPACES_SECRET_ACCESS_KEY": "b"}, True),
    ({"DIGITALOCEAN_TOKEN": "t", "SPACES_ACCESS_KEY_ID": "", "SPACES_SECRET_ACCESS_KEY": "b"}, False),
    ({"DIGITALOCEAN_TOKEN": "t", "OBJECT_STORE_ACCESS_KEY_ID": "a", "OBJECT_STORE_SECRET_ACCESS_KEY": "b"}, True),
    ({"DIGITALOCEAN_TOKEN": "t", "OBJECT_STORE_ACCESS_KEY_ID": "a", "SPACES_SECRET_ACCESS_KEY": "b"}, True),
])
def test_storage_configured_truth_table(clean_env, env, expected):
    for k, v in env.items():
        clean_env.setenv(k, v)
    assert cloud.storage_configured() is expected


def test_object_store_prefers_object_store_alias(clean_env):
    clean_env.setenv("DIGITALOCEAN_TOKEN", "t")
    clean_env.setenv("OBJECT_STORE_ACCESS_KEY_ID", "new-a")
    clean_env.setenv("OBJECT_STORE_SECRET_ACCESS_KEY", "new-b")
    clean_env.setenv("SPACES_ACCESS_KEY_ID", "old-a")
    clean_env.setenv("SPACES_SECRET_ACCESS_KEY", "old-b")
    assert config.object_store_access_key_id() == "new-a"
    assert config.object_store_secret_access_key() == "new-b"
    assert isinstance(cloud.object_store(), SpacesStore)


def test_backup_target_endpoint_and_repository_url(monkeypatch):
    monkeypatch.setattr(config, "BACKUP_ENDPOINT", "")
    monkeypatch.setattr(config, "BACKUP_REGION", "atl1")
    monkeypatch.setattr(config, "BACKUP_BUCKET", "bk")
    monkeypatch.setattr(config, "BACKUP_PREFIX", "backups/")
    target = cloud.backup_target()
    assert isinstance(target, BackupTarget)
    assert target.endpoint() == "https://atl1.digitaloceanspaces.com"
    assert target.repository_url("apps/7/db") == "s3:https://atl1.digitaloceanspaces.com/bk/backups/apps/7/db"
    monkeypatch.setattr(config, "BACKUP_PREFIX", "")
    assert target.repository_url("platform/db") == "s3:https://atl1.digitaloceanspaces.com/bk/platform/db"


def test_backup_target_ensure_egress_calls_runner_with_endpoint(monkeypatch):
    monkeypatch.setattr(config, "BACKUP_ENDPOINT", "https://example.invalid/")
    calls = []

    class Runner:
        def ensure_spaces_egress(self, endpoint):
            calls.append(endpoint)

    cloud.backup_target().ensure_egress(Runner())
    assert calls == ["https://example.invalid"]


def test_db_host_metrics_none_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "DB_METRICS_ENABLED", False)
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "t")
    assert cloud.db_host_metrics() is None


def test_db_host_metrics_none_without_token(monkeypatch):
    monkeypatch.setattr(config, "DB_METRICS_ENABLED", True)
    monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
    assert cloud.db_host_metrics() is None


def test_db_host_metrics_available_when_enabled_with_token(monkeypatch):
    monkeypatch.setattr(config, "DB_METRICS_ENABLED", True)
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "t")
    m = cloud.db_host_metrics()
    assert isinstance(m, DbHostMetrics) and isinstance(m, DoDbHostMetrics)


GENERIC_ENV = {"GARAGE_ADMIN_URL": "http://garage:3903", "GARAGE_ADMIN_TOKEN": "not-the-token",
               "OBJECT_STORE_ENDPOINT": "http://garage:3900/", "OBJECT_STORE_ACCESS_KEY_ID": "GKnot-a-key",
               "OBJECT_STORE_SECRET_ACCESS_KEY": "not-the-secret"}


@pytest.fixture
def generic_env(clean_env):
    clean_env.setattr(config, "CLOUD_PROVIDER", "generic")
    for k in ("GARAGE_ADMIN_URL", "GARAGE_ADMIN_TOKEN", "OBJECT_STORE_ENDPOINT", "OBJECT_STORE_REGION"):
        clean_env.delenv(k, raising=False)
    return clean_env


def test_generic_object_store_is_garage(generic_env):
    from app.providers.generic.garage import GarageStore

    for k, v in GENERIC_ENV.items():
        generic_env.setenv(k, v)
    store = cloud.object_store()
    assert isinstance(store, GarageStore)
    assert store.endpoint == "http://garage:3900" and store.region == "garage"


def test_generic_region_is_configurable(generic_env):
    for k, v in GENERIC_ENV.items():
        generic_env.setenv(k, v)
    generic_env.setenv("OBJECT_STORE_REGION", "eu-test")
    assert cloud.object_store().region == "eu-test"


@pytest.mark.parametrize("missing", ["GARAGE_ADMIN_URL", "GARAGE_ADMIN_TOKEN", "OBJECT_STORE_ENDPOINT",
                                     "OBJECT_STORE_ACCESS_KEY_ID", "OBJECT_STORE_SECRET_ACCESS_KEY"])
def test_generic_missing_env_raises_and_is_not_configured(generic_env, missing):
    for k, v in GENERIC_ENV.items():
        if k != missing:
            generic_env.setenv(k, v)
    assert cloud.storage_configured() is False
    with pytest.raises(ObjectStoreError, match=missing):
        cloud.object_store()


def test_generic_storage_configured(generic_env):
    for k, v in GENERIC_ENV.items():
        generic_env.setenv(k, v)
    assert cloud.storage_configured() is True


def test_generic_has_no_db_host_metrics(generic_env):
    generic_env.setenv("DIGITALOCEAN_TOKEN", "t")
    assert cloud.db_host_metrics() is None


def test_generic_backup_target_uses_s3_url_and_runner_egress(generic_env, monkeypatch):
    from app.providers.fake import FakeBackupRunner
    from app.providers.generic.backup_target import GenericBackupTarget

    monkeypatch.setattr(config, "BACKUP_ENDPOINT", "")
    monkeypatch.setattr(config, "BACKUP_BUCKET", "bk")
    monkeypatch.setattr(config, "BACKUP_PREFIX", "pre/")
    generic_env.setenv("OBJECT_STORE_ENDPOINT", "http://garage:3900/")
    target = cloud.backup_target()
    assert isinstance(target, GenericBackupTarget)
    assert target.endpoint() == "http://garage:3900"
    assert target.repository_url("apps/3/db") == "s3:http://garage:3900/bk/pre/apps/3/db"
    runner = FakeBackupRunner()
    target.ensure_egress(runner)
    assert runner.egress_endpoints == ["http://garage:3900"]
    monkeypatch.setattr(config, "BACKUP_ENDPOINT", "https://s3.example.test/")  # explicit wins
    assert target.endpoint() == "https://s3.example.test"
