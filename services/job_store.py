"""Durable job and delivery ledger, independent of SDK RPCs and ML runtimes.

Synchronous transactional storage. Async adapters MUST call through to_thread.
No network, process launch, media validation or automatic retries occur here.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid


class JobConflict(RuntimeError):
    """A stale actor, conflicting request or invalid transition cannot write."""


class JobNotFound(LookupError):
    """Missing and wrong-stream jobs are deliberately indistinguishable."""


@dataclass(frozen=True)
class Job:
    id: str
    stream_id: str
    request_token: str
    request: Dict[str, Any]
    selected_source: Optional[Dict[str, Any]]
    offer_id: Optional[str]
    selected_row: Optional[int]
    state: str
    stage: str
    revision: int
    run_token: Optional[str]
    unit_name: Optional[str]
    chunk_done: int
    chunk_total: int
    artifact_key: Optional[str]
    delivery_state: str
    delivery_token: Optional[str]
    consent_event: Optional[str]
    message_id: Optional[str]
    error: Optional[Dict[str, Any]]


def _text(value: str, label: str, length: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > length or any(ord(ch) < 32 for ch in value):
        raise ValueError('Invalid ' + label)
    return value


def _json(value: Dict[str, Any]) -> str:
    if not isinstance(value, dict):
        raise ValueError('Expected object')
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
    if len(data.encode('utf-8')) > 16384:
        raise ValueError('Job metadata exceeds 16KiB')
    return data


class JobStore:
    """Single-media-slot ledger; delivery state is not render state.

    request_token must come from a stable trusted invocation/message identity,
    not a model-invented identifier. An identical token with changed content is
    a conflict. auto_reply requires a persisted explicit user consent event.
    """
    def __init__(self, database: Path, max_pending: int = 3):
        if not database.is_absolute() or not 1 <= max_pending <= 16:
            raise ValueError('Invalid database location or queue capacity')
        if any(p.is_symlink() for p in (database, *database.parents)):
            raise ValueError('Symlinked ledger refused')
        self.path, self.max_pending = database, max_pending
        database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(database, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with self._connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    stream_id TEXT NOT NULL,
                    request_token TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    selected_source_json TEXT, offer_id TEXT, selected_row INTEGER,
                    state TEXT NOT NULL CHECK (state IN
                      ('searching','queued','running','needs_selection','ready','failed','cancel_requested','cancelled','interrupted')),
                    stage TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0,
                    run_token TEXT, unit_name TEXT,
                    chunk_done INTEGER NOT NULL DEFAULT 0,
                    chunk_total INTEGER NOT NULL DEFAULT 0,
                    artifact_key TEXT,
                    delivery_state TEXT NOT NULL CHECK (delivery_state IN
                      ('not_requested','pending','dispatching','sent','unknown','failed','cancelled')),
                    delivery_token TEXT, consent_event TEXT, message_id TEXT,
                    error_json TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(stream_id, request_token)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_media_worker ON jobs((1))
                  WHERE state IN ('running','cancel_requested');
                CREATE TABLE IF NOT EXISTS offers (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL,
                    candidates_json TEXT NOT NULL, expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                    event TEXT NOT NULL, revision INTEGER NOT NULL, created_at REAL NOT NULL
                );
            ''')

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _job(row: sqlite3.Row) -> Job:
        values = {field: row[field] for field in Job.__dataclass_fields__ if field not in ('request','error','selected_source')}
        return Job(**values, request=json.loads(row['request_json']),
                   selected_source=json.loads(row['selected_source_json']) if row['selected_source_json'] else None,
                   error=json.loads(row['error_json']) if row['error_json'] else None)

    @staticmethod
    def _owned(db: sqlite3.Connection, job_id: str, stream_id: str) -> sqlite3.Row:
        row = db.execute('SELECT * FROM jobs WHERE id=? AND stream_id=?', (job_id,stream_id)).fetchone()
        if row is None:
            raise JobNotFound('No job available to this conversation')
        return row

    @staticmethod
    def _change(db: sqlite3.Connection, row: sqlite3.Row, event: str, **values: Any) -> Job:
        # Field names are exclusively supplied by methods below, never user data.
        columns = ', '.join(name+'=?' for name in values)
        db.execute('UPDATE jobs SET '+columns+', revision=revision+1, updated_at=? WHERE id=? AND revision=?',
                   (*values.values(),time.time(),row['id'],row['revision']))
        db.execute('INSERT INTO events(job_id,event,revision,created_at) VALUES(?,?,?,?)',
                   (row['id'],event,row['revision']+1,time.time()))
        return JobStore._job(db.execute('SELECT * FROM jobs WHERE id=?',(row['id'],)).fetchone())

    def submit(self, stream_id: str, request_token: str, request: Dict[str, Any], *,
               auto_reply: bool = False, consent_event: Optional[str] = None) -> Tuple[Job, bool]:
        _text(stream_id,'stream'); _text(request_token,'request token')
        if type(auto_reply) is not bool:
            raise ValueError('auto_reply must be bool')
        if auto_reply:
            _text(consent_event,'explicit auto-reply consent')
        elif consent_event is not None:
            raise ValueError('Consent event supplied without auto_reply')
        document = _json(request)
        digest = hashlib.sha256(document.encode()).hexdigest()
        with self._transaction() as db:
            row = db.execute('SELECT * FROM jobs WHERE stream_id=? AND request_token=?',
                             (stream_id,request_token)).fetchone()
            if row:
                if row['request_hash'] != digest or row['consent_event'] != consent_event:
                    raise JobConflict('Same request token has different contents or consent')
                return self._job(row), False
            count = db.execute("SELECT count(*) FROM jobs WHERE state IN ('searching','queued','running','cancel_requested','needs_selection')").fetchone()[0]
            if count >= self.max_pending:
                raise JobConflict('Job queue is full')
            job_id, now = uuid.uuid4().hex, time.time()
            db.execute('''INSERT INTO jobs(id,stream_id,request_token,request_json,request_hash,state,stage,
                          delivery_state,consent_event,created_at,updated_at) VALUES(?,?,?,?,?,'searching','searching',?,?,?,?)''',
                       (job_id,stream_id,request_token,document,digest,'pending' if auto_reply else 'not_requested',consent_event,now,now))
            db.execute('INSERT INTO events(job_id,event,revision,created_at) VALUES(?,?,0,?)',(job_id,'submitted',now))
            return self._job(db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()), True

    def get(self, job_id: str, stream_id: str) -> Job:
        with self._connect() as db:
            return self._job(self._owned(db,job_id,stream_id))

    def offer(self, job_id: str, stream_id: str, candidates: list, *, expected_revision: int, ttl_s: int = 600) -> Job:
        """Save ranked, typed catalogue snapshot without occupying the media slot."""
        from .source_offer import snapshot
        document = _json({'items': snapshot(candidates)})
        if type(ttl_s) is not int or not 30 <= ttl_s <= 1800:
            raise ValueError('Invalid offer expiry')
        with self._transaction() as db:
            row = self._owned(db,job_id,stream_id)
            if row['revision'] != expected_revision or row['state'] not in ('searching','needs_selection'):
                raise JobConflict('Source search is stale or already selected')
            offer_id = uuid.uuid4().hex
            db.execute('INSERT INTO offers VALUES(?,?,?,?)',(offer_id,job_id,document,time.time()+ttl_s))
            return self._change(db,row,'offer_created',state='needs_selection',stage='awaiting_selection',offer_id=offer_id)

    def choices(self, job_id: str, stream_id: str) -> Dict[str, Any]:
        with self._connect() as db:
            row = self._owned(db,job_id,stream_id)
            offer = db.execute('SELECT * FROM offers WHERE id=? AND job_id=?',(row['offer_id'],job_id)).fetchone()
            if not offer:
                raise JobConflict('No candidate snapshot available')
            return {'offer_id':offer['id'], 'expires_at':offer['expires_at'],
                    'items':json.loads(offer['candidates_json'])['items']}

    def select(self, job_id: str, stream_id: str, offer_id: str, number: int) -> Job:
        """Both tool default-first and command manual selection use this operation.

        Only the displayed snapshot can be selected. The media adapter refreshes
        playback credentials by provider+track_id, NEVER by a fresh keyword search.
        """
        if type(number) is not int or number < 1:
            raise ValueError('Candidate number must be a positive integer')
        with self._transaction() as db:
            row = self._owned(db,job_id,stream_id)
            if row['offer_id'] != offer_id:
                raise JobConflict('Candidate snapshot has changed')
            if row['selected_row'] is not None:
                if row['selected_row'] == number:
                    return self._job(row)  # Duplicate click cannot queue another run.
                raise JobConflict('Source already bound; cancel before switching recordings')
            if row['state'] != 'needs_selection':
                raise JobConflict('Job is no longer awaiting selection')
            offer = db.execute('SELECT * FROM offers WHERE id=? AND job_id=?',(offer_id,job_id)).fetchone()
            if offer is None or time.time() >= offer['expires_at']:
                raise JobConflict('Candidate snapshot expired; refresh search explicitly')
            items = json.loads(offer['candidates_json'])['items']
            if number > len(items):
                raise ValueError('Candidate number is outside displayed list')
            selected = items[number-1]
            if selected['availability'] in ('requires_login','unavailable','over_limit'):
                raise JobConflict('Selected item is '+selected['availability']+'; no alternative substituted')
            return self._change(db,row,'source_selected',state='queued',stage='queued',selected_row=number,
                                selected_source_json=_json(selected))

    def claim_next(self) -> Optional[Job]:
        """Persist a run identity BEFORE a runner launches the exact named unit."""
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM jobs WHERE state IN ('running','cancel_requested')").fetchone():
                return None
            row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            return self._change(db,row,'run_claimed',state='running',stage='starting',
                                run_token=token,unit_name='maibot-sing-'+token)

    @staticmethod
    def _runner(db: sqlite3.Connection, job_id: str, run_token: str) -> sqlite3.Row:
        row = db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if row is None or row['run_token'] != run_token or row['state'] not in ('running','cancel_requested'):
            raise JobConflict('Stale or inactive worker cannot update job')
        return row

    def progress(self, job_id: str, run_token: str, *, stage: str, done: int, total: int) -> Job:
        stages = ('starting','resolving','downloading','separating','converting','encoding','validating','publishing')
        if stage not in stages or type(done) is not int or type(total) is not int or not 0 <= done <= total <= 60:
            raise ValueError('Invalid stage progress')
        with self._transaction() as db:
            row = self._runner(db,job_id,run_token)
            if stages.index(stage) < stages.index(row['stage']) or done < row['chunk_done']:
                raise JobConflict('Progress cannot move backwards in one worker run')
            if row['chunk_total'] and total != row['chunk_total']:
                raise JobConflict('Chunk count changed within frozen plan')
            return self._change(db,row,'progress',stage=stage,chunk_done=done,chunk_total=total)

    def ready(self, job_id: str, run_token: str, artifact_key: str) -> Job:
        """Caller must validate immutable committed artifact bytes before this call."""
        if not isinstance(artifact_key,str) or not re.fullmatch('[0-9a-f]{64}',artifact_key):
            raise ValueError('Invalid committed artifact identity')
        with self._transaction() as db:
            row = self._runner(db,job_id,run_token)
            # If cancel lost the race with media commit, keep the artifact but
            # preserve cancelled delivery: never send after cancellation.
            return self._change(db,row,'artifact_ready',state='ready',stage='completed',artifact_key=artifact_key)

    def finish_failure(self, job_id: str, run_token: str, error: Dict[str, Any], *,
                       interrupted: bool = False) -> Job:
        """Runner calls only after it proves the named unit has stopped.

        A missing/unknown service state is NOT proof. Checkpoints remain outside
        this ledger and are never deleted here, including on interruption.
        """
        _text(error.get('code'),'error code'); _text(error.get('message'),'error message',1500)
        document = _json(error)
        with self._transaction() as db:
            row = self._runner(db,job_id,run_token)
            state = 'cancelled' if row['state']=='cancel_requested' else 'interrupted' if interrupted else 'failed'
            return self._change(db,row,'worker_stopped',state=state,error_json=document)

    def cancel(self, job_id: str, stream_id: str) -> Job:
        with self._transaction() as db:
            row = self._owned(db,job_id,stream_id)
            if row['delivery_state'] in ('dispatching','unknown','sent'):
                raise JobConflict('Delivery was attempted; cannot promise to retract it')
            state = 'cancel_requested' if row['state'] in ('running','cancel_requested') else row['state'] if row['state']=='ready' else 'cancelled'
            return self._change(db,row,'cancel_requested',state=state,delivery_state='cancelled')

    def claim_delivery(self, job_id: str, stream_id: str) -> Optional[Job]:
        """One explicit consent → at most one automatic transport attempt."""
        with self._transaction() as db:
            row = self._owned(db,job_id,stream_id)
            if row['state'] != 'ready' or row['delivery_state'] != 'pending':
                return None
            if not row['consent_event']:
                raise JobConflict('No persisted authorization to auto-reply')
            return self._change(db,row,'delivery_claimed',delivery_state='dispatching',delivery_token=uuid.uuid4().hex)

    def delivery_result(self, job_id: str, delivery_token: str, outcome: str, *, message_id: Optional[str] = None) -> Job:
        """Only a positive acknowledgement proves sent. Ambiguous failures remain unknown."""
        if outcome not in ('sent','failed','unknown'):
            raise ValueError('Invalid delivery outcome')
        if message_id is not None:
            _text(message_id,'platform message id')
        with self._transaction() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
            if row is None or row['delivery_token'] != delivery_token or row['delivery_state'] not in ('dispatching','unknown'):
                raise JobConflict('Stale delivery acknowledgement')
            return self._change(db,row,'delivery_'+outcome,delivery_state=outcome,message_id=message_id)

    def recover_dispatches(self) -> int:
        """Coordinator startup only, after exclusive host ownership is acquired.

        Transport may already have accepted any old dispatch. Mark unknown,
        never reset to pending. Positive late acknowledgements can still land.
        """
        with self._transaction() as db:
            rows = db.execute("SELECT * FROM jobs WHERE delivery_state='dispatching'").fetchall()
            for row in rows:
                self._change(db,row,'delivery_uncertain_after_restart',delivery_state='unknown')
            return len(rows)

    def history(self, job_id: str, stream_id: str) -> list:
        with self._connect() as db:
            self._owned(db,job_id,stream_id)
            return [dict(row) for row in db.execute('SELECT event,revision,created_at FROM events WHERE job_id=? ORDER BY seq',(job_id,))]
