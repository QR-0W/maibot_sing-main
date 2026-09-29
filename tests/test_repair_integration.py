"""Cross-process error, selector, and catalog integration; no real services or sends."""
from pathlib import Path
import json
import os

import pytest

from test_local_backend import backend, fake_runtime_paths
from test_plugin_async_lifecycle import plugin, plugin_module, command


@pytest.mark.asyncio
async def test_worker_ambiguity_reaches_parent_before_cleanup(tmp_path, monkeypatch):
    model, index = tmp_path/'model', tmp_path/'index'
    model.write_bytes(b'model'); index.write_bytes(b'index')
    manager = backend.LocalBackend(tmp_path/'library', model=model, index=index, **fake_runtime_paths(tmp_path))
    class Process:
        returncode = 1
        async def communicate(self):
            return b'', b''
    async def run(*args, **kwargs):
        scratch = Path(args[args.index('--scratch')+1])
        (scratch/'error.json').write_text(json.dumps({'version':1,'code':'source_ambiguous',
            'message':'找到多个同名发行，尚未开始翻唱与发送。',
            'candidates':[{'source':'NeteaseMusicClient','identifier':'1','album':'Album A','duration_s':237.9}]}))
        return Process()
    async def stop(unit):
        manager._units.discard(unit)
    monkeypatch.setattr(backend.asyncio, 'create_subprocess_exec', run)
    monkeypatch.setattr(manager, '_stop_unit', stop)
    try:
        with pytest.raises(backend.CoverStageError) as failure:
            await manager.cover('Creep','Radiohead')
        assert failure.value.code == 'source_ambiguous'
        assert failure.value.candidates[0]['identifier'] == '1'
        assert not list((manager.output/'.scratch').iterdir())
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_explicit_choices_have_separate_cache_and_readable_names(tmp_path, monkeypatch):
    model, index = tmp_path/'model', tmp_path/'index'
    model.write_bytes(b'model'); index.write_bytes(b'index')
    manager = backend.LocalBackend(tmp_path/'library', model=model, index=index, **fake_runtime_paths(tmp_path))
    calls=[]
    class Process:
        returncode=0
        async def communicate(self):
            return b'',b''
    async def run(*args, **kwargs):
        scratch=Path(args[args.index('--scratch')+1])
        album=args[args.index('--album')+1]
        identifier=args[args.index('--source-id')+1]
        calls.append((album,identifier))
        (scratch/'cover.mp3').write_bytes(b'x'*2048)
        (scratch/'result.json').write_text(json.dumps({'verified_source':{'source':'NeteaseMusicClient',
            'identifier':identifier,'title':'Creep','artist':'Radiohead','album':album}}))
        return Process()
    monkeypatch.setattr(backend.asyncio,'create_subprocess_exec',run)
    try:
        one=await manager.cover('Creep','Radiohead',album='Album A',source_id='1')
        two=await manager.cover('Creep','Radiohead',album='Album B',source_id='2')
        repeat=await manager.cover('Creep','Radiohead',album='Album B',source_id='2')
        assert one.key != two.key and repeat.key == two.key
        assert calls == [('Album A','1'),('Album B','2')]
        assert one.public_path.is_file() and two.public_path.is_file()
        assert 'Creep' in one.public_path.name and 'Radiohead' in one.public_path.name
        assert len(json.loads((manager.output/'library-index.json').read_text())['entries']) == 2
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_old_duration_cluster_is_never_reused_or_overwritten(tmp_path, monkeypatch):
    model, index = tmp_path/'model', tmp_path/'index'
    model.write_bytes(b'model'); index.write_bytes(b'index')
    manager = backend.LocalBackend(tmp_path/'library', model=model, index=index,
                                   **fake_runtime_paths(tmp_path))
    source={'type':'musicdl-native','platform':'NeteaseMusicClient','identifier':'2',
            'title':'Creep','artist':'Radiohead','album':'40 Jaar Pinkpop'}
    old_key=backend.cache_key(source,backend.digest_file(model),backend.digest_file(index),False)
    old=manager.output/old_key;old.mkdir(parents=True)
    (old/'cover.mp3').write_bytes(b'old-unsafe-selection'*100)
    backend.atomic_json(old/'metadata.json',{'status':'completed','key':old_key,'source':source,
       'model_sha256':backend.digest_file(model),'index_sha256':backend.digest_file(index),
       'parameters':backend.PARAMETERS,'instrumental':False,'title':'Creep','artist':'Radiohead',
       'release_selection':{'tolerance_s':4.0,'reason':'studio claim'},
       'sha256':backend.digest_file(old/'cover.mp3')})
    class Process:
        returncode=0
        async def communicate(self): return b'',b''
    async def worker(*args,**kwargs):
        scratch=Path(args[args.index('--scratch')+1])
        (scratch/'cover.mp3').write_bytes(b'new-explicit-selection'*100)
        (scratch/'result.json').write_text(json.dumps({'verified_source':{
            'source':'NeteaseMusicClient','identifier':'2','title':'Creep','artist':'Radiohead',
            'album':'40 Jaar Pinkpop','selection':{'policy':'explicit-source-v1'}}}))
        return Process()
    monkeypatch.setattr(backend.asyncio,'create_subprocess_exec',worker)
    try:
        result=await manager.cover('Creep','Radiohead',album='40 Jaar Pinkpop',source_id='2')
        assert result.key!=old_key and result.path.read_bytes()==b'new-explicit-selection'*100
        assert (old/'cover.mp3').read_bytes()==b'old-unsafe-selection'*100
        assert os.path.samefile(result.path, result.public_path)
        assert result.album=='40 Jaar Pinkpop' and result.source_id=='2'
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_missing_external_runtime_fails_without_starting_worker(tmp_path):
    model, index=tmp_path/'model',tmp_path/'index'
    model.write_bytes(b'model');index.write_bytes(b'index')
    manager=backend.LocalBackend(tmp_path/'library',model=model,index=index)
    try:
        with pytest.raises(RuntimeError,match='请配置绝对路径'):
            await manager.cover('Song','Artist')
        assert not (manager.output/'.scratch').exists()
        assert not manager._units
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_command_reports_ambiguous_choices_without_sending(plugin):
    instance, sends = plugin
    await instance.on_load()
    try:
        ok, result, _ = await instance.handle_cover_command(**command())
        assert ok and '待选择任务' in result
        assert 'Album' in sends[-1][0] and 'ID id-1' in sends[-1][0]
        assert '请选择明确版本' in sends[-1][0]
        assert '发不出去' not in sends[-1][0]
        job_id = result.split()[-1]
        saved = instance._jobs.store.get(job_id, 'stream-1')
        assert saved.state == 'needs_selection' and saved.delivery_state == 'not_requested'
        tool = await instance.handle_cover_tool('Song - Artist', stream_id='forged', auto_reply=True)
        assert '未入队' in tool['content'] and '--auto-reply' in tool['content']
        assert instance._jobs.store.get(job_id, 'stream-1').state == 'needs_selection'
    finally:
        await instance.on_unload()


def test_cover_command_parses_explicit_album_and_id():
    import ast,re
    path=Path(plugin_module.__file__)
    tree=ast.parse(path.read_text())
    method=next(n for n in ast.walk(tree) if isinstance(n,ast.AsyncFunctionDef) and n.name=='handle_cover_command')
    pattern=next(k.value.value for d in method.decorator_list for k in d.keywords if k.arg=='pattern')
    match=re.fullmatch(pattern,'/翻唱 Creep - Radiohead --album The Best Of --source-id 22558968')
    assert match['query']=='Creep - Radiohead'
    assert match['album']=='The Best Of' and match['source_id']=='22558968'
