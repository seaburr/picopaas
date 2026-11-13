"""Rule-based health advisor: every rule as a table of fake facts (no I/O), plus the guard rails."""
from datetime import datetime, timedelta

import pytest

from app import advisor, config
from app.advisor import AppFacts, BackupFact, EventFact, SampleFact

NOW = datetime(2026, 11, 12, 15, 0, 0)
HOUR = timedelta(hours=1)


def facts(**kw) -> AppFacts:
    base = dict(app_id=7, name="demo", now=NOW, created_at=NOW - timedelta(days=30))
    base.update(kw)
    return AppFacts(**base)


def limits(**kw):
    d = {"cpu_limit_m": 200, "mem_limit_mi": 256, "ephemeral_limit_mi": 250, "storage_quota_mb": 100}
    d.update(kw)
    return d


def ceil(**kw):
    d = {"cpu_limit_m": 200, "mem_limit_mi": 384, "ephemeral_limit_mi": 250, "storage_quota_mb": 100}
    d.update(kw)
    return d


def ev(kind, minutes_ago, msg=""):
    return EventFact(kind, NOW - timedelta(minutes=minutes_ago), msg)


def samples(values, key="mem_mi", other=10):
    n = len(values)
    out = []
    for i, v in enumerate(values):
        t = NOW - timedelta(minutes=n - i)
        out.append(SampleFact(t, **{"cpu_m": other, "mem_mi": other, key: v}))
    return out


def run(rule, f):
    return dict((rid, fn) for rid, fn, _ in advisor.RULES)[rule](f)


def no_admin_hint(finding):
    text = (finding.title + " " + finding.evidence + " " + (finding.action or {}).get("label", "")).lower()
    assert "admin" not in text and "ask " not in text and "request" not in text


def test_all_sixteen_rules_registered_and_only_stuck_error_is_admin_only():
    assert len(advisor.RULES) == 16
    assert [r for r, _, admin in advisor.RULES if admin] == ["app.stuck_error"]


# --- mem.oom ---

def test_oom_warning_with_raise_action():
    f = facts(limits=limits(), ceilings=ceil(), events=[ev("oom", 30)], samples=samples([251]))
    x = run("mem.oom", f)
    assert x.severity == "warning"
    assert x.action == {"kind": "set_resource", "label": "Raise memory to 320 MB", "field": "mem_limit_mi",
                        "from": 256, "to": 320, "unit": "MB", "restarts_app": True}
    assert "OOM-killed 1 time in 24 h" in x.evidence and "Memory limit 256 MB" in x.evidence and "peak 251 MB" in x.evidence


def test_oom_critical_at_three():
    f = facts(limits=limits(), ceilings=ceil(), events=[ev("oom", m) for m in (10, 100, 600)])
    assert run("mem.oom", f).severity == "critical"
    assert config.ADVISOR_OOM_CRITICAL_COUNT == 3


def test_oom_raise_is_capped_at_the_ceiling():
    f = facts(limits=limits(mem_limit_mi=352), ceilings=ceil(), events=[ev("oom", 30)])
    assert run("mem.oom", f).action["to"] == 384


def test_oom_at_cap_has_no_action_and_no_upsell():
    f = facts(limits=limits(mem_limit_mi=384), ceilings=ceil(), events=[ev("oom", 30)])
    x = run("mem.oom", f)
    assert x.action is None and "At the platform cap (384 MB). Reduce memory use." in x.evidence
    no_admin_hint(x)


@pytest.mark.parametrize("age_min", [25 * 60])
def test_oom_old_event_does_not_fire(age_min):
    assert run("mem.oom", facts(events=[ev("oom", age_min)])) is None


# --- mem.near_limit ---

def test_near_limit_fires_after_five_samples():
    f = facts(limits=limits(), ceilings=ceil(), samples=samples([240] * 5))
    x = run("mem.near_limit", f)
    assert x.severity == "warning" and x.action["to"] == 320
    assert "90% of its 256 MB limit for 5 samples in a row" in x.evidence


def test_near_limit_needs_consecutive_samples():
    f = facts(limits=limits(), ceilings=ceil(), samples=samples([240, 240, 240, 240, 10, 240, 240, 240, 240]))
    assert run("mem.near_limit", f) is None


def test_near_limit_at_cap():
    f = facts(limits=limits(mem_limit_mi=384), ceilings=ceil(), samples=samples([380] * 6))
    x = run("mem.near_limit", f)
    assert x.action is None and "At the platform cap (384 MB)" in x.evidence
    no_admin_hint(x)


