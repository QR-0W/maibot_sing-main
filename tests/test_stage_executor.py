"""Real local child processes only; no systemd, ML, network or QQ."""
from pathlib import Path
import importlib.util
import json
import sys

import pytest

runtime = Path(__file__).resolve().parents[1] / 'runtime'
sys.path.insert(0, str(runtime))
spec = importlib.util.spec_from_file_location('isolated_stage_executor', runtime/'stage_executor.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def execute(tmp_path, argv, *, inputs=('input.bin',), outputs=('result.bin',),
            stage='convert_000', timeout_s=3):
    return module.execute(tmp_path, 'a'*64, stage, argv, inputs, outputs,
                          timeout_s=timeout_s, lock_path=tmp_path/'shared.lock')


def test_real_child_and_resume_without_repeat(tmp_path):
    (tmp_path/'input.bin').write_bytes(b'frozen')
    program = "from pathlib import Path; p=Path('count'); p.write_text(p.read_text()+'x' if p.exists() else 'x'); Path('result.bin').write_bytes(b'converted')"
    argv = [sys.executable, '-c', program]
    first = execute(tmp_path, argv)
    assert first['state'] == 'completed' and first['reused'] is False
    assert execute(tmp_path, argv)['reused'] is True
    assert (tmp_path/'count').read_text() == 'x'
    receipt = json.loads((tmp_path/'.receipts/convert_000.json').read_text())
    assert receipt['inputs']['input.bin'] and receipt['outputs']['result.bin']['bytes']==9
    assert json.loads((tmp_path/'.receipts/convert_000.status.json').read_text())['state']=='completed'


def test_modified_input_and_output_block_resume(tmp_path):
    (tmp_path/'input.bin').write_bytes(b'original')
    argv = [sys.executable, '-c', "open('result.bin','wb').write(b'ok')"]
    execute(tmp_path, argv)
    (tmp_path/'input.bin').write_bytes(b'modified')
    with pytest.raises(module.CheckpointError): execute(tmp_path, argv)
    assert (tmp_path/'result.bin').read_bytes()==b'ok'
    (tmp_path/'input.bin').write_bytes(b'original')
    (tmp_path/'result.bin').write_bytes(b'changed')
    with pytest.raises(module.CheckpointError): execute(tmp_path, argv)
    assert (tmp_path/'result.bin').read_bytes()==b'changed'


def test_failure_retains_output_and_structured_state(tmp_path):
    (tmp_path/'input.bin').write_bytes(b'frozen')
    program="from pathlib import Path; Path('result.bin').write_bytes(b'partial'); raise SystemExit(9)"
    with pytest.raises(module.StageFailure) as exc:
        execute(tmp_path, [sys.executable, '-c', program])
    assert exc.value.code=='stage_exit'
    assert (tmp_path/'result.bin').read_bytes()==b'partial'
    assert not (tmp_path/'.receipts/convert_000.json').exists()
    status=json.loads((tmp_path/'.receipts/convert_000.status.json').read_text())
    assert status['state']=='failed' and status['code']=='stage_exit'
    with pytest.raises(module.StageFailure,match='Existing unsealed output'):
        execute(tmp_path, [sys.executable, '-c', program])


def test_timeout_terminates_and_does_not_seal(tmp_path):
    (tmp_path/'input.bin').write_bytes(b'frozen')
    argv=[sys.executable,'-c',"import time; time.sleep(20)"]
    with pytest.raises(module.StageFailure) as exc:
        execute(tmp_path, argv, timeout_s=1)
    assert exc.value.code=='stage_timeout'
    assert not (tmp_path/'.receipts/convert_000.json').exists()
    status=json.loads((tmp_path/'.receipts/convert_000.status.json').read_text())
    assert status['state']=='failed' and status['code']=='stage_timeout'


def test_unknown_output_contract_is_not_reused(tmp_path):
    (tmp_path/'input.bin').write_bytes(b'frozen')
    execute(tmp_path,[sys.executable,'-c',"open('result.bin','wb').write(b'x')"])
    with pytest.raises(module.CheckpointError,match='output contract'):
        execute(tmp_path,[sys.executable,'-c',"open('other.bin','wb').write(b'x')"],outputs=('other.bin',))
    assert not (tmp_path/'other.bin').exists()
