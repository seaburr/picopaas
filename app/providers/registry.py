"""Real `Registry` implementation: Docker Registry v2 HTTP API via httpx.

Reads image metadata only — never pulls layers. Supports the anonymous token
flow used by Docker Hub and GHCR (and anything else implementing the same
`WWW-Authenticate: Bearer realm=...` dance) for public images. Private pull
creds are out of scope for v1 (ARCHITECTURE.md §2 "Image size").

Size = sum of each layer's `size` field (compressed size on the wire) from
the `application/vnd.docker.distribution.manifest.v2+json` /
`application/vnd.oci.image.manifest.v1+json` manifest. A manifest *list* /
OCI *index* (multi-arch images) is resolved by picking the `linux/amd64`
entry (falling back to the first entry if none matches) before summing.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import re
import socket
from typing import Any, Optional

import httpcore
import httpx
from urllib.parse import urljoin

from app.providers.base import ImageInfo, Registry, RegistryError

_MANIFEST_LIST_TYPES = {
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.index.v1+json",
}

_MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.oci.image.index.v1+json",
    ]
)

log = logging.getLogger(__name__)

_DOCKER_HUB_REGISTRY = "registry-1.docker.io"
_DOCKER_HUB_ALIASES = {"docker.io", "index.docker.io", "registry.hub.docker.com", "registry-1.docker.io"}
_WWW_AUTH_PARAM_RE = re.compile(r'(\w+)="([^"]*)"')


def _parse_image_ref(image_ref: str) -> tuple[str, str, str]:
    """Split `[registry/]repository[:tag|@digest]` into (registry, repository, reference)."""
    ref = image_ref.strip()
    if not ref:
        raise RegistryError("empty image reference")

    if "@" in ref:
        name, reference = ref.split("@", 1)
    else:
        last_slash = ref.rfind("/")
        last_colon = ref.rfind(":")
        if last_colon > last_slash:
            name, reference = ref.rsplit(":", 1)
        else:
            name, reference = ref, "latest"

    first_segment, _, rest = name.partition("/")
    looks_like_host = "." in first_segment or ":" in first_segment or first_segment == "localhost"
    if looks_like_host and rest:
        registry, repository = first_segment, rest
        if registry in _DOCKER_HUB_ALIASES:
            registry = _DOCKER_HUB_REGISTRY
            if "/" not in repository:
                repository = f"library/{repository}"
    else:
        registry = _DOCKER_HUB_REGISTRY
        repository = name if "/" in name else f"library/{name}"

    return registry, repository, reference


def _check_public_host(host: str) -> None:
    """SSRF guard: refuse registry/token hosts that resolve to non-public addresses.

    Unresolvable names pass (the request itself will fail to connect)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise RegistryError("image registry not allowed")


class _PinnedBackend(httpcore.SyncBackend):
    """Network backend that closes the DNS-rebinding TOCTOU.

    Resolves the name ONCE at connect time, requires every returned address to be
    global, then connects to that exact validated IP (no second resolution). TLS
    SNI / certificate verification still use the original hostname (httpcore passes
    the origin host as server_hostname), so only the address is pinned."""

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise httpcore.ConnectError(str(exc)) from exc
        ips = [i[4][0].split("%")[0] for i in infos]
        if not ips or not all(ipaddress.ip_address(ip).is_global for ip in ips):
            raise RegistryError("image registry not allowed")
        return super().connect_tcp(ips[0], port, timeout=timeout, local_address=local_address,
                                   socket_options=socket_options)


def _pinned_client(timeout: float) -> httpx.Client:
    # trust_env=False: no env proxies (a proxy would resolve the name itself).
    transport = httpx.HTTPTransport()
    transport._pool._network_backend = _PinnedBackend()  # httpx exposes no public hook
    return httpx.Client(transport=transport, timeout=timeout, trust_env=False, follow_redirects=False)


_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_BODY_BYTES = 4 * 1024 * 1024  # manifests and image configs are small JSON


