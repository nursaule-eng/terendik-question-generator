"""Storage layer.

SQLite is used by default (local work, tests). When DATABASE_URL is set to a
PostgreSQL URL (for example a free Neon/Supabase database), the same SQL runs
through a thin compatibility wrapper, so documents, questions, tests and
results survive restarts and redeploys of a free hosting instance.
"""
import hashlib
import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get('APP_DATA_DIR', ROOT / 'data'))

PG_NOW = "to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')"


def database_url():
    url = os.environ.get('DATABASE_URL', '').strip()
    if url.startswith('postgres://'):
        url = 'postgresql://' + url[len('postgres://'):]
    return url if url.startswith('postgresql://') else ''


def backend():
    return 'postgres' if database_url() else 'sqlite'


def _integrity_errors():
    errors = [sqlite3.IntegrityError]
    try:
        import psycopg
        errors.append(psycopg.errors.IntegrityError)
    except ImportError:
        pass
    return tuple(errors)


IntegrityError = _integrity_errors()


# ---------------------------------------------------------------- PostgreSQL
class Row(dict):
    """dict that also supports positional access, like sqlite3.Row."""

    def __init__(self, names, values):
        super().__init__(zip(names, values))
        self._values = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


def _row_factory(cursor):
    names = [c.name for c in cursor.description] if cursor.description else []
    return lambda values: Row(names, values)


_TRANSLATED = {}


def translate(sql):
    cached = _TRANSLATED.get(sql)
    if cached is not None:
        return cached
    text = sql.strip()
    if re.match(r'(?i)^BEGIN(\s+IMMEDIATE)?;?$', text):
        result = ''
    else:
        result = sql.replace('%', '%%').replace('?', '%s')
        result = re.sub(r'\browid\b', 'ctid', result)
        result = re.sub(r'\bCURRENT_TIMESTAMP\b', PG_NOW, result)
    _TRANSLATED[sql] = result
    return result


class _Empty:
    rowcount = 0

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def __iter__(self):
        return iter(())


class PgConnection:
    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, params=()):
        query = translate(sql)
        if not query:
            return _Empty()
        cursor = self.raw.cursor()
        cursor.execute(query, tuple(params))
        return cursor

    def executescript(self, script):
        for statement in script.split(';'):
            if statement.strip():
                self.raw.execute(statement)


import atexit

_pool = None
_pool_url = None
_pool_lock = threading.Lock()


def _pg_pool():
    global _pool, _pool_url
    url = database_url()
    with _pool_lock:
        if _pool is None or _pool_url != url:
            from psycopg_pool import ConnectionPool
            if _pool is not None:
                _pool.close()
            _pool = ConnectionPool(url, min_size=1, max_size=int(os.getenv('DB_POOL_SIZE', '6')),
                                   kwargs={'row_factory': _row_factory, 'autocommit': False,
                                           # Works behind PgBouncer/Neon pooler: no server-side prepared statements.
                                           'prepare_threshold': None},
                                   open=True, timeout=30, check=ConnectionPool.check_connection)
            _pool_url = url
        return _pool


@atexit.register
def close_pool():
    global _pool, _pool_url
    with _pool_lock:
        if _pool is not None:
            _pool.close()
        _pool = _pool_url = None


@contextmanager
def connect():
    if backend() == 'postgres':
        with _pg_pool().connection() as raw:
            db = PgConnection(raw)
            try:
                yield db
                raw.commit()
            except Exception:
                raw.rollback()
                raise
        return
    DATA.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA / 'app.sqlite3', timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ---------------------------------------------------------------- schema
