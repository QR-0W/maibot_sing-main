"""Exercise real source parsers/session reuse through HTTP transport fixtures, not SongInfo mocks."""
from pathlib import Path
import importlib
import importlib.util
import json
import sys

import httpx
import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('catalogue_test_pkg',ROOT/'__init__.py',submodule_search_locations=[str(ROOT)])
package=importlib.util.module_from_spec(spec);sys.modules[spec.name]=package;spec.loader.exec_module(package)
music=importlib.import_module('catalogue_test_pkg.music.search')
service=importlib.import_module('catalogue_test_pkg.services.catalogue_service')


@pytest.fixture(autouse=True)
def clean_proxy_environment(monkeypatch):
    for key in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY','http_proxy','https_proxy','all_proxy','no_proxy'):
        monkeypatch.delenv(key,raising=False)


async def setup(handler):
    client=music.MusicSearchClient()
    await client._netease_client.aclose()
    client._netease_client=httpx.AsyncClient(transport=httpx.MockTransport(handler),trust_env=False)
    await client._qq_client.aclose()
    client._qq_client=httpx.AsyncClient(transport=httpx.MockTransport(handler),trust_env=False)
    return client,service.CatalogueService(client)


def song(identifier=1,name='Creep',artist='Radiohead',album='Pablo Honey',duration=238640):
    return {'id':identifier,'name':name,'artists':[{'name':artist}],
            'album':{'name':album},'duration':duration,'fee':1}


@pytest.mark.asyncio
async def test_search_preserves_rank_and_vip_results_without_playback_probe():
    requests=[]
    def handler(request):
        requests.append(request)
        assert request.url.path=='/api/search/get/web'
        return httpx.Response(200,json={'code':200,'result':{'songs':[
            song(1),song(2,album='Creep EP'),song(3,name='Creep (Acoustic)',album='Creep EP'),
            song(4,artist='TLC',album='CrazySexyCool')]}})
    client,gateway=await setup(handler)
    try:
        items=await gateway.search('radiohead creep')
        assert [i.track_id for i in items]==['1','2','3','4']
        assert items[0].duration_s==238.64
        assert items[0].availability=='unknown'  # fee flag isn't a rights verdict.
        assert items[2].version=='acoustic' and items[0].version=='unspecified'
        assert len(requests)==1
        assert 'url' not in items[0].document()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_selected_id_resolves_on_same_session_without_research():
    requests=[]
    def handler(request):
        requests.append(request)
        if request.url.path=='/api/search/get/web':
            assert 'MUSIC_U=test-first' in request.headers['cookie']
            return httpx.Response(200,json={'code':200,'result':{'songs':[song()]}})
        assert request.url.path=='/api/song/enhance/player/url'
        assert 'MUSIC_U=test-new-login' in request.headers['cookie']
        assert [str(v) for v in json.loads(request.url.params['ids'])]==['1']
        return httpx.Response(200,json={'code':200,'data':[{'id':1,'url':'https://cdn.example/audio?token=test-private','size':9000,'freeTrialInfo':None}]})
    client,gateway=await setup(handler)
    try:
        client.apply_netease_cookies({'MUSIC_U':'test-first'})
        chosen=(await gateway.search('radiohead creep'))[0]
        client.apply_netease_cookies({'MUSIC_U':'test-new-login'})
        playback=await gateway.resolve(chosen)
        assert len(requests)==2 and playback.song_id=='1'
        assert playback.size_bytes==9000
        assert 'test-private' not in repr(playback)
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('payload,code',[
    ({'code':200,'data':[{'id':1,'url':None}]},'source_unavailable'),
    ({'code':200,'data':[{'id':1,'url':'https://cdn.example/a.mp3','freeTrialInfo':{'start':0,'end':30}}]},'source_preview'),
    ({'code':200,'data':[{'id':2,'url':'https://cdn.example/wrong.mp3'}]},'source_identity_mismatch'),
    ({'code':403},'source_access_denied'),
    ({'code':200,'data':None},'source_protocol_error'),
    ({'code':200,'data':[{'id':1,'url':'file:///private/path'}]},'source_protocol_error'),
])
async def test_access_failure_is_not_empty_search_or_substitution(payload,code):
    calls=[]
    def handler(request):
        calls.append(request.url.path)
        if request.url.path=='/api/search/get/web':
            return httpx.Response(200,json={'code':200,'result':{'songs':[song(),song(2)]}})
        return httpx.Response(200,json=payload)
    client,gateway=await setup(handler)
    try:
        items=await gateway.search('radiohead creep')
        assert len(items)==2
        with pytest.raises(service.CatalogueError) as error:
            await gateway.resolve(items[0])
        assert error.value.code==code
        assert calls==['/api/search/get/web','/api/song/enhance/player/url']
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_qq_media_id_and_session_are_preserved():
    calls=[]
    def handler(request):
        data=json.loads(request.content); calls.append(data)
        assert request.headers.get('cookie')
        if 'req_1' in data:
            return httpx.Response(200,json={'code':0,'req_1':{'code':0,'data':{'body':{'song':{'list':[
                {'mid':'mid1','name':'Creep','singer':[{'name':'Radiohead'}],
                 'album':{'name':'Pablo Honey'},'file':{'media_mid':'media1'},'interval':238}
            ]}}}}})
        assert data['req_0']['param']['songmid']==['mid1']*4
        assert any('media1' in filename for filename in data['req_0']['param']['filename'])
        first=data['req_0']['param']['filename'][0]
        return httpx.Response(200,json={'code':0,'req_0':{'code':0,'data':{
            'sip':['https://dl.stream.qqmusic.qq.com/'],'midurlinfo':[{'filename':first,'purl':'test.flac?vkey=test'}]}}})
    client,gateway=await setup(handler)
    try:
        client.apply_qq_cookies({'uin':'test-user','qqmusic_key':'test-key'})
        chosen=(await gateway.search('radiohead creep','qq'))[0]
        assert chosen.media_id=='media1' and chosen.duration_s==238
        playback=await gateway.resolve(chosen)
        assert playback.platform=='qq' and len(calls)==2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_unknown_duration_is_visible_and_known_overlimit_is_marked():
    def handler(request):
        return httpx.Response(200,json={'code':200,'result':{'songs':[song(duration=None),song(2,duration=400000)]}})
    client,gateway=await setup(handler)
    try:
        items=await gateway.search('creep')
        assert items[0].duration_s is None and items[0].availability=='unknown'
        assert items[1].availability=='over_limit'
        with pytest.raises(service.CatalogueError) as error:
            await gateway.resolve(items[1])
        assert error.value.code=='source_duration_limit'
    finally:
        await client.close()


def test_only_explicit_version_tags_are_classified():
    assert service.explicit_version('Live Forever')=='unspecified'
    assert service.explicit_version('Creep (Live at BBC)')=='live'
    assert service.explicit_version('Creep')=='unspecified'
