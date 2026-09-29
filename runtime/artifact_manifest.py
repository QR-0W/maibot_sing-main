"""Shared verification of current committed metadata, without filesystem I/O."""
from .recipe_identity import fingerprint, SCHEMA


def validate_manifest(data, key, size):
    recipe=data['recipe']
    if (data.get('recipe_schema')!=SCHEMA or fingerprint(recipe)!=key
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
            or data['parameters']!={'policy':SCHEMA}):
        raise ValueError('Artifact mix settings mismatch')
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
