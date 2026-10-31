from unittest.mock import MagicMock

from kubernetes.dynamic.exceptions import NotFoundError

from app.providers.kube import KubeClientProvider


class _R:
    def __init__(self, d):
        self._d = d

    def to_dict(self):
        return self._d


def _dyn(pods=None, deployments=None):
    dyn = MagicMock()
    pod_res, dep_res = MagicMock(), MagicMock()
    pod_res.get.return_value = _R({"items": pods or []})
    dep_res.get.return_value = _R({"items": deployments or []})
    dyn.resources.get.side_effect = lambda api_version, kind: {"Pod": pod_res, "Deployment": dep_res}.get(kind, MagicMock())
    return dyn


def test_apply_namespace_first_with_field_manager():
    dyn = MagicMock()
    ms = [
        {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "s", "namespace": "n"}},
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "n"}},
    ]
    KubeClientProvider(dyn).apply(ms)
    calls = dyn.server_side_apply.call_args_list
    assert calls[0].kwargs["body"]["kind"] == "Namespace"
    assert calls[0].kwargs["field_manager"] == "picopaas"
    assert calls[0].kwargs["force_conflicts"] is True


def test_delete_namespace_ignores_404():
    dyn = MagicMock()
    dyn.resources.get.return_value.delete.side_effect = NotFoundError(MagicMock())
    KubeClientProvider(dyn).delete_namespace("n")


def test_status_running():
    k = KubeClientProvider(_dyn(deployments=[{"metadata": {"generation": 2}, "spec": {"replicas": 1},
                                              "status": {"readyReplicas": 1, "updatedReplicas": 1,
                                                         "observedGeneration": 2}}]))
    assert k.get_status("n") == "running"


def test_status_rollout_in_progress_is_deploying():
    # old pod still ready, new pod not created (e.g. quota-rejected): must not report running
    stuck = {"metadata": {"generation": 3}, "spec": {"replicas": 1},
             "status": {"readyReplicas": 1, "updatedReplicas": 0, "observedGeneration": 3}}
    assert KubeClientProvider(_dyn(deployments=[stuck])).get_status("n") == "deploying"
    stale = {"metadata": {"generation": 4}, "spec": {"replicas": 1},
             "status": {"readyReplicas": 1, "updatedReplicas": 1, "observedGeneration": 3}}
    assert KubeClientProvider(_dyn(deployments=[stale])).get_status("n") == "deploying"


def test_status_deploying():
    assert KubeClientProvider(_dyn(deployments=[{"status": {}}])).get_status("n") == "deploying"
    assert KubeClientProvider(_dyn()).get_status("n") == "deploying"


def _pod(reason, message=""):
    return {"status": {"containerStatuses": [{"state": {"waiting": {"reason": reason, "message": message}}}]}}


def test_status_errors():
    for r, code in (("ImagePullBackOff", "image_pull"), ("ErrImagePull", "image_pull"),
                    ("CrashLoopBackOff", "crash_loop"), ("CreateContainerConfigError", "config")):
        assert KubeClientProvider(_dyn(pods=[_pod(r)])).get_status("n") == f"error: {code}"
    s = KubeClientProvider(_dyn(pods=[_pod("CreateContainerConfigError", "container has runAsNonRoot")])).get_status("n")
    assert s == "error: root_image"


def test_status_old_pod_still_in_rollout_is_deploying():
    # new pod ready, old one not yet gone: replicas 2 > updated 1
    mid = {"metadata": {"generation": 3}, "spec": {"replicas": 1},
           "status": {"readyReplicas": 2, "replicas": 2, "updatedReplicas": 1, "observedGeneration": 3}}
    assert KubeClientProvider(_dyn(deployments=[mid])).get_status("n") == "deploying"


def _dep(spec_replicas=1, **status):
    return {"metadata": {"generation": 3}, "spec": {"replicas": spec_replicas},
            "status": {"observedGeneration": 3, **status}}


def test_status_rollout_edge_cases():
    # settled: running
    assert KubeClientProvider(_dyn(deployments=[_dep(readyReplicas=1, replicas=1, updatedReplicas=1)])).get_status("n") == "running"
    # replicas=None in status (older clusters / not yet reported) with a ready pod: not stuck "deploying" via the new check
    assert KubeClientProvider(_dyn(deployments=[_dep(readyReplicas=1, updatedReplicas=1)])).get_status("n") == "running"
    # suspended (scaled to 0): status.replicas absent/0 never triggers the new check
    assert KubeClientProvider(_dyn(deployments=[_dep(0)])).get_status("n") == "deploying"
    # new pod never ready while the old one serves: now deploying (the reconciler times it out), was "running"
    stuck = _dep(readyReplicas=1, replicas=2, updatedReplicas=1)
    assert KubeClientProvider(_dyn(deployments=[stuck])).get_status("n") == "deploying"
    # an error state still wins over the rollout check
    assert KubeClientProvider(_dyn(pods=[_pod("ImagePullBackOff")], deployments=[stuck])).get_status("n") == "error: image_pull"
