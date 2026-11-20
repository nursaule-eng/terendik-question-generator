import hashlib
import json
import os
import random
import secrets
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import database as store
from app.database import connect, dumps, question
from app.schemas import (AssignmentRequest, AssignmentUpdate, Candidate, Edit, Generate, Review, SelfScores,
                         StudentStart, Submit, TestRequest)
from app.services import export as exporter
from app.services.documents import chunks, extract, legacy_font_used
from app.services.generation import is_running_here, run_generation, summary
from app.services.jobs import recover
from app.services.llm import LLM, ModelError, configuration
from app.services.testing import grade_attempt, needs_grading, present, statistics, validate_answers
from app.services.topics import find_topics, read_topics
from app.services.validation import duplicate, evaluate, annotate_duplicates
from app.services.language import detect_language
from app.services.options import shuffle_options

ASSETS = Path(__file__).parent / 'static'
MAX_BYTES = 20 * 1024 * 1024
FORMATS = ('pdf', 'txt', 'docx')


@asynccontextmanager
async def lifespan(app):
    store.init_db()
    if os.getenv('APP_DISABLE_RECOVERY') != '1':
        recover()
    yield


app = FastAPI(title='Вопросы по материалу', lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost', '127.0.0.1', 'testserver'] + [
    h.strip() for h in (os.getenv('APP_ALLOWED_HOSTS', '') + ',' + os.getenv('RENDER_EXTERNAL_HOSTNAME', '')).split(',') if h.strip()
])
app.mount('/static', StaticFiles(directory=ASSETS), name='static')


@app.middleware('http')
async def local_only(request: Request, call_next):
    # No auth: this is a local teacher tool. Reject cross-origin mutations.
    origin = request.headers.get('origin')
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and origin and origin != str(request.base_url).rstrip('/'):
        return JSONResponse({'detail': 'Запрос разрешён только из интерфейса этого приложения.'}, status_code=403)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    return response


@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc):
    return JSONResponse({'detail': 'Проверьте заполнение полей: ' + '; '.join('.'.join(map(str, e['loc'])) + ': ' + e['msg'] for e in exc.errors())}, status_code=422)


@app.exception_handler(ModelError)
async def model_failure(request, exc):
    return JSONResponse({'detail': str(exc)}, status_code=503)


def require_document(db, doc_id):
    row = db.execute('SELECT * FROM documents WHERE id=? AND deleted_at IS NULL', (doc_id,)).fetchone()
    if not row:
        raise HTTPException(404, 'Документ не найден.')
    return row


def require_question(db, question_id):
    row = db.execute('SELECT * FROM questions WHERE id=?', (question_id,)).fetchone()
    if not row:
        raise HTTPException(404, 'Вопрос не найден.')
    return row


def require_ready(db, doc_id, topic_id=None):
    doc = require_document(db, doc_id)
    if doc['status'] != 'ready':
        raise HTTPException(409, 'Сначала дождитесь успешного извлечения текста.')
    if topic_id is not None and not db.execute('SELECT id FROM topics WHERE id=? AND document_id=?', (topic_id, doc_id)).fetchone():
        raise HTTPException(422, 'Тема не принадлежит выбранному документу. Обновите список тем.')


@app.get('/')
def home():
    return FileResponse(ASSETS / 'index.html')


@app.get('/api/config')
def config():
    return configuration()


@app.post('/api/model/check')
def check_model():
    return LLM().probe()


@app.get('/api/documents')
def documents(archived: bool = False):
    with connect() as db:
        clause = 'IS NOT NULL' if archived else 'IS NULL'
        return [dict(r) for r in db.execute('SELECT id,name,format,created_at,status,error,topic_status,topic_error,language,deleted_at,warning FROM documents WHERE deleted_at ' + clause + ' ORDER BY created_at DESC,rowid DESC')]


def process_document(doc_id, path, extension):
    try:
        pages = extract(path, extension)
        warning = None
        try:
            if legacy_font_used(path, extension):
                warning = ('В файле использован старый казахский шрифт (KZ). Буквы ә, ғ, қ, ң, ө, ұ, ү, һ восстановлены '
                           'автоматически; проверьте цитаты в нескольких вопросах.')
        except ValueError:
            pass
        fragments = chunks(pages)
        if not fragments:
            raise ValueError('Не найден доступный текст.')
        with connect() as db:
            db.execute('DELETE FROM fragments WHERE document_id=?', (doc_id,))
            for f in fragments:
                db.execute('INSERT INTO fragments(document_id,ordinal,text,page_start,page_end) VALUES(?,?,?,?,?)',
                           (doc_id, f['ordinal'], f['text'], f['page_start'], f['page_end']))
            text = '\n\n'.join(t for _, t in pages)
            db.execute("UPDATE documents SET text=?,language=?,status='ready',topic_status='processing',warning=? WHERE id=?", (text, detect_language(text), warning, doc_id))
        find_topics(doc_id)
    except Exception as exc:
        message = str(exc) if isinstance(exc, ValueError) else 'Не удалось обработать файл. Проверьте формат и повторите загрузку.'
        with connect() as db:
            db.execute("UPDATE documents SET status='error',error=?,topic_status='error' WHERE id=?", (message, doc_id))


