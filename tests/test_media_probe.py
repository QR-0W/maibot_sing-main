"""Probe plans from streamed decoded frames, never container padding."""
from pathlib import Path
import asyncio
import importlib
import importlib.util
import json
import sys

import pytest

root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('probe_test_pkg',root/'__init__.py',submodule_search_locations=[str(root)])
pkg=importlib.util.module_from_spec(spec);sys.modules[spec.name]=pkg;spec.loader.exec_module(pkg)
module=importlib.import_module('probe_test_pkg.services.media_probe')
item=importlib.import_module('probe_test_pkg.services.source_offer').CatalogueItem
errors=importlib.import_module('probe_test_pkg.services.catalogue_service')


class FakeStream:
    def __init__(self,data=b'',total=None,gate=None,started=None):
        self.data=data
        self.offset=0
        self.total=total
        self.gate=gate
        self.started=started

    async def read(self,amount):
        if self.started is not None:
            self.started.set()
        if self.gate is not None:
            await self.gate.wait()
            return b''
        if self.total is not None:
            if self.total<=0:return b''
            size=min(amount,self.total);self.total-=size
            return b'\0'*size
        if self.offset>=len(self.data):return b''
        block=self.data[self.offset:self.offset+amount];self.offset+=len(block)
        return block


class FakeProcess:
    def __init__(self,stdout,stderr=b'',code=0,block_wait=False,started=None):
        self.stdout=stdout if hasattr(stdout,'read') else FakeStream(stdout)
        self.stderr=stderr if hasattr(stderr,'read') else FakeStream(stderr)
        self.returncode=None
        self.code=code
        self.block_wait=block_wait
        self.exit=asyncio.Event()
        self.killed=False
        self.waited=False
        self.started=started

    async def wait(self):
        self.waited=True
        if self.started is not None:self.started.set()
        if self.block_wait:await self.exit.wait()
        self.returncode=-9 if self.killed else self.code
        return self.returncode

    def kill(self):
        self.killed=True
        self.returncode=-9
        self.exit.set()
        gate=getattr(self.stdout,'gate',None)
        if gate is not None:gate.set()
        gate=getattr(self.stderr,'gate',None)
        if gate is not None:gate.set()


def chosen(duration=238.64):
    return item('163','22558968','Creep','Radiohead','The Best Of',duration)


def metadata(duration=238.64,audio=True):
    return json.dumps({'format':{'duration':duration},
        'streams':[{'codec_type':'audio'}] if audio else []}).encode()


def install(monkeypatch,*processes):
    calls=[]
    async def create(*argv,**kwargs):
        calls.append((argv,kwargs))
        assert processes
        return processes[len(calls)-1]
    monkeypatch.setattr(module.asyncio,'create_subprocess_exec',create)
    return calls


def normal_processes(container_duration,frames):
    return (FakeProcess(metadata(container_duration)),
            FakeProcess(FakeStream(total=frames*8)))


@pytest.mark.asyncio
@pytest.mark.parametrize('container_duration,frames,expected_error',[
    (999.,round(238.64*44100),None),
    (30.,30*44100,'source_duration_mismatch'),
    (301.,300*44100+1,'source_duration_limit'),
])
async def test_duration_checks_use_decoded_frames_not_container(
        tmp_path,monkeypatch,container_duration,frames,expected_error):
    src=tmp_path/'source.audio';src.write_bytes(b'synthetic stub media')
    calls=install(monkeypatch,*normal_processes(container_duration,frames))
    if expected_error:
        with pytest.raises(errors.CatalogueError) as exc:
            await module.probe_download(src,chosen())
        assert exc.value.code==expected_error
    else:
        report=await module.probe_download(src,chosen())
        assert report['frames']==frames and report['sample_rate']==44100
        assert report['duration_s']==frames/44100
        assert report['container_duration_s']==999.
        assert report['source_id']=='22558968' and report['audio_streams']==1
        assert 'studio' not in str(report)
    assert calls[0][0][0]=='ffprobe'
    ffmpeg=calls[1][0]
    assert ffmpeg[0]=='ffmpeg' and ('-map','0:a:0')==ffmpeg[ffmpeg.index('-map'):ffmpeg.index('-map')+2]
    assert ('-ar','44100')==ffmpeg[ffmpeg.index('-ar'):ffmpeg.index('-ar')+2]
    assert ('-ac','2')==ffmpeg[ffmpeg.index('-ac'):ffmpeg.index('-ac')+2]
    assert ('-c:a','pcm_f32le')==ffmpeg[ffmpeg.index('-c:a'):ffmpeg.index('-c:a')+2]
    assert '-threads' in ffmpeg and '-max_alloc' in ffmpeg and 'pipe:1' in ffmpeg


