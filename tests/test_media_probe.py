"""Source duration checks never assert studio identity or successful QQ delivery."""
from pathlib import Path
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


class FakeProbe:
    def __init__(self,data,code=0):self.raw=data;self.returncode=code
    async def communicate(self):return self.raw,b''


def chosen(duration=238.64):
    return item('163','22558968','Creep','Radiohead','The Best Of',duration)


@pytest.mark.asyncio
@pytest.mark.parametrize('duration,expected_error',[
    (238.64,None),(30.,'source_duration_mismatch'),(301.,'source_duration_limit'),
    (float('nan'),'source_duration_limit')])
async def test_download_media_duration_is_not_recording_identity(tmp_path,monkeypatch,duration,expected_error):
    src=tmp_path/'source.audio';src.write_bytes(b'synthetic stub media')
    async def probe(*args,**kwargs):
        assert args[-1]==str(src)
        return FakeProbe(json.dumps({'format':{'duration':duration},'streams':[{'codec_type':'audio'}]}).encode())
    monkeypatch.setattr(module.asyncio,'create_subprocess_exec',probe)
    if expected_error:
        with pytest.raises(errors.CatalogueError) as exc:
            await module.probe_download(src,chosen())
        assert exc.value.code==expected_error
    else:
        report=await module.probe_download(src,chosen())
        assert report['source_id']=='22558968' and report['audio_streams']==1
        assert 'studio' not in str(report)


@pytest.mark.asyncio
async def test_missing_audio_track_rejected(tmp_path,monkeypatch):
    src=tmp_path/'source.audio';src.write_bytes(b'not audio')
    async def probe(*args,**kwargs):
        return FakeProbe(b'{"format":{"duration":"238"},"streams":[]}')
    monkeypatch.setattr(module.asyncio,'create_subprocess_exec',probe)
    with pytest.raises(errors.CatalogueError) as exc:
        await module.probe_download(src,chosen())
    assert exc.value.code=='source_duration_limit'