@app.post('/api/documents', status_code=201)
async def upload(file: UploadFile, background_tasks: BackgroundTasks):
    name = (file.filename or 'Без названия').replace('\\', '/').split('/')[-1][:250]
    extension = Path(name).suffix.lower().lstrip('.')
    if extension not in FORMATS:
        raise HTTPException(415, 'Поддерживаются PDF с текстовым слоем, DOCX и TXT.')
    doc_id = uuid.uuid4().hex
    folder = store.DATA / 'uploads'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (doc_id + '.' + extension)
    size = 0
    digest = hashlib.sha256()
    try:
        with path.open('wb') as output:
            while block := await file.read(65536):
                size += len(block)
                if size > MAX_BYTES:
                    raise HTTPException(413, 'Максимальный размер файла — 20 МБ.')
                output.write(block)
                digest.update(block)
        if size == 0:
            raise HTTPException(422, 'Файл пуст.')
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT id,status,name FROM documents WHERE file_hash=? AND deleted_at IS NULL ORDER BY rowid LIMIT 1', (digest.hexdigest(),)).fetchone()
            if existing:
                path.unlink(missing_ok=True)
                return JSONResponse({'id': existing['id'], 'status': existing['status'], 'already_uploaded': True,
                                     'message': 'Этот файл уже загружен. Открыт существующий документ: ' + existing['name']}, status_code=200)
            db.execute("INSERT INTO documents(id,name,format,status,file_hash) VALUES(?,?,?,'processing',?)", (doc_id, name, extension, digest.hexdigest()))
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    background_tasks.add_task(process_document, doc_id, path, extension)
    return {'id': doc_id, 'status': 'processing'}


@app.delete('/api/documents/{doc_id}')
def archive_document(doc_id: str):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        doc = require_document(db, doc_id)
        active = db.execute("SELECT id FROM jobs WHERE document_id=? AND status IN ('queued','running','waiting','paused')", (doc_id,)).fetchone()
        if active or doc['status'] == 'processing' or doc['topic_status'] == 'processing':
            raise HTTPException(409, 'Дождитесь завершения обработки или генерации.')
        db.execute('UPDATE documents SET deleted_at=CURRENT_TIMESTAMP WHERE id=?', (doc_id,))
    return {'message': 'Документ перемещён в корзину вместе с банком. Попытки сохранены; документ можно восстановить.'}


@app.post('/api/documents/{doc_id}/restore')
def restore_document(doc_id: str):
    with connect() as db:
        result = db.execute('UPDATE documents SET deleted_at=NULL WHERE id=? AND deleted_at IS NOT NULL', (doc_id,))
        if not result.rowcount:
            raise HTTPException(404, 'Документ в корзине не найден.')
    return {'message': 'Документ восстановлен.'}


@app.get('/api/documents/{doc_id}')
def document(doc_id: str):
    with connect() as db:
        doc = dict(require_document(db, doc_id))
        doc['text_length'] = len(doc.pop('text'))
        doc['fragment_count'] = db.execute('SELECT count(*) FROM fragments WHERE document_id=?', (doc_id,)).fetchone()[0]
        doc['topics'] = read_topics(db, doc_id)
        return doc


@app.post('/api/documents/{doc_id}/topics', status_code=202)
def topics(doc_id: str, background_tasks: BackgroundTasks):
    if not configuration()['configured']:
        raise ModelError('Заполните настройки модели в .env и перезапустите сервер.')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        require_ready(db, doc_id)
        if db.execute("SELECT id FROM jobs WHERE document_id=? AND status IN ('queued','running','waiting')", (doc_id,)).fetchone():
            raise HTTPException(409, 'Дождитесь окончания генерации.')
        row = require_document(db, doc_id)
        if row['topic_status'] == 'processing':
            raise HTTPException(409, 'Темы уже обрабатываются.')
        db.execute("UPDATE documents SET topic_status='processing',topic_error=NULL WHERE id=?", (doc_id,))
    background_tasks.add_task(find_topics, doc_id)
    return {'status': 'processing'}


