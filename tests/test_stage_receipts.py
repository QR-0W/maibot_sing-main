"""Real filesystem checks; never invoke ML or touch user audio."""
import importlib.util
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('stage_receipts_test',Path(__file__).resolve().parents[1]/'runtime/stage_receipts.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


def test_resume_and_idempotence(tmp_path):
    (tmp_path/'vocal.wav').write_bytes(b'validated-media-placeholder')
    store=m.StageReceipts(tmp_path,'a'*64)
    inputs={'source':'b'*64}
    assert store.verify('separate',inputs) is None
    receipt=store.seal('separate',inputs,['vocal.wav'])
    assert m.StageReceipts(tmp_path,'a'*64).verify('separate',inputs)==receipt
    assert store.seal('separate',inputs,['vocal.wav'])==receipt


@pytest.mark.parametrize('change',['bytes','truncated','recipe','input','receipt'])
def test_corruption_never_means_complete(tmp_path,change):
    output=tmp_path/'out.wav';output.write_bytes(b'abc')
    store=m.StageReceipts(tmp_path,'a'*64)
    store.seal('convert_0',{'input':'b'*64},['out.wav'])
    inputs={'input':'b'*64}
    if change=='bytes':output.write_bytes(b'xyz')
    if change=='truncated':output.write_bytes(b'a')
    if change=='recipe':store=m.StageReceipts(tmp_path,'c'*64)
    if change=='input':inputs={'input':'c'*64}
    if change=='receipt':(tmp_path/'.receipts/convert_0.json').write_text('{')
    with pytest.raises(m.CheckpointError):store.verify('convert_0',inputs)
    assert output.exists()


@pytest.mark.parametrize('name',['../outside','/absolute','a/../out.wav','out.wav.part','.receipts/x','a//b'])
def test_unsafe_paths(tmp_path,name):
    with pytest.raises(m.CheckpointError):
        m.StageReceipts(tmp_path,'a'*64).seal('stage',{},[name])


def test_unsealed_partial_does_not_resume(tmp_path):
    (tmp_path/'out.wav.part').write_bytes(b'partial')
    assert m.StageReceipts(tmp_path,'a'*64).verify('convert',{}) is None


def test_explicit_retry_archives_only_unsealed_outputs(tmp_path):
    store=m.StageReceipts(tmp_path,'a'*64)
    (tmp_path/'first.wav').write_bytes(b'completed first chunk')
    store.seal('convert_000',{'input':'b'*64},['first.wav'])
    (tmp_path/'failed.wav').write_bytes(b'partial second chunk')
    unit='maibot-sing-'+('a'*32)
    with pytest.raises(m.CheckpointError,match='not been proven'):
        store.archive_incomplete('convert_001',['failed.wav'],unit_name=unit,confirmed_stopped=False)
    assert (tmp_path/'failed.wav').exists()
    location=store.archive_incomplete('convert_001',['failed.wav'],unit_name=unit,confirmed_stopped=True)
    assert location is not None and (location/'failed.wav').read_bytes()==b'partial second chunk'
    assert not (tmp_path/'failed.wav').exists()
    assert store.verify('convert_000',{'input':'b'*64}) is not None
    (tmp_path/'failed.wav').write_bytes(b'finished second chunk')
    store.seal('convert_001',{'input':'c'*64},['failed.wav'])
    assert store.verify('convert_001',{'input':'c'*64}) is not None
    with pytest.raises(m.CheckpointError,match='receipt'):
        store.archive_incomplete('convert_001',['failed.wav'],unit_name=unit,confirmed_stopped=True)
    assert (location/'failed.wav').read_bytes()==b'partial second chunk'


def test_symlinks_and_budget(tmp_path):
    (tmp_path/'data').write_bytes(b'1234')
    (tmp_path/'link').symlink_to(tmp_path/'data')
    store=m.StageReceipts(tmp_path,'a'*64,max_bytes=3)
    for name in ['data','link']:
        with pytest.raises(m.CheckpointError):store.seal('stage',{},[name])
    assert not (tmp_path/'.receipts/stage.json').exists()
