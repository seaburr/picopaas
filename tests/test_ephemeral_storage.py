"""250 MB scratch-disk cap: manifests, quota sizing, eviction mapping, health node conditions."""
from app import config, k8s_manifests as k8s, reconciler
from app.providers.kube import KubeClientProvider
from tests import test_kube_provider as tk
from tests.test_admin_health import _admin, _node, kube  # noqa: F401  (kube is a fixture)


def _evicted(msg="Pod ephemeral local storage usage exceeds the total limit of containers 250Mi."):
    return {"status": {"phase": "Failed", "reason": "Evicted", "message": msg},
            "spec": {"containers": [{"resources": {"limits": {"ephemeral-storage": "250Mi"}}}]}}


def test_deployment_has_ephemeral_requests_and_limits():
    c = k8s.deployment_manifest(1, "img", 8080)["spec"]["template"]["spec"]["containers"][0]["resources"]
    assert c["limits"]["ephemeral-storage"] == f"{config.EPHEMERAL_LIMIT_MI}Mi" == "250Mi"
    assert c["requests"]["ephemeral-storage"] == "64Mi"


def test_quota_has_ephemeral_keys_with_surge_room():
    hard = k8s.resource_quota_manifest(1)["spec"]["hard"]
    assert hard["limits.ephemeral-storage"] == hard["requests.ephemeral-storage"] == "500Mi"
    wide = k8s.resource_quota_manifest(1, used_limits={"ephemeral_limit_mi": 1024})["spec"]["hard"]
    assert wide["limits.ephemeral-storage"] == "1274Mi"


def test_used_limits_sums_ephemeral_and_ignores_evicted():
    live = {"status": {"phase": "Running"},
            "spec": {"containers": [{"resources": {"limits": {"cpu": "100m", "memory": "384Mi", "ephemeral-storage": "250Mi"}}}]}}
    used = KubeClientProvider(tk._dyn(pods=[live, _evicted()])).get_used_limits("n")
    assert used["ephemeral_limit_mi"] == 250


def test_eviction_maps_to_disk_unless_replacement_serving():
    assert KubeClientProvider(tk._dyn(pods=[_evicted()])).get_status("n") == "error: disk"
    ready = {"status": {"phase": "Running", "containerStatuses": [{"ready": True}]}}
    st = KubeClientProvider(tk._dyn(pods=[_evicted(), ready])).get_status("n")
    assert st != "error: disk"
    other = _evicted("The node was low on resource: memory.")
    assert KubeClientProvider(tk._dyn(pods=[other])).get_status("n") != "error: disk"


def test_disk_message_and_reason():
    assert "disk" in reconciler.STATUS_MESSAGES
    assert reconciler.STATUS_MESSAGES["disk"].format(disk=250).startswith("Your app used more than 250 MB of local disk")
    assert reconciler._REASON_OF.get("disk", "disk") == "disk"


def test_me_limits_expose_ephemeral(make_client):
    from tests.conftest import register_and_login
    c = make_client()
    register_and_login(c, "u@example.com")
    assert c.get("/auth/me").json()["limits"]["ephemeral_mi"] == 250


def test_health_passes_node_conditions_and_disk(make_client, kube):  # noqa: F811
    admin = _admin(make_client)
    kube.snapshot = {
        "nodes": [_node(conditions={"DiskPressure": True, "MemoryPressure": False, "PIDPressure": False},
                        disk_capacity_mi=50000, disk_allocatable_mi=45000)],
        "pods": [], "metrics_available": True, "errors": [],
    }
    n = admin.get("/admin/health").json()["nodes"][0]
    assert n["conditions"]["DiskPressure"] is True and n["disk_allocatable_mi"] == 45000
