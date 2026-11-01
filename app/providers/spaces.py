"""Compatibility shim: the Spaces object store moved to app/providers/digitalocean/spaces.py."""
from app.providers.digitalocean.spaces import DO_API, SpacesStore, _key_name, summarize_objects  # noqa: F401
