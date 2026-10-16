"""Current plans are exact; historical identities retain their original bytes."""
from dataclasses import replace
from pathlib import Path
import copy
import hashlib
import importlib.util
import json
import sys

import pytest

root=Path(__file__).resolve().parents[1]/'runtime'
for name in ('context_window','render_plan','recipe_identity'):
    spec=importlib.util.spec_from_file_location(name,root/(name+'.py'))
    module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
plan=sys.modules['render_plan']; recipe=sys.modules['recipe_identity']
RATE=44100


def document(*,frames=238*RATE,mode='full',instrumental=False):
    hashes={name:format(i+1,'064x') for i,name in enumerate(recipe.HASH_NAMES)}
    versions={name:'1.2.3' for name in recipe.VERSION_NAMES}
    args=dict(workspace='/workspace',worker_python='/python',worker_script='/media_stage.py',
        rvc_script='/rvc.py',model='/model.pth',index='/index',hubert='/hubert.pt',
        demucs_repo='/private/htdemucs-repo')
    steps=plan.build_plan(**args,frames=frames,render_mode=mode,instrumental=instrumental)
    return recipe.recipe_document(provider='163',track_id='22558968',hashes=hashes,
        versions=versions,steps=steps,source_frames=frames,render_mode=mode,**args),args,steps


def test_identity_has_no_private_path_and_is_stable():
    first,args,_=document()
    relocated={**args,'workspace':'/another-private-workspace','worker_python':'/other-python',
        'worker_script':'/other-media-stage','rvc_script':'/elsewhere-rvc','model':'/private-model',
        'index':'/different-index','hubert':'/different-hubert','demucs_repo':'/different-local-repo'}
    second=recipe.recipe_document(provider='163',track_id='22558968',
        hashes=first['hashes'],versions=first['versions'],source_frames=238*RATE,
        steps=plan.build_plan(**relocated,frames=238*RATE),**relocated)
    assert recipe.fingerprint(first)==recipe.fingerprint(second)
    assert '/workspace' not in str(first) and '/hubert.pt' not in str(first)
    assert len([s for s in first['steps'] if s['name'].startswith('convert_')])==12
    assert first['schema']=='sing-render-v4'
    assert first['context_window']=={'start_frame':0,'end_frame':238*RATE}


@pytest.mark.parametrize('name',recipe.HASH_NAMES)
def test_every_asset_change_invalidates_existing_cache(name):
    doc,_,_=document(); other=copy.deepcopy(doc)
    other['hashes'][name]='e'*64
    assert recipe.fingerprint(doc)!=recipe.fingerprint(other)


@pytest.mark.parametrize('name',recipe.VERSION_NAMES)
def test_every_runtime_version_changes_identity(name):
    doc,_,_=document(); other=copy.deepcopy(doc)
    other['versions'][name]='9.9.9'
    assert recipe.fingerprint(doc)!=recipe.fingerprint(other)


def test_pitch_change_is_rejected_not_a_new_legal_recipe():
    doc,args,steps=document()
    chunk=next(i for i,s in enumerate(steps) if s.name.startswith('convert_'))
    original=steps[chunk]; argv=list(original.argv);argv[argv.index('--pitch')+1]='1'
    changed=list(steps);changed[chunk]=replace(original,argv=tuple(argv))
    with pytest.raises(recipe.RecipeError):
        recipe.recipe_document(provider='163',track_id='22558968',hashes=doc['hashes'],
            versions=doc['versions'],steps=changed,source_frames=238*RATE,**args)
    assert doc['steps'][chunk]['argv'][doc['steps'][chunk]['argv'].index('--hubert')+1]=='${HUBERT}'


@pytest.mark.parametrize('mode',['full','excerpt'])
@pytest.mark.parametrize('field',['argv','timeout_s','inputs','outputs','name'])
def test_every_step_must_match_the_frozen_builder(mode,field):
    doc,_,_=document(mode=mode)
    for number in range(len(doc['steps'])):
        changed=copy.deepcopy(doc);step=changed['steps'][number]
        if field=='argv': step[field].append('--invented')
        elif field=='timeout_s': step[field]-=1
        elif field=='inputs': step[field]=['source.audio'] if step[field]!=['source.audio'] else ['original.wav']
        elif field=='outputs': step[field]=['forged.wav']
        else: step[field]='forged_'+str(number)
        with pytest.raises(recipe.RecipeError):recipe.fingerprint(changed)


@pytest.mark.parametrize('frames',[300*RATE+1,600*RATE,900*RATE])
def test_long_excerpt_has_source_and_frozen_context_identity(frames):
    doc,_,_=document(frames=frames,mode='excerpt')
    assert doc['source_frames']==frames
    assert doc['context_window']==sys.modules['context_window'].freeze_context(frames)
    assert len(doc['steps'])==7
    assert len(recipe.fingerprint(doc))==64
    with pytest.raises(ValueError):document(frames=frames,mode='full')


