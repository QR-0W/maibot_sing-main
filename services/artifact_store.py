"""Durable immutable covers, independent from delivery and derived catalogues.

Call in a worker thread while holding per-job scheduler ownership. A directory
rename is the commit point; completed artifacts never depend on scratch after
that point. Failed staging attempts are retained, not silently deleted.
"""
from dataclasses import dataclass
from pathlib import Path
import ctypes
import hashlib
import json
import os
import shutil
import time
import uuid

from ..runtime.recipe_identity import fingerprint, SCHEMA, READABLE_SCHEMAS
from ..runtime.artifact_manifest import validate_excerpt_evidence, validate_manifest
from ..runtime.stage_receipts import StageReceipts, CheckpointError
from . import library_catalog
from .ownership import exclusive


class ArtifactError(RuntimeError):
    def __init__(self,code,message):
        super().__init__(message)
        self.code=code


@dataclass(frozen=True)
class Artifact:
    key: str
    path: Path
    sha256: str
    bytes: int


def _sync_directory(path):
    fd=os.open(path,os.O_DIRECTORY|os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _rename_exclusive(source,destination):
    # This backend already requires Linux/systemd. Unlike os.replace or plain
    # rename, RENAME_NOREPLACE also refuses an existing EMPTY destination dir.
    library=ctypes.CDLL(None,use_errno=True)
    rename=getattr(library,'renameat2',None)
    if rename is None:
        raise ArtifactError('atomic_publish_unavailable','Linux renameat2 is required')
    rename.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
    rename.restype=ctypes.c_int
    if rename(-100,os.fsencode(source),-100,os.fsencode(destination),1):
        code=ctypes.get_errno()
        raise OSError(code,os.strerror(code))


class ArtifactStore:
    def __init__(self,root: Path,*,max_bytes=64*1024**2,min_free_bytes=64*1024**2):
        if (not root.is_absolute() or not root.is_dir() or type(max_bytes) is not int
                or not 1024<=max_bytes<=64*1024**2 or type(min_free_bytes) is not int
                or min_free_bytes<0):
            raise ValueError('Invalid artifact storage bounds')
        StageReceipts._safe(root)
        self.root=root
        self.max_bytes=max_bytes
        self.min_free_bytes=min_free_bytes

    def verify(self,key):
        """Reopen committed media without relying on a surviving job workspace."""
        try:
            data,path=library_catalog._verify(self.root,key,max_bytes=self.max_bytes)
            if data.get('recipe_schema') not in READABLE_SCHEMAS:
                raise ValueError('Not a supported committed recipe')
            return Artifact(key,path,data['sha256'],data['bytes'])
        except (OSError,KeyError,TypeError,ValueError,AttributeError) as exc:
            raise ArtifactError('artifact_invalid','Existing artifact is invalid; never overwrite it') from exc

    def _evidence(self,workspace,recipe,key):
        receipts=StageReceipts(workspace,key)
        source=receipts._files(['source.audio'])['source.audio']
        if source['sha256']!=recipe['hashes']['source']:
            raise ArtifactError('source_changed','Source bytes do not match render recipe')
        final=recipe['steps'][-1]
        expected=['ffmpeg','-nostdin','-v','error','-xerror','-threads','1','-i',
                  '${WORK}/cover.mp3','-f','null','-']
        if (final['name']!='validate' or final['argv']!=expected
                or final['inputs']!=['cover.mp3'] or final['outputs']!=['cover.mp3']):
            raise ArtifactError('validation_missing','Recipe lacks the required full decoding stage')
        evidence=None
        excerpt_receipt=None
        for step in recipe['steps']:
            files=receipts._files(step['inputs'])
            hashes={name:entry['sha256'] for name,entry in files.items()}
            evidence=receipts.verify(step['name'],hashes)
            if evidence is None or set(evidence['outputs'])!=set(step['outputs']):
                raise ArtifactError('stage_unverified','Every planned media stage must have a valid receipt')
            if step['name']=='excerpt':
                excerpt_receipt=evidence
        selection=None
        if recipe.get('render_mode')=='excerpt':
            path=workspace/'selection.json'
            try:
                if path.is_symlink() or not path.is_file() or not 0<path.stat().st_size<=65536:
                    raise ValueError('Unsafe or oversized selection document')
                selection=json.loads(path.read_text(encoding='utf-8'))
                validate_excerpt_evidence(recipe,key,selection,excerpt_receipt)
            except (OSError,ValueError,TypeError,RecursionError) as exc:
                raise ArtifactError('selection_invalid','Excerpt selection evidence is invalid') from exc
        return evidence,selection,excerpt_receipt

    def publish(self,workspace: Path,recipe: dict,*,title: str,artist: str,album: str=''):
        """Retry-safe commit. No URLs, stream IDs, consent or private paths stored.

        The caller must inventory actual dependencies before constructing recipe;
        receipt hashes alone cannot prove which model code ran or media quality.
        """
        # Freeze caller-owned dictionaries before hashing/copying.
        recipe=json.loads(json.dumps(recipe,allow_nan=False))
        key=fingerprint(recipe)
        if recipe['schema']!=SCHEMA:
            raise ArtifactError('recipe_read_only','Legacy recipes can only reopen committed artifacts')
        with exclusive(self.root/'.publish.lock'):
            final=self.root/key
            if final.exists() or final.is_symlink():
                result=self.verify(key)
                # A previous process may have died after rename, before its
                # directory fsync. Replay completes durability as well as reads.
                _sync_directory(final)
                _sync_directory(self.root)
                return result
            title=library_catalog._label(title)
            artist=library_catalog._label(artist)
            album=library_catalog._label(album) if album else ''
            try:
                evidence,selection,excerpt_receipt=self._evidence(workspace,recipe,key)
            except (OSError,CheckpointError) as exc:
                raise ArtifactError('stage_unverified','Media receipts or bytes could not be verified') from exc
            record=evidence['outputs']['cover.mp3']
            size=record['bytes']
            if not 0<size<=self.max_bytes:
                raise ArtifactError('artifact_size_limit','Encoded media exceeds the publication budget')
            if shutil.disk_usage(self.root).free<size+self.min_free_bytes:
                raise ArtifactError('artifact_disk_full','Insufficient free disk for durable publication')
            staging_root=self.root/'.publishing'
            StageReceipts._safe(staging_root)
            staging_root.mkdir(mode=0o700,exist_ok=True)
            # Retain interrupted copies for explicit maintenance, but never allow
            # repeated failing submissions to grow staging without a bound.
            if sum(1 for _ in staging_root.iterdir())>=16:
                raise ArtifactError('artifact_attempt_limit','Retained publish attempts require inspection')
            staging=staging_root/(key+'-'+uuid.uuid4().hex)
            staging.mkdir(mode=0o700)
            _sync_directory(staging_root)
            digest=hashlib.sha256()
            count=0
            with (workspace/'cover.mp3').open('rb') as source, (staging/'cover.mp3').open('xb') as output:
                for block in iter(lambda:source.read(1024*1024),b''):
                    count+=len(block)
                    if count>size:
                        raise ArtifactError('artifact_changed','Media changed while copying')
                    digest.update(block)
                    output.write(block)
                output.flush()
                os.fchmod(output.fileno(),0o444)
                os.fsync(output.fileno())
            if count!=size or digest.hexdigest()!=record['sha256']:
                raise ArtifactError('artifact_changed','Media changed while copying')
            mix=next((s for s in recipe['steps'] if s['name']=='mix'),None)
            if mix is None:
                raise ArtifactError('recipe_invalid','Render recipe has no mixing stage')
            metadata={'status':'completed','key':key,'sha256':record['sha256'],'bytes':size,
                'title':title,'artist':artist,'album':album,
                'recipe_schema':SCHEMA,'recipe':recipe,'validation':evidence,
                'source':{'type':'provider-track','platform':recipe['provider'],'identifier':recipe['track_id']},
                'model_sha256':recipe['hashes']['model'],'index_sha256':recipe['hashes']['index'],
                'parameters':{'policy':SCHEMA},'instrumental':'--instrumental' in mix['argv'],
                'completed_at':time.time()}
            if selection is not None:
                metadata.update(selection=selection,excerpt_receipt=excerpt_receipt)
            validate_manifest(metadata,key,size)
            with (staging/'metadata.json').open('x',encoding='utf-8') as output:
                json.dump(metadata,output,sort_keys=True,ensure_ascii=False,allow_nan=False)
                output.flush()
                os.fchmod(output.fileno(),0o444)
                os.fsync(output.fileno())
            _sync_directory(staging)
            # No automatic cleanup, including after rename or fsync failure.
            _rename_exclusive(staging,final)
            _sync_directory(staging_root)
            _sync_directory(self.root)
            return self.verify(key)

    def reconcile_catalog(self):
        """Derived browsing index failure is not a failed audio publication."""
        warnings=[]
        try:
            library_catalog.rebuild_library(self.root,skip_corrupt=True,warnings=warnings)
        except (OSError,ValueError,KeyError,TypeError):
            warnings.append('catalog_rebuild_failed')
        return warnings


def publish_job(store,artifacts: ArtifactStore,*,job_id,stream_id,run_token,workspace,recipe):
    """One synchronous disk transaction boundary; async callers use to_thread.

    Ownership lives INSIDE this function, so cancelling the awaiting coroutine
    cannot release a lock while copying continues in its worker thread. A crash
    after rename and before ready is repaired by replaying the same publication.
    """
    from .job_store import JobConflict
    job=store.get(job_id,stream_id)
    key=fingerprint(recipe)
    with exclusive(store.path.parent/(job.id+'.scheduler.lock')):
        job=store.get(job_id,stream_id)
        selected=job.selected_source
        if (job.run_token!=run_token or not selected
                or selected['provider']!=recipe['provider'] or selected['track_id']!=recipe['track_id']):
            raise JobConflict('Publication does not match the owned selected source')
        if job.state=='ready':
            if job.artifact_key!=key:
                raise JobConflict('Job already references a different artifact')
            result=artifacts.verify(key)
        else:
            attempts=store.stage_attempts(job.id,stream_id)
            if (job.state not in ('running','cancel_requested') or job.active_step!='validate'
                    or [a['step'] for a in attempts]!=[s['name'] for s in recipe['steps']]
                    or any(a['status']!='completed' for a in attempts)):
                raise JobConflict('All planned units must be settled before publication')
            result=artifacts.publish(workspace,recipe,title=selected['title'],
                                     artist=selected['artist'],album=selected.get('album',''))
            # Cancellation may arrive during copying: retain the artifact but
            # re-read its state so explicit cancellation of delivery stays intact.
            observed=store.get(job.id,stream_id)
            job=store.ready(job.id,run_token,result.key,expected_unit=job.unit_name,
                            expected_revision=observed.revision)
        warnings=artifacts.reconcile_catalog()
        return {'job':job,'artifact':result,'catalog_warnings':warnings}
