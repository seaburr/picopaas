"""deploy/platform/rbac.yaml: the platform ServiceAccount's two-tier grant.

A tiny RBAC evaluator over the checked-in manifest proves that (a) everything the reconciler writes is still allowed
(cluster-wide only for Namespace/RoleBinding, otherwise only inside a namespace bound to ClusterRole picopaas-app-ns),
and (b) the SA can no longer write app resources in kube-system/default/etc. Also pins the ValidatingAdmissionPolicy
that restricts where the SA may create RoleBindings. Not a substitute for the live `kubectl auth can-i` check in
docs/requests/0005-rbac-narrowing-runbook.md, but it fails the build if someone moves a rule back or drops one.
"""
from pathlib import Path

import pytest
import yaml

from app import k8s_manifests as k8s

RBAC = Path(__file__).resolve().parent.parent / "deploy" / "platform" / "rbac.yaml"
SA = "system:serviceaccount:picopaas-system:picopaas"

# kind -> (apiGroup, resource) for every object the reconciler server-side-applies.
KIND_RESOURCE = {
    "Namespace": ("", "namespaces"),
    "RoleBinding": ("rbac.authorization.k8s.io", "rolebindings"),
    "ResourceQuota": ("", "resourcequotas"),
    "LimitRange": ("", "limitranges"),
    "NetworkPolicy": ("networking.k8s.io", "networkpolicies"),
    "Secret": ("", "secrets"),
    "Deployment": ("apps", "deployments"),
    "Service": ("", "services"),
    "Ingress": ("networking.k8s.io", "ingresses"),
}
CLUSTER_WIDE_KINDS = {"Namespace", "RoleBinding"}
APP_NS_WORKLOAD_RESOURCES = {"deployments", "ingresses", "networkpolicies"}


@pytest.fixture(scope="module")
def docs():
    return [d for d in yaml.safe_load_all(RBAC.read_text()) if d]


@pytest.fixture(scope="module")
def roles(docs):
    return {d["metadata"]["name"]: d["rules"] for d in docs if d["kind"] == "ClusterRole"}


def _rule_allows(rule, verb, group, resource, name=None):
    if group not in rule.get("apiGroups", []) or resource not in rule.get("resources", []):
        return False
    if verb not in rule.get("verbs", []):
        return False
    names = rule.get("resourceNames")
    return not names or name in names


def _allowed(roles, docs, verb, group, resource, namespace=None, name=None, bound_namespaces=()):
    """True if the SA may do verb on group/resource (in `namespace`, None = cluster scope / all namespaces)."""
    cluster_rules = roles["picopaas-reconciler"]  # the only ClusterRoleBinding for the SA
    if any(_rule_allows(r, verb, group, resource, name) for r in cluster_rules):
        return True
    if namespace is None:
        return False
    in_ns = namespace in bound_namespaces or namespace == "picopaas-system"  # RoleBinding picopaas-platform
    return in_ns and any(_rule_allows(r, verb, group, resource, name) for r in roles["picopaas-app-ns"])


def test_bindings_wire_the_two_tiers(docs):
    crb = next(d for d in docs if d["kind"] == "ClusterRoleBinding")
    assert crb["roleRef"]["name"] == "picopaas-reconciler"
    rb = next(d for d in docs if d["kind"] == "RoleBinding")
    assert (rb["metadata"]["namespace"], rb["roleRef"]["name"]) == ("picopaas-system", "picopaas-app-ns")
    # The runtime per-app binding must reference the same role the manifest defines.
    assert k8s.role_binding_manifest(1)["roleRef"]["name"] == k8s.APP_NS_CLUSTERROLE == "picopaas-app-ns"


def test_apply_order_widens_before_narrowing(docs):
    names = [(d["kind"], d["metadata"]["name"]) for d in docs]
    assert names.index(("ClusterRole", "picopaas-app-ns")) < names.index(("ClusterRole", "picopaas-reconciler"))


def test_cluster_wide_role_has_no_app_resource_writes(roles):
    for rule in roles["picopaas-reconciler"]:
        res = set(rule.get("resources", []))
        assert not res & (APP_NS_WORKLOAD_RESOURCES | {"secrets", "services", "resourcequotas", "limitranges"}), rule
        if res & {"pods", "pods/log", "events", "nodes"}:
            assert set(rule["verbs"]) <= {"get", "list"}, rule  # read-only


def test_cluster_wide_role_is_exactly_the_documented_set(roles):
    got = {(g, r) for rule in roles["picopaas-reconciler"] for g in rule["apiGroups"] for r in rule["resources"]}
    assert got == {
        ("", "namespaces"), ("rbac.authorization.k8s.io", "rolebindings"), ("rbac.authorization.k8s.io", "clusterroles"),
        ("", "pods"), ("", "pods/log"), ("", "events"), ("", "nodes"), ("metrics.k8s.io", "nodes"), ("metrics.k8s.io", "pods"),
    }
    bind = [r for r in roles["picopaas-reconciler"] if "clusterroles" in r["resources"]]
    assert bind == [{"apiGroups": ["rbac.authorization.k8s.io"], "resources": ["clusterroles"], "verbs": ["bind"],
                     "resourceNames": ["picopaas-app-ns"]}]
    assert not any(v in rule["verbs"] for rule in roles["picopaas-reconciler"] for v in ("escalate", "impersonate", "*"))


