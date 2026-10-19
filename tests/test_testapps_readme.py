"""Every yaml example in testapps/README.md must be a valid spec using real refs."""
import pathlib
import re

from app.env_refs import ALLOWED_REFS
from app.spec import parse_spec

README = pathlib.Path(__file__).resolve().parents[1] / "testapps" / "README.md"


def test_readme_examples_are_valid_specs():
    blocks = re.findall(r"```yaml\n(.*?)```", README.read_text(), re.S)
    assert len(blocks) >= 4
    for block in blocks:
        spec = parse_spec(block)
        for value in spec.env.values():
            for ref in re.findall(r"\$\{\{\s*([\w.]+)\s*\}\}", value):
                assert ref in ALLOWED_REFS, ref
