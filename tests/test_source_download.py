"""Mock provider transport: exact-ID streaming and hostile redirect handling."""
from pathlib import Path
import asyncio
import hashlib
import importlib
import importlib.util
import sys
import threading

import httpx
import pytest
import pytest_asyncio

root=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('download_test_pkg',root/'__init__.py',submodule_search_locations=[str(root)])
package=importlib.util.module_from_spec(spec);sys.modules[spec.name]=package;spec.loader.exec_module(package)
source=importlib.import_module('download_test_pkg.services.source_download')
service=importlib.import_module('download_test_pkg.services.catalogue_service')
music=importlib.import_module('download_test_pkg.music.search')
Item=importlib.import_module('download_test_pkg.services.source_offer').CatalogueItem


@pytest.fixture(autouse=True)
def clear_proxy_environment(monkeypatch):
    for key in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY',
                'http_proxy','https_proxy','all_proxy','no_proxy'):
        monkeypatch.delenv(key,raising=False)


@pytest_asyncio.fixture
async def gateway():
    client=music.MusicSearchClient()
    await client._netease_client.aclose()
    yield client,service.CatalogueService(client)
    await client.close()


def chosen():
    return Item('163','1','Creep','Radiohead','Pablo Honey',238.64)


def mock(client,handler):
    client._netease_client=httpx.AsyncClient(transport=httpx.MockTransport(handler),trust_env=False)


@pytest.mark.asyncio
async def test_exact_id_and_stream_saves_hash_without_url_in_metadata(tmp_path,gateway):
    client,catalogue=gateway
    calls=[]
    def handler(request):
        calls.append((request.method,str(request.url)))
        if request.method=='GET' and request.url.path.endswith('/url'):
            return httpx.Response(200,json={'code':200,'data':[{'id':1,'url':'https://cdn.music.126.net/track.mp3?sig=private','size':2048}]})
        assert request.url.host=='cdn.music.126.net' and request.method=='GET'
        return httpx.Response(200,content=b'A'*2048)
    mock(client,handler)
    path,digest,amount=await source.download_selected(catalogue,chosen(),tmp_path)
    assert path.name=='source.audio' and path.read_bytes()==b'A'*2048
    assert amount==2048 and digest==hashlib.sha256(b'A'*2048).hexdigest()
    assert len(calls)==2 and all('/api/search/' not in url for _,url in calls)
    assert not list(tmp_path.glob('*.part'))


@pytest.mark.asyncio
@pytest.mark.parametrize('bad',[
 'file:///etc/passwd','http://cdn.music.126.net/a','https://cdn.music.126.net.evil.example/a',
 'https://localhost/a','https://cdn.music.126.net:8080/a','https://user:password@cdn.music.126.net/a'])
async def test_bad_source_url_never_downloaded(tmp_path,gateway,bad):
    client,catalogue=gateway
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json={'code':200,'data':[{'id':1,'url':bad}]})
    mock(client,handler)
    with pytest.raises((source.DownloadError, service.CatalogueError)) as exc:
        await source.download_selected(catalogue,chosen(),tmp_path)
    assert exc.value.code in ('source_url_rejected','source_protocol_error') and len(calls)==1
    assert not (tmp_path/'source.audio').exists()
    assert not list(tmp_path.glob('*.part'))


@pytest.mark.asyncio
async def test_redirect_to_other_host_is_blocked_without_request(tmp_path,gateway):
    client,catalogue=gateway; calls=[]
    def handler(request):
        calls.append(request.url.host)
        if request.url.path.endswith('/url'):
            return httpx.Response(200,json={'code':200,'data':[{'id':1,'url':'https://cdn.music.126.net/a'}]})
        return httpx.Response(302,headers={'Location':'https://evil.example/audio'})
    mock(client,handler)
    with pytest.raises(source.DownloadError,match='outside'):
        await source.download_selected(catalogue,chosen(),tmp_path)
    assert calls==['music.163.com','cdn.music.126.net']
    assert not (tmp_path/'source.audio').exists()


@pytest.mark.asyncio
async def test_real_stream_bytes_limit_keeps_part_and_no_completed_file(tmp_path,gateway):
    client,catalogue=gateway
    def handler(request):
        if request.url.path.endswith('/url'):
            return httpx.Response(200,json={'code':200,'data':[{'id':1,'url':'https://cdn.music.126.net/a'}]})
        # Deliberately declare less than the bytes actually delivered.
        return httpx.Response(200,content=b'A'*2048,headers={'Content-Length':'1024'})
    mock(client,handler)
    with pytest.raises(source.DownloadError,match='byte limit'):
        await source.download_selected(catalogue,chosen(),tmp_path,max_bytes=1024)
    assert not (tmp_path/'source.audio').exists()
    assert list(tmp_path.glob('*.part'))


@pytest.mark.asyncio
async def test_known_preview_is_rejected_before_get(tmp_path,gateway):
    client,catalogue=gateway; calls=[]
    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(200,json={'code':200,'data':[{'id':1,'url':'https://cdn.music.126.net/a',
                                                            'freeTrialInfo':{'start':0,'end':30}}]})
    mock(client,handler)
    with pytest.raises(service.CatalogueError) as exc:
        await source.download_selected(catalogue,chosen(),tmp_path)
    assert exc.value.code=='source_preview' and calls==['music.163.com']
    assert not list(tmp_path.glob('*.part'))


