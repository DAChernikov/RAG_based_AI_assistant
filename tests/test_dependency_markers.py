from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from packaging.markers import Marker

ROOT = Path(__file__).resolve().parents[1]


def test_torch_sources_are_platform_exclusive_and_cpu_only_on_linux():
    with (ROOT / "pyproject.toml").open("rb") as source:
        project = tomllib.load(source)
    torch_entries = project["tool"]["poetry"]["group"]["worker"]["dependencies"]["torch"]
    assert len(torch_entries) == 2
    by_source = {entry["source"]: entry for entry in torch_entries}
    assert by_source["PyPI"]["version"] == "2.9.0"
    assert by_source["pytorch-cpu"]["version"] == "2.9.0+cpu"

    darwin = {"sys_platform": "darwin"}
    linux = {"sys_platform": "linux"}
    assert Marker(by_source["PyPI"]["markers"]).evaluate(darwin)
    assert not Marker(by_source["PyPI"]["markers"]).evaluate(linux)
    assert Marker(by_source["pytorch-cpu"]["markers"]).evaluate(linux)
    assert not Marker(by_source["pytorch-cpu"]["markers"]).evaluate(darwin)


def test_lock_selects_native_macos_torch_for_current_platform():
    if sys.platform != "darwin":
        return
    lock = (ROOT / "poetry.lock").read_text()
    mac_section = lock.split('name = "torch"', 1)[1].split("[[package]]", 1)[0]
    assert 'version = "2.9.0"' in mac_section
    assert 'markers = "sys_platform == \\"darwin\\""' in mac_section
    assert "2.9.0+cpu" not in mac_section
