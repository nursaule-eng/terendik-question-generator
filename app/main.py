import json
import random
import sqlite3
import uuid
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import database as store
from app.database import connect, dumps, question
from app.schemas import Candidate, Edit, Generate, Review, SelfScores, Submit, TestRequest
from app.services.documents import chunks, extract
from app.services.generation import run_generation, summary
from app.services.llm import LLM, ModelError, configuration
from app.services.testing import present, validate_answers
from app.services.topics import find_topics, read_topics
from app.services.validation import duplicate, evaluate, annotate_duplicates
from app.services.language import detect_language
from app.services.options import shuffle_options

ASSETS = Path(__file__).parent / 'static'
MAX_BYTES = 20 * 1024 * 1024


@asynccontextmanager
async def lifespan(app):
    store.init_db()
    yield


app = FastAPI(title='Вопросы по материалу', lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost', '127.0.0.1', 'testserver'])
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
        return [dict(r) for r in db.execute('SELECT id,name,format,created_at,status,error,topic_status,topic_error,language,deleted_at FROM documents WHERE deleted_at ' + clause + ' ORDER BY created_at DESC,rowid DESC')]


def process_document(doc_id, path, extension):
    try:
        pages = extract(path, extension)
        fragments = chunks(pages)
        if not fragments:
            raise ValueError('Не найден доступный текст.')
        with connect() as db:
            for f in fragments:
                db.execute('INSERT INTO fragments(document_id,ordinal,text,page_start,page_end) VALUES(?,?,?,?,?)',
                           (doc_id, f['ordinal'], f['text'], f['page_start'], f['page_end']))
            text = '\n\n'.join(t for _, t in pages)
            db.execute("UPDATE documents SET text=?,language=?,status='ready',topic_status='processing' WHERE id=?", (text, detect_language(text), doc_id))
        find_topics(doc_id)
    except Exception as exc:
        message = str(exc) if isinstance(exc, ValueError) else 'Не удалось обработать файл. Проверьте формат и повторите загрузку.'
        with connect() as db:
            db.execute("UPDATE documents SET status='error',error=?,topic_status='error' WHERE id=?", (message, doc_id))


@app.post('/api/documents', status_code=201)
async def upload(file: UploadFile, background_tasks: BackgroundTasks):
    name = (file.filename or 'Без названия').replace('\\', '/').split('/')[-1][:250]
    extension = Path(name).suffix.lower().lstrip('.')
    if extension not in ('pdf', 'txt'):
        raise HTTPException(415, 'Поддерживаются только PDF с текстовым слоем и TXT.')
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
        active = db.execute("SELECT id FROM jobs WHERE document_id=? AND status IN ('queued','running')", (doc_id,)).fetchone()
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
        if db.execute("SELECT id FROM jobs WHERE document_id=? AND status IN ('queued','running')", (doc_id,)).fetchone():
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
            db.execute("INSERT INTO jobs(id,document_id,status,requested) VALUES(?,?,'queued',?)", (job_id, params.document_id, params.count))
    except sqlite3.IntegrityError:
        raise HTTPException(409, 'Генерация для этого документа уже выполняется.') from None
    background_tasks.add_task(run_generation, job_id, params)
    return {'id': job_id}


@app.get('/api/jobs')
def jobs(document_id: str):
    with connect() as db:
        result = []
        for row in db.execute('SELECT * FROM jobs WHERE document_id=? ORDER BY created_at DESC,rowid DESC LIMIT 10', (document_id,)):
            item = dict(row)
            item['details'] = json.loads(item['details'])
            if item['status'] == 'done':
                if not item['details'] and len(item['message']) > 250:
                    item['details'] = [item['message']]
                item['message'] = summary(item['requested'], item['created'], item['rejected'])
                if item['created'] < item['requested']:
                    item['message'] += ' Подтверждённых неповторяющихся вопросов недостаточно; проверьте подробности.'
            result.append(item)
        return result


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


@app.post('/api/attempts', status_code=201)
def start_attempt(params: TestRequest):
    with connect() as db:
        require_ready(db, params.document_id, params.topic_id)
        topics = read_topics(db, params.document_id)
        topic = next((t['name'] for t in topics if t['id'] == params.topic_id), None)
        pool = [question(r) for r in db.execute("SELECT * FROM questions WHERE document_id=? AND review_status='accepted' AND auto_status='passed'", (params.document_id,))]
        pool = [q for q in pool if (not topic or q['topic'] == topic) and (not params.level or q['checked_level'] == params.level)
                and q.get('checked_level') in range(1, 6)
                and (params.language == 'auto' or (q.get('language') or detect_language(q['text'])) == params.language)
                and (params.type == 'mixed' or q['type'] == params.type)]
        if not pool:
            raise HTTPException(409, 'Нет принятых вопросов с выбранными параметрами. Примите вопросы в банке или измените фильтры.')
        unique_pool = []
        for q in pool:
            if not duplicate(q['text'], [other['text'] for other in unique_pool]):
                unique_pool.append(q)
        pool = unique_pool
        snapshot = [shuffle_options(q) for q in random.sample(pool, min(params.count, len(pool)))]
        attempt_id = uuid.uuid4().hex
        db.execute('INSERT INTO attempts(id,document_id,snapshot) VALUES(?,?,?)', (attempt_id, params.document_id, dumps(snapshot)))
        result = present(db.execute('SELECT * FROM attempts WHERE id=?', (attempt_id,)).fetchone())
        result['requested'] = params.count
        result['available'] = len(snapshot)
        return result


@app.get('/api/attempts')
def attempts(document_id: str):
    with connect() as db:
        return [dict(r) for r in db.execute('SELECT id,status,created_at,finished_at FROM attempts WHERE document_id=? ORDER BY created_at DESC,rowid DESC', (document_id,))]


@app.get('/api/attempts/{aid}')
def attempt(aid: str):
    with connect() as db:
        row = db.execute('SELECT * FROM attempts WHERE id=?', (aid,)).fetchone()
        if not row:
            raise HTTPException(404, 'Попытка не найдена.')
        return present(row)


@app.post('/api/attempts/{aid}/finish')
def finish(aid: str, submit: Submit):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM attempts WHERE id=?', (aid,)).fetchone()
        if not row:
            raise HTTPException(404, 'Попытка не найдена.')
        if row['status'] == 'active':
            validate_answers(json.loads(row['snapshot']), submit.answers)
            db.execute("UPDATE attempts SET answers=?,status='finished',finished_at=CURRENT_TIMESTAMP WHERE id=?", (dumps(submit.answers), aid))
        return present(db.execute('SELECT * FROM attempts WHERE id=?', (aid,)).fetchone())


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
