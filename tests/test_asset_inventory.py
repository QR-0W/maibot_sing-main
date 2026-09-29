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
        path = tmp_path / name
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
