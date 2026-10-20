"""app/k8s_manifests.py: pure manifest-rendering functions, no cluster access."""
from app import k8s_manifests as k8s

APP_ID = 42
HOSTNAME = "clever-robin-a1b2c"
IMAGE = "ghcr.io/example/demo:1.0"
PORT = 8080
ENV = {"LOG_LEVEL": "info", "DATABASE_URL": "mysql://app_42:secret@mysql:3306/app_42"}
MYSQL_CIDR = "10.0.5.7"


def _rendered():
    return k8s.render_app_manifests(APP_ID, HOSTNAME, IMAGE, PORT, ENV, MYSQL_CIDR)


def test_namespace_name_and_labels():
    ns = k8s.namespace_manifest(APP_ID)
    assert ns["kind"] == "Namespace"
    assert ns["metadata"]["name"] == "app-42"
    assert ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"
    assert ns["metadata"]["labels"]["picopaas.io/app-id"] == "42"
    assert ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce-version"] == "latest"


def test_resource_quota_caps():
    rq = k8s.resource_quota_manifest(APP_ID)
    hard = rq["spec"]["hard"]
    assert hard["limits.cpu"] == "200m"
    assert hard["limits.memory"] == "768Mi"
    assert hard["requests.cpu"] == "200m"
    assert hard["requests.memory"] == "768Mi"
    assert hard["pods"] == "2"
    assert hard["services"] == "1"
    assert hard["services.loadbalancers"] == "0"
    assert hard["services.nodeports"] == "0"
    assert rq["metadata"]["namespace"] == "app-42"


def test_limit_range_defaults():
    lr = k8s.limit_range_manifest(APP_ID)
    limits = lr["spec"]["limits"][0]
    assert limits["default"] == {"cpu": "100m", "memory": "384Mi"}
    assert limits["defaultRequest"] == {"cpu": "64m", "memory": "128Mi"}


def _policy(policies, name):
    return next(p for p in policies if p["metadata"]["name"] == name)


def test_network_policies_default_deny_isolates_everything():
    policies = k8s.network_policies(APP_ID, PORT, MYSQL_CIDR)
    deny = _policy(policies, "default-deny")
    assert deny["spec"]["podSelector"] == {}
    assert set(deny["spec"]["policyTypes"]) == {"Ingress", "Egress"}
    assert deny["spec"]["ingress"] == []
    assert deny["spec"]["egress"] == []


def test_network_policy_allows_ingress_controller_only_on_app_port():
    policies = k8s.network_policies(APP_ID, PORT, MYSQL_CIDR)
    allow = _policy(policies, "allow-ingress-controller")
    rule = allow["spec"]["ingress"][0]
    assert rule["ports"] == [{"protocol": "TCP", "port": PORT}]
    ns_selector = rule["from"][0]["namespaceSelector"]["matchLabels"]
    assert ns_selector == {"kubernetes.io/metadata.name": "traefik"}


def test_network_policy_allows_dns_egress():
    policies = k8s.network_policies(APP_ID, PORT, MYSQL_CIDR)
    dns = _policy(policies, "allow-egress-dns")
    rule = dns["spec"]["egress"][0]
    assert {"protocol": "UDP", "port": 53} in rule["ports"]
    assert {"protocol": "TCP", "port": 53} in rule["ports"]
    ns_selector = rule["to"][0]["namespaceSelector"]["matchLabels"]
    assert ns_selector == {"kubernetes.io/metadata.name": "kube-system"}


def test_network_policy_internet_egress_excludes_private_ranges():
    policies = k8s.network_policies(APP_ID, PORT, MYSQL_CIDR)
    internet = _policy(policies, "allow-egress-internet")
    ip_block = internet["spec"]["egress"][0]["to"][0]["ipBlock"]
    assert ip_block["cidr"] == "0.0.0.0/0"
    assert set(ip_block["except"]) == {
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
    }
    # Everything except SMTP (see test_network_policy_blocks_smtp_egress).
    assert "ports" in internet["spec"]["egress"][0]


def test_network_policy_blocks_smtp_egress():
    internet = _policy(k8s.network_policies(APP_ID, PORT, MYSQL_CIDR), "allow-egress-internet")
    ports = internet["spec"]["egress"][0]["ports"]
    tcp = [(p["port"], p.get("endPort", p["port"])) for p in ports if p["protocol"] == "TCP"]
    assert tcp == [(1, 24), (26, 464), (466, 586), (588, 65535)]
    for blocked in (25, 465, 587):
        assert not any(lo <= blocked <= hi for lo, hi in tcp)
    assert {"protocol": "UDP", "port": 1, "endPort": 65535} in ports


def test_suspended_deployment_has_zero_replicas():
    m = k8s.render_app_manifests(APP_ID, HOSTNAME, IMAGE, PORT, ENV, MYSQL_CIDR, replicas=0)
    assert m["deployment"]["spec"]["replicas"] == 0


def test_network_policy_mysql_egress_targets_cidr_and_port_3306():
    policies = k8s.network_policies(APP_ID, PORT, MYSQL_CIDR)
    mysql = _policy(policies, "allow-egress-mysql")
    rule = mysql["spec"]["egress"][0]
    assert rule["to"][0]["ipBlock"]["cidr"] == "10.0.5.7/32"
    assert rule["ports"] == [{"protocol": "TCP", "port": 3306}]


