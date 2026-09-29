"""Validate Manifest v2 and the installed MaiBot SDK without starting the plugin.

The repository is standalone. Host-specific checks run only when the caller
explicitly sets ``MAIBOT_TEST_HOST_ROOT`` to a readable MaiBot checkout; no
machine-specific path is guessed or embedded here.
"""
from pathlib import Path
import importlib
import importlib.util
import json
import os
import re
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))
_HOST_ROOT_VALUE = os.environ.get("MAIBOT_TEST_HOST_ROOT", "").strip()
HOST_ROOT = Path(_HOST_ROOT_VALUE).expanduser().resolve() if _HOST_ROOT_VALUE else None
_PACKAGE_NAME = "_maibot_sing_manifest_test"


def test_manifest_matches_documented_schema_fields():
    """Keep the field set within Manifest v2 and version in step with config."""
    assert MANIFEST["manifest_version"] == 2
    assert MANIFEST["id"] == "qr0w.maibot-sing"
    version = MANIFEST["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), version
    assert set(MANIFEST["urls"]) <= {"repository", "homepage", "documentation", "issues"}
    assert MANIFEST["urls"]["repository"].startswith("https://")
    # Every place that records the version must agree with the manifest.
    assert f'config_version = "{version}"' in (ROOT / "config.example.toml").read_text(encoding="utf-8")
    assert f'config_version: str = Field(default="{version}"' in (ROOT / "plugin.py").read_text(encoding="utf-8")
    assert f"## {version}" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert (ROOT / MANIFEST["changelog"]).is_file()
    assert {item["name"] for item in MANIFEST["dependencies"]} == {
        "aiohttp",
        "httpx",
        "cryptography",
        "segno",
    }
    assert all(item["type"] == "python_package" for item in MANIFEST["dependencies"])


def test_runner_owned_config_and_sqlite_files_are_ignored():
    patterns = {
        line.strip()
        for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert "/config.toml" in patterns
    assert {
        "*.db",
        "*.db-shm",
        "*.db-wal",
        "*.sqlite",
        "*.sqlite-shm",
        "*.sqlite-wal",
        "*.sqlite3",
        "*.sqlite3-shm",
        "*.sqlite3-wal",
    } <= patterns


def _assert_host_checkout() -> Path:
    assert HOST_ROOT is not None, "MAIBOT_TEST_HOST_ROOT 未设置"
    assert HOST_ROOT.is_dir(), f"MAIBOT_TEST_HOST_ROOT 不是目录: {HOST_ROOT}"
    validator = HOST_ROOT / "src/plugin_runtime/runner/manifest_validator.py"
    assert validator.is_file(), f"指定目录缺少 Host ManifestValidator: {validator}"
    return HOST_ROOT


@pytest.mark.skipif(HOST_ROOT is None, reason="未显式设置 MAIBOT_TEST_HOST_ROOT")
def test_real_host_validator_accepts_manifest():
    """Run the selected read-only Host's exact load-gating validator."""
    host_root = _assert_host_checkout()
    sys.path.insert(0, str(host_root))
    try:
        from src.plugin_runtime.runner.manifest_validator import ManifestValidator
    finally:
        sys.path.pop(0)
    validator = ManifestValidator(project_root=host_root, log_errors=False)
    parsed = validator.parse_manifest(MANIFEST, source=MANIFEST["id"])
    assert parsed is not None, validator.errors
    assert not validator.errors
    assert parsed.id == MANIFEST["id"]
    assert parsed.version == MANIFEST["version"]


def _load_plugin_definition():
    spec = importlib.util.spec_from_file_location(
        _PACKAGE_NAME,
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[_PACKAGE_NAME] = package
    spec.loader.exec_module(package)
    return importlib.import_module(_PACKAGE_NAME + ".plugin")


@pytest.mark.skipif(HOST_ROOT is None, reason="未显式设置 MAIBOT_TEST_HOST_ROOT")
def test_installed_sdk_builds_plugin_config_schema():
    """Import definitions and call installed SDK schema generation only."""
    _assert_host_checkout()
    try:
        entry = _load_plugin_definition()
        schema = entry.SingPlugin.build_config_schema(
            plugin_id=MANIFEST["id"],
            plugin_name=MANIFEST["name"],
            plugin_version=MANIFEST["version"],
            plugin_description=MANIFEST["description"],
            plugin_author=MANIFEST["author"]["name"],
        )
    finally:
        for module_name in tuple(sys.modules):
            if module_name == _PACKAGE_NAME or module_name.startswith(_PACKAGE_NAME + "."):
                sys.modules.pop(module_name, None)
    assert schema["plugin_id"] == MANIFEST["id"]
    assert schema["plugin_info"]["version"] == MANIFEST["version"]
    assert schema["sections"]["plugin"]["fields"]["config_version"]["default"] == MANIFEST["version"]
    assert schema["sections"]["plugin"]["fields"]["enabled"]["default"] is False
