"""Plugin registration, schema and manifest, checked without the Hermes host."""

import re

from helpers import PLUGIN_DIR, plugin


class RecordingContext:
    def __init__(self):
        self.tools = []

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)


def test_register_adds_one_tool():
    ctx = RecordingContext()
    plugin.register(ctx)
    assert len(ctx.tools) == 1
    entry = ctx.tools[0]
    assert entry["name"] == "diff_review"
    assert entry["toolset"] == "diff_review"
    assert entry["schema"]["name"] == "diff_review"
    assert entry["handler"] is plugin.tools.diff_review


def test_schema_keeps_v1_parameters():
    params = plugin.schemas.DIFF_REVIEW["parameters"]
    assert set(params["properties"]) == {"base", "target", "diff_text"}
    assert params["properties"]["base"]["default"] == "HEAD"
    assert params["required"] == []
    assert all(p["type"] == "string" for p in params["properties"].values())


def test_manifest_matches_registration():
    manifest = (PLUGIN_DIR / "plugin.yaml").read_text()
    assert re.search(r"^version: 1\.1\.0$", manifest, re.M)
    assert re.search(r"^provides_tools:\n  - diff_review$", manifest, re.M)


def test_license_is_mit():
    text = (PLUGIN_DIR / "LICENSE").read_text()
    assert text.startswith("MIT License")
    assert "Copyright (c) 2026 sprmn24" in text