@pytest.mark.parametrize('mode',['full','excerpt'])
@pytest.mark.parametrize('frames',[None,True,30*RATE-1,30*RATE+.5,'1323000',900*RATE+1])
def test_recipe_source_frames_are_required_exact_and_bounded(mode,frames):
    doc,args,steps=document(mode=mode)
    with pytest.raises(recipe.RecipeError):
        recipe.recipe_document(provider='163',track_id='1',hashes=doc['hashes'],
            versions=doc['versions'],steps=steps,source_frames=frames,render_mode=mode,**args)
    with pytest.raises(TypeError):
        recipe.recipe_document(provider='163',track_id='1',hashes=doc['hashes'],
            versions=doc['versions'],steps=steps,render_mode=mode,**args)


def test_supplied_context_must_match_not_be_silently_replaced():
    doc,args,steps=document(frames=900*RATE,mode='excerpt')
    for frozen in ({'start_frame':0,'end_frame':90*RATE},
                   {'start_frame':doc['context_window']['start_frame']+1,
                    'end_frame':doc['context_window']['end_frame']+1}):
        with pytest.raises(recipe.RecipeError):
            recipe.recipe_document(provider='163',track_id='1',hashes=doc['hashes'],
                versions=doc['versions'],steps=steps,source_frames=900*RATE,
                render_mode='excerpt',context_window=frozen,**args)


@pytest.mark.parametrize('mode',[None,True,1,[],{},'','FULL','short'])
def test_invalid_render_mode_is_value_error(mode):
    doc,args,steps=document()
    with pytest.raises(recipe.RecipeError,match='render mode'):
        recipe.recipe_document(provider='163',track_id='1',hashes=doc['hashes'],
            versions=doc['versions'],steps=steps,source_frames=238*RATE,render_mode=mode,**args)


def test_modes_and_instrumental_have_separate_identity_and_cannot_relabel():
    full,_,_=document();excerpt,_,_=document(mode='excerpt');backed,_,_=document(instrumental=True)
    assert len({recipe.fingerprint(d) for d in (full,excerpt,backed)})==3
    for original,wrong in ((full,'excerpt'),(excerpt,'full')):
        changed=copy.deepcopy(original);changed['render_mode']=wrong
        with pytest.raises(recipe.RecipeError):recipe.fingerprint(changed)


def legacy_document():
    # Frozen from production runtime/render_plan.py's pre-window v3 excerpt
    # implementation, read only. Never call the current builder to invent history.
    opts=['--scratch','${WORK}','--model','${MODEL}','--index','${INDEX}','--render-mode','excerpt']
    def step(name,argv,timeout,inputs,outputs):
        return dict(name=name,argv=argv,timeout_s=timeout,inputs=inputs,outputs=outputs)
    steps=[
        step('decode',['ffmpeg','-nostdin','-v','error','-xerror','-threads','1','-i',
            '${WORK}/source.audio','-map','0:a:0','-ar','44100','-ac','2','-c:a','pcm_f32le',
            '${WORK}/original.wav'],120,['source.audio'],['original.wav']),
        step('separate',['${PYTHON}','${MEDIA_STAGE}','separate',*opts,'--demucs-repo',
            '${DEMUCS_REPO}'],600,['original.wav'],['vocals.wav','backing.wav']),
        step('excerpt',['${PYTHON}','${MEDIA_STAGE}','excerpt',*opts],60,
            ['vocals.wav','backing.wav'],['selection.json','vocal_000.wav','excerpt_backing.wav']),
        step('convert_000',['${PYTHON}','${RVC}','--model','${MODEL}','--index','${INDEX}',
            '--hubert','${HUBERT}','--input','${WORK}/vocal_000.wav','--output',
            '${WORK}/vocal_000_converted.wav','--limit-seconds','25','--pitch','0',
            '--f0-method','harvest','--index-rate','0.5','--filter-radius','3',
            '--rms-mix-rate','0.25','--protect','0.33','--seed','20260928','--resample-sr','44100'],
            180,['selection.json','vocal_000.wav'],['vocal_000_converted.wav']),
        step('mix',['${PYTHON}','${MEDIA_STAGE}','mix',*opts],60,
            ['selection.json','excerpt_backing.wav','vocal_000.wav','vocal_000_converted.wav'],['mixed.wav']),
        step('encode',['ffmpeg','-nostdin','-v','error','-xerror','-threads','1','-i',
            '${WORK}/mixed.wav','-c:a','libmp3lame','-b:a','192k','${WORK}/cover.mp3'],90,['mixed.wav'],['cover.mp3']),
        step('validate',['ffmpeg','-nostdin','-v','error','-xerror','-threads','1','-i',
            '${WORK}/cover.mp3','-f','null','-'],60,['cover.mp3'],['cover.mp3'])]
    names=('source','model','index','hubert','demucs_repo','rvc_script','rvc_upstream',
           'media_stage','worker','render_plan','stage_executor','excerpt_selection')
    return dict(schema='sing-render-v3',provider='163',track_id='22558968',render_mode='excerpt',
        hashes={name:format(i+1,'064x') for i,name in enumerate(names)},
        versions={name:'1.2.3' for name in recipe.VERSION_NAMES},steps=steps)