def test_near_limit_suppressed_when_oom_open():
    f = facts(limits=limits(), ceilings=ceil(), samples=samples([250] * 6), events=[ev("oom", 20)])
    assert [x.rule for x in advisor.evaluate(f) if x.rule.startswith("mem.")] == ["mem.oom"]


# --- cpu.saturated ---

def test_cpu_saturated_run_at_cap_has_no_action():
    f = facts(samples=samples([190] * 5, key="cpu_m"))
    x = run("cpu.saturated", f)
    assert x.action is None and "At the platform cap (200 m). Reduce CPU use." in x.evidence
    assert "190" not in x.evidence or "limit" in x.evidence
    no_admin_hint(x)


def test_cpu_saturated_owner_lowered_gets_raise():
    f = facts(limits=limits(cpu_limit_m=100), samples=samples([95] * 5, key="cpu_m"))
    assert run("cpu.saturated", f).action == {"kind": "set_resource", "label": "Raise CPU to 150 m", "field": "cpu_limit_m",
                                              "from": 100, "to": 150, "unit": "m", "restarts_app": True}


def test_cpu_saturated_by_share_of_samples():
    vals = ([190, 10, 10, 10, 10, 10, 10, 10, 10, 10] * 3)  # 3 of 30 = 10%, never 5 in a row
    x = run("cpu.saturated", facts(samples=samples(vals, key="cpu_m")))
    assert x is not None and "in 3 of 30 samples" in x.evidence


def test_cpu_not_saturated_and_too_few_samples():
    assert run("cpu.saturated", facts(samples=samples([10] * 30, key="cpu_m"))) is None
    assert run("cpu.saturated", facts(samples=samples([199] * 4, key="cpu_m"))) is None


# --- restart.loop / flapping ---

def test_loop_by_status_links_to_logs():
    x = run("restart.loop", facts(status="error", status_reason="crash_loop"))
    assert x.severity == "critical" and x.action == {"kind": "link", "label": "Open logs", "to": "/apps/7#logs"}


def test_loop_by_burst_with_exit_code():
    f = facts(events=[ev("crashed", m, "Restarted: your app exited with code 1.") for m in (2, 4, 6)])
    x = run("restart.loop", f)
    assert x is not None and "exit code 1" in x.evidence and "3 crashes in 24 h" in x.evidence


def test_loop_rollback_when_started_after_image_update():
    f = facts(image_digest="sha256:" + "d" * 64, previous_digest="sha256:" + "a" * 64,
              current_digest_at=NOW - timedelta(minutes=20),
              events=[ev("crashed", m) for m in (2, 4, 6)])
    x = run("restart.loop", f)
    assert x.action == {"kind": "rollback", "label": "Roll back to " + "a" * 12, "digest": "sha256:" + "a" * 64,
                        "restarts_app": True}
    assert "Started after the image update" in x.evidence


def test_loop_no_rollback_when_crashes_long_after_update():
    f = facts(image_digest="sha256:" + "d" * 64, previous_digest="sha256:" + "a" * 64,
              current_digest_at=NOW - timedelta(days=3), events=[ev("crashed", m) for m in (2, 4, 6)])
    assert run("restart.loop", f).action["kind"] == "link"


def test_old_burst_is_not_a_loop():
    f = facts(events=[ev("crashed", m) for m in (600, 602, 604)])
    assert run("restart.loop", f) is None


def test_flapping_fires_not_loop_and_is_superseded_by_loop():
    spread = facts(events=[ev("crashed", m, "exited with code 137") for m in (100, 400, 900)])
    x = run("restart.flapping", spread)
    assert x.severity == "warning" and "Exited 3 times in 24 h" in x.evidence and run("restart.loop", spread) is None
    assert [r.rule for r in advisor.evaluate(spread) if r.rule.startswith("restart.")] == ["restart.flapping"]
    looped = facts(status="error", status_reason="crash_loop", events=[ev("crashed", m) for m in (2, 4, 6)])
    assert [r.rule for r in advisor.evaluate(looped) if r.rule.startswith("restart.")] == ["restart.loop"]


def test_flapping_below_threshold():
    assert run("restart.flapping", facts(events=[ev("crashed", 10), ev("crashed", 500)])) is None


# --- disk.scratch ---

def test_disk_scratch_raise_when_below_ceiling():
    f = facts(limits=limits(ephemeral_limit_mi=128), ceilings=ceil(), events=[ev("disk", 60)])
    x = run("disk.scratch", f)
    assert x.action["field"] == "ephemeral_limit_mi" and x.action["to"] == 160 and x.action["label"] == "Raise scratch disk to 160 MB"


