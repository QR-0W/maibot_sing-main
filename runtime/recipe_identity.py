"""Versioned identity of the *actual* source, weights, engine and media plan.

Pure standard library: callers hash their real files and inventory their real
runtime before calling. A declared digest is not proof that a file was used.
"""
from pathlib import Path
import hashlib
import json
import re

if __package__:
    from .context_window import freeze_context, full_context, validate_context_window
else:
    from context_window import freeze_context, full_context, validate_context_window

SCHEMA = 'sing-render-v4'
PREVIOUS_SCHEMA = 'sing-render-v3'
LEGACY_SCHEMA = 'sing-render-v2'
READABLE_SCHEMAS = (LEGACY_SCHEMA, PREVIOUS_SCHEMA, SCHEMA)
LEGACY_HASH_NAMES = ('source', 'model', 'index', 'hubert', 'demucs_repo',
                     'rvc_script', 'rvc_upstream', 'media_stage', 'worker',
                     'render_plan', 'stage_executor')
PREVIOUS_HASH_NAMES = (*LEGACY_HASH_NAMES, 'excerpt_selection')
HASH_NAMES = (*PREVIOUS_HASH_NAMES, 'context_window')
VERSION_NAMES = ('python','numpy','torch','torchaudio','demucs','soundfile',
                 'librosa','pyworld','faiss','fairseq','scipy','praat-parselmouth',
                  'torchcrepe','omegaconf','numba','ffmpeg','libmp3lame')


class RecipeError(ValueError):
    pass


def recipe_document(*, provider, track_id, hashes, versions, steps, source_frames,
                    workspace, worker_python, worker_script, rvc_script,
                    model, index, hubert, demucs_repo, render_mode='full', context_window=None):
    """Canonical identity, with local filesystem paths replaced by placeholders.

    Settings are sourced from exactly the argv tuples later executed, so chunk
    policy, original pitch, separation, gain, interpolation and encoding changes
    cannot silently reuse an old artifact if the corresponding code hashes change.
    """
    if not isinstance(render_mode,str) or render_mode not in ('full','excerpt'):
        raise RecipeError('Invalid render mode')
    try:
        expected_context=(freeze_context(source_frames) if render_mode=='excerpt'
                          else full_context(source_frames))
        if context_window is not None:
            validate_context_window(context_window, source_frames,
                                    frozen=render_mode=='excerpt')
        context_window=expected_context
    except ValueError as exc:
        raise RecipeError('Invalid source frame bounds') from exc
    if render_mode=='full' and source_frames>300*44100:
        raise RecipeError('Full render source exceeds 300 seconds')
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
    return validate_document({'schema':SCHEMA,'provider':provider,'track_id':track_id,
            'render_mode':render_mode,'source_frames':source_frames,
            'context_window':context_window,
            'hashes':dict(hashes),'versions':dict(versions),'steps':normalized})


