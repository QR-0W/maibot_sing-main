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
        rvc_script='/rvc.py',model='/model.pth',index='/index',hubert='/hubert.pt',
        demucs_repo='/private/htdemucs-repo')
    steps=plan.build_plan(**args,frames=238*44100)
    return recipe.recipe_document(provider='163',track_id='22558968',hashes=hashes,
                                   versions=versions,steps=steps,**args),args,steps


def test_identity_has_no_private_path_and_is_stable():
    first,args,steps=document()
    relocated={**args,'workspace':'/another-private-workspace','worker_python':'/other-python',
               'rvc_script':'/elsewhere-rvc','model':'/private-model','index':'/different-index',
               'hubert':'/different-hubert','demucs_repo':'/different-local-repo'}
    second=recipe.recipe_document(provider='163',track_id='22558968',
        hashes=first['hashes'],versions=first['versions'],
        steps=plan.build_plan(**relocated,frames=238*44100),**relocated)
    assert recipe.fingerprint(first)==recipe.fingerprint(second)
    assert '/workspace' not in str(first) and '/hubert.pt' not in str(first)
    assert len([s for s in first['steps'] if s['name'].startswith('convert_')])==12


@pytest.mark.parametrize('field',[('hashes','model'),('hashes','hubert'),('hashes','demucs_repo'),
    ('hashes','source'),('hashes','rvc_upstream'),('hashes','worker'),('versions','pyworld'),
    ('versions','ffmpeg'),('hashes','excerpt_selection')])
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
    sep=next(step for step in doc['steps'] if step['name']=='separate')
    assert sep['argv'][sep['argv'].index('--demucs-repo')+1]=='${DEMUCS_REPO}'
    assert doc['schema']=='sing-render-v3' and doc['render_mode']=='full'


def test_v1_recipe_or_unbound_demucs_stage_cannot_be_reused():
    doc,_,_=document()
    old=copy.deepcopy(doc)
    old['schema']='sing-render-v1'
    with pytest.raises(recipe.RecipeError):
        recipe.fingerprint(old)
    unbound=copy.deepcopy(doc)
    sep=next(step for step in unbound['steps'] if step['name']=='separate')
    sep['argv']=sep['argv'][:-2]
    with pytest.raises(recipe.RecipeError,match='Demucs'):
        recipe.fingerprint(unbound)


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


def test_v3_modes_have_separate_identity_and_cannot_relabel_plan():
    full,args,_=document()
    excerpt=recipe.recipe_document(provider='163',track_id='22558968',
        hashes=full['hashes'],versions=full['versions'],render_mode='excerpt',
        steps=plan.build_plan(**args,frames=238*44100,render_mode='excerpt'),**args)
    assert recipe.fingerprint(full)!=recipe.fingerprint(excerpt)
    assert [step['name'] for step in excerpt['steps']]==[
        'decode','separate','excerpt','convert_000','mix','encode','validate']
    for original,wrong in ((full,'excerpt'),(excerpt,'full')):
        changed=copy.deepcopy(original);changed['render_mode']=wrong
        with pytest.raises(recipe.RecipeError):
            recipe.fingerprint(changed)


@pytest.mark.parametrize('mode',[None,True,1,[],{},'','FULL','short'])
def test_v3_invalid_render_mode_is_value_error(mode):
    doc,args,steps=document()
    with pytest.raises(recipe.RecipeError,match='render mode'):
        recipe.recipe_document(provider='163',track_id='1',hashes=doc['hashes'],
            versions=doc['versions'],steps=steps,render_mode=mode,**args)


def test_v2_fingerprint_is_readable_but_never_built_with_legacy_hashes():
    doc,args,steps=document()
    legacy=copy.deepcopy(doc)
    legacy['schema']='sing-render-v2'
    legacy.pop('render_mode');legacy['hashes'].pop('excerpt_selection')
    before=copy.deepcopy(legacy)
    assert len(recipe.fingerprint(legacy))==64 and legacy==before
    with pytest.raises(recipe.RecipeError,match='digests'):
        recipe.recipe_document(provider='163',track_id='1',hashes=legacy['hashes'],
            versions=legacy['versions'],steps=steps,**args)
    mixed=copy.deepcopy(legacy);mixed['hashes']['excerpt_selection']='f'*64
    with pytest.raises(recipe.RecipeError):recipe.fingerprint(mixed)
    current=copy.deepcopy(doc);current['hashes'].pop('excerpt_selection')
    with pytest.raises(recipe.RecipeError):recipe.fingerprint(current)


@pytest.mark.parametrize('mode',['full','excerpt'])
@pytest.mark.parametrize('mutation',['missing','duplicate','wrong','missing_value'])
def test_v3_rejects_argv_render_mode_disagreement(mode,mutation):
    doc,args,_=document()
    doc=recipe.recipe_document(provider='163',track_id='1',hashes=doc['hashes'],
        versions=doc['versions'],render_mode=mode,
        steps=plan.build_plan(**args,frames=238*44100,render_mode=mode),**args)
    for step in doc['steps']:
        if step['name'] not in ('separate','excerpt','mix'):continue
        changed=copy.deepcopy(doc)
        argv=next(item for item in changed['steps'] if item['name']==step['name'])['argv']
        position=argv.index('--render-mode')
        if mutation=='missing':del argv[position:position+2]
        elif mutation=='duplicate':argv.extend(['--render-mode',mode])
        elif mutation=='wrong':argv[position+1]='excerpt' if mode=='full' else 'full'
        else:del argv[position+1:]
        with pytest.raises(recipe.RecipeError):recipe.fingerprint(changed)