@app.post('/api/generate', status_code=202)
def generate(params: Generate, background_tasks: BackgroundTasks):
    if not configuration()['configured']:
        raise ModelError('Модель не настроена. Заполните три значения LLM_* в .env и перезапустите сервер.')
    job_id = uuid.uuid4().hex
    try:
        with connect() as db:
            require_ready(db, params.document_id, params.topic_id)
            if require_document(db, params.document_id)['topic_status'] == 'processing':
                raise HTTPException(409, 'Дождитесь завершения поиска тем.')
            db.execute("INSERT INTO jobs(id,document_id,status,requested,params,stage) VALUES(?,?,'queued',?,?,?)",
                       (job_id, params.document_id, params.count, params.model_dump_json(), 'Подготовка: читаем материал.'))
    except store.IntegrityError:
        raise HTTPException(409, 'Генерация для этого документа уже выполняется или стоит на паузе. Продолжите или остановите её.') from None
    background_tasks.add_task(run_generation, job_id, params)
    return {'id': job_id}


@app.get('/api/jobs')
def jobs(document_id: str):
    with connect() as db:
        result = []
        for row in db.execute('SELECT * FROM jobs WHERE document_id=? ORDER BY created_at DESC,rowid DESC LIMIT 10', (document_id,)):
            item = dict(row)
            item['details'] = json.loads(item['details'] or '[]')
            try:
                item['params'] = json.loads(item.get('params') or '{}')
            except json.JSONDecodeError:
                item['params'] = {}
            if item['status'] == 'done':
                if not item['details'] and len(item['message']) > 250:
                    item['details'] = [item['message']]
                item['message'] = summary(item['requested'], item['created'], item['rejected'])
                if item['created'] < item['requested']:
                    item['message'] += ' Подтверждённых неповторяющихся вопросов недостаточно; проверьте подробности.'
            result.append(item)
        return result


@app.post('/api/jobs/{job_id}/cancel')
def cancel_job(job_id: str):
    with connect() as db:
        row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Запуск не найден.')
        if row['status'] not in store.ACTIVE_JOB_STATES:
            raise HTTPException(409, 'Этот запуск уже завершён.')
        if row['status'] == 'paused' or (row['status'] in ('queued', 'waiting') and not is_running_here(job_id)):
            db.execute("UPDATE jobs SET status='cancelled',cancel_requested=1,stage='',message=? WHERE id=?",
                       (f'Генерация остановлена. Сохранено прошедших проверку: {row["created"]} из {row["requested"]}.', job_id))
            return {'message': 'Генерация остановлена. Созданные вопросы сохранены в банке.'}
        db.execute("UPDATE jobs SET cancel_requested=1,stage='Останавливаем после текущего вопроса…' WHERE id=?", (job_id,))
    return {'message': 'Остановка запрошена: текущий вопрос будет завершён и сохранён.'}


@app.post('/api/jobs/{job_id}/resume', status_code=202)
def resume_job(job_id: str, background_tasks: BackgroundTasks):
    if not configuration()['configured']:
        raise ModelError('Модель не настроена.')
    with connect() as db:
        row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Запуск не найден.')
        if row['status'] not in ('paused', 'error'):
            raise HTTPException(409, 'Продолжить можно только приостановленную или прерванную генерацию.')
        if row['status'] == 'error' and not json.loads(row['params'] or '{}'):
            raise HTTPException(409, 'Для этого старого запуска нет сохранённых параметров. Запустите новую генерацию.')
        try:
            db.execute("UPDATE jobs SET status='queued',resume_at=NULL,cancel_requested=0,stage='Продолжаем генерацию.' WHERE id=?", (job_id,))
        except store.IntegrityError:
            raise HTTPException(409, 'Для документа уже идёт другая генерация.') from None
    background_tasks.add_task(run_generation, job_id)
    return {'message': 'Генерация продолжена с места остановки.'}


@app.get('/api/documents/{doc_id}/capacity')
def capacity(doc_id: str, topic_id: int | None = None):
    """Rough estimate of how many distinct, source-backed questions the material supports."""
    with connect() as db:
        require_document(db, doc_id)
        fragments = [dict(r) for r in db.execute('SELECT id,text FROM fragments WHERE document_id=?', (doc_id,))]
        topic = next((t for t in read_topics(db, doc_id) if t['id'] == topic_id), None) if topic_id else None
        items = [question(r) for r in db.execute("SELECT * FROM questions WHERE document_id=? AND auto_status='passed'", (doc_id,))]
    if topic:
        ids = set(topic['fragment_ids'])
        fragments = [f for f in fragments if f['id'] in ids]
        items = [q for q in items if q.get('topic') == topic['name']]
    chars = sum(len(f['text']) for f in fragments)
    # Five levels can reuse one fact with different cognitive tasks: roughly one distinct
    # question per ~250 characters of real content, at least three per fragment.
    estimate = max(3 * len(fragments), round(chars / 250)) if fragments else 0
    return {'fragments': len(fragments), 'chars': chars, 'estimate': estimate, 'existing': len(items),
            'remaining': max(0, estimate - len(items))}


