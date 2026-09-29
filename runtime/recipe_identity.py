"""Versioned identity of the *actual* source, weights, engine and media plan.

Pure standard library: callers hash their real files and inventory their real
runtime before calling. A declared digest is not proof that a file was used.
"""
from pathlib import Path
import hashlib
import json
import re

SCHEMA = 'sing-render-v2'
HASH_NAMES = ('source', 'model', 'index', 'hubert', 'demucs_repo',
              'rvc_script', 'rvc_upstream', 'media_stage', 'worker',
              'render_plan', 'stage_executor')
VERSION_NAMES = ('python','numpy','torch','torchaudio','demucs','soundfile',
                 'librosa','pyworld','faiss','fairseq','scipy','praat-parselmouth',
                  'torchcrepe','omegaconf','numba','ffmpeg','libmp3lame')


class RecipeError(ValueError):
    pass


def recipe_document(*, provider, track_id, hashes, versions, steps,
                    workspace, worker_python, worker_script, rvc_script,
                    model, index, hubert, demucs_repo):
    """Canonical identity, with local filesystem paths replaced by placeholders.

    Settings are sourced from exactly the argv tuples later executed, so chunk
    policy, original pitch, separation, gain, interpolation and encoding changes
    cannot silently reuse an old artifact if the corresponding code hashes change.
    """
    if provider not in ('163','qq') or not isinstance(track_id,str) or not re.fullmatch('[A-Za-z0-9_-]{1,100}',track_id):
        raise RecipeError('Invalid selected provider and ID')
    if not isinstance(hashes,dict) or set(hashes)!=set(HASH_NAMES):
        raise RecipeError('Missing or extra content digests')
    if any(not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value) for value in hashes.values()):
        raise RecipeError('All referenced files need SHA256 evidence')
    if not isinstance(versions,dict) or set(versions)!=set(VERSION_NAMES):
        raise RecipeError('Missing or extra runtime versions')
    if any(not isinstance(value,str) or not re.fullmatch('[0-9A-Za-z.+_~!-]{1,80}',value) for value in versions.values()):
        raise RecipeError('Runtime versions must be short plain identifiers')
    placeholders={}
    for label, raw in (('WORK',workspace),('PYTHON',worker_python),('MEDIA_STAGE',worker_script),
                       ('RVC',rvc_script),('MODEL',model),('INDEX',index),('HUBERT',hubert),
                        ('DEMUCS_REPO',demucs_repo)):
        value=str(raw)
        if not Path(value).is_absolute() or value in placeholders or '://' in value:
            raise RecipeError('Private absolute paths must be distinct')
        placeholders[value]='${'+label+'}'
    work=str(workspace).rstrip('/')+'/'
    if not isinstance(steps,(tuple,list)) or not 4<=len(steps)<=32:
        raise RecipeError('No bounded media plan')
    normalized=[]
    seen=set()
    for step in steps:
        if step.name in seen or not re.fullmatch('[a-z][a-z0-9_-]{0,63}',step.name):
            raise RecipeError('Invalid or duplicated media step')
        seen.add(step.name)
        if type(step.timeout_s) is not int or not 1<=step.timeout_s<=600:
            raise RecipeError('Unbounded stage')
        argv=[]
        for token in step.argv:
            if not isinstance(token,str) or '\x00' in token or '://' in token:
                raise RecipeError('URLs and non-plain arguments are forbidden in media plan')
            if token in placeholders:
                token=placeholders[token]
            elif token.startswith(work):
                token='${WORK}/'+token[len(work):]
            elif token.startswith('/'):
                raise RecipeError('Unidentified executable or file path in media plan')
            argv.append(token)
        normalized.append({'name':step.name,'argv':argv,'timeout_s':step.timeout_s,
                           'inputs':list(step.inputs),'outputs':list(step.outputs)})
    return {'schema':SCHEMA,'provider':provider,'track_id':track_id,
            'hashes':dict(hashes),'versions':dict(versions),'steps':normalized}


def validate_document(document):
    """Validate persisted recipes too, not only documents built in this process."""
    if not isinstance(document,dict) or set(document)!= {'schema','provider','track_id','hashes','versions','steps'} or document['schema']!=SCHEMA:
        raise RecipeError('Unrecognized render recipe schema')
    if (document['provider'] not in ('163','qq') or not isinstance(document['track_id'],str)
            or not re.fullmatch('[A-Za-z0-9_-]{1,100}',document['track_id'])):
        raise RecipeError('Invalid source identity')
    for field,names,pattern in (('hashes',HASH_NAMES,'[0-9a-f]{64}'),
                                 ('versions',VERSION_NAMES,'[0-9A-Za-z.+_~!-]{1,80}')):
        values=document[field]
        if (not isinstance(values,dict) or set(values)!=set(names) or
                any(not isinstance(v,str) or not re.fullmatch(pattern,v) for v in values.values())):
            raise RecipeError('Invalid '+field)
    steps=document['steps']
    if not isinstance(steps,list) or not 4<=len(steps)<=32:
        raise RecipeError('Invalid bounded steps')
    seen=set()
    produced={'source.audio'}
    for step in steps:
        if not isinstance(step,dict) or set(step)!={'name','argv','timeout_s','inputs','outputs'}:
            raise RecipeError('Invalid step schema')
        name=step['name']
        if not isinstance(name,str) or not re.fullmatch('[a-z][a-z0-9_-]{0,63}',name) or name in seen:
            raise RecipeError('Invalid step name')
        seen.add(name)
        if type(step['timeout_s']) is not int or not 1<=step['timeout_s']<=600:
            raise RecipeError('Invalid stage budget')
        argv=step['argv']
        if not isinstance(argv,list) or not 1<=len(argv)<=128:
            raise RecipeError('Invalid arguments')
        for token in argv:
            if (not isinstance(token,str) or len(token)>2048 or '://' in token
                    or any(ord(c)<32 for c in token) or token.startswith('/')):
                raise RecipeError('Nonportable or private recipe argument')
        for field in ('inputs','outputs'):
            values=step[field]
            if not isinstance(values,list) or not 1<=len(values)<=64:
                raise RecipeError('Invalid stage paths')
            for value in values:
                if (not isinstance(value,str) or not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_./-]{0,159}',value)
                        or any(p in ('','.','..','.receipts','.incomplete') for p in value.split('/'))
                        or value.endswith('.part')):
                    raise RecipeError('Unsafe stage path')
            if len(set(values))!=len(values):
                raise RecipeError('Duplicated stage path')
        if not set(step['inputs'])<=produced:
            raise RecipeError('Stage input has no preceding producer')
        produced.update(step['outputs'])
    separate = next((step for step in steps if step['name'] == 'separate'), None)
    if separate is None or separate['argv'].count('--demucs-repo') != 1:
        raise RecipeError('Separation stage lacks an explicit local Demucs repo')
    option = separate['argv'].index('--demucs-repo')
    if option + 1 >= len(separate['argv']) or separate['argv'][option + 1] != '${DEMUCS_REPO}':
        raise RecipeError('Separation stage must bind the inventoried Demucs repo')
    return document


def fingerprint(document):
    validate_document(document)
    data=json.dumps(document,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')
    if len(data)>65536:
        raise RecipeError('Oversized recipe')
    return hashlib.sha256(data).hexdigest()
