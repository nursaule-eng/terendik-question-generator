import json
import os
import sqlite3
import hashlib
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get('APP_DATA_DIR', ROOT / 'data'))


@contextmanager
def connect():
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


def init_db():
    with connect() as db:
        db.executescript('''
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
        CREATE UNIQUE INDEX IF NOT EXISTS one_job_per_document ON jobs(document_id)
          WHERE status IN ('queued','running');
        CREATE TABLE IF NOT EXISTS attempts (
          id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
          snapshot TEXT NOT NULL, answers TEXT, self_scores TEXT DEFAULT '{}',
          status TEXT DEFAULT 'active', created_at TEXT DEFAULT CURRENT_TIMESTAMP, finished_at TEXT);
        ''')
        columns = {r['name'] for r in db.execute('PRAGMA table_info(documents)')}
        for name, declaration in (('language', "TEXT DEFAULT 'ru'"), ('file_hash', 'TEXT'), ('deleted_at', 'TEXT')):
            if name not in columns:
                db.execute(f'ALTER TABLE documents ADD COLUMN {name} {declaration}')
        job_columns = {r['name'] for r in db.execute('PRAGMA table_info(jobs)')}
        if 'details' not in job_columns:
            db.execute("ALTER TABLE jobs ADD COLUMN details TEXT DEFAULT '[]'")
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
        db.execute('CREATE INDEX IF NOT EXISTS documents_hash ON documents(file_hash)')
        # A local restart cannot resume an in-flight external request safely.
        db.execute("UPDATE jobs SET status='error', message='Сервер перезапущен. Повторите генерацию.' WHERE status IN ('queued','running')")
        db.execute("UPDATE documents SET status='error', error='Обработка прервана перезапуском. Загрузите файл повторно.' WHERE status='processing'")
        db.execute("UPDATE documents SET topic_status='error', topic_error='Поиск тем прерван. Повторите поиск.' WHERE topic_status='processing'")


def dumps(value):
    return json.dumps(value, ensure_ascii=False)


def question(row):
    result = json.loads(row['payload'])
    result.update({k: row[k] for k in ('id', 'document_id', 'auto_status', 'review_status', 'revision', 'created_at')})
    return result


def upload_path(doc):
    import re
    if not re.fullmatch('[0-9a-f]{32}', doc['id']) or doc['format'] not in ('pdf', 'txt'):
        return None
    return DATA / 'uploads' / (doc['id'] + '.' + doc['format'])