@app.get('/api/documents/{doc_id}/bank-status')
def bank_status(doc_id: str):
    with connect() as db:
        require_document(db, doc_id)
        items = [question(r) for r in db.execute('SELECT * FROM questions WHERE document_id=?', (doc_id,))]
    ready = [q for q in items if q['auto_status'] == 'passed' and q['review_status'] == 'accepted' and q.get('checked_level') in range(1, 6)]
    return {'total': len(items), 'accepted_for_test': len(ready),
            'auto_passed': sum(q['auto_status'] == 'passed' for q in items),
            'unchecked_level': sum(q.get('checked_level') not in range(1, 6) for q in items)}


@app.get('/api/questions')
def questions(document_id: str, status: str | None = None, topic: str | None = None, level: int | None = None, type: str | None = None, auto_status: str | None = None):
    with connect() as db:
        require_document(db, document_id)
        items = [question(r) for r in db.execute('SELECT * FROM questions WHERE document_id=? ORDER BY created_at DESC,rowid DESC', (document_id,))]
    annotate_duplicates(items)
    return [q for q in items if (not status or q['review_status'] == status) and (not topic or q['topic'] == topic)
            and (not level or q['checked_level'] == level) and (not type or q['type'] == type)
            and (not auto_status or q['auto_status'] == auto_status)]


@app.put('/api/questions/{qid}')
def edit_question(qid: str, edit: Edit):
    candidate = Candidate.model_validate(edit.model_dump(exclude={'requested_level', 'revision', 'language'}))
    from app.services.validation import validate_sources
    with connect() as db:
        row = require_question(db, qid)
        fragments = [dict(r) for r in db.execute('SELECT * FROM fragments WHERE document_id=?', (row['document_id'],))]
        try:
            sources = validate_sources(candidate, fragments, row['document_id'])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        payload = {**candidate.model_dump(), 'sources': sources, 'requested_level': edit.requested_level,
                   'language': edit.language,
                   'checked_level': None, 'difficulty_reason': '', 'level_mismatch': False, 'needs_recheck': True,
                   'validation': {'passed': False, 'reasons': ['Вопрос изменён. Нужна повторная автоматическая проверка.']}}
        result = db.execute("UPDATE questions SET payload=?,auto_status='stale',review_status='pending',revision=revision+1 WHERE id=? AND revision=?",
                            (dumps(payload), qid, edit.revision))
        if result.rowcount == 0:
            raise HTTPException(409, 'Вопрос изменён в другом окне. Обновите банк.')
    return {'message': 'Изменения сохранены. Статус сброшен; выполните AI-проверку.'}


@app.post('/api/questions/{qid}/recheck')
def recheck(qid: str):
    with connect() as db:
        row = require_question(db, qid)
        revision, doc_id = row['revision'], row['document_id']
        old = question(row)
        fragments = [dict(r) for r in db.execute('SELECT * FROM fragments WHERE document_id=?', (doc_id,))]
        existing_questions = [json.loads(r[0]) for r in db.execute('SELECT payload FROM questions WHERE document_id=? AND id!=?', (doc_id, qid))]
        existing = [q['text'] for q in existing_questions]
        language = old.get('language') or require_document(db, doc_id)['language']
    candidate = Candidate.model_validate({k: old[k] for k in Candidate.model_fields} | {'sources': [{'fragment_id': s['fragment_id'], 'quote': s['quote']} for s in old['sources']]})
    if duplicate(candidate.text, existing):
        payload = {k: v for k, v in old.items() if k not in ('id', 'document_id', 'auto_status', 'review_status', 'revision', 'created_at')}
        payload.update(needs_recheck=False, validation={'passed': False, 'reasons': ['Обнаружен похожий вопрос.']})
        status = 'failed'
    else:
        try:
            payload, status = evaluate(LLM(), candidate, fragments, doc_id, old['requested_level'], old['topic'], existing_questions=existing_questions, target_language=language)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
    with connect() as db:
        result = db.execute("UPDATE questions SET payload=?,auto_status=?,review_status='pending',revision=revision+1 WHERE id=? AND revision=?",
                            (dumps(payload), status, qid, revision))
        if not result.rowcount:
            raise HTTPException(409, 'Вопрос был изменён во время проверки. Повторите проверку.')
    return {'message': 'AI-проверка завершена. Решение принимает преподаватель.', 'auto_status': status}


