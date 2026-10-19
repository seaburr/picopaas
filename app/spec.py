"""Parsing and strict validation of the PicoPaaS YAML spec (docs/ARCHITECTURE.md "YAML spec (v1)").

Safety: yaml.safe_load only, a 64KB size cap, and YAML aliases/anchors are rejected outright
(they are the "billion laughs" expansion vector and have no use in a spec this small).
"""
from typing import Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError

MAX_SPEC_BYTES = 64 * 1024


class SpecError(ValueError):
    pass


class _Resources(BaseModel):
    model_config = ConfigDict(extra="forbid")
    db: StrictBool = False
    storage: StrictBool = False


class AppSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    apiVersion: Literal["picopaas/v1"]
    name: StrictStr = Field(min_length=1, max_length=255)
    image: StrictStr = Field(min_length=1, max_length=512)
    port: StrictInt = Field(gt=0, le=65535)
    resources: _Resources = Field(default_factory=_Resources)
    env: Dict[StrictStr, StrictStr] = Field(default_factory=dict)
    secrets: List[StrictStr] = Field(default_factory=list)


def parse_spec(text: str) -> AppSpec:
    if len(text.encode("utf-8")) > MAX_SPEC_BYTES:
        raise SpecError(f"spec is larger than {MAX_SPEC_BYTES // 1024}KB")
    try:
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent):
                raise SpecError("YAML aliases/anchors are not allowed in a spec")
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SpecError(f"invalid YAML: {exc.__class__.__name__}")
    except RecursionError:
        raise SpecError("spec is nested too deeply")
    if not isinstance(data, dict):
        raise SpecError("spec must be a YAML mapping")
    try:
        spec = AppSpec.model_validate(data)
    except ValidationError as exc:
        parts = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"]) or "spec"
            parts.append(f"{loc}: {err['msg']}")
        raise SpecError("; ".join(parts))
    if len(set(spec.secrets)) != len(spec.secrets):
        raise SpecError("secrets: duplicate names")
    overlap = set(spec.secrets) & set(spec.env)
    if overlap:
        raise SpecError(f"names in both env and secrets: {', '.join(sorted(overlap))}")
    return spec