def validate_document(document):
    """Validate persisted recipes too, not only documents built in this process."""
    if not isinstance(document,dict) or document.get('schema') not in READABLE_SCHEMAS:
        raise RecipeError('Unrecognized render recipe schema')
    current = document['schema'] == SCHEMA
    versioned = document['schema'] in (PREVIOUS_SCHEMA,SCHEMA)
    fields = {'schema','provider','track_id','hashes','versions','steps'}
    if versioned:
        fields.add('render_mode')
    if current:
        fields.update(('source_frames','context_window'))
    if set(document) != fields:
        raise RecipeError('Unrecognized render recipe fields')
    if versioned and (not isinstance(document['render_mode'],str)
                      or document['render_mode'] not in ('full','excerpt')):
        raise RecipeError('Invalid render mode')
    if current:
        source_frames=document['source_frames']
        try:
            validate_context_window(document['context_window'],source_frames,
                                    frozen=document['render_mode']=='excerpt')
        except ValueError as exc:
            raise RecipeError('Invalid recipe context window') from exc
        if document['render_mode']=='full' and source_frames>300*44100:
            raise RecipeError('Full render source exceeds 300 seconds')
    hash_names = (HASH_NAMES if current else
                  PREVIOUS_HASH_NAMES if versioned else LEGACY_HASH_NAMES)
    if (document['provider'] not in ('163','qq') or not isinstance(document['track_id'],str)
            or not re.fullmatch('[A-Za-z0-9_-]{1,100}',document['track_id'])):
        raise RecipeError('Invalid source identity')
    for field,names,pattern in (('hashes',hash_names,'[0-9a-f]{64}'),
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
    if versioned:
        for step in steps:
            if step['name'] not in ('separate','excerpt','mix'):
                continue
            argv = step['argv']
            if argv.count('--render-mode') != 1:
                raise RecipeError('Media stage requires one explicit render mode')
            position = argv.index('--render-mode')
            if position + 1 >= len(argv) or argv[position + 1] != document['render_mode']:
                raise RecipeError('Media stage render mode differs from recipe')
        excerpt = next((step for step in steps if step['name'] == 'excerpt'), None)
        if document['render_mode'] == 'excerpt':
            if (excerpt is None or excerpt['inputs'] != ['vocals.wav','backing.wav']
                    or set(excerpt['outputs']) != {'vocal_000.wav','excerpt_backing.wav','selection.json'}
                    or [step['name'] for step in steps] !=
                       ['decode','separate','excerpt','convert_000','mix','encode','validate']
                    or 'selection.json' not in steps[4]['inputs']
                    or 'excerpt_backing.wav' not in steps[4]['inputs']):
                raise RecipeError('Excerpt mode requires the synchronized selection plan')
        elif excerpt is not None or any('selection.json' in step['inputs'] + step['outputs']
                                        for step in steps):
            raise RecipeError('Full mode cannot contain an excerpt selection plan')
    if current:
        expected_options={'--source-frames':str(document['source_frames']),
                          '--context-start-frame':str(document['context_window']['start_frame']),
                          '--context-end-frame':str(document['context_window']['end_frame'])}
        for step in steps:
            if step['name'] not in ('separate','excerpt','mix'):
                continue
            for option,value in expected_options.items():
                argv=step['argv']
                if argv.count(option)!=1:
                    raise RecipeError('Media stage lacks frozen context parameters')
                position=argv.index(option)
                if position+1>=len(argv) or argv[position+1]!=value:
                    raise RecipeError('Media stage context differs from recipe')
        decode=steps[0]
        filters=[decode['argv'][index+1] for index,token in enumerate(decode['argv'][:-1])
                 if token=='-af']
        if document['render_mode']=='excerpt':
            expected_filter=('aresample=44100,atrim=start_sample=%d:end_sample=%d,asetpts=PTS-STARTPTS' %
                             (document['context_window']['start_frame'],
                              document['context_window']['end_frame']))
            if filters!=[expected_filter]:
                raise RecipeError('Excerpt decode differs from frozen context')
        elif filters:
            raise RecipeError('Full decode cannot trim the source timeline')
        if __package__:
            from .render_plan import build_plan
        else:
            from render_plan import build_plan
        paths=dict(workspace='/canonical/work',worker_python='/canonical/python',
                   worker_script='/canonical/media',rvc_script='/canonical/rvc',
                   model='/canonical/model',index='/canonical/index',
                   hubert='/canonical/hubert',demucs_repo='/canonical/demucs')
        canonical=build_plan(**paths,frames=source_frames,
                             render_mode=document['render_mode'],
                             instrumental='--instrumental' in steps[-3]['argv'])
        substitutions={paths[name]:'${'+label+'}' for name,label in (
            ('workspace','WORK'),('worker_python','PYTHON'),('worker_script','MEDIA_STAGE'),
            ('rvc_script','RVC'),('model','MODEL'),('index','INDEX'),
            ('hubert','HUBERT'),('demucs_repo','DEMUCS_REPO'))}
        expected=[]
        for step in canonical:
            argv=[]
            for token in step.argv:
                if token in substitutions:
                    token=substitutions[token]
                elif token.startswith(paths['workspace']+'/'):
                    token='${WORK}/'+token[len(paths['workspace'])+1:]
                argv.append(token)
            expected.append(dict(name=step.name,argv=argv,timeout_s=step.timeout_s,
                                 inputs=list(step.inputs),outputs=list(step.outputs)))
        if steps!=expected:
            raise RecipeError('Plan differs from the frozen v4 media policy')
    return document


def validate_private_steps(document, steps, workspace):
    """Bind a frozen private plan to its portable bytes without today's assets.

    Used before claimed-unit reconciliation for EVERY readable schema. Legacy
    recipes are never rebuilt or reinterpreted through the current plan builder.
    """
    if len(steps)!=len(document['steps']):
        raise RecipeError('Private plan length differs from portable recipe')
    bindings={}
    for actual,expected in zip(steps,document['steps']):
        if (actual.name!=expected['name'] or type(actual.timeout_s) is not int
                or actual.timeout_s!=expected['timeout_s']
                or list(actual.inputs)!=expected['inputs']
                or list(actual.outputs)!=expected['outputs']
                or len(actual.argv)!=len(expected['argv'])):
            raise RecipeError('Private plan differs from portable recipe')
        for token,portable in zip(actual.argv,expected['argv']):
            if not isinstance(token,str):
                raise RecipeError('Invalid private plan argument')
            if portable.startswith('${WORK}/'):
                if token!=str(workspace).rstrip('/')+'/'+portable[len('${WORK}/'):]:
                    raise RecipeError('Private workspace argument changed')
            elif re.fullmatch(r'\$\{(?:WORK|PYTHON|MEDIA_STAGE|RVC|MODEL|INDEX|HUBERT|DEMUCS_REPO)\}',portable):
                if (not Path(token).is_absolute() or '://' in token
                        or any(ord(ch)<32 for ch in token)):
                    raise RecipeError('Invalid frozen asset path')
                if portable=='${WORK}' and token!=str(workspace):
                    raise RecipeError('Private workspace argument changed')
                if portable in bindings and bindings[portable]!=token:
                    raise RecipeError('Inconsistent frozen asset bindings')
                bindings[portable]=token
            elif token!=portable:
                raise RecipeError('Private plan argument differs from portable recipe')


def fingerprint(document):
    validate_document(document)
    data=json.dumps(document,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')
    if len(data)>65536:
        raise RecipeError('Oversized recipe')
    return hashlib.sha256(data).hexdigest()