def test_disk_scratch_at_cap():
    x = run("disk.scratch", facts(events=[ev("disk", 60)]))
    assert x.action is None and "At the platform cap (250 MB). Reduce scratch file use." in x.evidence
    no_admin_hint(x)


def test_disk_scratch_old_event():
    assert run("disk.scratch", facts(events=[ev("disk", 8 * 24 * 60)])) is None


# --- quotas and volume ---

MB = 1024 * 1024


@pytest.mark.parametrize("size,over,sev", [(40 * MB, False, "warning"), (48 * MB, False, "critical"), (10 * MB, True, "critical")])
def test_db_quota_fires_without_action(size, over, sev):
    f = facts(db_size_bytes=size, db_quota_bytes=50 * MB, db_over_quota=over)
    x = run("db.near_quota", f)
    assert x.severity == sev and x.action is None and "Delete data to get under the quota." in x.evidence
    no_admin_hint(x)


def test_db_quota_below_threshold_and_not_attached():
    assert run("db.near_quota", facts(db_size_bytes=39 * MB, db_quota_bytes=50 * MB)) is None
    assert run("db.near_quota", facts()) is None


def test_bucket_quota_at_ceiling_no_action():
    x = run("bucket.near_quota", facts(bucket_size_bytes=85 * MB, bucket_quota_bytes=100 * MB))
    assert x.severity == "warning" and x.action is None and "Delete objects to get under the quota." in x.evidence
    no_admin_hint(x)


def test_bucket_quota_owner_lowered_gets_raise_without_restart():
    f = facts(limits=limits(storage_quota_mb=40), bucket_size_bytes=35 * MB, bucket_quota_bytes=40 * MB)
    x = run("bucket.near_quota", f)
    assert x.action == {"kind": "set_resource", "label": "Raise bucket quota to 50 MB", "field": "storage_quota_mb",
                        "from": 40, "to": 50, "unit": "MB", "restarts_app": False}


def test_bucket_quota_clear():
    assert run("bucket.near_quota", facts(bucket_size_bytes=10 * MB, bucket_quota_bytes=100 * MB)) is None


@pytest.mark.parametrize("used_mb,sev", [(820, "warning"), (930, "critical")])
def test_volume_near_full(used_mb, sev):
    x = run("volume.near_full", facts(volume_size_gi=1, volume_used_bytes=used_mb * MB))
    assert x.severity == sev and x.action is None and "of 1024 MB" in x.evidence
    no_admin_hint(x)


def test_volume_ok_or_unmeasured():
    assert run("volume.near_full", facts(volume_size_gi=1, volume_used_bytes=100 * MB)) is None
    assert run("volume.near_full", facts(volume_size_gi=1, volume_used_bytes=None)) is None
    assert run("volume.near_full", facts()) is None


# --- addon.unused ---

def test_addon_unused_after_seven_days():
    f = facts(db_size_bytes=0, db_created_at=NOW - timedelta(days=12))
    x = run("addon.unused", f)
    assert x.severity == "info" and "Database attached 12 d ago, still empty (0 MB)." in x.evidence and x.action is None


def test_addon_unused_not_yet_or_not_empty():
    assert run("addon.unused", facts(db_size_bytes=0, db_created_at=NOW - timedelta(days=2))) is None
    assert run("addon.unused", facts(bucket_size_bytes=5, bucket_created_at=NOW - timedelta(days=20))) is None


# --- image ---

@pytest.mark.parametrize("image,fires", [("ghcr.io/x/y:latest", True), ("ghcr.io/x/y", True), ("ghcr.io/x/y:1.4", False),
                                         ("ghcr.io/x/y@sha256:" + "a" * 64, False)])
def test_mutable_tag(image, fires):
    x = run("image.mutable_tag", facts(image=image))
    assert (x is not None) is fires
    if fires:
        assert x.severity == "info" and str(config.IMAGE_WATCH_MINUTES) + " min" in x.evidence
        assert x.action == {"kind": "link", "label": "Open settings", "to": "/apps/7/edit"}


def test_mutable_tag_quiet_without_auto_update():
    assert run("image.mutable_tag", facts(image="x/y:latest", auto_update=False)) is None


def test_image_rejected():
    x = run("image.rejected", facts(rejected_digest="sha256:" + "b" * 64, image_update_error="New image runs as root."))
    assert x.severity == "warning" and "b" * 12 in x.evidence and "runs as root" in x.evidence
    assert run("image.rejected", facts()) is None