@pytest.mark.asyncio
@pytest.mark.parametrize('requested,container_duration',[(44.99,45.035102),(300.,300.042449)])
async def test_padding_boundaries_return_exact_normalized_frames(
        tmp_path,monkeypatch,requested,container_duration):
    src=tmp_path/'source.audio';src.write_bytes(b'synthetic mp3 fixture')
    frames=round(requested*44100)
    install(monkeypatch,*normal_processes(container_duration,frames))
    report=await module.probe_download(src,chosen(requested),max_seconds=300)
    assert report['frames']==frames
    assert report['duration_s']==requested
    assert report['container_duration_s']==container_duration


@pytest.mark.asyncio
async def test_missing_audio_track_rejected_before_decode(tmp_path,monkeypatch):
    src=tmp_path/'source.audio';src.write_bytes(b'not audio')
    calls=install(monkeypatch,FakeProcess(metadata(238.,audio=False)))
    with pytest.raises(errors.CatalogueError) as exc:
        await module.probe_download(src,chosen())
    assert exc.value.code=='source_duration_limit'
    assert len(calls)==1


@pytest.mark.asyncio
async def test_non_frame_aligned_pcm_is_rejected(tmp_path,monkeypatch):
    src=tmp_path/'source.audio';src.write_bytes(b'bad decoded shape')
    install(monkeypatch,FakeProcess(metadata(31.)),FakeProcess(FakeStream(total=31*44100*8-1)))
    with pytest.raises(errors.CatalogueError) as exc:
        await module.probe_download(src,chosen(31.))
    assert exc.value.code=='source_invalid_media'


@pytest.mark.asyncio
async def test_metadata_output_limit_kills_and_reaps_owned_process(tmp_path,monkeypatch):
    src=tmp_path/'source.audio';src.write_bytes(b'hostile metadata')
    process=FakeProcess(b'x'*(module._STDOUT_LIMIT+1),block_wait=True)
    install(monkeypatch,process)
    with pytest.raises(errors.CatalogueError) as exc:
        await module.probe_download(src,chosen())
    assert exc.value.code=='source_invalid_media'
    assert process.killed and process.waited and process.returncode is not None


@pytest.mark.asyncio
@pytest.mark.parametrize('stage',['ffprobe','ffmpeg'])
async def test_cancellation_kills_and_reaps_only_owned_child(tmp_path,monkeypatch,stage):
    src=tmp_path/'source.audio';src.write_bytes(b'blocked media')
    started=asyncio.Event();gate=asyncio.Event()
    blocked=FakeProcess(FakeStream(gate=gate,started=started),
                        stderr=FakeStream(gate=gate),block_wait=True,started=started)
    if stage=='ffprobe':
        processes=(blocked,)
    else:
        processes=(FakeProcess(metadata(31.)),blocked)
    install(monkeypatch,*processes)
    task=asyncio.create_task(module.probe_download(src,chosen(31.)))
    await asyncio.wait_for(started.wait(),1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task,1)
    assert blocked.killed and blocked.waited and blocked.returncode is not None


@pytest.mark.asyncio
async def test_cancellation_wait_is_bounded_while_killed_child_reaps_late(tmp_path,monkeypatch):
    src=tmp_path/'source.audio';src.write_bytes(b'blocked media')
    started=asyncio.Event();gate=asyncio.Event()
    class SlowReap(FakeProcess):
        def kill(self):
            self.killed=True  # Simulate delayed child-watcher acknowledgement.
    process=SlowReap(FakeStream(gate=gate,started=started),
                     stderr=FakeStream(gate=gate),block_wait=True,started=started)
    install(monkeypatch,process)
    monkeypatch.setattr(module,'_PROCESS_CLEANUP_S',.02)
    task=asyncio.create_task(module.probe_download(src,chosen(31.)))
    await asyncio.wait_for(started.wait(),1)
    before=asyncio.get_running_loop().time();task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task,.2)
    assert asyncio.get_running_loop().time()-before<.15 and process.killed
    process.exit.set()
    for _ in range(100):
        if process.returncode is not None:break
        await asyncio.sleep(.005)
    assert process.returncode is not None