@app.post('/api/questions/{qid}/review')
def review(qid: str, review: Review):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = require_question(db, qid)
        if row['revision'] != review.revision:
            raise HTTPException(409, 'Вопрос изменён. Обновите банк.')
        if review.status == 'accepted' and (row['auto_status'] != 'passed' or json.loads(row['payload']).get('checked_level') not in range(1, 6)):
            raise HTTPException(409, 'Сначала нужна успешная автоматическая проверка.')
        if review.status == 'accepted':
            accepted = [json.loads(r[0])['text'] for r in db.execute("SELECT payload FROM questions WHERE document_id=? AND id!=? AND review_status='accepted' AND auto_status='passed'", (row['document_id'], qid))]
            if duplicate(json.loads(row['payload'])['text'], accepted):
                raise HTTPException(409, 'Похожий вопрос уже принят. Измените проверяемое знание или отклоните повтор.')
        db.execute('UPDATE questions SET review_status=?,revision=revision+1 WHERE id=?', (review.status, qid))
    return {'message': 'Решение преподавателя сохранено.'}


@app.delete('/api/questions/{qid}')
def delete(qid: str):
    with connect() as db:
        require_question(db, qid)
        db.execute('DELETE FROM questions WHERE id=?', (qid,))
    return {'message': 'Вопрос удалён. Снимки в попытках сохранены.'}


def question_pool(db, params):
    """Accepted, auto-checked, non-duplicate questions matching the filters."""
    require_ready(db, params.document_id, params.topic_id)
    topics = read_topics(db, params.document_id)
    topic = next((t['name'] for t in topics if t['id'] == params.topic_id), None)
    pool = [question(r) for r in db.execute("SELECT * FROM questions WHERE document_id=? AND review_status='accepted' AND auto_status='passed'", (params.document_id,))]
    pool = [q for q in pool if (not topic or q['topic'] == topic) and (not params.level or q['checked_level'] == params.level)
            and q.get('checked_level') in range(1, 6)
            and (params.language == 'auto' or (q.get('language') or detect_language(q['text'])) == params.language)
            and (params.type == 'mixed' or q['type'] == params.type)]
    unique_pool = []
    for q in pool:
        if not duplicate(q['text'], [other['text'] for other in unique_pool]):
            unique_pool.append(q)
    return unique_pool


def balanced_sample(pool, count):
    """Random sample that keeps every available level represented when possible."""
    if len(pool) <= count:
        return random.sample(pool, len(pool))
    by_level = defaultdict(list)
    for q in pool:
        by_level[q['checked_level']].append(q)
    for items in by_level.values():
        random.shuffle(items)
    chosen = []
    while len(chosen) < count:
        for level in sorted(by_level):
            if by_level[level] and len(chosen) < count:
                chosen.append(by_level[level].pop())
    random.shuffle(chosen)
    return chosen


SNAPSHOT_KEYS = ('id', 'text', 'type', 'options', 'correct_index', 'reference_answer', 'explanation', 'topic', 'language',
                 'requested_level', 'checked_level', 'difficulty_reason', 'sources')


def snapshot_of(q):
    return {k: q.get(k) for k in SNAPSHOT_KEYS}


@app.post('/api/attempts', status_code=201)
def start_attempt(params: TestRequest):
    with connect() as db:
        pool = question_pool(db, params)
        if not pool:
            raise HTTPException(409, 'Нет принятых вопросов с выбранными параметрами. Примите вопросы в банке или измените фильтры.')
        snapshot = [shuffle_options(q) for q in balanced_sample(pool, params.count)]
        attempt_id = uuid.uuid4().hex
        db.execute('INSERT INTO attempts(id,document_id,snapshot) VALUES(?,?,?)', (attempt_id, params.document_id, dumps(snapshot)))
        result = present(db.execute('SELECT * FROM attempts WHERE id=?', (attempt_id,)).fetchone())
        result['requested'] = params.count
        result['available'] = len(snapshot)
        return result


@app.get('/api/attempts')
def attempts(document_id: str):
    with connect() as db:
        rows = db.execute('SELECT * FROM attempts WHERE document_id=? AND assignment_id IS NULL ORDER BY created_at DESC,rowid DESC LIMIT 30', (document_id,)).fetchall()
        result = []
        for row in rows:
            item = {k: row[k] for k in ('id', 'status', 'created_at', 'finished_at')}
            if row['status'] == 'finished':
                summary_ = present(row)
                item.update({k: summary_[k] for k in ('correct', 'total', 'percent', 'points', 'max_points', 'grading_status')})
            result.append(item)
        return result


@app.get('/api/attempts/{aid}')
def attempt(aid: str):
    with connect() as db:
        row = db.execute('SELECT * FROM attempts WHERE id=?', (aid,)).fetchone()
        if not row:
            raise HTTPException(404, 'Попытка не найдена.')
        return present(row)


def finish_attempt(db, row, answers, background_tasks):
    if row['status'] == 'active':
        validate_answers(json.loads(row['snapshot']), answers)
        grading = 'pending' if needs_grading(json.loads(row['snapshot'])) and configuration()['configured'] else ''
        db.execute("UPDATE attempts SET answers=?,status='finished',finished_at=CURRENT_TIMESTAMP,grading_status=? WHERE id=?",
                   (dumps(answers), grading, row['id']))
        if grading:
            background_tasks.add_task(grade_attempt, row['id'])