# --- health ---

def test_health_none_after_a_day_only_when_running():
    x = run("health.none", facts())
    assert x.severity == "info" and "TCP check on port 8080" in x.evidence and x.action["to"] == "/apps/7/edit"
    assert run("health.none", facts(health_check_enabled=True)) is None
    assert run("health.none", facts(created_at=NOW - timedelta(hours=5))) is None
    assert run("health.none", facts(status="error")) is None


def test_health_failing():
    x = run("health.failing", facts(status="error", status_reason="not_ready", health_check_enabled=True,
                                    health_check_path="/healthz"))
    assert x.severity == "critical" and "/healthz is not returning 2xx/3xx" in x.evidence
    assert run("health.failing", facts(status="error", status_reason="image_pull")) is None
    assert run("health.failing", facts(status="running", status_reason="not_ready")) is None


# --- backup ---

def test_backup_failed_and_overdue_and_fine():
    failed = BackupFact(last_success_at=NOW - timedelta(days=3), failed=[("Database", "dump timed out")])
    x = run("backup.failing", facts(backup=failed))
    assert x.severity == "warning" and "Database backup failed: dump timed out. Last good backup 3 d ago." in x.evidence
    overdue = BackupFact(last_success_at=NOW - timedelta(days=3))
    assert run("backup.failing", facts(backup=overdue)).title == "Backups overdue"
    assert run("backup.failing", facts(backup=BackupFact(last_success_at=NOW - HOUR))) is None
    assert run("backup.failing", facts(backup=BackupFact(last_success_at=NOW - timedelta(days=3), schedule="weekly"))) is None


def test_backup_quiet_when_off_or_none():
    assert run("backup.failing", facts()) is None
    assert run("backup.failing", facts(backup=BackupFact(enabled=False, failed=[("Files", None)]))) is None
    assert run("backup.failing", facts(backup=BackupFact(admin_disabled=True, failed=[("Files", None)]))) is None
    assert run("backup.failing", facts(backup=BackupFact(schedule="off", failed=[("Files", None)]))) is None


# --- app.stuck_error ---

def test_stuck_error_admin_only():
    f = facts(status="error", status_reason="image_pull", error_age_s=9 * 86400 + 5)
    x = run("app.stuck_error", f)
    assert x.severity == "info" and "In error for at least 9 d (image_pull)." in x.evidence
    assert "app.stuck_error" not in [r.rule for r in advisor.evaluate(f)]
    assert "app.stuck_error" in [r.rule for r in advisor.evaluate(f, admin=True)]
    assert run("app.stuck_error", facts(status="error", error_age_s=86400)) is None


# --- guard rails ---

def test_suspended_and_young_apps_get_nothing():
    base = dict(events=[ev("oom", 5)], limits=limits(), ceilings=ceil())
    assert advisor.evaluate(facts(status="suspended", **base)) == []
    assert advisor.evaluate(facts(created_at=NOW - timedelta(minutes=30), **base)) == []
    assert advisor.evaluate(facts(**base))


def test_findings_sorted_critical_first():
    f = facts(limits=limits(), ceilings=ceil(), events=[ev("oom", 5)], samples=samples([100] * 3),
              db_size_bytes=49 * MB, db_quota_bytes=50 * MB)
    sevs = [x.severity for x in advisor.evaluate(f)]
    assert sevs == sorted(sevs, key=advisor.SEVERITY_ORDER.get) and sevs[0] == "critical"


def test_settle_drops_samples_just_after_a_new_pod_appears():
    t0 = NOW - timedelta(minutes=60)
    old = [SampleFact(t0 + timedelta(minutes=i), 10, 10, frozenset({"a"})) for i in range(30)]
    new = [SampleFact(t0 + timedelta(minutes=30 + i), 10, 400, frozenset({"b"})) for i in range(30)]
    kept = advisor.settle(old + new)
    assert len(kept) == 30 + (30 - 10)  # the first 10 min of pod b are dropped, pod a (pre-existing) is never dropped
    assert all(not (s.pods == {"b"} and s.ts - (t0 + timedelta(minutes=30)) < timedelta(minutes=10)) for s in kept)
    assert advisor.settle([]) == []


def test_round_up_and_clamp_use_steps_and_floor():
    f = facts(limits=limits(mem_limit_mi=100), ceilings=ceil(), events=[ev("oom", 5)])
    assert run("mem.oom", f).action["to"] == 128  # 100 * 1.25 = 125 -> 128 (32 MB step)