def test_mysql_cidr_passthrough_when_already_a_cidr():
    policies = k8s.network_policies(APP_ID, PORT, "10.0.0.0/24")
    mysql = _policy(policies, "allow-egress-mysql")
    assert mysql["spec"]["egress"][0]["to"][0]["ipBlock"]["cidr"] == "10.0.0.0/24"


def test_network_policy_mysql_egress_in_cluster_selectors():
    policies = k8s.network_policies(APP_ID, PORT, None)
    peer = _policy(policies, "allow-egress-mysql")["spec"]["egress"][0]["to"][0]
    assert "ipBlock" not in peer
    assert peer["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": "picopaas-data"}
    assert peer["podSelector"]["matchLabels"] == {"app": "mysql"}


def test_secret_contains_resolved_env_only():
    secret = k8s.secret_manifest(APP_ID, ENV)
    assert secret["kind"] == "Secret"
    assert secret["type"] == "Opaque"
    assert secret["stringData"] == ENV
    assert "${{" not in str(secret)


def test_deployment_security_context_hardening():
    dep = k8s.deployment_manifest(APP_ID, IMAGE, PORT)
    assert dep["spec"]["replicas"] == 1
    pod_spec = dep["spec"]["template"]["spec"]
    assert pod_spec["automountServiceAccountToken"] is False
    assert pod_spec["securityContext"]["runAsNonRoot"] is k8s.RUN_AS_NON_ROOT
    assert pod_spec["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}

    container = pod_spec["containers"][0]
    assert container["image"] == IMAGE
    assert container["ports"] == [{"containerPort": PORT}]
    assert container["envFrom"] == [{"secretRef": {"name": "app-42-env"}}]
    sc = container["securityContext"]
    assert sc["runAsNonRoot"] is True
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["capabilities"]["drop"] == ["ALL"]
    assert sc["seccompProfile"] == {"type": "RuntimeDefault"}
    assert container["readinessProbe"]["tcpSocket"]["port"] == PORT


def test_service_is_cluster_ip_on_app_port():
    svc = k8s.service_manifest(APP_ID, PORT)
    assert svc["spec"]["type"] == "ClusterIP"
    assert svc["spec"]["ports"] == [{"port": PORT, "targetPort": PORT, "protocol": "TCP"}]
    assert svc["spec"]["selector"] == {"app": "app-42"}


def test_ingress_uses_traefik_class_and_wildcard_host():
    ing = k8s.ingress_manifest(APP_ID, HOSTNAME, PORT)
    assert ing["spec"]["ingressClassName"] == "traefik"
    full_host = f"{HOSTNAME}.raisehellpraisedale.xyz"
    assert ing["spec"]["rules"][0]["host"] == full_host
    assert ing["spec"]["tls"] == [{"hosts": [full_host]}]
    backend = ing["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]
    assert backend == {"name": "app-42", "port": {"number": PORT}}


def test_render_app_manifests_includes_everything():
    rendered = _rendered()
    assert set(rendered) == {
        "namespace",
        "resource_quota",
        "limit_range",
        "network_policies",
        "secret",
        "deployment",
        "service",
        "ingress",
    }
    assert len(rendered["network_policies"]) == 5


def test_flatten_manifests_order_and_count():
    flat = k8s.flatten_manifests(_rendered())
    assert flat[0]["kind"] == "Namespace"
    assert flat[1]["kind"] == "ResourceQuota"
    assert flat[2]["kind"] == "LimitRange"
    assert [m["kind"] for m in flat[3:8]] == ["NetworkPolicy"] * 5
    assert flat[8]["kind"] == "Secret"
    assert flat[9]["kind"] == "Deployment"
    assert flat[10]["kind"] == "Service"
    assert flat[11]["kind"] == "Ingress"
    assert len(flat) == 12


def test_env_change_rolls_pod_via_checksum_annotation():
    from app.k8s_manifests import deployment_manifest, _env_checksum

    a = deployment_manifest(1, "img:1", 8080, env_checksum=_env_checksum({"DB_PASSWORD": "old"}))
    b = deployment_manifest(1, "img:1", 8080, env_checksum=_env_checksum({"DB_PASSWORD": "new"}))
    ann = lambda d: d["spec"]["template"]["metadata"]["annotations"]["picopaas.io/env-checksum"]
    assert ann(a) != ann(b)
    # The checksum must not reveal the values themselves.
    assert "old" not in ann(a) and "new" not in ann(b)


def test_deployment_rolling_update_strategy():
    dep = k8s.deployment_manifest(APP_ID, IMAGE, PORT)
    assert dep["spec"]["strategy"] == {
        "type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}}


def _container_sc(**kw):
    d = k8s.deployment_manifest(APP_ID, IMAGE, PORT, **kw)
    return d["spec"]["template"]["spec"]["containers"][0]["securityContext"]


def test_run_as_user_only_when_set():
    sc = _container_sc()
    assert "runAsUser" not in sc and "runAsGroup" not in sc
    sc = _container_sc(run_as_uid=10001)
    assert sc["runAsUser"] == 10001 and sc["runAsGroup"] == 10001
    assert sc["runAsNonRoot"] is True and sc["allowPrivilegeEscalation"] is False
    assert sc["capabilities"] == {"drop": ["ALL"]}


def test_render_passes_run_as_uid():
    r = k8s.render_app_manifests(APP_ID, HOSTNAME, IMAGE, PORT, ENV, MYSQL_CIDR, run_as_uid=10001)
    sc = r["deployment"]["spec"]["template"]["spec"]["containers"][0]["securityContext"]
    assert sc["runAsUser"] == 10001
