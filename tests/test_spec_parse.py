"""app/spec.py::parse_spec directly: safety limits, error wording, port bounds, and cross-field rules."""
import pytest

from app import spec
from app.spec import SpecError, parse_spec

BASE = "apiVersion: picopaas/v1\nname: demo\nimage: ghcr.io/example/demo:1.0\nport: 8080\n"


def err(text):
    with pytest.raises(SpecError) as ei:
        parse_spec(text)
    return str(ei.value)


def test_minimal_spec_defaults():
    s = parse_spec(BASE)
    assert (s.name, s.port, s.resources.db, s.resources.storage, s.healthCheck.enabled) == ("demo", 8080, False, False, False)
    assert s.env == {} and s.secrets == []


@pytest.mark.parametrize("port", [1024, 1025, 8080, 65534, 65535])
def test_port_boundaries_ok(port):
    assert parse_spec(BASE.replace("8080", str(port))).port == port


@pytest.mark.parametrize("port", [0, 1, 80, 443, 1023, 65536, 99999, -1, -8080])
def test_port_out_of_range_message_explains_the_range(port):
    m = err(BASE.replace("8080", str(port)))
    assert m.startswith("port: must be 1024-65535") and "privileged" in m


@pytest.mark.parametrize("val", ['"8080"', "8080.0", "8080.5", "true", "null", "[8080]", "0x1F90x"])
def test_port_wrong_type_is_rejected_without_the_range_message(val):
    m = err(BASE.replace("8080", val))
    assert m.startswith("port:") and "1024-65535" not in m


def test_missing_port_and_unknown_field():
    assert "port: is required" in err(BASE.replace("port: 8080\n", ""))
    assert "colour: isn't a known field" in err(BASE + "colour: red\n")


def test_api_version_must_match():
    assert "apiVersion: must be" in err(BASE.replace("picopaas/v1", "picopaas/v2"))
    assert "apiVersion: is required" in err(BASE.replace("apiVersion: picopaas/v1\n", ""))


def test_size_limit_is_bytes_not_characters():
    pad = "# " + "é" * (spec.MAX_SPEC_BYTES // 2) + "\n"  # 2 bytes per char: over the cap in bytes only
    assert len(pad) < spec.MAX_SPEC_BYTES < len(pad.encode())
    assert "KB limit" in err(BASE + pad)
    assert parse_spec(BASE + "# " + "x" * 1000 + "\n").name == "demo"


def test_aliases_and_anchors_rejected():
    assert "anchors or aliases" in err(BASE + "env:\n  A: &x one\n  B: *x\n")


def test_invalid_yaml_reports_line():
    m = err("name: [unclosed\nport: 1")
    assert "isn't valid YAML" in m and "line" in m


def test_deeply_nested_yaml_does_not_crash():
    m = err(BASE + "env: " + "[" * 5000 + "]" * 5000 + "\n")
    assert "nested too deeply" in m or "isn't valid YAML" in m or "wrong type" in m


@pytest.mark.parametrize("text", ["", "- a\n- b\n", "just a string", "42"])
def test_non_mapping_documents(text):
    assert "must be a YAML mapping" in err(text)


def test_python_object_tags_are_not_executed():
    assert "isn't valid YAML" in err(BASE + "env:\n  A: !!python/object/apply:os.system ['true']\n")


def test_secrets_duplicates_and_overlap():
    assert "each name can only be listed once" in err(BASE + "secrets: [A, A]\n")
    m = err(BASE + "env:\n  A: x\nsecrets: [A]\n")
    assert "A appears under both env and secrets" in m


def test_nested_location_wording():
    m = err(BASE + "healthCheck:\n  enabled: yes-please\n")
    assert m.startswith("healthCheck, enabled:") and "wrong type" in m
    m = err(BASE + "secrets: [1]\n")
    assert "secrets, item 1" in m


def test_empty_and_overlong_strings():
    assert "name: can't be empty" in err(BASE.replace("name: demo", "name: ''"))
    assert "name: is too long" in err(BASE.replace("name: demo", "name: " + "x" * 256))
    assert "image: is too long" in err(BASE.replace("ghcr.io/example/demo:1.0", "x" * 513))


def test_non_string_env_values_rejected():
    assert "wrong type" in err(BASE + "env:\n  A: 1\n")  # StrictStr: quote numbers


def test_health_path_validation_flows_through():
    assert parse_spec(BASE + "healthCheck:\n  enabled: true\n  path: /up\n").healthCheck.path == "/up"
    assert err(BASE + "healthCheck:\n  path: nope\n")
