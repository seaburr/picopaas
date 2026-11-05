"""The edge block on /.env and similar paths: the rule in deploy/error-pages/sensitive-paths.yaml, its wiring, and the
per-app opt-out. Traefik's behaviour itself was proven on a local cluster; these keep the rule and the opt-out from drifting."""
import re
from pathlib import Path

import pytest
import yaml

from app import k8s_manifests as k8s, models
from app.spec import parse_spec
from tests.conftest import register_and_login

ROOT = Path(__file__).resolve().parent.parent
RULE = yaml.safe_load((ROOT / "deploy/error-pages/sensitive-paths.yaml").read_text())
PRIORITY_KEY = "traefik.ingress.kubernetes.io/router.priority"


def _blocked(path: str) -> bool:
    regex = RULE["spec"]["rules"][0]["http"]["paths"][0]["path"]
    return re.search(regex, path) is not None  # unanchored, like Traefik's PathRegexp


@pytest.mark.parametrize("path", [
    "/.env", "/.ENV", "/.env.local", "/.env.production", "/api/.env", "/a/b/.env", "/.env/", "/.git", "/.git/config",
    "/.GIT/HEAD", "/.aws/credentials", "/.ssh/id_rsa", "/id_rsa", "/id_ed25519", "/wp-config.php", "/wp-config.php.bak",
    "/wp/wp-config.php", "/terraform.tfstate", "/x/prod.tfstate.backup", "/prod.tfvars", "/.htpasswd", "/.npmrc", "/.netrc",
    "/.git-credentials", "/privkey.pem", "/server.key", "/.git;x/config", "/a/.aws;p/credentials",
])
def test_rule_blocks(path):
    assert _blocked(path)


@pytest.mark.parametrize("path", [
    "/", "/.well-known/acme-challenge/x", "/.github/workflows/ci.yml", "/repo.git", "/repo.git/info/refs", "/wp-admin/",
    "/wp-login.php", "/index.php", "/foo.env", "/environment", "/assets/app.key.js", "/id_rsa.pub", "/backup.zip", "/config.json",
])
def test_rule_leaves_real_app_paths_alone(path):
    assert not _blocked(path)


def test_rule_is_a_hostless_catch_all_kubernetes_accepts():
    rule = RULE["spec"]["rules"][0]
    assert "host" not in rule
    http_path = rule["http"]["paths"][0]
    assert http_path["pathType"] == "ImplementationSpecific" and http_path["path"].startswith("/")
    assert RULE["metadata"]["annotations"]["traefik.ingress.kubernetes.io/router.pathmatcher"] == "PathRegexp"


def test_opt_out_priority_beats_the_rule():
    assert k8s.SENSITIVE_PATHS_OPT_OUT_PRIORITY > int(RULE["metadata"]["annotations"][PRIORITY_KEY])


@pytest.mark.parametrize("workflow", ["deploy.yml", "bootstrap.yml"])
def test_rule_is_applied_by_the_workflows(workflow):
    assert "kubectl apply -f deploy/error-pages/sensitive-paths.yaml" in (ROOT / ".github/workflows" / workflow).read_text()


def test_generic_target_ships_the_rule_and_its_backend():
    text = (ROOT / "deploy/generic/platform/kustomization.yaml").read_text()
    assert "sensitive-paths.yaml" in text and "error-pages/error-pages.yaml" in text


def test_ingresses_carry_no_priority_by_default():
    assert "annotations" not in k8s.ingress_manifest(1, "demo", 8080)["metadata"]
    assert PRIORITY_KEY not in k8s.custom_domain_ingress_manifest(1, "app.example.org", 8080)["metadata"]["annotations"]


def test_opt_out_sets_priority_on_both_ingresses():
    want = str(k8s.SENSITIVE_PATHS_OPT_OUT_PRIORITY)
    assert k8s.ingress_manifest(1, "demo", 8080, allow_sensitive_paths=True)["metadata"]["annotations"][PRIORITY_KEY] == want
    custom = k8s.custom_domain_ingress_manifest(1, "app.example.org", 8080, allow_sensitive_paths=True)["metadata"]["annotations"]
    assert custom[PRIORITY_KEY] == want
    assert custom["cert-manager.io/cluster-issuer"]  # the issuer stays


def test_render_passes_the_flag_through_to_both_ingresses():
    kw = dict(app_id=1, hostname="demo", image="i", port=8080, env={}, mysql_cidr="10.0.0.1/32", custom_host="app.example.org")
    on = k8s.render_app_manifests(**kw, allow_sensitive_paths=True)
    assert PRIORITY_KEY in on["ingress"]["metadata"]["annotations"]
    assert PRIORITY_KEY in on["custom_ingress"]["metadata"]["annotations"]
    off = k8s.render_app_manifests(**kw)
    assert "annotations" not in off["ingress"]["metadata"]
    assert PRIORITY_KEY not in off["custom_ingress"]["metadata"]["annotations"]


def test_api_defaults_off_and_owner_can_change_it(client):
    register_and_login(client, "owner-sensitive@example.com")
    body = {"name": "demo", "image": "example/app:1", "port": 8080}
    created = client.post("/apps", json=body)
    assert created.status_code == 201, created.text
    assert created.json()["allow_sensitive_paths"] is False
    app_id = created.json()["id"]

    on = client.patch(f"/apps/{app_id}", json={"allow_sensitive_paths": True})
    assert on.status_code == 200, on.text
    assert on.json()["allow_sensitive_paths"] is True and on.json()["status"] == "pending"
    assert client.patch(f"/apps/{app_id}", json={"name": "demo2"}).json()["allow_sensitive_paths"] is True  # omitted = unchanged
    assert client.patch(f"/apps/{app_id}", json={"allow_sensitive_paths": False}).json()["allow_sensitive_paths"] is False


def test_another_user_cannot_change_it(client, make_client):
    register_and_login(client, "owner-sensitive2@example.com")
    app_id = client.post("/apps", json={"name": "demo", "image": "example/app:1", "port": 8080}).json()["id"]
    other = make_client()
    register_and_login(other, "other-sensitive@example.com")
    assert other.patch(f"/apps/{app_id}", json={"allow_sensitive_paths": True}).status_code in (403, 404)


def test_spec_field_is_strict_and_defaults_off():
    base = "apiVersion: picopaas/v1\nname: demo\nimage: example/app:1\nport: 8080\n"
    assert parse_spec(base).allowSensitivePaths is False
    assert parse_spec(base + "allowSensitivePaths: true\n").allowSensitivePaths is True
    with pytest.raises(ValueError):
        parse_spec(base + 'allowSensitivePaths: "yes"\n')


def test_forgejo_template_and_example_opt_out_only_forgejo(client):
    register_and_login(client, "templates-sensitive@example.com")
    templates = client.get("/templates").json()
    assert {t["id"] for t in templates if t["allow_sensitive_paths"]} == {"forgejo"}
    assert yaml.safe_load((ROOT / "examples/forgejo/forgejo.yaml").read_text())["allowSensitivePaths"] is True


def test_model_column_defaults_off():
    assert models.App.__table__.c.allow_sensitive_paths.server_default is not None
