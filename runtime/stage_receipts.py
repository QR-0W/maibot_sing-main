"""Small immutable completion receipts. Caller owns the job-directory lock.

No model imports. Hash checks detect corruption, not malicious concurrent writers.
A receipt proves byte identity, not audio quality; validate media before seal().
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid


class CheckpointError(RuntimeError):
    pass


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise CheckpointError('Invalid identity digest')
    return value


class StageReceipts:
    def __init__(self, root, recipe_sha256, max_bytes=1024**3):
        self.root = Path(root)
        self.recipe = _digest(recipe_sha256)
        if type(max_bytes) is not int or not 1 <= max_bytes <= 2 * 1024**3:
            raise CheckpointError('Invalid stage byte budget')
        self.max_bytes = max_bytes
        if not self.root.is_absolute() or not self.root.is_dir():
            raise CheckpointError('Workspace must be an existing absolute directory')
        self._safe(self.root)
        self.directory = self.root / '.receipts'
        self._safe(self.directory)
        self.directory.mkdir(mode=0o700, exist_ok=True)

    @staticmethod
    def _safe(path):
        for component in (path, *path.parents):
            if component.is_symlink():
                raise CheckpointError('Symlink refused')

    def _receipt(self, stage):
        if not isinstance(stage, str) or not re.fullmatch('[a-z][a-z0-9_-]{0,63}', stage):
            raise CheckpointError('Invalid stage name')
        path = self.directory / (stage + '.json')
        self._safe(path)
        return path

    def _files(self, names):
        if not isinstance(names, list) or not 1 <= len(names) <= 64 or len(set(names)) != len(names):
            raise CheckpointError('Invalid output list')
        records, total = {}, 0
        for name in names:
            if not isinstance(name, str) or not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_./-]{0,159}', name):
                raise CheckpointError('Invalid artifact name')
            if any(part in ('', '.', '..', '.receipts') for part in name.split('/')) or name.endswith('.part'):
                raise CheckpointError('Unsafe or unfinished artifact')
            path = self.root / name
            self._safe(path)
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
                raise CheckpointError('Artifact is not a nonempty regular file')
            total += info.st_size
            if total > self.max_bytes:
                raise CheckpointError('Stage exceeds byte budget')
            records[name] = {'bytes': info.st_size, 'sha256': sha256(path)}
        return records

    @staticmethod
    def _inputs(inputs):
        if not isinstance(inputs, dict) or len(inputs) > 64:
            raise CheckpointError('Invalid input fingerprints')
        for key, value in inputs.items():
            if (not isinstance(key, str) or not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_./-]{0,159}', key)
                    or any(part in ('', '.', '..', '.receipts') for part in key.split('/'))
                    or key.endswith('.part')):
                raise CheckpointError('Invalid input name')
            _digest(value)
        return dict(inputs)

    def verify(self, stage, inputs):
        path = self._receipt(stage)
        inputs = self._inputs(inputs)
        if path.is_symlink():
            raise CheckpointError('Symlinked receipt refused')
        if not path.exists():
            return None
        try:
            if path.stat().st_size > 32768:
                raise CheckpointError('Oversized receipt')
            document = json.loads(path.read_text(encoding='utf-8'))
            if (set(document) != {'schema','recipe','stage','inputs','outputs'} or
                    type(document['schema']) is not int or document['schema'] != 1 or
                    document['recipe'] != self.recipe or document['stage'] != stage or
                    document['inputs'] != inputs or not isinstance(document['outputs'], dict)):
                raise CheckpointError('Receipt identity mismatch')
            actual = self._files(list(document['outputs']))
            if actual != document['outputs']:
                raise CheckpointError('Artifact bytes changed')
            return document
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise CheckpointError('Unreadable or corrupt stage receipt') from exc

    def archive_incomplete(self, stage, outputs, *, unit_name, confirmed_stopped):
        """Explicit retry preparation; never silently replace an unsealed output.

        Caller must hold exclusive job ownership and its inference lock, query
        the exact prior systemd unit, and pass True only for a proven stopped
        unit. Unknown/active is not permission to move anything. Previous
        completed receipts are never archived or invalidated.
        """
        receipt = self._receipt(stage)
        if not isinstance(unit_name, str) or not re.fullmatch('maibot-sing-[a-z0-9_-]{12,110}', unit_name):
            raise CheckpointError('Invalid previous unit identity')
        if confirmed_stopped is not True:
            raise CheckpointError('Prior worker inactivity has not been proven')
        if receipt.exists() or receipt.is_symlink():
            raise CheckpointError('Completed or corrupt receipt must not be archived')
        if not isinstance(outputs, (tuple, list)) or not outputs or len(set(outputs)) != len(outputs):
            raise CheckpointError('Invalid interrupted stage outputs')
        pending = []
        for name in outputs:
            if (not isinstance(name, str) or not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_./-]{0,159}', name)
                    or any(part in ('', '.', '..', '.receipts', '.incomplete') for part in name.split('/'))
                    or name.endswith('.part')):
                raise CheckpointError('Unsafe interrupted output')
            path = self.root / name
            self._safe(path)
            if path.exists():
                if not stat.S_ISREG(path.stat().st_mode):
                    raise CheckpointError('Interrupted output is not a regular file')
                pending.append((name, path))
        if not pending:
            return None
        folder = self.root / '.incomplete' / stage / uuid.uuid4().hex
        self._safe(folder)
        folder.mkdir(parents=True, mode=0o700, exist_ok=False)
        archived = []
        # os.rename never overwrites in a unique private folder. If a crash
        # interrupts this move, both already-archived and remaining outputs
        # remain available for an administrator to reconcile by byte hash.
        for name, path in pending:
            dest = folder / name
            dest.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            os.rename(path, dest)
            archived.append(name)
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return folder

    def seal(self, stage, inputs, outputs):
        destination = self._receipt(stage)
        document = {'schema':1, 'recipe':self.recipe, 'stage':stage,
                    'inputs':self._inputs(inputs), 'outputs':self._files(outputs)}
        # A receipt must not become durable before the bytes it certifies.
        for name in outputs:
            with (self.root / name).open('rb') as artifact:
                os.fsync(artifact.fileno())
        for parent in { (self.root / name).parent for name in outputs }:
            fd = os.open(parent, os.O_DIRECTORY | os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        if destination.exists():
            if self.verify(stage, inputs) != document:
                raise CheckpointError('Cannot overwrite a completed stage')
            return document
        data = json.dumps(document, sort_keys=True, separators=(',', ':')).encode()
        if len(data) > 32768:
            raise CheckpointError('Oversized receipt')
        temporary = self.directory / (uuid.uuid4().hex + '.part')
        try:
            with temporary.open('xb') as stream:
                os.chmod(temporary, 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, destination)  # Exclusive publication; never replace.
            fd = os.open(self.directory, os.O_DIRECTORY | os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)
        return document
