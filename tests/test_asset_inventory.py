"""Real byte inventory with all runtime/version lookups locally mocked."""
from pathlib import Path
import hashlib
import importlib
import importlib.util
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'inventory_test_pkg', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
pkg = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pkg
spec.loader.exec_module(pkg)
inventory = importlib.import_module('inventory_test_pkg.services.asset_inventory')
recipe = importlib.import_module('inventory_test_pkg.runtime.recipe_identity')


def versions():
    return {name: '1.2.3' for name in recipe.VERSION_NAMES}


def assets(tmp_path):
    paths = {}
    for number, name in enumerate(name for name in recipe.HASH_NAMES if name != 'source'):
        path = tmp_path / ('upstream' if name == 'rvc_upstream' else name)
        if name == 'rvc_upstream':
            path.mkdir()
            (path / 'infer.py').write_text('synthetic code')
        elif name == 'demucs_repo':
            path.mkdir()
            (path / 'htdemucs.yaml').write_text("models: ['955717e8']\n")
            (path / '955717e8-8726e21a.th').write_bytes(b'synthetic local checkpoint')
        else:
            path.write_bytes(('asset-%s-%d' % (name, number)).encode())
        paths[name] = path
    return inventory.AssetPaths(**paths)


def test_inventory_hashes_actual_source_assets_and_versions(tmp_path):
    source = tmp_path / 'source.audio'
    source.write_bytes(b'exact selected source bytes')
    paths = assets(tmp_path)
    service = inventory.AssetInventory(paths, versions)
    first = service.build(source)
    assert set(first.hashes) == set(recipe.HASH_NAMES)
    assert set(first.versions) == set(recipe.VERSION_NAMES)
    assert first.hashes['source'] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert first.hashes['model'] == hashlib.sha256(paths.model.read_bytes()).hexdigest()
    assert first.versions['ffmpeg'] == '1.2.3'

    paths.model.write_bytes(b'changed real model')
    second = service.build(source)
    assert second.hashes['model'] != first.hashes['model']
    assert {key: value for key, value in second.hashes.items() if key != 'model'} == {
        key: value for key, value in first.hashes.items() if key != 'model'}


def test_directory_inventory_is_deterministic_and_content_sensitive(tmp_path):
    tree = tmp_path / 'weights'
    (tree / 'nested').mkdir(parents=True)
    (tree / 'a.bin').write_bytes(b'a')
    (tree / 'nested' / 'b.bin').write_bytes(b'b')
    first = inventory.sha256_asset(tree)
    assert inventory.sha256_asset(tree) == first
    (tree / 'nested' / 'b.bin').write_bytes(b'changed')
    assert inventory.sha256_asset(tree) != first


def test_source_tree_ignores_only_regenerable_metadata(tmp_path):
    upstream = tmp_path / 'upstream'
    upstream.mkdir()
    script = upstream / 'infer.py'
    script.write_text('code-v1')
    before = inventory.sha256_asset(upstream, source_tree=True)
    (upstream / '__pycache__').mkdir()
    (upstream / '__pycache__' / 'infer.cpython-39.pyc').write_bytes(b'bytecode-v1')
    (upstream / '.git').mkdir()
    (upstream / '.git' / 'HEAD').write_text('main')
    assert inventory.sha256_asset(upstream, source_tree=True) == before
    (upstream / '__pycache__' / 'infer.cpython-39.pyc').write_bytes(b'bytecode-v2')
    assert inventory.sha256_asset(upstream, source_tree=True) == before
    script.write_text('code-v2')
    assert inventory.sha256_asset(upstream, source_tree=True) != before
    assert inventory.sha256_asset(upstream) != before


def test_inventory_rejects_detached_rvc_upstream(tmp_path):
    source = tmp_path / 'source.audio'
    source.write_bytes(b'synthetic')
    from dataclasses import replace
    paths = assets(tmp_path)
    detached = tmp_path / 'detached-upstream'
    detached.mkdir()
    (detached / 'infer.py').write_text('different source')
    with pytest.raises(inventory.InventoryError) as error:
        inventory.AssetInventory(replace(paths, rvc_upstream=detached), versions).build(source)
    assert error.value.code == 'rvc_upstream_mismatch'


def test_offline_demucs_repo_refuses_missing_or_misleading_bag(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    with pytest.raises(inventory.InventoryError, match='repo'):
        inventory.validate_demucs_repo(repo)
    (repo / 'htdemucs.yaml').write_text("models: ['different']\n")
    (repo / '955717e8-8726e21a.th').write_bytes(b'synthetic checkpoint')
    with pytest.raises(inventory.InventoryError, match='955717e8'):
        inventory.validate_demucs_repo(repo)
    (repo / 'htdemucs.yaml').write_text("models: ['955717e8']\n")
    assert inventory.validate_demucs_repo(repo) == repo
    (repo / 'unexpected.th').write_bytes(b'cannot silently load other model')
    with pytest.raises(inventory.InventoryError, match='only'):
        inventory.validate_demucs_repo(repo)


def test_symlinked_or_incomplete_inventory_is_refused(tmp_path):
    target = tmp_path / 'target'
    target.write_bytes(b'asset')
    link = tmp_path / 'link'
    link.symlink_to(target)
    with pytest.raises(inventory.InventoryError) as error:
        inventory.sha256_asset(link)
    assert error.value.code == 'asset_symlink'

    source = tmp_path / 'source.audio'
    source.write_bytes(b'source')
    paths = assets(tmp_path)
    incomplete = versions()
    incomplete.pop('torch')
    with pytest.raises(inventory.InventoryError) as error:
        inventory.AssetInventory(paths, lambda: incomplete).build(source)
    assert error.value.code == 'runtime_version_missing'
