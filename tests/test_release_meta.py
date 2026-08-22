import json
import pathlib
import tomllib

import pmox

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_versions_are_in_sync():
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    plugin = json.loads((_ROOT / "plugin/.claude-plugin/plugin.json").read_text(encoding="utf-8"))
    assert pmox.__version__ == pyproject["project"]["version"]
    assert plugin["version"] == pyproject["project"]["version"]
