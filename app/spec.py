"""Parsing and strict validation of the PicoPaaS YAML spec (docs/ARCHITECTURE.md "YAML spec (v1)").

Safety: yaml.safe_load only, a 64KB size cap, and YAML aliases/anchors are rejected outright
(they are the "billion laughs" expansion vector and have no use in a spec this small).
"""
from typing import Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError, field_validator

from app.config import HEALTH_TIMEOUT_MAX_S, HEALTH_TIMEOUT_MIN_S, VOLUME_DEFAULT_MOUNT_PATH
from app import platform_settings
from app.volumes import MOUNT_PATH_MAX, validate_mount_path
from app.health_path import DEFAULT_HEALTH_PATH, validate_health_path

MAX_SPEC_BYTES = 64 * 1024


class SpecError(ValueError):
    pass


class _Volume(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sizeGi: StrictInt = Field(default=1, ge=1, le=platform_settings.BY_KEY["override_volume_max_gi"].max)  # real ceiling: volumes.apply_request
    mountPath: StrictStr = Field(default=VOLUME_DEFAULT_MOUNT_PATH, max_length=MOUNT_PATH_MAX)

    @field_validator("mountPath")
    @classmethod
    def _validate_mount_path(cls, v: str) -> str:
        return validate_mount_path(v)


class _Resources(BaseModel):
    model_config = ConfigDict(extra="forbid")
    db: StrictBool = False
    db_engine: Literal["mysql", "pg"] = "mysql"  # engine for `db: true`; immutable once the app has a database
    storage: StrictBool = False
    volume: Optional[_Volume] = None  # persistent volume (platform must have VOLUMES_ENABLED); can't be removed later


class _HealthCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool = False
    path: StrictStr = DEFAULT_HEALTH_PATH
    # Probe timeout in seconds; omitted = platform defaults (readiness 3s / liveness 5s).
    timeoutSeconds: Optional[StrictInt] = Field(default=None, ge=HEALTH_TIMEOUT_MIN_S, le=HEALTH_TIMEOUT_MAX_S)

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v: str) -> str:
        return validate_health_path(v)


class AppSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    apiVersion: Literal["picopaas/v1"]
    name: StrictStr = Field(min_length=1, max_length=255)
    image: StrictStr = Field(min_length=1, max_length=512)
    port: StrictInt = Field(ge=1024, le=65535)
    resources: _Resources = Field(default_factory=_Resources)
    healthCheck: _HealthCheck = Field(default_factory=_HealthCheck)
    env: Dict[StrictStr, StrictStr] = Field(default_factory=dict)
    secrets: List[StrictStr] = Field(default_factory=list)
    project: Optional[StrictStr] = Field(default=None, min_length=1, max_length=80)  # project slug; default personal


def _loc(loc) -> str:
    """env.0.value -> 'env, item 1, value'; empty -> 'spec'."""
    parts = []
    for p in loc:
        if isinstance(p, int):
            parts.append(f"item {p + 1}")
        else:
            parts.append(str(p))
    return ", ".join(parts) or "spec"


def _plain_msg(err) -> str:
    """Pydantic's wording -> plain English (keeps our own ValueError text untouched)."""
    t, msg = err.get("type", ""), err["msg"]
    if t == "missing":
        return "is required"
    if t == "extra_forbidden":
        return "isn't a known field"
    if t == "literal_error":
        return f"must be {err.get('ctx', {}).get('expected', 'a supported value')}"
    if t.endswith("_type") or t in ("string_type", "bool_type", "int_type", "dict_type", "list_type"):
        return "has the wrong type"
    if t == "value_error":
        return msg.removeprefix("Value error, ")
    if t in ("greater_than_equal", "less_than_equal") and err.get("loc", ())[-1:] == ("port",):
        return "must be 1024-65535 (non-root containers can't bind privileged ports)"
    if t in ("greater_than", "greater_than_equal", "less_than_equal", "string_too_short", "string_too_long"):
        return "is out of range" if t.startswith(("greater", "less")) else ("can't be empty" if t == "string_too_short" else "is too long")
    return msg


def parse_spec(text: str) -> AppSpec:
    if len(text.encode("utf-8")) > MAX_SPEC_BYTES:
        raise SpecError(f"Spec is over the {MAX_SPEC_BYTES // 1024} KB limit. Remove comments or unused entries.")
    try:
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent):
                raise SpecError("Specs can't use YAML anchors or aliases (& and *). Write the values out in full.")
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark is not None else ""
        reason = getattr(exc, "problem", None)
        reason = f": {reason}" if reason else ""
        raise SpecError(f"Your spec isn't valid YAML{where}{reason}. Check indentation and colons.")
    except RecursionError:
        raise SpecError("Your spec is nested too deeply. Flatten it.")
    if not isinstance(data, dict):
        raise SpecError("A spec must be a YAML mapping of keys and values, starting with apiVersion: picopaas/v1.")
    try:
        spec = AppSpec.model_validate(data)
    except ValidationError as exc:
        parts = []
        for err in exc.errors():
            parts.append(f"{_loc(err['loc'])}: {_plain_msg(err)}")
        raise SpecError("; ".join(parts))
    if len(set(spec.secrets)) != len(spec.secrets):
        raise SpecError("secrets: each name can only be listed once.")
    overlap = set(spec.secrets) & set(spec.env)
    if overlap:
        raise SpecError(f"{', '.join(sorted(overlap))} appears under both env and secrets. Keep it in one place.")
    return spec
