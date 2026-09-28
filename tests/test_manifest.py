"""Validate the shipped manifest with MaiBot's own strict validator when available.

The plugin repository is standalone, so this test is skipped outside a MaiBot
checkout. It exists because a hand-written manifest can pass local JSON checks
while the real Host rejects unknown fields at load time.
"""
from pathlib import Path
import json
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / '_manifest.json').read_text(encoding='utf-8'))
# The plugin lives at <host>/plugins/<id>, so the host root is two levels up.
HOST_ROOT = ROOT.parents[1]


def test_manifest_matches_documented_schema_fields():
    """Keep the field set within Manifest v2 and version in step with the config."""
    assert MANIFEST['manifest_version'] == 2
    assert MANIFEST['id'] == 'qr0w.maibot-sing'
    version = MANIFEST['version']
    assert re.fullmatch(r'\d+\.\d+\.\d+', version), version
    assert set(MANIFEST['urls']) <= {'repository', 'homepage', 'documentation', 'issues'}
    assert MANIFEST['urls']['repository'].startswith('https://')
    # Every place that records the version must agree with the manifest.
    assert f'config_version = "{version}"' in (ROOT / 'config.example.toml').read_text(encoding='utf-8')
    assert f'config_version: str = Field(default="{version}"' in (ROOT / 'plugin.py').read_text(encoding='utf-8')
    assert f'## {version}' in (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
    assert (ROOT / MANIFEST['changelog']).is_file()
    assert {item['name'] for item in MANIFEST['dependencies']} == {
        'aiohttp', 'httpx', 'cryptography', 'segno'}
    assert all(item['type'] == 'python_package' for item in MANIFEST['dependencies'])


@pytest.mark.skipif(not (HOST_ROOT / 'src/plugin_runtime/runner/manifest_validator.py').is_file(),
                    reason='需要 MaiBot 主程序目录')
def test_real_host_validator_accepts_manifest():
    """Run the Host's own ManifestValidator, the check that gates plugin loading."""
    import sys
    sys.path.insert(0, str(HOST_ROOT))
    try:
        from src.plugin_runtime.runner.manifest_validator import ManifestValidator
    finally:
        sys.path.pop(0)
    validator = ManifestValidator(project_root=HOST_ROOT, log_errors=False)
    parsed = validator.parse_manifest(MANIFEST, source=MANIFEST['id'])
    assert parsed is not None, validator.errors
    assert not validator.errors
    assert parsed.id == MANIFEST['id']
    assert parsed.version == MANIFEST['version']
