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

import re
from typing import Any, Optional

import httpx

from app.providers.base import Registry, RegistryError

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

_DOCKER_HUB_REGISTRY = "registry-1.docker.io"
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
    else:
        registry = _DOCKER_HUB_REGISTRY
        repository = name if "/" in name else f"library/{name}"

    return registry, repository, reference


def _parse_www_authenticate(header: str) -> dict[str, str]:
    return dict(_WWW_AUTH_PARAM_RE.findall(header))


class RegistryClient(Registry):
    def __init__(self, client: Optional[httpx.Client] = None, timeout: float = 10.0) -> None:
        self._client = client or httpx.Client(timeout=timeout)

    def image_size_bytes(self, image_ref: str) -> int:
        registry, repository, reference = _parse_image_ref(image_ref)
        try:
            token = self._get_token(registry, repository)
            manifest, content_type = self._get_manifest(registry, repository, reference, token)
            if self._is_manifest_list(manifest, content_type):
                manifest, _ = self._get_manifest(
                    registry, repository, self._select_amd64_digest(manifest), token
                )
            return self._sum_layer_sizes(manifest)
        except RegistryError:
            raise
        except httpx.HTTPError as exc:
            raise RegistryError(f"registry request failed: {exc}") from exc

    def _get_token(self, registry: str, repository: str) -> Optional[str]:
        resp = self._client.get(f"https://{registry}/v2/")
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

        token_resp = self._client.get(realm, params=query)
        if token_resp.status_code != 200:
            raise RegistryError("image not found or not public")
        data = token_resp.json()
        return data.get("token") or data.get("access_token")

    def _get_manifest(
        self, registry: str, repository: str, reference: str, token: Optional[str]
    ) -> tuple[dict[str, Any], str]:
        headers = {"Accept": _MANIFEST_ACCEPT}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = f"https://{registry}/v2/{repository}/manifests/{reference}"
        resp = self._client.get(url, headers=headers)
        if resp.status_code in (401, 404):
            raise RegistryError("image not found or not public")
        if resp.status_code != 200:
            raise RegistryError(f"registry returned HTTP {resp.status_code} for {image_ref_desc(repository, reference)}")
        return resp.json(), resp.headers.get("content-type", "")

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
        # No exact linux/amd64 match (e.g. attestation-only lists) — fall back to the first entry.
        return candidates[0]["digest"]

    @staticmethod
    def _sum_layer_sizes(manifest: dict[str, Any]) -> int:
        layers = manifest.get("layers")
        if layers is None:
            raise RegistryError("manifest has no 'layers' field; unsupported schema")
        return sum(int(layer.get("size", 0)) for layer in layers)


def image_ref_desc(repository: str, reference: str) -> str:
    return f"{repository}:{reference}"
