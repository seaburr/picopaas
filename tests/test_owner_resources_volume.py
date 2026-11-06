"""An app with a persistent volume has higher owner floors: its backup pod is clamped to the app's limits (pentest K115)."""
from types import SimpleNamespace

from app import config, k8s_manifests, resources


def _app(volume=None, **owner):
    return SimpleNamespace(volume_size_gi=volume, want_storage=False, db_engine="mysql", **owner)


def test_plain_app_keeps_the_low_floor():
    assert resources.controls(_app())["mem_limit_mi"]["min"] == config.OWNER_MEM_MIN_MI
    assert not resources.validate_owner_changes(_app(), {"mem_limit_mi": config.OWNER_MEM_MIN_MI})


def test_volume_app_has_higher_floors_and_says_why():
    app = _app(volume=1)
    c = resources.controls(app)
    assert c["mem_limit_mi"]["min"] == config.OWNER_VOLUME_MEM_MIN_MI
    assert c["ephemeral_limit_mi"]["min"] == config.OWNER_VOLUME_EPHEMERAL_MIN_MI
    assert c["cpu_limit_m"]["min"] == config.OWNER_CPU_MIN_M
    errors = resources.validate_owner_changes(app, {"mem_limit_mi": config.OWNER_MEM_MIN_MI})
    assert "persistent volume" in errors["mem_limit_mi"]
    assert not resources.validate_owner_changes(app, {"mem_limit_mi": config.OWNER_VOLUME_MEM_MIN_MI})


def test_volume_added_later_lifts_a_low_choice_so_backups_fit():
    app = _app(volume=1, owner_mem_limit_mi=64, owner_ephemeral_limit_mi=64)
    eff = resources.effective(app)
    assert eff["mem_limit_mi"] == config.OWNER_VOLUME_MEM_MIN_MI
    assert eff["ephemeral_limit_mi"] == config.OWNER_VOLUME_EPHEMERAL_MIN_MI
    assert resources.ephemeral_limit_mi(app) == config.OWNER_VOLUME_EPHEMERAL_MIN_MI
    assert k8s_manifests.backup_pod_resources(eff)["mem_limit_mi"] == config.OWNER_VOLUME_MEM_MIN_MI


def test_a_low_admin_ceiling_still_wins_over_the_volume_floor():
    app = _app(volume=1, mem_limit_mi=96, owner_mem_limit_mi=64)
    assert resources.effective(app)["mem_limit_mi"] == 96
    assert resources.controls(app)["mem_limit_mi"]["min"] == 96