async def wait_thread_flag(flag):
    for _ in range(200):
        if flag.is_set():
            return
        await asyncio.sleep(.005)
    raise AssertionError('background disk operation did not start')


def mock_success(client,content=b'A'*2048):
    def handler(request):
        if request.url.path.endswith('/url'):
            return httpx.Response(200,json={'code':200,'data':[{
                'id':1,'url':'https://cdn.music.126.net/track.mp3?sig=private',
                'size':len(content)}]})
        return httpx.Response(200,content=content)
    mock(client,handler)


@pytest.mark.asyncio
async def test_blocked_disk_write_does_not_block_event_loop(tmp_path,gateway,monkeypatch):
    client,catalogue=gateway
    mock_success(client)
    entered,release=threading.Event(),threading.Event()
    original=source._append_block
    def blocked(*args):
        entered.set()
        if not release.wait(2):
            raise AssertionError('test did not release disk write')
        return original(*args)
    monkeypatch.setattr(source,'_append_block',blocked)
    task=asyncio.create_task(source.download_selected(catalogue,chosen(),tmp_path))
    try:
        await wait_thread_flag(entered)
        ticks=0
        async def heartbeat():
            nonlocal ticks
            for _ in range(10):
                await asyncio.sleep(.005)
                ticks+=1
        await heartbeat()
        assert ticks==10 and not task.done()
    finally:
        release.set()
    path,digest,amount=await task
    assert path.read_bytes()==b'A'*2048 and amount==2048
    assert digest==hashlib.sha256(b'A'*2048).hexdigest()


@pytest.mark.asyncio
async def test_cancelled_default_offload_observes_late_error():
    entered,release=threading.Event(),threading.Event()
    def broken():
        entered.set()
        if not release.wait(2):
            raise AssertionError('test did not release broken offload')
        raise RuntimeError('late disk failure')
    task=asyncio.create_task(source._offload_io(None,broken))
    await wait_thread_flag(entered)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError) as exc:
        await task
    assert isinstance(exc.value.__cause__,RuntimeError)
    assert str(exc.value.__cause__)=='late disk failure'


@pytest.mark.asyncio
async def test_cancelled_blocked_write_finishes_only_private_part(tmp_path,gateway,monkeypatch):
    client,catalogue=gateway
    mock_success(client)
    entered,release=threading.Event(),threading.Event()
    original=source._append_block
    def blocked(*args):
        entered.set()
        if not release.wait(2):
            raise AssertionError('test did not release disk write')
        return original(*args)
    monkeypatch.setattr(source,'_append_block',blocked)
    task=asyncio.create_task(source.download_selected(catalogue,chosen(),tmp_path))
    await wait_thread_flag(entered)
    task.cancel()
    await asyncio.sleep(.02)
    assert not task.done() and not (tmp_path/'source.audio').exists()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    parts=list(tmp_path.glob('source-*.part'))
    assert len(parts)==1 and parts[0].read_bytes()==b'A'*2048
    assert not (tmp_path/'source.audio').exists()


@pytest.mark.asyncio
async def test_cancelled_publish_never_leaves_completed_target(tmp_path,gateway,monkeypatch):
    client,catalogue=gateway
    mock_success(client)
    entered,release=threading.Event(),threading.Event()
    original=source._publish_part
    def blocked(*args):
        entered.set()
        if not release.wait(2):
            raise AssertionError('test did not release publication')
        return original(*args)
    monkeypatch.setattr(source,'_publish_part',blocked)
    task=asyncio.create_task(source.download_selected(catalogue,chosen(),tmp_path))
    await wait_thread_flag(entered)
    task.cancel()
    await asyncio.sleep(.02)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (tmp_path/'source.audio').exists()
    assert len(list(tmp_path.glob('source-*.part')))==1


@pytest.mark.asyncio
async def test_exclusive_publish_never_overwrites_racing_source(tmp_path,gateway,monkeypatch):
    client,catalogue=gateway
    mock_success(client)
    original=source._publish_part
    def raced(temporary,target,workspace,cancelled):
        target.write_bytes(b'other scheduler source')
        return original(temporary,target,workspace,cancelled)
    monkeypatch.setattr(source,'_publish_part',raced)
    with pytest.raises(source.DownloadError) as exc:
        await source.download_selected(catalogue,chosen(),tmp_path)
    assert exc.value.code=='source_exists'
    assert (tmp_path/'source.audio').read_bytes()==b'other scheduler source'
    assert len(list(tmp_path.glob('source-*.part')))==1


@pytest.mark.asyncio
async def test_injected_offload_owns_every_file_operation(tmp_path,gateway):
    client,catalogue=gateway
    mock_success(client)
    calls=[]
    async def offload(function,*args,**kwargs):
        calls.append(function.__name__)
        return await asyncio.to_thread(function,*args,**kwargs)
    await source.download_selected(catalogue,chosen(),tmp_path,offload=offload)
    assert calls[0]=='_prepare_destination'
    assert '_append_block' in calls and calls[-1]=='_publish_part'
