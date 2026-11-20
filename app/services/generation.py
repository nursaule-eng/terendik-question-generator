"""Question generation job.

A job keeps its parameters and progress in the database, so it can
- wait for a provider rate limit and continue automatically,
- pause on a daily quota and be resumed by the teacher,
- be cancelled between questions,
- continue after a server restart (app.services.jobs.recover()).
Every accepted or rejected candidate is saved immediately.
"""
import json
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

from app.database import connect, dumps
from app.schemas import Batch, Generate
from app.services.llm import LLM, ModelError, ModelFormatError, RUBRIC
from app.services.retrieval import retrieve
from app.services.topics import read_topics
from app.services.validation import duplicate, evaluate
from app.services.language import LANGUAGES
from app.services.options import shuffle_options

COGNITIVE_TASKS = {
    1: 'Спросить один факт или определение, явно данное в источнике. Не требовать вычислений или анализа.',
    2: 'Попросить объяснить смысл или простое различие своими словами. Не требовать вычислений, выбора оптимального плана, вывода диапазона параметра или анализа ошибок.',
    3: 'Дать новый, явно условный пример с изменёнными исходными числами и попросить применить один изученный метод. Не спрашивать готовый числовой вывод, уже выписанный в источнике.',
    4: 'Дать новую, явно условную ситуацию или ошибочное решение и потребовать выявить причину ошибки, сравнить допустимые альтернативы и обосновать вывод. Это должен быть самостоятельный анализ, а не повтор готового разбора из источника.',
    5: 'Попросить самостоятельно построить решение/модель/план для нового условного случая, где одновременно действуют не меньше двух взаимосвязанных требований или ограничений (перечисли их в вопросе), выбрать критерий или метод и обосновать выбор и ограничения решения. Ответ нельзя получить подстановкой в одну формулу или выбором из готового списка. В эталоне дай полное образцовое решение и критерии оценки. Не повторять готовый пример источника.',
}

MAX_WAITS = 12           # automatic waits for rate limits per job
MAX_AUTO_WAIT = 15 * 60  # longer waits pause the job until the teacher resumes it
ACTIVE = ('queued', 'running', 'waiting', 'paused')

_running = set()
_running_lock = threading.Lock()


def summary(requested, created, rejected):
    return f'Запрошено {requested}. Проверено кандидатов: {created + rejected}; прошли проверку: {created}; отклонены: {rejected}.'


def now_utc():
    return datetime.now(timezone.utc)


def iso(moment):
    return moment.strftime('%Y-%m-%d %H:%M:%S')


def plan(params):
    """Deterministic level/type schedule for slot index (stable across restarts)."""
    levels = [params.level or (slot % 5 + 1) for slot in range(params.count)]
    return levels


def kind_for(params, index, level):
    if params.type != 'mixed':
        return params.type
    if level == 5:
        return 'open'  # synthesis is checked by constructing a solution, not by picking one
    return 'mcq' if index % 2 == 0 else 'open'


class Cancelled(Exception):
    pass


def update_job(job_id, **fields):
    fields['updated_at'] = iso(now_utc())
    keys = ','.join(f'{k}=?' for k in fields)
    with connect() as db:
        db.execute(f'UPDATE jobs SET {keys} WHERE id=?', (*fields.values(), job_id))


def cancel_requested(job_id):
    with connect() as db:
        row = db.execute('SELECT cancel_requested FROM jobs WHERE id=?', (job_id,)).fetchone()
    return bool(row and row[0])


def wait_until(job_id, moment):
    while now_utc() < moment:
        if cancel_requested(job_id):
            raise Cancelled()
        time.sleep(min(2.0, max(0.05, (moment - now_utc()).total_seconds())))


def claim(job_id):
    """Only one thread per job; a job is claimed from queued/waiting state."""
    with _running_lock:
        if job_id in _running:
            return False
        with connect() as db:
            result = db.execute("UPDATE jobs SET status='running' WHERE id=? AND status IN ('queued','waiting','running')", (job_id,))
            if not result.rowcount:
                return False
        _running.add(job_id)
        return True


def release(job_id):
    with _running_lock:
        _running.discard(job_id)


def is_running_here(job_id):
    with _running_lock:
        return job_id in _running