SQLITE_SCHEMA = '''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, format TEXT NOT NULL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP, status TEXT NOT NULL,
  error TEXT, text TEXT DEFAULT '', topic_status TEXT DEFAULT 'pending', topic_error TEXT);
CREATE TABLE IF NOT EXISTS fragments (
  id INTEGER PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
  ordinal INTEGER NOT NULL, text TEXT NOT NULL, page_start INTEGER, page_end INTEGER);
CREATE TABLE IF NOT EXISTS topics (
  id INTEGER PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
  name TEXT NOT NULL, keywords TEXT NOT NULL, fragment_ids TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS questions (
  id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
  payload TEXT NOT NULL, auto_status TEXT NOT NULL, review_status TEXT DEFAULT 'pending',
  revision INTEGER DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
  status TEXT NOT NULL, requested INTEGER NOT NULL, created INTEGER DEFAULT 0,
  rejected INTEGER DEFAULT 0, message TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS attempts (
  id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
  snapshot TEXT NOT NULL, answers TEXT, self_scores TEXT DEFAULT '{}',
  status TEXT DEFAULT 'active', created_at TEXT DEFAULT CURRENT_TIMESTAMP, finished_at TEXT);
CREATE TABLE IF NOT EXISTS assignments (
  id TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL REFERENCES documents(id),
  title TEXT NOT NULL, settings TEXT NOT NULL DEFAULT '{}', snapshot TEXT NOT NULL,
  is_open INTEGER NOT NULL DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS files (
  document_id TEXT PRIMARY KEY REFERENCES documents(id), content BLOB NOT NULL, size INTEGER NOT NULL);
'''

PG_SCHEMA = f'''
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, format TEXT NOT NULL,
  created_at TEXT DEFAULT ({PG_NOW}), status TEXT NOT NULL,
  error TEXT, text TEXT DEFAULT '', topic_status TEXT DEFAULT 'pending', topic_error TEXT);
CREATE TABLE IF NOT EXISTS fragments (
  id BIGSERIAL PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
  ordinal INTEGER NOT NULL, text TEXT NOT NULL, page_start INTEGER, page_end INTEGER);
CREATE TABLE IF NOT EXISTS topics (
  id BIGSERIAL PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
  name TEXT NOT NULL, keywords TEXT NOT NULL, fragment_ids TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS questions (
  id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
  payload TEXT NOT NULL, auto_status TEXT NOT NULL, review_status TEXT DEFAULT 'pending',
  revision INTEGER DEFAULT 1, created_at TEXT DEFAULT ({PG_NOW}));
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
  status TEXT NOT NULL, requested INTEGER NOT NULL, created INTEGER DEFAULT 0,
  rejected INTEGER DEFAULT 0, message TEXT DEFAULT '', created_at TEXT DEFAULT ({PG_NOW}));
CREATE TABLE IF NOT EXISTS attempts (
  id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
  snapshot TEXT NOT NULL, answers TEXT, self_scores TEXT DEFAULT '{{}}',
  status TEXT DEFAULT 'active', created_at TEXT DEFAULT ({PG_NOW}), finished_at TEXT);
CREATE TABLE IF NOT EXISTS assignments (
  id TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE, document_id TEXT NOT NULL REFERENCES documents(id),
  title TEXT NOT NULL, settings TEXT NOT NULL DEFAULT '{{}}', snapshot TEXT NOT NULL,
  is_open INTEGER NOT NULL DEFAULT 1, created_at TEXT DEFAULT ({PG_NOW}));
CREATE TABLE IF NOT EXISTS files (
  document_id TEXT PRIMARY KEY REFERENCES documents(id), content BYTEA NOT NULL, size INTEGER NOT NULL)
'''

# Columns added after the first release. (table, column, declaration)
MIGRATIONS = [
    ('documents', 'language', "TEXT DEFAULT 'ru'"),
    ('documents', 'file_hash', 'TEXT'),
    ('documents', 'deleted_at', 'TEXT'),
    ('documents', 'warning', 'TEXT'),
    ('jobs', 'details', "TEXT DEFAULT '[]'"),
    ('jobs', 'params', "TEXT DEFAULT '{}'"),
    ('jobs', 'stage', "TEXT DEFAULT ''"),
    ('jobs', 'resume_at', 'TEXT'),
    ('jobs', 'cancel_requested', 'INTEGER DEFAULT 0'),
    ('jobs', 'attempts_used', 'INTEGER DEFAULT 0'),
    ('jobs', 'updated_at', 'TEXT'),
    ('questions', 'job_id', 'TEXT'),
    ('attempts', 'assignment_id', 'TEXT'),
    ('attempts', 'student_name', 'TEXT'),
    ('attempts', 'access_key', 'TEXT'),
    ('attempts', 'grades', "TEXT DEFAULT '{}'"),
    ('attempts', 'grading_status', "TEXT DEFAULT ''"),
    ('attempts', 'mode', "TEXT DEFAULT 'standard'"),
    ('attempts', 'adaptive', "TEXT DEFAULT '{}'"),
]