def _verify_digest(content: bytes, expected: str) -> str:
    """Return `expected` if sha256/sha512 of `content` matches it; else RegistryError."""
    algo, _, hexdigest = expected.partition(":")
    if algo not in ("sha256", "sha512") or not hexdigest:
        raise RegistryError("unsupported digest algorithm")
    if not hmac.compare_digest(hashlib.new(algo, content).hexdigest(), hexdigest.lower()):
        log.warning("registry content does not match digest %s", expected)
        raise RegistryError("image content does not match its digest")
    return expected


def _parse_www_authenticate(header: str) -> dict[str, str]:
    return dict(_WWW_AUTH_PARAM_RE.findall(header))


class RegistryClient(Registry):
    def __init__(self, client: Optional[httpx.Client] = None, timeout: float = 10.0) -> None:
        self._client = client or _pinned_client(timeout)

    def _get(self, url: str, **kw: Any) -> httpx.Response:
        """GET with manual, validated redirects. Every hop (including the first) is
        host-checked; auto-follow is off. Connect-time IP pinning (see _PinnedBackend)
        closes the rebinding gap for the default client; with an injected client
        (tests) only the per-hop name check applies. Body is capped at _MAX_BODY_BYTES."""
        kw.pop("follow_redirects", None)
        headers = dict(kw.pop("headers", None) or {})
        origin = httpx.URL(url).host
        for _ in range(_MAX_REDIRECTS + 1):
            parsed = httpx.URL(url)
            if parsed.scheme != "https":
                raise RegistryError("image registry not allowed")
            _check_public_host(parsed.host)
            if parsed.host != origin:
                headers.pop("Authorization", None)  # never leak the bearer token cross-host
            with self._client.stream("GET", url, headers=headers, follow_redirects=False, **kw) as resp:
                if resp.status_code in _REDIRECT_STATUSES and resp.headers.get("location"):
                    url = urljoin(str(resp.url), resp.headers["location"])
                    kw.pop("params", None)
                    continue
                body = bytearray()
                for chunk in resp.iter_bytes():
                    body += chunk
                    if len(body) > _MAX_BODY_BYTES:
                        raise RegistryError("registry response too large")
                return httpx.Response(resp.status_code, headers=resp.headers, content=bytes(body),
                                      request=resp.request)
        raise RegistryError("image registry not allowed")  # redirect chain too long

    def image_size_bytes(self, image_ref: str) -> int:
        return self._sum_layer_sizes(self._wrap(lambda: self._resolve(image_ref)[0]))

    def resolve_digest(self, image_ref: str) -> str:
        """Cheap: token + one manifest GET (no config blob). For an index, the amd64 entry's digest."""

        def _do() -> str:
            registry, repository, reference = _parse_image_ref(image_ref)
            token = self._get_token(registry, repository)
            manifest, content_type, digest = self._get_manifest(registry, repository, reference, token)
            if self._is_manifest_list(manifest, content_type):
                return self._select_amd64_digest(manifest)
            return digest

        return self._wrap(_do)

    def inspect(self, image_ref: str) -> ImageInfo:
        def _do() -> ImageInfo:
            manifest, registry, repository, token, manifest_digest = self._resolve(image_ref)
            size = self._sum_layer_sizes(manifest)
            digest = (manifest.get("config") or {}).get("digest")
            if not digest:
                raise RegistryError("manifest has no config digest; unsupported schema")
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            # Blobs are commonly served via a redirect to object storage.
            resp = self._get(f"https://{registry}/v2/{repository}/blobs/{digest}", headers=headers)
            if resp.status_code != 200:
                log.warning("registry blob fetch %s/%s: HTTP %s", registry, repository, resp.status_code)
                raise RegistryError("image not found or not public")
            # The config carries USER: it must be the exact blob the manifest names.
            _verify_digest(resp.content, digest)
            # RESIDUAL: the size cap counts compressed layer bytes. Neither the manifest nor the
            # config exposes uncompressed layer sizes (rootfs.diff_ids are hashes only), so a
            # decompressed-layer bomb is not bounded here. Layers are deliberately not fetched
            # or inflated (that would be its own DoS); kubelet ephemeral-storage limits apply.
            user = (resp.json().get("config") or {}).get("User") or ""
            return ImageInfo(size_bytes=size, user=user, digest=manifest_digest)

        return self._wrap(_do)

    def _resolve(self, image_ref: str) -> tuple[dict[str, Any], str, str, Optional[str], str]:
        """(manifest, registry, repository, token, digest-of-that-manifest)."""
        registry, repository, reference = _parse_image_ref(image_ref)
        token = self._get_token(registry, repository)
        manifest, content_type, digest = self._get_manifest(registry, repository, reference, token)
        if self._is_manifest_list(manifest, content_type):
            digest = self._select_amd64_digest(manifest)
            manifest, _, digest = self._get_manifest(registry, repository, digest, token)
        return manifest, registry, repository, token, digest

    @staticmethod
    def _wrap(fn):
        try:
            return fn()
        except RegistryError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise RegistryError(f"registry request failed: {exc}") from exc

    def _get_token(self, registry: str, repository: str) -> Optional[str]:
        resp = self._get(f"https://{registry}/v2/")
        if resp.status_code == 200:
            return None
        if resp.status_code != 401:
            raise RegistryError(f"unexpected response from {registry}/v2/: HTTP {resp.status_code}")

        params = _parse_www_authenticate(resp.headers.get("www-authenticate", ""))
        realm = params.get("realm")
        if not realm:
            raise RegistryError("registry did not advertise a token realm")

        query = {"scope": f"repository:{repository}:pull"}
        if "service" in params:
            query["service"] = params["service"]

        if not realm.startswith("https://"):
            raise RegistryError("registry advertised an insecure token realm")
        token_resp = self._get(realm, params=query)
        if token_resp.status_code != 200:
            log.warning("registry token fetch %s: HTTP %s", registry, token_resp.status_code)
            raise RegistryError("image not found or not public")
        data = token_resp.json()
        return data.get("token") or data.get("access_token")

    def _get_manifest(
        self, registry: str, repository: str, reference: str, token: Optional[str]
    ) -> tuple[dict[str, Any], str, str]:
        headers = {"Accept": _MANIFEST_ACCEPT}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = f"https://{registry}/v2/{repository}/manifests/{reference}"
        resp = self._get(url, headers=headers)
        if resp.status_code in (401, 404):
            log.warning("registry manifest %s/%s: HTTP %s", registry, repository, resp.status_code)
            raise RegistryError("image not found or not public")
        if resp.status_code != 200:
            raise RegistryError(f"registry returned HTTP {resp.status_code} for {image_ref_desc(repository, reference)}")
        # The digest we pin to is the one we HASHED, never just a header value.
        if ":" in reference:
            digest = _verify_digest(resp.content, reference)  # asked for exact content
        else:
            digest = "sha256:" + hashlib.sha256(resp.content).hexdigest()
        header = resp.headers.get("docker-content-digest")
        if header and header != digest:
            if header.partition(":")[0] != "sha256":
                _verify_digest(resp.content, header)  # other algo: must still match the bytes
            else:
                log.warning("registry Docker-Content-Digest header mismatch for %s", reference)
                raise RegistryError("image content does not match its digest")
        return resp.json(), resp.headers.get("content-type", ""), digest

    @staticmethod
    def _is_manifest_list(manifest: dict[str, Any], content_type: str) -> bool:
        if content_type.split(";")[0].strip() in _MANIFEST_LIST_TYPES:
            return True
        if manifest.get("mediaType") in _MANIFEST_LIST_TYPES:
            return True
        return "manifests" in manifest

    @staticmethod
    def _select_amd64_digest(manifest_list: dict[str, Any]) -> str:
        candidates = manifest_list.get("manifests", [])
        if not candidates:
            raise RegistryError("manifest list has no entries")
        for entry in candidates:
            platform = entry.get("platform", {})
            if platform.get("os") == "linux" and platform.get("architecture") == "amd64":
                return entry["digest"]
        # No exact linux/amd64 match: fall back to the first non-attestation entry.
        for entry in candidates:
            if entry.get("platform", {}).get("os") != "unknown":
                return entry["digest"]
        return candidates[0]["digest"]

    @staticmethod
    def _sum_layer_sizes(manifest: dict[str, Any]) -> int:
        layers = manifest.get("layers")
        if layers is None:
            raise RegistryError("manifest has no 'layers' field; unsupported schema")
        return sum(int(layer.get("size", 0)) for layer in layers)


def image_ref_desc(repository: str, reference: str) -> str:
    return f"{repository}:{reference}"
