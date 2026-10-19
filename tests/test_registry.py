"""app/providers/registry.py: real Registry impl, HTTP mocked via httpx.MockTransport.

Covers the anonymous Bearer-token flow (Docker Hub / GHCR style), summing
compressed layer sizes, manifest-list (multi-arch) resolution to linux/amd64,
and error mapping to RegistryError.
"""
import httpx
import pytest

from app.providers.base import RegistryError
from app.providers.registry import RegistryClient, _parse_image_ref

AUTH_HEADER = 'Bearer realm="https://auth.example.com/token",service="example-registry"'

SINGLE_MANIFEST = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
    "config": {"size": 1500},
    "layers": [{"size": 100}, {"size": 200}, {"size": 300}],
}

MANIFEST_LIST = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.docker.distribution.manifest.list.v2+json",
    "manifests": [
        {"digest": "sha256:arm64digest", "platform": {"os": "linux", "architecture": "arm64"}},
        {"digest": "sha256:amd64digest", "platform": {"os": "linux", "architecture": "amd64"}},
    ],
}

AMD64_MANIFEST = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
    "config": {"size": 999},
    "layers": [{"size": 1000}, {"size": 2000}],
}


def _client_with(handler) -> RegistryClient:
    return RegistryClient(client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_anonymous_token_flow_and_layer_sum():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/":
            return httpx.Response(401, headers={"www-authenticate": AUTH_HEADER})
        if request.url.host == "auth.example.com":
            assert request.url.params["scope"] == "repository:library/nginx:pull"
            assert request.url.params["service"] == "example-registry"
            return httpx.Response(200, json={"token": "faketoken"})
        if request.url.path == "/v2/library/nginx/manifests/latest":
            assert request.headers["authorization"] == "Bearer faketoken"
            return httpx.Response(
                200,
                json=SINGLE_MANIFEST,
                headers={"content-type": "application/vnd.docker.distribution.manifest.v2+json"},
            )
        return httpx.Response(404)

    client = _client_with(handler)
    assert client.image_size_bytes("nginx") == 600  # 100 + 200 + 300, no implicit host


def test_ghcr_style_ref_with_explicit_host_and_tag():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/":
            return httpx.Response(401, headers={"www-authenticate": AUTH_HEADER})
        if request.url.host == "auth.example.com":
            assert request.url.params["scope"] == "repository:example/demo:pull"
            return httpx.Response(200, json={"token": "ghcrtoken"})
        if request.url.path == "/v2/example/demo/manifests/1.2":
            return httpx.Response(
                200,
                json=SINGLE_MANIFEST,
                headers={"content-type": "application/vnd.docker.distribution.manifest.v2+json"},
            )
        return httpx.Response(404)

    client = _client_with(handler)
    assert client.image_size_bytes("ghcr.io/example/demo:1.2") == 600


def test_manifest_list_resolves_to_linux_amd64():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/":
            return httpx.Response(401, headers={"www-authenticate": AUTH_HEADER})
        if request.url.host == "auth.example.com":
            return httpx.Response(200, json={"token": "tok"})
        if request.url.path == "/v2/library/multiarch/manifests/latest":
            return httpx.Response(
                200,
                json=MANIFEST_LIST,
                headers={
                    "content-type": "application/vnd.docker.distribution.manifest.list.v2+json"
                },
            )
        if request.url.path == "/v2/library/multiarch/manifests/sha256:amd64digest":
            return httpx.Response(
                200,
                json=AMD64_MANIFEST,
                headers={"content-type": "application/vnd.docker.distribution.manifest.v2+json"},
            )
        if request.url.path == "/v2/library/multiarch/manifests/sha256:arm64digest":
            raise AssertionError("should not fetch the arm64 entry")
        return httpx.Response(404)

    client = _client_with(handler)
    assert client.image_size_bytes("multiarch") == 3000  # 1000 + 2000


def test_manifest_not_found_raises_registry_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/":
            return httpx.Response(401, headers={"www-authenticate": AUTH_HEADER})
        if request.url.host == "auth.example.com":
            return httpx.Response(200, json={"token": "tok"})
        return httpx.Response(404)

    client = _client_with(handler)
    with pytest.raises(RegistryError):
        client.image_size_bytes("does-not/exist:latest")


def test_private_image_token_denied_raises_registry_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/":
            return httpx.Response(401, headers={"www-authenticate": AUTH_HEADER})
        if request.url.host == "auth.example.com":
            return httpx.Response(403)
        return httpx.Response(404)

    client = _client_with(handler)
    with pytest.raises(RegistryError):
        client.image_size_bytes("private/image:latest")


def test_manifest_missing_layers_field_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/":
            return httpx.Response(401, headers={"www-authenticate": AUTH_HEADER})
        if request.url.host == "auth.example.com":
            return httpx.Response(200, json={"token": "tok"})
        return httpx.Response(200, json={"schemaVersion": 1}, headers={"content-type": "application/json"})

    client = _client_with(handler)
    with pytest.raises(RegistryError):
        client.image_size_bytes("weird/schema:latest")


@pytest.mark.parametrize(
    "image_ref,expected_registry,expected_repository,expected_reference",
    [
        ("nginx", "registry-1.docker.io", "library/nginx", "latest"),
        ("nginx:1.25", "registry-1.docker.io", "library/nginx", "1.25"),
        ("someuser/app:latest", "registry-1.docker.io", "someuser/app", "latest"),
        ("ghcr.io/me/app:1.2", "ghcr.io", "me/app", "1.2"),
        ("localhost:5000/app:dev", "localhost:5000", "app", "dev"),
        ("ghcr.io/me/app@sha256:abcd", "ghcr.io", "me/app", "sha256:abcd"),
    ],
)
def test_parse_image_ref(image_ref, expected_registry, expected_repository, expected_reference):
    registry, repository, reference = _parse_image_ref(image_ref)
    assert registry == expected_registry
    assert repository == expected_repository
    assert reference == expected_reference


# --- Docker Hub / ghcr.io / SSRF ------------------------------------------

HUB_AUTH = 'Bearer realm="https://auth.docker.io/token",service="registry.docker.io",scope="repository:library/nginx:pull"'
OCI_INDEX = {
    "mediaType": "application/vnd.oci.image.index.v1+json",
    "manifests": [
        {"digest": "sha256:att", "platform": {"os": "unknown", "architecture": "unknown"}},
        {"digest": "sha256:arm", "platform": {"os": "linux", "architecture": "arm64"}},
        {"digest": "sha256:amd", "platform": {"os": "linux", "architecture": "amd64"}},
    ],
}
IMG_MANIFEST = {
    "mediaType": "application/vnd.oci.image.manifest.v1+json",
    "config": {"digest": "sha256:cfg"},
    "layers": [{"size": 10}, {"size": 20}],
}


def _hub_handler(seen, repo, user="10001", auth_host="auth.docker.io", registry_host="registry-1.docker.io",
                 challenge=HUB_AUTH):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == auth_host and request.url.path in ("/token",):
            assert request.url.params["scope"] == f"repository:{repo}:pull"
            return httpx.Response(200, json={"token": "t0k"})
        assert request.url.host == registry_host
        if request.url.path == "/v2/":
            return httpx.Response(401, headers={"www-authenticate": challenge})
        assert request.headers["authorization"] == "Bearer t0k"
        base = f"/v2/{repo}"
        if request.url.path.startswith(base + "/manifests/sha256:amd"):
            return httpx.Response(200, json=IMG_MANIFEST,
                                  headers={"content-type": "application/vnd.oci.image.manifest.v1+json"})
        if request.url.path.startswith(base + "/manifests/"):
            return httpx.Response(200, json=OCI_INDEX,
                                  headers={"content-type": "application/vnd.oci.image.index.v1+json"})
        if request.url.path == base + "/blobs/sha256:cfg":
            return httpx.Response(200, json={"config": {"User": user}})
        return httpx.Response(404)
    return handler


@pytest.mark.parametrize("image,repo", [("nginx:latest", "library/nginx"), ("nginx", "library/nginx"),
                                        ("someuser/img:1", "someuser/img")])
def test_docker_hub_normalisation_index_to_amd64_and_user(image, repo):
    seen = []
    client = _client_with(_hub_handler(seen, repo, user="0"))
    info = client.inspect(image)
    assert (info.size_bytes, info.user) == (30, "0")
    assert all(r.url.host in ("registry-1.docker.io", "auth.docker.io") for r in seen)
    accept = next(r for r in seen if "/manifests/" in r.url.path).headers["accept"]
    for mt in ("oci.image.index.v1", "manifest.list.v2", "oci.image.manifest.v1", "distribution.manifest.v2"):
        assert mt in accept
    assert any("manifests/sha256:amd" in r.url.path for r in seen)


def test_ghcr_bearer_challenge_inspect():
    challenge = 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:o/r:pull"'
    seen = []
    client = _client_with(_hub_handler(seen, "o/r", auth_host="ghcr.io", registry_host="ghcr.io",
                                       challenge=challenge))
    assert client.inspect("ghcr.io/o/r:latest").user == "10001"


def test_non_public_registry_host_rejected(monkeypatch):
    monkeypatch.setattr("app.providers.registry.socket.getaddrinfo",
                        lambda host, port: [(2, 1, 6, "", ("169.254.169.254", 0))])
    client = _client_with(lambda r: httpx.Response(200))
    with pytest.raises(RegistryError):
        client.inspect("metadata.internal.example/foo/bar")


def test_non_public_token_realm_rejected(monkeypatch):
    def fake_resolve(host, port):
        ip = "10.0.0.5" if host == "evil.example" else "93.184.216.34"
        return [(2, 1, 6, "", (ip, 0))]
    monkeypatch.setattr("app.providers.registry.socket.getaddrinfo", fake_resolve)

    def handler(request):
        return httpx.Response(401, headers={"www-authenticate": 'Bearer realm="https://evil.example/token"'})
    with pytest.raises(RegistryError):
        _client_with(handler).inspect("ghcr.io/o/r")