ACTIVE_JOB_STATES = ('queued', 'running', 'waiting', 'paused')


def columns(db, table):
    if backend() == 'postgres':
        return {r[0] for r in db.execute('SELECT column_name FROM information_schema.columns WHERE table_name=?', (table,))}
    return {r['name'] for r in db.execute(f'PRAGMA table_info({table})')}


def init_db():
    with connect() as db:
        db.executescript(PG_SCHEMA if backend() == 'postgres' else SQLITE_SCHEMA)
        for table, name, declaration in MIGRATIONS:
            if name not in columns(db, table):
                db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')
        db.execute('DROP INDEX IF EXISTS one_job_per_document')
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_active_job_per_document ON jobs(document_id) "
                   "WHERE status IN ('queued','running','waiting','paused')")
        db.execute('CREATE INDEX IF NOT EXISTS documents_hash ON documents(file_hash)')
        db.execute('CREATE INDEX IF NOT EXISTS fragments_document ON fragments(document_id)')
        db.execute('CREATE INDEX IF NOT EXISTS questions_document ON questions(document_id)')
        db.execute('CREATE INDEX IF NOT EXISTS attempts_document ON attempts(document_id)')
        db.execute('CREATE INDEX IF NOT EXISTS attempts_assignment ON attempts(assignment_id)')
        from app.services.language import detect_language
        for doc in db.execute('SELECT id,format,text,file_hash FROM documents').fetchall():
            if doc['text']:
                db.execute('UPDATE documents SET language=? WHERE id=?', (detect_language(doc['text']), doc['id']))
            if not doc['file_hash']:
                path = upload_path(doc)
                if path and path.is_file():
                    with path.open('rb') as content:
                        digest = hashlib.file_digest(content, 'sha256').hexdigest()
                    db.execute('UPDATE documents SET file_hash=? WHERE id=?', (digest, doc['id']))
        # Interrupted work is resumed by app.services.jobs.recover() after start-up.
        db.execute("UPDATE jobs SET status='queued', stage='Сервер перезапускался. Генерация продолжится автоматически.' WHERE status='running'")


def save_file(db, document_id, content):
    """Keep the original upload in the database: free hosting disks are wiped on restart,
    and the source viewer needs the real PDF pages."""
    if not db.execute('SELECT document_id FROM files WHERE document_id=?', (document_id,)).fetchone():
        db.execute('INSERT INTO files(document_id,content,size) VALUES(?,?,?)', (document_id, content, len(content)))


def load_file(db, document_id):
    row = db.execute('SELECT content FROM files WHERE document_id=?', (document_id,)).fetchone()
    return bytes(row[0]) if row else None


def dumps(value):
    return json.dumps(value, ensure_ascii=False)


REVIEW_MATCHES = ('fuzzy', 'parts', 'partial')


def verification(q):
    """Source-verification status shown to the teacher:
    confirmed — every check passed and every quote is found in the document;
    review — passed, but something needs a human look (level, inexact quote, edited, possible repeat);
    rejected — the document does not confirm the question or answer: never used in tests."""
    if q.get('auto_status') == 'failed':
        return 'rejected', list(q.get('validation', {}).get('reasons') or [])[:6]
    notes = []
    if q.get('auto_status') != 'passed':
        notes.append('Вопрос изменён после проверки — запустите проверку ещё раз.')
    if q.get('level_mismatch'):
        notes.append(f"Уровень: запрошен {q.get('requested_level')}, проверка определила {q.get('checked_level')}.")
    if any(s.get('match') in REVIEW_MATCHES for s in q.get('sources') or []):
        notes.append('Цитата найдена в документе не дословно — сверьте с источником.')
    if q.get('possible_duplicate'):
        notes.append('Похож на другой вопрос банка.')
    return ('review' if notes else 'confirmed'), notes


def question(row):
    result = json.loads(row['payload'])
    result.update({k: row[k] for k in ('id', 'document_id', 'auto_status', 'review_status', 'revision', 'created_at')})
    result['verification'], result['verification_notes'] = verification(result)
    return result


def upload_path(doc):
    if not re.fullmatch('[0-9a-f]{32}', doc['id']) or doc['format'] not in ('pdf', 'txt', 'docx'):
        return None
    return DATA / 'uploads' / (doc['id'] + '.' + doc['format'])
