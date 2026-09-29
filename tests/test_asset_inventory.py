"""Real byte inventory with all runtime/version lookups locally mocked."""
from pathlib import Path
import copy
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


def generation_context(tmp_path, paths, *, artifact_root=None):
    execution = {name: getattr(paths,name) for name in inventory.RUNTIME_HASH_NAMES}
    execution.update(worker_python=tmp_path/'venv/bin/python',
                     worker_script=paths.media_stage,
                     inference_lock=tmp_path/'inference.lock')
    return {
        'execution_paths': execution,
        'artifact_root': artifact_root or tmp_path/'covers',
        'limits': {'max_duration_s': 300, 'max_download_bytes': 64*1024*1024},
        'parameter_policy': {
            'schema': 'sing-render-policy-v1',
            'sample_rate_hz': 44100,
            'chunk_seconds': 20,
            'tail_fold_seconds': 5,
            'pitch_semitones': 0,
            'f0_method': 'harvest',
            'index_rate': 0.5,
            'filter_radius': 3,
            'rms_mix_rate': 0.25,
            'protect': 0.33,
            'seed': 20260928,
            'resample_hz': 44100,
            'encode': {'codec': 'libmp3lame', 'bitrate': '192k'},
            'instrumental_modes': [False, True],
            'source_binding': 'selected-provider-track-id',
        },
    }


def test_inventory_hashes_actual_source_assets_and_versions(tmp_path):
    source = tmp_path / 'source.audio'
    source.write_bytes(b'exact selected source bytes')
    paths = assets(tmp_path)
    service = inventory.AssetInventory(paths, versions)
    runtime = service.build_runtime()
    assert set(runtime.hashes) == set(recipe.HASH_NAMES)-{'source'}
    assert 'source' not in runtime.hashes
    assert set(runtime.versions) == set(recipe.VERSION_NAMES)
    first = service.build(source)
    assert set(first.hashes) == set(recipe.HASH_NAMES)
    assert set(first.versions) == set(recipe.VERSION_NAMES)
    assert {name:first.hashes[name] for name in runtime.hashes}==runtime.hashes
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


def test_runtime_generation_is_stable_and_sensitive_to_real_runtime(tmp_path):
    paths=assets(tmp_path)
    context=generation_context(tmp_path,paths)
    first_runtime=inventory.AssetInventory(paths,versions).build_runtime()
    first=inventory.runtime_generation(first_runtime,context)
    restarted=inventory.AssetInventory(paths,versions).build_runtime()
    assert inventory.runtime_generation(restarted,copy.deepcopy(context))==first
    newer_versions=versions();newer_versions['torch']='9.9.9'
    changed_version=inventory.AssetInventory(paths,lambda:newer_versions).build_runtime()
    assert inventory.runtime_generation(changed_version,context)!=first

    source_a=tmp_path/'source-a.audio';source_a.write_bytes(b'first source')
    source_b=tmp_path/'source-b.audio';source_b.write_bytes(b'second source')
    complete_a=inventory.AssetInventory(paths,versions).build(source_a)
    complete_b=inventory.AssetInventory(paths,versions).build(source_b)
    assert complete_a.hashes['source']!=complete_b.hashes['source']
    assert inventory.runtime_generation(first_runtime,context)==first
    other_worker=copy.deepcopy(context)
    other_worker['execution_paths']['worker_python']=tmp_path/'other-venv/bin/python'
    assert inventory.runtime_generation(first_runtime,other_worker)!=first

    paths.model.write_bytes(b'changed model bytes at the same path')
    changed=inventory.AssetInventory(paths,versions).build_runtime()
    assert inventory.runtime_generation(changed,context)!=first
    other_root=generation_context(tmp_path,paths,artifact_root=tmp_path/'other-covers')
    assert inventory.runtime_generation(changed,other_root)!=inventory.runtime_generation(changed,context)


def test_runtime_generation_ignores_regenerable_source_tree_metadata(tmp_path):
    paths=assets(tmp_path)
    service=inventory.AssetInventory(paths,versions)
    context=generation_context(tmp_path,paths)
    before=inventory.runtime_generation(service.build_runtime(),context)
    cache=paths.rvc_upstream/'__pycache__';cache.mkdir()
    (cache/'infer.cpython-39.pyc').write_bytes(b'generated bytecode')
    git=paths.rvc_upstream/'.git';git.mkdir();(git/'HEAD').write_text('main')
    assert inventory.runtime_generation(service.build_runtime(),context)==before
    (paths.rvc_upstream/'infer.py').write_text('changed executable code')
    assert inventory.runtime_generation(service.build_runtime(),context)!=before


def test_runtime_generation_rejects_missing_nan_and_oversized_context(tmp_path):
    paths=assets(tmp_path)
    runtime=inventory.AssetInventory(paths,versions).build_runtime()
    base=generation_context(tmp_path,paths)

    missing=copy.deepcopy(base);missing.pop('artifact_root')
    with pytest.raises(inventory.InventoryError) as error:
        inventory.runtime_generation(runtime,missing)
    assert error.value.code=='runtime_context_invalid'
    missing_path=copy.deepcopy(base);missing_path['execution_paths'].pop('worker_python')
    with pytest.raises(inventory.InventoryError) as error:
        inventory.runtime_generation(runtime,missing_path)
    assert error.value.code=='runtime_context_invalid'
    missing_limit=copy.deepcopy(base);missing_limit['limits'].pop('max_duration_s')
    with pytest.raises(inventory.InventoryError) as error:
        inventory.runtime_generation(runtime,missing_limit)
    assert error.value.code=='runtime_context_invalid'

    nan=copy.deepcopy(base);nan['parameter_policy']['gain']=float('nan')
    with pytest.raises(inventory.InventoryError) as error:
        inventory.runtime_generation(runtime,nan)
    assert error.value.code=='runtime_context_invalid'

    long_value=copy.deepcopy(base);long_value['parameter_policy']['name']='x'*2049
    with pytest.raises(inventory.InventoryError) as error:
        inventory.runtime_generation(runtime,long_value)
    assert error.value.code=='runtime_context_invalid'

    oversized=copy.deepcopy(base)
    oversized['parameter_policy']['matrix']=['x'*1000 for _ in range(100)]
    with pytest.raises(inventory.InventoryError) as error:
        inventory.runtime_generation(runtime,oversized)
    assert error.value.code=='runtime_context_oversized'


def test_runtime_generation_requires_all_non_source_hashes_and_versions(tmp_path):
    paths=assets(tmp_path)
    runtime=inventory.AssetInventory(paths,versions).build_runtime()
    context=generation_context(tmp_path,paths)
    missing_hash=dict(runtime.hashes);missing_hash.pop('model')
    with pytest.raises(inventory.InventoryError) as error:
        inventory.runtime_generation(inventory.RuntimeInventory(missing_hash,runtime.versions),context)
    assert error.value.code=='runtime_inventory_invalid'
    missing_version=dict(runtime.versions);missing_version.pop('torch')
    with pytest.raises(inventory.InventoryError) as error:
        inventory.runtime_generation(inventory.RuntimeInventory(runtime.hashes,missing_version),context)
    assert error.value.code=='runtime_inventory_invalid'


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