@pytest.mark.parametrize("variant", [
    dict(),
    dict(custom_host="app.example.org"),
    dict(include_ingress=False, replicas=0),
    dict(storage_ips=["10.0.0.1"], platform_subdomain="shop", mysql_cidr=None),
])
def test_every_reconciler_apply_is_still_allowed(roles, docs, variant):
    """SSA = PATCH (+ CREATE when the object is new) for every manifest the reconciler renders, in app-<id>."""
    kwargs = dict(app_id=7, hostname="h", image="img:1", port=8080, env={"A": "b"}, mysql_cidr="10.0.5.7")
    kwargs.update(variant)
    flat = k8s.flatten_manifests(k8s.render_app_manifests(**kwargs))
    bound: set[str] = set()
    for m in flat:  # in apply order, as the reconciler does
        group, resource = KIND_RESOURCE[m["kind"]]
        ns = m["metadata"].get("namespace")
        for verb in ("create", "patch"):
            assert _allowed(roles, docs, verb, group, resource, ns, bound_namespaces=bound), (verb, m["kind"], ns)
        if m["kind"] == "RoleBinding":
            bound.add(ns)  # from here on the per-namespace grant exists
        elif m["kind"] not in CLUSTER_WIDE_KINDS:
            assert ns in bound, f"{m['kind']} applied before the RoleBinding in {ns}"


def test_reconciler_reads_deletes_and_status_calls_are_allowed(roles, docs):
    b = {"app-7"}
    ok = lambda *a, **kw: _allowed(roles, docs, *a, bound_namespaces=b, **kw)
    # delete_ingress (suspend / custom-domain detach), delete_namespace, list_app_namespaces
    assert ok("delete", "networking.k8s.io", "ingresses", "app-7")
    assert ok("delete", "", "namespaces") and ok("list", "", "namespaces") and ok("get", "", "namespaces")
    # get_status / get_used_limits / pending_hint / pod_logs / app_pod_events / cluster_snapshot
    assert ok("list", "", "pods", "app-7") and ok("get", "", "pods", "app-7") and ok("list", "", "pods")
    assert ok("get", "", "pods/log", "app-7")
    assert ok("delete", "", "pods", "app-7")  # finished-pod cleanup, app namespaces only
    assert not _allowed(roles, docs, "delete", "", "pods", "kube-system", bound_namespaces=b)
    assert not _allowed(roles, docs, "delete", "", "pods")  # not cluster-wide
    assert ok("list", "", "events", "app-7")  # pod_events (download)
    assert ok("list", "apps", "deployments", "app-7") and ok("get", "apps", "deployments", "app-7")
    assert ok("list", "", "nodes") and ok("list", "metrics.k8s.io", "nodes") and ok("list", "metrics.k8s.io", "pods")
    # RoleBinding bootstrap in a brand-new namespace (nothing bound yet) + the `bind` it needs
    assert _allowed(roles, docs, "create", "rbac.authorization.k8s.io", "rolebindings", "app-8")
    assert _allowed(roles, docs, "patch", "rbac.authorization.k8s.io", "rolebindings", "app-8")
    assert _allowed(roles, docs, "bind", "rbac.authorization.k8s.io", "clusterroles", name="picopaas-app-ns")


@pytest.mark.parametrize("ns", ["kube-system", "default", "cert-manager", "traefik", "picopaas-data"])
@pytest.mark.parametrize("resource,group", [
    ("deployments", "apps"), ("ingresses", "networking.k8s.io"), ("networkpolicies", "networking.k8s.io"),
    ("secrets", ""), ("services", ""), ("resourcequotas", ""), ("limitranges", ""),
])
def test_no_writes_outside_bound_namespaces(roles, docs, ns, resource, group):
    for verb in ("create", "patch", "update", "delete"):
        assert not _allowed(roles, docs, verb, group, resource, ns, bound_namespaces={"app-7"}), (verb, resource, ns)


def test_admission_policy_confines_rolebindings_to_app_namespaces(docs):
    vap = next(d for d in docs if d["kind"] == "ValidatingAdmissionPolicy")
    binding = next(d for d in docs if d["kind"] == "ValidatingAdmissionPolicyBinding")
    assert binding["spec"]["policyName"] == vap["metadata"]["name"]
    assert binding["spec"]["validationActions"] == ["Deny"]
    spec = vap["spec"]
    assert spec["failurePolicy"] == "Fail"
    (rule,) = spec["matchConstraints"]["resourceRules"]
    assert rule["resources"] == ["rolebindings"] and rule["apiGroups"] == ["rbac.authorization.k8s.io"]
    assert {"CREATE", "UPDATE"} <= set(rule["operations"])
    # Only ever evaluated for the platform SA, so other users/controllers are unaffected.
    (cond,) = spec["matchConditions"]
    assert cond["expression"] == f'request.userInfo.username == "{SA}"'
    (check,) = spec["validations"]
    expr = check["expression"]
    assert expr == "request.namespace.startsWith('app-')"  # app-* ONLY (pentest N1): no picopaas-* allowance
    assert "picopaas-" not in expr and "||" not in expr and "&&" not in expr
    # Evaluate the rule the way CEL would: allowed in app-*, denied in the system / platform namespaces.
    allows = lambda ns: ns.startswith("app-")
    assert allows("app-105") and allows("app-1")
    for ns in ("picopaas-data", "picopaas-system", "kube-system", "default", "cert-manager", "traefik"):
        assert not allows(ns), ns
    assert "picopaas-" not in check["messageExpression"]
    # The per-app RoleBinding the reconciler creates satisfies the policy.
    assert k8s.role_binding_manifest(7)["metadata"]["namespace"].startswith("app-")
    assert k8s.PLATFORM_SA_NAMESPACE == "picopaas-system" and k8s.PLATFORM_SA_NAME == "picopaas"