def run_generation(job_id, params=None):
    if params is None:
        with connect() as db:
            row = db.execute('SELECT params FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            return
        params = Generate.model_validate_json(row[0] or '{}', strict=False)
    if not claim(job_id):
        return
    try:
        _run(job_id, params)
    finally:
        release(job_id)


def _run(job_id, params):
    with connect() as db:
        job = dict(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
        fragments = [dict(r) for r in db.execute('SELECT * FROM fragments WHERE document_id=? ORDER BY ordinal', (params.document_id,))]
        topics = read_topics(db, params.document_id)
        doc_language = db.execute('SELECT language FROM documents WHERE id=?', (params.document_id,)).fetchone()[0]
        mine = [json.loads(r[0]) | {'auto_status': r[1]} for r in db.execute(
            'SELECT payload,auto_status FROM questions WHERE job_id=?', (job_id,))]
        existing = [json.loads(r[0])['text'] for r in db.execute('SELECT payload FROM questions WHERE document_id=?', (params.document_id,))]
    created = sum(q['auto_status'] == 'passed' for q in mine)
    rejected = len(mine) - created
    produced = Counter(q['requested_level'] for q in mine if q['auto_status'] == 'passed')
    notes = json.loads(job.get('details') or '[]')
    start = job.get('attempts_used') or 0
    waits = 0
    target_language = doc_language if params.language == 'auto' else params.language
    schedule = plan(params)
    targets = Counter(schedule)
    selected = next((t for t in topics if t['id'] == params.topic_id), None)
    progress = {'stage': ''}

    def status_line(extra=''):
        return f'Создано {created} из {params.count}; отклонено {rejected}. ' + progress['stage'] + (' ' + extra if extra else '')

    def on_wait(message):
        update_job(job_id, message=status_line(message), stage=message or progress['stage'])

    def set_stage(message):
        progress['stage'] = message
        update_job(job_id, message=status_line(), stage=message)

    llm = LLM(on_wait=on_wait)
    budget = params.count * 2
    index = start
    try:
        while index < budget and created < params.count:
            if cancel_requested(job_id):
                raise Cancelled()
            level = schedule[index % len(schedule)]
            if produced[level] >= targets[level]:
                index += 1
                continue
            topic = selected or (topics[index % len(topics)] if topics else None)
            kind = kind_for(params, index, level)
            offset = index * 3 if selected else round(index * max(0, len(fragments) - 1) / max(1, params.count - 1))
            context = retrieve(fragments, topic, offset=offset)
            try:
                status, payload = make_question(llm, params, job_id, level, kind, topic, context, existing, target_language, set_stage, notes)
            except ModelFormatError as exc:
                status, payload = 'skipped', None
                notes.append(str(exc))
                rejected += 1
            except ModelError as exc:
                if not exc.transient:
                    raise
                delay = int(exc.retry_after or 60)
                waits += 1
                if exc.daily or delay > MAX_AUTO_WAIT or waits > MAX_WAITS:
                    resume = now_utc() + timedelta(seconds=delay)
                    update_job(job_id, status='paused', created=created, rejected=rejected, attempts_used=index,
                               resume_at=iso(resume), details=dumps(list(dict.fromkeys(notes))[:100]),
                               stage='Пауза: лимит модели.',
                               message=f'Создано {created} из {params.count}; отклонено {rejected}. {exc} '
                                       'Нажмите «Продолжить», когда лимит восстановится.')
                    return
                resume = now_utc() + timedelta(seconds=delay)
                update_job(job_id, status='waiting', resume_at=iso(resume), attempts_used=index,
                           stage=f'Лимит запросов модели. Продолжим автоматически через {delay} сек.',
                           message=status_line(f'Лимит запросов модели, ожидание {delay} сек. Созданные вопросы сохранены.'))
                wait_until(job_id, resume)
                update_job(job_id, status='running', resume_at=None, stage='Продолжаем генерацию.')
                continue  # same slot again
            if status == 'passed':
                created += 1
                produced[level] += 1
                existing.append(payload['text'])
            elif status == 'failed':
                rejected += 1
                existing.append(payload['text'])
                notes.extend(payload['validation']['reasons'])
            elif status == 'empty':
                rejected += 1
                if payload and payload not in notes:
                    notes.append(payload)
            index += 1
            update_job(job_id, created=created, rejected=rejected, attempts_used=index,
                       details=dumps(list(dict.fromkeys(notes))[:100]),
                       message=f'Создано {created} из {params.count}; отклонено {rejected}.')
        message = summary(params.count, created, rejected)
        if created < params.count:
            message += ' Лимит попыток исчерпан: недостаточно неповторяющихся подтверждённых вопросов.'
        update_job(job_id, status='done', message=message, stage='', resume_at=None,
                   details=dumps(list(dict.fromkeys(notes))[:100]))
    except Cancelled:
        update_job(job_id, status='cancelled', stage='', resume_at=None, created=created, rejected=rejected,
                   message=f'Генерация остановлена преподавателем. Сохранено прошедших проверку: {created} из {params.count}; отклонено {rejected}.',
                   details=dumps(list(dict.fromkeys(notes))[:100]))
    except Exception as exc:
        message = str(exc) if isinstance(exc, ModelError) else 'Ошибка генерации. Уже созданные вопросы сохранены; повторите запрос.'
        message = f'Запрошено {params.count}, сохранено допустимых {created}, отклонено {rejected}. ' + message
        update_job(job_id, status='error', created=created, rejected=rejected, message=message, stage='', resume_at=None,
                   details=dumps(list(dict.fromkeys(notes))[:100]))


GENERATE_INSTRUCTION = (
    'Создай один новый вопрос с ответом, объяснением и дословной подтверждающей цитатой. '
    'Для mcq ровно четыре варианта и correct_index от 0 до 3; для open options=[] и correct_index=null. '
    'Неправильные варианты mcq должны быть правдоподобными (типичные ошибки), а не абсурдными. '
    'В вопросе и объяснении не ссылайся на буквы, номера или порядок вариантов: сервер перемешивает их. '
    'Варианты должны сохранять смысл при любом порядке. '
    'Для условной задачи явно укажи в самом вопросе все необходимые числа, ограничения и критерий: ученик не видит контекст запроса к модели. '
    'Не повторяй существующие вопросы. Все source.fragment_id берутся из контекста. '
    'Поле quote скопируй дословно из text выбранного фрагмента: не исправляй грамматику, окончания слов, '
    'дефисы и опечатки исходника. Допустима короткая цитата вместо целого предложения. '
    'Не заменяй в quote знаки <= на ≤ и наоборот. Для нового условного примера цитируй исходный метод решения, а не новые числа или несуществующий готовый ответ. '
    'Не придумывай номера страниц. Не добавляй вопросы при нехватке материала. ' + RUBRIC)


def make_question(llm, params, job_id, level, kind, topic, context, existing, target_language, set_stage, notes):
    """Generate, check, optionally revise once to hit the level, and save. Returns (status, payload)."""
    set_stage(f'Создаётся вопрос уровня {level}.')
    request = {'count': 1, 'level': level, 'cognitive_task': COGNITIVE_TASKS[level], 'type': kind,
               'target_language': LANGUAGES[target_language], 'topic': topic['name'] if topic else 'Весь материал',
               'avoid_questions': [text[:400] for text in existing[-40:]],
               'fragments': [{'fragment_id': f['id'], 'text': ' '.join(f['text'].split())} for f in context]}
    response = llm.json(GENERATE_INSTRUCTION, request, Batch)
    if response.reason:
        notes.append(response.reason)
    if not response.questions:
        return 'empty', response.reason or 'Модель не нашла материала для нового вопроса.'
    candidate = response.questions[0]
    if topic:
        candidate = candidate.model_copy(update={'topic': topic['name']})
    payload, status = check_candidate(llm, params, candidate, kind, level, topic, context, target_language, set_stage)
    if status == 'passed' and payload.get('level_mismatch') and payload['validation'].get('passed'):
        # One focused revision: ask the author to adjust the cognitive demand to the requested level.
        set_stage(f'Уточняется сложность вопроса (нужен уровень {level}, проверка дала {payload["checked_level"]}).')
        revised = llm.json(
            GENERATE_INSTRUCTION + ' Это доработка существующего вопроса. Рецензент определил другой уровень сложности; '
            'измени задание так, чтобы оно требовало именно действия целевого уровня (cognitive_task), '
            'сохранив тему, язык и подтверждение источником.',
            request | {'previous_question': candidate.model_dump(), 'reviewer_level': payload['checked_level'],
                       'reviewer_reason': payload.get('difficulty_reason', '')}, Batch)
        if revised.questions:
            second = revised.questions[0]
            if topic:
                second = second.model_copy(update={'topic': topic['name']})
            if second.type == kind:
                payload2, status2 = check_candidate(llm, params, second, kind, level, topic, context, target_language, set_stage)
                if status2 == 'passed' and not payload2.get('level_mismatch'):
                    payload, status = payload2, status2
    if status == 'passed':
        payload = shuffle_options(payload)
    payload['job_id'] = job_id
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current_texts = [json.loads(r[0])['text'] for r in db.execute('SELECT payload FROM questions WHERE document_id=?', (params.document_id,))]
        if status == 'passed' and duplicate(payload['text'], current_texts):
            status = 'failed'
            payload['validation'].update(passed=False, reasons=['Похожий вопрос появился в банке во время генерации.'])
        db.execute('INSERT INTO questions(id,document_id,payload,auto_status,job_id) VALUES(?,?,?,?,?)',
                   (uuid.uuid4().hex, params.document_id, dumps(payload), status, job_id))
    return status, payload


def check_candidate(llm, params, candidate, kind, level, topic, context, target_language, set_stage):
    base = {**candidate.model_dump(), 'requested_level': level, 'checked_level': None,
            'language': target_language, 'difficulty_reason': '', 'needs_recheck': False, 'level_mismatch': False}
    try:
        if candidate.type != kind:
            raise ValueError('Модель вернула другой тип вопроса.')
        with connect() as db:
            existing_questions = [json.loads(r[0]) for r in db.execute('SELECT payload FROM questions WHERE document_id=?', (params.document_id,))]
        if duplicate(candidate.text, [q['text'] for q in existing_questions]):
            raise ValueError('Обнаружен похожий вопрос в документе.')
        set_stage(f'Проверяется вопрос уровня {level}.')
        return evaluate(llm, candidate, context, params.document_id, level, topic['name'] if topic else None,
                        existing_questions=existing_questions, target_language=target_language)
    except ValueError as exc:
        return {**base, 'validation': {'passed': False, 'reasons': [str(exc)]}}, 'failed'
