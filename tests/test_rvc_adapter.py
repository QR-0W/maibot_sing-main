"""Packaged adapter contracts; synthetic guards, no model imports or inference."""
from pathlib import Path
from types import SimpleNamespace
import builtins
import importlib.util
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


adapter = load_file('sing_rvc_adapter_test', ROOT / 'engines/rvc/rvc_infer.py')


class GuardStopped(RuntimeError):
    pass


@pytest.mark.parametrize('stop_at', ['limits', 'headroom'])
def test_guard_precedes_all_model_imports(monkeypatch, stop_at):
    calls = []

    def limits():
        calls.append('limits')
        if stop_at == 'limits':
            raise GuardStopped()

    def headroom():
        calls.append('headroom')
        raise GuardStopped()

    fake = SimpleNamespace(verify_limits=limits, verify_host_headroom=headroom)
    spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda module: None))
    monkeypatch.setattr(adapter.importlib.util, 'spec_from_file_location', lambda *args: spec)
    monkeypatch.setattr(adapter.importlib.util, 'module_from_spec', lambda spec: fake)
    original_import = builtins.__import__

    def no_model_import(name, *args, **kwargs):
        if name.split('.')[0] in {'torch', 'numpy', 'fairseq', 'faiss', 'librosa', 'soundfile', 'infer'}:
            pytest.fail('Model import occurred before the budget refusal: ' + name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', no_model_import)
    monkeypatch.setenv('INVOCATION_ID', 'synthetic-contained-unit')
    monkeypatch.setattr(sys, 'argv', ['rvc_infer.py', '--model', '/model', '--index', '/index',
                                    '--input', '/input', '--output', '/output', '--hubert', '/hubert'])
    with pytest.raises(GuardStopped):
        adapter.main()
    assert calls == (['limits'] if stop_at == 'limits' else ['limits', 'headroom'])


@pytest.mark.parametrize('memory_mib,tasks,cpu', [(2048, 64, 150), (3072, 32, 100)])
def test_adapter_shared_guard_accepts_frozen_budget(tmp_path, monkeypatch, memory_mib, tasks, cpu):
    guard = load_file('sing_rvc_shared_guard_test', ROOT / 'runtime/worker.py')
    root = tmp_path / 'cgroup'
    unit = root / 'unit.service'
    unit.mkdir(parents=True)
    values = {'MEMORY_MAX': memory_mib * 1024**2, 'MEMORY_HIGH': memory_mib * 1024**2 * 3 // 4,
              'TASKS_MAX': tasks, 'CPU_PERCENT': cpu, 'HEADROOM': 512 * 1024**2}
    for key, value in values.items():
        monkeypatch.setenv('MAIBOT_SING_BUDGET_' + key, str(value))
    monkeypatch.setenv('INVOCATION_ID', 'synthetic-contained-unit')
    files = {'memory.max': values['MEMORY_MAX'], 'memory.high': values['MEMORY_HIGH'],
             'memory.swap.max': 0, 'memory.oom.group': 1, 'pids.max': tasks,
             'cpu.max': '%d 100000' % (cpu * 1000)}
    for name, value in files.items():
        (unit / name).write_text(str(value))
    (root / 'memory.max').write_text(str(8 * 1024**3))
    (root / 'memory.current').write_text(str(4 * 1024**3))
    monkeypatch.setattr(guard, '_unit_cgroup', lambda: unit)
    monkeypatch.setattr(guard, 'Path', lambda path: root if str(path) == '/sys/fs/cgroup' else Path(path))
    guard.verify_limits()
    guard.verify_host_headroom()
    (unit / 'memory.max').write_text(str(4 * 1024**3))
    with pytest.raises(RuntimeError, match='memory.max'):
        guard.verify_limits()
    (unit / 'memory.max').write_text(str(values['MEMORY_MAX']))
    monkeypatch.delenv('MAIBOT_SING_BUDGET_MEMORY_MAX')
    with pytest.raises(RuntimeError, match='MEMORY_MAX'):
        guard.verify_limits()


def test_hubert_has_no_machine_default(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['rvc_infer.py', '--model', '/model', '--index', '/index',
                                    '--input', '/input', '--output', '/output'])
    with pytest.raises(SystemExit) as error:
        adapter.main()
    assert error.value.code == 2


def test_unknown_hubert_never_reaches_deserialization(tmp_path):
    checkpoint = tmp_path / 'unknown.pt'
    checkpoint.write_bytes(b'untrusted checkpoint')
    calls = []
    fake_torch = SimpleNamespace(load=lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError, match='hash differs'):
        adapter.load_hubert(checkpoint, fake_torch)
    assert calls == []