def legacy_full_document():
    # Exact old 30-second full plan: two 20/10-second chunks, no selection.
    old=legacy_document();old['render_mode']='full'
    old['steps']=[s for s in old['steps'] if s['name']!='excerpt']
    first=next(s for s in old['steps'] if s['name']=='convert_000')
    first['inputs']=['vocal_000.wav']
    second=copy.deepcopy(first);second['name']='convert_001'
    second['argv']=[token.replace('vocal_000','vocal_001') for token in first['argv']]
    second['inputs']=['vocal_001.wav'];second['outputs']=['vocal_001_converted.wav']
    old['steps'].insert(3,second)
    for s in old['steps']:
        if s['name'] in ('separate','mix'):
            position=s['argv'].index('--render-mode');s['argv'][position+1]='full'
        if s['name']=='separate':s['outputs']=['vocals.wav','backing.wav','vocal_000.wav','vocal_001.wav']
        if s['name']=='mix':s['inputs']=['vocals.wav','backing.wav','vocal_000.wav',
            'vocal_001.wav','vocal_000_converted.wav','vocal_001_converted.wav']
    return old


@pytest.mark.parametrize('schema,mode',[('sing-render-v2','full'),
                                       ('sing-render-v3','full'),('sing-render-v3','excerpt')])
def test_historical_fingerprint_preserves_original_canonical_bytes(schema,mode):
    old=legacy_full_document() if mode=='full' else legacy_document()
    if schema=='sing-render-v2':
        old['schema']=schema;old.pop('render_mode');old['hashes'].pop('excerpt_selection')
        for step in old['steps']:
            if '--render-mode' in step['argv']:
                position=step['argv'].index('--render-mode');del step['argv'][position:position+2]
    before=copy.deepcopy(old)
    encoded=json.dumps(old,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
    assert recipe.fingerprint(old)==hashlib.sha256(encoded).hexdigest()
    assert old==before
    assert 'context_window' not in old and 'source_frames' not in old
    assert '--source-frames' not in str(old)
    for name in ('context_window','excerpt_selection'):
        mixed=copy.deepcopy(old);mixed['hashes'][name]='f'*64
        if name not in old['hashes']:
            with pytest.raises(recipe.RecipeError):recipe.fingerprint(mixed)


def test_missing_assets_url_and_unbound_demucs_are_rejected():
    doc,args,steps=document();missing=dict(doc['hashes']);missing.pop('hubert')
    with pytest.raises(recipe.RecipeError):
        recipe.recipe_document(provider='163',track_id='1',hashes=missing,
            versions=doc['versions'],steps=steps,source_frames=238*RATE,**args)
    malformed=list(steps);malformed[0]=replace(steps[0],argv=('https://evil.example/audio',))
    with pytest.raises(recipe.RecipeError):
        recipe.recipe_document(provider='163',track_id='1',hashes=doc['hashes'],
            versions=doc['versions'],steps=malformed,source_frames=238*RATE,**args)
    unbound=copy.deepcopy(doc)
    sep=next(step for step in unbound['steps'] if step['name']=='separate')
    position=sep['argv'].index('--demucs-repo');del sep['argv'][position:position+2]
    with pytest.raises(recipe.RecipeError,match='Demucs'):recipe.fingerprint(unbound)


@pytest.mark.parametrize('mode',['full','excerpt'])
@pytest.mark.parametrize('mutation',['missing','duplicate','wrong','missing_value'])
def test_current_argv_render_mode_must_agree(mode,mutation):
    doc,_,_=document(mode=mode)
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


def test_hash_sets_and_missing_current_assets():
    assert recipe.PREVIOUS_HASH_NAMES==tuple(legacy_document()['hashes'])
    assert recipe.HASH_NAMES==(*recipe.PREVIOUS_HASH_NAMES,'context_window')
    doc,args,steps=document(); missing=dict(doc['hashes']);missing.pop('context_window')
    with pytest.raises(recipe.RecipeError):
        recipe.recipe_document(provider='163',track_id='1',hashes=missing,
            versions=doc['versions'],steps=steps,source_frames=238*RATE,**args)
    old=copy.deepcopy(doc);old['schema']='sing-render-v1'
    with pytest.raises(recipe.RecipeError):recipe.fingerprint(old)