@app.post('/api/attempts/{aid}/finish')
def finish(aid: str, submit: Submit, background_tasks: BackgroundTasks):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM attempts WHERE id=?', (aid,)).fetchone()
        if not row:
            raise HTTPException(404, 'Попытка не найдена.')
        finish_attempt(db, row, submit.answers, background_tasks)
        return present(db.execute('SELECT * FROM attempts WHERE id=?', (aid,)).fetchone())


@app.post('/api/attempts/{aid}/grade', status_code=202)
def regrade(aid: str, background_tasks: BackgroundTasks):
    if not configuration()['configured']:
        raise ModelError('Модель не настроена.')
    with connect() as db:
        row = db.execute('SELECT * FROM attempts WHERE id=?', (aid,)).fetchone()
        if not row or row['status'] != 'finished':
            raise HTTPException(409, 'Проверить можно только завершённую попытку.')
        db.execute("UPDATE attempts SET grading_status='pending' WHERE id=?", (aid,))
    background_tasks.add_task(grade_attempt, aid)
    return {'message': 'AI проверяет открытые ответы.'}


@app.post('/api/attempts/{aid}/self-scores')
def self_scores(aid: str, scores: SelfScores):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM attempts WHERE id=?', (aid,)).fetchone()
        if not row:
            raise HTTPException(404, 'Попытка не найдена.')
        if row['status'] != 'finished':
            raise HTTPException(409, 'Сначала завершите тест.')
        open_ids = {q['id'] for q in json.loads(row['snapshot']) if q['type'] == 'open'}
        if set(scores.scores) - open_ids or any(v < 0 or v > 5 for v in scores.scores.values()):
            raise HTTPException(422, 'Самооценка 0–5 допустима только для открытых вопросов этой попытки.')
        saved = json.loads(row['self_scores']) | scores.scores
        db.execute('UPDATE attempts SET self_scores=? WHERE id=?', (dumps(saved), aid))
    return {'message': 'Самооценка сохранена. Это ваша оценка, а не объективная проверка.'}


# ------------------------------------------------------------ statistics and export
@app.get('/api/documents/{doc_id}/stats')
def stats(doc_id: str):
    with connect() as db:
        require_document(db, doc_id)
        return statistics(db, doc_id)


@app.get('/api/documents/{doc_id}/export')
def export_questions(doc_id: str, format: str = 'docx', scope: str = 'accepted', answers: bool = True):
    if format not in ('docx', 'csv', 'json') or scope not in ('accepted', 'passed', 'all'):
        raise HTTPException(422, 'Формат: docx, csv или json; набор: accepted, passed или all.')
    with connect() as db:
        doc = require_document(db, doc_id)
        items = [question(r) for r in db.execute('SELECT * FROM questions WHERE document_id=? ORDER BY created_at,rowid', (doc_id,))]
    if scope == 'accepted':
        items = [q for q in items if q['review_status'] == 'accepted' and q['auto_status'] == 'passed']
    elif scope == 'passed':
        items = [q for q in items if q['auto_status'] == 'passed' and q['review_status'] != 'rejected']
    if not items:
        raise HTTPException(409, 'Нет вопросов для выгрузки с выбранными условиями.' + (' Сначала примите вопросы в банке.' if scope == 'accepted' else ''))
    items.sort(key=lambda q: (exporter.level_of(q) or 9, q.get('topic') or ''))
    base = doc['name'].rsplit('.', 1)[0]
    if format == 'csv':
        content, media = exporter.to_csv(items), 'text/csv; charset=utf-8'
    elif format == 'json':
        content, media = exporter.to_json(items, doc['name']), 'application/json'
    else:
        content = exporter.to_docx(items, base, answers=answers)
        media = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    suffix = '' if answers or format != 'docx' else '-dlya-studentov'
    return download(content, media, f'{base}{suffix}.{format}')


