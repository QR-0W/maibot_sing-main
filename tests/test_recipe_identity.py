"""Ensure cache identities actually move with media-relevant inputs."""
from dataclasses import replace
from pathlib import Path
import copy
import importlib.util
import sys

import pytest

root=Path(__file__).resolve().parents[1]/'runtime'
for name in ('render_plan','recipe_identity'):
    spec=importlib.util.spec_from_file_location(name,root/(name+'.py'))
    module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
plan=sys.modules['render_plan']; recipe=sys.modules['recipe_identity']


def document():
    hashes={name:format(i+1,'064x') for i,name in enumerate(recipe.HASH_NAMES)}
    versions={name:'1.2.3' for name in recipe.VERSION_NAMES}
    args=dict(workspace='/workspace',worker_python='/python',worker_script='/media_stage.py',
        rvc_script='/rvc.py',model='/model.pth',index='/index',hubert='/hubert.pt')
    steps=plan.build_plan(**args,frames=238*44100)
    return recipe.recipe_document(provider='163',track_id='22558968',hashes=hashes,
                                   versions=versions,steps=steps,**args),args,steps


def test_identity_has_no_private_path_and_is_stable():
    first,args,steps=document()
    relocated={**args,'workspace':'/another-private-workspace','worker_python':'/other-python',
               'rvc_script':'/elsewhere-rvc','model':'/private-model','index':'/different-index',
               'hubert':'/different-hubert'}
    second=recipe.recipe_document(provider='163',track_id='22558968',
        hashes=first['hashes'],versions=first['versions'],
        steps=plan.build_plan(**relocated,frames=238*44100),**relocated)
    assert recipe.fingerprint(first)==recipe.fingerprint(second)
    assert '/workspace' not in str(first) and '/hubert.pt' not in str(first)
    assert len([s for s in first['steps'] if s['name'].startswith('convert_')])==12


@pytest.mark.parametrize('field',[('hashes','model'),('hashes','hubert'),('hashes','demucs_weights'),
    ('hashes','source'),('hashes','rvc_upstream'),('hashes','worker'),('versions','pyworld'),
    ('versions','ffmpeg')])
def test_every_asset_change_invalidates_existing_cache(field):
    doc,_,_=document()
    other=copy.deepcopy(doc)
    other[field[0]][field[1]]='e'*64 if field[0]=='hashes' else '9.9.9'
    assert recipe.fingerprint(doc)!=recipe.fingerprint(other)


def test_pitch_chunk_alignment_and_hubert_path_are_explicit():
    doc,args,steps=document()
    chunk=next(i for i,s in enumerate(steps) if s.name.startswith('convert_'))
    original=steps[chunk]
    argv=list(original.argv);argv[argv.index('--pitch')+1]='1'
    changed=list(steps);changed[chunk]=replace(original,argv=tuple(argv))
    newer=recipe.recipe_document(provider='163',track_id='22558968',hashes=doc['hashes'],
                                 versions=doc['versions'],steps=changed,**args)
    assert recipe.fingerprint(newer)!=recipe.fingerprint(doc)
    assert doc['steps'][chunk]['argv'][doc['steps'][chunk]['argv'].index('--hubert')+1]=='${HUBERT}'


def test_missing_assets_and_url_are_rejected():
    doc,args,steps=document()
    missing=dict(doc['hashes']);missing.pop('hubert')
    with pytest.raises(recipe.RecipeError):
        recipe.recipe_document(provider='163',track_id='1',hashes=missing,
                               versions=doc['versions'],steps=steps,**args)
    malformed=list(steps);malformed[0]=replace(steps[0],argv=('https://evil.example/audio',))
    with pytest.raises(recipe.RecipeError):
        recipe.recipe_document(provider='163',track_id='1',hashes=doc['hashes'],
                               versions=doc['versions'],steps=malformed,**args)
