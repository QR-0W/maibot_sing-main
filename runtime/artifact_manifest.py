"""Shared verification of committed v2/v3 metadata, without filesystem I/O."""
import hashlib
import re

from .recipe_identity import fingerprint, SCHEMA, READABLE_SCHEMAS
from .excerpt_selection import canonical_selection_json, ExcerptSelectionError


def validate_excerpt_evidence(recipe, key, selection, proof):
    """Bind embedded canonical selection bytes to the executed excerpt receipt."""
    try:
        raw = canonical_selection_json(selection).encode('utf-8')
    except (ExcerptSelectionError, TypeError, ValueError, RecursionError) as exc:
        raise ValueError('Invalid artifact selection document') from exc
    if not 0 < len(raw) <= 65536:
        raise ValueError('Oversized artifact selection document')
    step = next((step for step in recipe['steps'] if step['name'] == 'excerpt'), None)
    if (step is None or not isinstance(proof, dict)
            or set(proof) != {'schema','recipe','stage','inputs','outputs'}
            or type(proof['schema']) is not int or proof['schema'] != 1
            or proof['recipe'] != key or proof['stage'] != 'excerpt'
            or not isinstance(proof['inputs'], dict)
            or set(proof['inputs']) != set(step['inputs'])
            or not isinstance(proof['outputs'], dict)
            or set(proof['outputs']) != set(step['outputs'])):
        raise ValueError('Artifact excerpt receipt mismatch')
    for digest in proof['inputs'].values():
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('Invalid excerpt receipt input digest')
    for record in proof['outputs'].values():
        if (not isinstance(record, dict) or set(record) != {'bytes','sha256'}
                or type(record['bytes']) is not int or not 0 < record['bytes'] <= 512*1024**2
                or not isinstance(record['sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', record['sha256'])):
            raise ValueError('Invalid excerpt receipt output record')
    expected = {'bytes':len(raw), 'sha256':hashlib.sha256(raw).hexdigest()}
    if proof['outputs'].get('selection.json') != expected:
        raise ValueError('Artifact canonical selection digest mismatch')
    return selection


def validate_manifest(data, key, size):
    recipe=data['recipe']
    schema=data.get('recipe_schema')
    if (schema not in READABLE_SCHEMAS or fingerprint(recipe)!=key
            or schema!=recipe['schema']
            or data.get('status')!='completed' or data.get('key')!=key
            or type(data.get('bytes')) is not int or data['bytes']!=size):
        raise ValueError('Artifact manifest identity mismatch')
    if (data['model_sha256']!=recipe['hashes']['model'] or data['index_sha256']!=recipe['hashes']['index']
            or data['source'].get('platform')!=recipe['provider']
            or data['source'].get('identifier')!=recipe['track_id']):
        raise ValueError('Artifact source/model provenance mismatch')
    mix=next((s for s in recipe['steps'] if s['name']=='mix'),None)
    if (mix is None or type(data['instrumental']) is not bool
            or data['instrumental']!=('--instrumental' in mix['argv'])
            or data['parameters']!={'policy':schema}):
        raise ValueError('Artifact mix settings mismatch')
    if schema==SCHEMA and recipe['render_mode']=='excerpt':
        validate_excerpt_evidence(recipe,key,data.get('selection'),data.get('excerpt_receipt'))
    elif 'selection' in data or 'excerpt_receipt' in data:
        raise ValueError('Non-excerpt artifact cannot claim selection evidence')
    final=recipe['steps'][-1]
    if (final['name']!='validate' or final['inputs']!=['cover.mp3'] or final['outputs']!=['cover.mp3']
            or final['argv']!=['ffmpeg','-nostdin','-v','error','-xerror','-threads','1','-i',
                              '${WORK}/cover.mp3','-f','null','-']):
        raise ValueError('Required final decoding stage missing')
    proof=data['validation']
    if (not isinstance(proof,dict) or set(proof)!={'schema','recipe','stage','inputs','outputs'}
            or type(proof['schema']) is not int or proof['schema']!=1 or proof['recipe']!=key
            or proof['stage']!='validate' or proof['inputs']!={'cover.mp3':data['sha256']}
            or proof['outputs']!={'cover.mp3':{'bytes':size,'sha256':data['sha256']}}):
        raise ValueError('Artifact final decoding receipt mismatch')
    return data
