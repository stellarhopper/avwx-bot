"""The configurator's JavaScript and the bot's Python must agree on two things:
the link format, and what counts as a valid config. Node runs the site's own
modules so this tests the shipped code, not a copy of it."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from bot import guildcfg

SITE = Path(__file__).resolve().parent.parent / "site"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def run_node(script: str) -> str:
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=30, cwd=SITE,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


DOC = {"schema_version": 1, "home_airports": ["KBJC"], "_guild": {"name": "Pilots ✈️", "channels": []}}


def test_python_link_decodes_in_javascript():
    text = guildcfg.encode_fragment(DOC)
    out = run_node(
        f"import {{ decodeFragment }} from './codec.js';"
        f"console.log(JSON.stringify(await decodeFragment({json.dumps(text)})));"
    )
    assert json.loads(out) == DOC


def test_javascript_link_decodes_in_python():
    out = run_node(
        f"import {{ encodeFragment }} from './codec.js';"
        f"console.log(await encodeFragment({json.dumps(DOC)}));"
    )
    assert guildcfg.decode_fragment(out) == DOC


@pytest.mark.parametrize(
    "doc",
    [
        guildcfg.defaults(),
        {"schema_version": 1, "brief": {"time": "6:30"}},
        {"schema_version": 1, "home_airports": ["KBJC", "KBJC"]},
        {"schema_version": 1, "alerts": {"area": {"radius_nm": 2}}},
        {"schema_version": 1, "nope": True},
        {"schema_version": 1, "brief": {"channel_id": 5}},
    ],
)
def test_javascript_validator_agrees_with_jsonschema(doc):
    schema = guildcfg.schema()
    out = run_node(
        "import { validate } from './validate.js';"
        f"console.log(JSON.stringify(validate({json.dumps(schema)}, {json.dumps(doc)})));"
    )
    assert bool(json.loads(out)) == bool(guildcfg.validate(doc))