def download(content, media, filename):
    from urllib.parse import quote
    ascii_name = ''.join(c if c.isascii() and (c.isalnum() or c in '.-_') else '_' for c in filename)
    return Response(content, media_type=media, headers={
        'Content-Disposition': f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"})


@app.get('/api/backup')
def backup():
    stamp = time.strftime('%Y-%m-%d')
    return download(exporter.backup(), 'application/json', f'terendik-backup-{stamp}.json')


@app.post('/api/backup')
async def restore_backup(file: UploadFile):
    raw = await file.read(200 * 1024 * 1024 + 1)
    if len(raw) > 200 * 1024 * 1024:
        raise HTTPException(413, 'Резервная копия больше 200 МБ.')
    try:
        result = exporter.restore(raw)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return {**result, 'message': f'Восстановлено документов: {result["added"]}; уже существовали и пропущены: {result["skipped"]}.'}


@app.get('/api/storage')
def storage():
    persistent = store.backend() == 'postgres' or os.getenv('APP_PERSISTENT_DISK') == '1'
    return {'backend': store.backend(), 'persistent': persistent,
            'notice': '' if persistent else os.getenv('APP_STORAGE_NOTICE', '')}


# ------------------------------------------------------------ assignments (tests by link)
def assignment_summary(db, row):
    item = {k: row[k] for k in ('id', 'token', 'document_id', 'title', 'created_at')}
    item['is_open'] = bool(row['is_open'])
    item['settings'] = json.loads(row['settings'] or '{}')
    item['question_count'] = len(json.loads(row['snapshot']))
    rows = db.execute('SELECT * FROM attempts WHERE assignment_id=? ORDER BY created_at DESC,rowid DESC', (row['id'],)).fetchall()
    results = []
    for attempt_row in rows:
        entry = {'id': attempt_row['id'], 'student_name': attempt_row['student_name'], 'status': attempt_row['status'],
                 'created_at': attempt_row['created_at'], 'finished_at': attempt_row['finished_at']}
        if attempt_row['status'] == 'finished':
            data = present(attempt_row)
            entry.update({k: data[k] for k in ('correct', 'total', 'points', 'max_points', 'percent', 'grading_status')})
        results.append(entry)
    item['results'] = results
    item['finished'] = sum(r['status'] == 'finished' for r in results)
    return item


@app.post('/api/assignments', status_code=201)
def create_assignment(params: AssignmentRequest):
    generate_like = TestRequest(document_id=params.document_id, topic_id=params.topic_id, count=min(params.count, 50),
                                level=params.level, type=params.type, language=params.language)
    with connect() as db:
        pool = question_pool(db, generate_like)
        if not pool:
            raise HTTPException(409, 'Нет принятых вопросов с выбранными параметрами. Сначала примите вопросы в банке.')
        chosen = [snapshot_of(q) for q in balanced_sample(pool, params.count)]
        assignment_id, token = uuid.uuid4().hex, secrets.token_urlsafe(9)
        settings = params.model_dump(exclude={'document_id', 'title'}) | {'available': len(pool)}
        db.execute('INSERT INTO assignments(id,token,document_id,title,settings,snapshot) VALUES(?,?,?,?,?,?)',
                   (assignment_id, token, params.document_id, params.title, dumps(settings), dumps(chosen)))
        result = assignment_summary(db, db.execute('SELECT * FROM assignments WHERE id=?', (assignment_id,)).fetchone())
    if len(chosen) < params.count:
        result['message'] = f'В задание вошло {len(chosen)} из {params.count}: больше принятых вопросов с такими параметрами нет.'
    return result


@app.get('/api/assignments')
def list_assignments(document_id: str):
    with connect() as db:
        require_document(db, document_id)
        rows = db.execute('SELECT * FROM assignments WHERE document_id=? ORDER BY created_at DESC,rowid DESC', (document_id,)).fetchall()
        return [assignment_summary(db, r) for r in rows]


@app.patch('/api/assignments/{assignment_id}')
def update_assignment(assignment_id: str, update: AssignmentUpdate):
    with connect() as db:
        result = db.execute('UPDATE assignments SET is_open=? WHERE id=?', (int(update.is_open), assignment_id))
        if not result.rowcount:
            raise HTTPException(404, 'Задание не найдено.')
    return {'message': 'Приём ответов открыт.' if update.is_open else 'Приём ответов закрыт. Ссылка показывает студентам, что тест завершён.'}


@app.delete('/api/assignments/{assignment_id}')
def delete_assignment(assignment_id: str):
    with connect() as db:
        if db.execute("SELECT id FROM attempts WHERE assignment_id=? LIMIT 1", (assignment_id,)).fetchone():
            raise HTTPException(409, 'По заданию уже есть результаты студентов. Закройте приём ответов вместо удаления.')
        result = db.execute('DELETE FROM assignments WHERE id=?', (assignment_id,))
        if not result.rowcount:
            raise HTTPException(404, 'Задание не найдено.')
    return {'message': 'Задание удалено.'}


@app.get('/api/assignments/{assignment_id}/results.csv')
def assignment_results_csv(assignment_id: str):
    import csv
    import io
    with connect() as db:
        row = db.execute('SELECT * FROM assignments WHERE id=?', (assignment_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Задание не найдено.')
        item = assignment_summary(db, row)
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=';')
    writer.writerow(['Студент', 'Статус', 'Начал', 'Завершил', 'Тестовые: верно', 'Тестовые: всего', 'Баллы', 'Максимум', 'Процент'])
    for r in item['results']:
        writer.writerow([r['student_name'], 'завершён' if r['status'] == 'finished' else 'не завершён', r['created_at'], r['finished_at'] or '',
                         r.get('correct', ''), r.get('total', ''), r.get('points', ''), r.get('max_points', ''),
                         '' if r.get('percent') is None else r['percent']])
    return download(('\ufeff' + buffer.getvalue()).encode('utf-8'), 'text/csv; charset=utf-8', f'{item["title"]}-results.csv')


@app.get('/api/assignment-attempts/{aid}')
def assignment_attempt(aid: str):
    with connect() as db:
        row = db.execute('SELECT * FROM attempts WHERE id=? AND assignment_id IS NOT NULL', (aid,)).fetchone()
        if not row:
            raise HTTPException(404, 'Попытка не найдена.')
        return present(row)


# ------------------------------------------------------------ student side (public by link)
_student_starts = defaultdict(deque)


def open_assignment(db, token):
    row = db.execute('SELECT a.*, d.deleted_at AS doc_deleted FROM assignments a JOIN documents d ON d.id=a.document_id WHERE a.token=?', (token,)).fetchone()
    if not row or row['doc_deleted']:
        raise HTTPException(404, 'Тест не найден. Проверьте ссылку у преподавателя.')
    return row


def student_attempt(db, token, aid, key):
    assignment = open_assignment(db, token)
    row = db.execute('SELECT * FROM attempts WHERE id=? AND assignment_id=?', (aid, assignment['id'])).fetchone()
    if not row or not key or not secrets.compare_digest(row['access_key'] or '', key):
        raise HTTPException(404, 'Попытка не найдена.')
    return assignment, row


def student_view(assignment, row):
    settings = json.loads(assignment['settings'] or '{}')
    data = present(row, show_answers=settings.get('show_answers', True))
    data['show_answers'] = settings.get('show_answers', True)
    return data


@app.get('/s/{token}')
def student_page(token: str):
    return FileResponse(ASSETS / 'student.html')


@app.get('/api/s/{token}')
def student_assignment(token: str):
    with connect() as db:
        row = open_assignment(db, token)
        settings = json.loads(row['settings'] or '{}')
        snapshot = json.loads(row['snapshot'])
        return {'title': row['title'], 'is_open': bool(row['is_open']), 'question_count': len(snapshot),
                'open_questions': sum(q['type'] == 'open' for q in snapshot),
                'ui_language': settings.get('ui_language', 'ru'), 'show_answers': settings.get('show_answers', True)}


@app.post('/api/s/{token}/start', status_code=201)
def student_start(token: str, start: StudentStart, request: Request):
    ip = request.client.host if request.client else 'unknown'
    bucket = _student_starts[ip]
    now = time.monotonic()
    while bucket and bucket[0] < now - 600:
        bucket.popleft()
    if len(bucket) >= 30:
        raise HTTPException(429, 'Слишком много попыток с этого устройства. Повторите позже.')
    bucket.append(now)
    with connect() as db:
        assignment = open_assignment(db, token)
        if not assignment['is_open']:
            raise HTTPException(409, 'Преподаватель закрыл приём ответов по этому тесту.')
        questions = json.loads(assignment['snapshot'])
        random.shuffle(questions)
        snapshot = [shuffle_options(q) for q in questions]
        attempt_id, key = uuid.uuid4().hex, secrets.token_urlsafe(24)
        db.execute('INSERT INTO attempts(id,document_id,snapshot,assignment_id,student_name,access_key) VALUES(?,?,?,?,?,?)',
                   (attempt_id, assignment['document_id'], dumps(snapshot), assignment['id'], ' '.join(start.name.split()), key))
        data = student_view(assignment, db.execute('SELECT * FROM attempts WHERE id=?', (attempt_id,)).fetchone())
    return data | {'access_key': key}


@app.get('/api/s/{token}/attempts/{aid}')
def student_get(token: str, aid: str, x_attempt_key: str = Header(default='')):
    with connect() as db:
        assignment, row = student_attempt(db, token, aid, x_attempt_key)
        return student_view(assignment, row)


@app.post('/api/s/{token}/attempts/{aid}/finish')
def student_finish(token: str, aid: str, submit: Submit, background_tasks: BackgroundTasks, x_attempt_key: str = Header(default='')):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        assignment, row = student_attempt(db, token, aid, x_attempt_key)
        if row['status'] == 'active' and not assignment['is_open']:
            raise HTTPException(409, 'Преподаватель закрыл приём ответов по этому тесту.')
        finish_attempt(db, row, submit.answers, background_tasks)
        return student_view(assignment, db.execute('SELECT * FROM attempts WHERE id=?', (aid,)).fetchone())
