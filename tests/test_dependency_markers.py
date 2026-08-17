from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from packaging.markers import Marker

ROOT = Path(__file__).resolve().parents[1]


def test_torch_sources_are_platform_exclusive_and_cpu_only_on_linux():
    with (ROOT / "pyproject.toml").open("rb") as source:
        project = tomllib.load(source)
    torch_entries = project["tool"]["poetry"]["group"]["embedding"]["dependencies"]["torch"]
    assert len(torch_entries) == 3
    mac_entry = next(
        entry for entry in torch_entries if entry["markers"] == "sys_platform == 'darwin'"
    )
    linux_entry = next(
        entry for entry in torch_entries if entry["markers"] == "sys_platform == 'linux'"
    )
    other_entry = next(entry for entry in torch_entries if entry not in (mac_entry, linux_entry))
    assert mac_entry["source"] == "PyPI"
    assert mac_entry["version"] == "2.13.0"
    assert linux_entry["source"] == "pytorch-cpu"
    assert linux_entry["version"] == "2.13.0+cpu"
    assert other_entry["source"] == "PyPI"
    assert other_entry["version"] == "2.13.0"

    darwin = {"sys_platform": "darwin"}
    linux = {"sys_platform": "linux"}
    assert Marker(mac_entry["markers"]).evaluate(darwin)
    assert not Marker(mac_entry["markers"]).evaluate(linux)
    assert Marker(linux_entry["markers"]).evaluate(linux)
    assert not Marker(linux_entry["markers"]).evaluate(darwin)
    assert not Marker(other_entry["markers"]).evaluate(darwin)
    assert not Marker(other_entry["markers"]).evaluate(linux)


def test_lock_selects_native_macos_torch_for_current_platform():
    if sys.platform != "darwin":
        return
    lock = (ROOT / "poetry.lock").read_text()
    mac_section = lock.split('name = "torch"', 1)[1].split("[[package]]", 1)[0]
    assert 'version = "2.13.0"' in mac_section
    assert 'markers = "sys_platform != \\"linux\\""' in mac_section
    assert "2.13.0+cpu" not in mac_section
