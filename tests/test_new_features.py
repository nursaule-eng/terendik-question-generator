"""Persistence, resumable jobs, student links, grading, export, Kazakh handling."""
import io
import json

import pytest
from fastapi.testclient import TestClient

from app import database
from app.database import connect, dumps, init_db
from app.schemas import Batch, Grade, IndependentAnswer, Verdict
from app.services import calc
from app.services.documents import decode_legacy, repair_kazakh
from app.services.language import detect_language, obvious_language_mismatch
from app.services.llm import LLM, ModelError


def model_env(monkeypatch):
    for name, value in [('LLM_API_KEY', 'TEST_ONLY'), ('LLM_BASE_URL', 'https://example.invalid/v1'), ('LLM_MODEL', 'TEST_ONLY')]:
        monkeypatch.setenv(name, value)


def provider(candidate, failures=None, level=1, calls=None, variants=True):
    """TEST ONLY model double. failures: list of exceptions raised on successive Batch calls."""
    failures = list(failures or [])
    counter = {'n': 0}

    def fake(self, instruction, data, schema):
        if calls is not None:
            calls.append(schema)
        if schema is Batch:
            if failures:
                raise failures.pop(0)
            counter['n'] += 1
            text = candidate['text'] if not variants else f"{candidate['text']} Вариант {counter['n']}: условие {counter['n'] * 7}."
            return Batch(questions=[candidate | {'text': text}], reason='')
        if schema is Verdict:
            return Verdict(relevant=True, supported=True, clear=True, single_correct=True, is_duplicate=False, language_matches=True,
                           level=level, difficulty_reason='Определение', reasons=[])
        if schema is Grade:
            return Grade(score=8, strengths='Суть верна', missed='Нет примера', advice='Повторите определение')
        if schema is IndependentAnswer:
            return IndependentAnswer(has_single_answer=True, correct_index=candidate.get('correct_index'), answer='x', reasoning='решено')
        raise AssertionError(schema)
    return fake


def no_sleep(monkeypatch):
    monkeypatch.setattr('app.services.generation.wait_until', lambda job_id, moment: None)


# ------------------------------------------------------------------ resumable generation
def test_rate_limit_waits_and_continues(client, doc, candidate, monkeypatch):
    model_env(monkeypatch)
    no_sleep(monkeypatch)
    limit = ModelError('limit', 429, retry_after=20, transient=True)
    monkeypatch.setattr(LLM, 'json', provider(candidate, failures=[limit, limit]))
    assert client.post('/api/generate', json={'document_id': doc, 'count': 2, 'type': 'mcq', 'level': 1}).status_code == 202
    job = client.get('/api/jobs', params={'document_id': doc}).json()[0]
    assert job['status'] == 'done' and job['created'] == 2


def test_daily_limit_pauses_and_resume_finishes(client, doc, candidate, monkeypatch):
    model_env(monkeypatch)
    no_sleep(monkeypatch)
    daily = ModelError('day', 429, retry_after=30000, transient=True, daily=True)
    fake = provider(candidate)
    state = {'calls': 0}

    def flaky(self, instruction, data, schema):
        if schema is Batch:
            state['calls'] += 1
            if state['calls'] == 2:
                raise daily
        return fake(self, instruction, data, schema)
    monkeypatch.setattr(LLM, 'json', flaky)
    client.post('/api/generate', json={'document_id': doc, 'count': 3, 'type': 'mcq', 'level': 1})
    job = client.get('/api/jobs', params={'document_id': doc}).json()[0]
    assert job['status'] == 'paused' and job['created'] == 1 and job['resume_at']
    # A paused job blocks a parallel one, but can be resumed from the same slot.
    assert client.post('/api/generate', json={'document_id': doc}).status_code == 409
    assert client.post('/api/jobs/' + job['id'] + '/resume').status_code == 202
    job = client.get('/api/jobs', params={'document_id': doc}).json()[0]
    assert job['status'] == 'done' and job['created'] == 3
    assert len(client.get('/api/questions', params={'document_id': doc}).json()) == 3


def test_restart_resumes_running_job(client, doc, candidate, monkeypatch):
    model_env(monkeypatch)
    monkeypatch.setattr(LLM, 'json', provider(candidate))
    from app.schemas import Generate
    params = Generate(document_id=doc, count=2, type='mcq', level=1)
    with connect() as db:
        db.execute("INSERT INTO jobs(id,document_id,status,requested,params) VALUES('j1',?,'running',2,?)", (doc, params.model_dump_json()))
    init_db()  # restart: running → queued
    with connect() as db:
        assert db.execute("SELECT status FROM jobs WHERE id='j1'").fetchone()[0] == 'queued'
    from app.services.generation import run_generation
    run_generation('j1')
    job = client.get('/api/jobs', params={'document_id': doc}).json()[0]
    assert job['status'] == 'done' and job['created'] == 2


def test_cancel_paused_job(client, doc, candidate, monkeypatch):
    model_env(monkeypatch)
    with connect() as db:
        db.execute("INSERT INTO jobs(id,document_id,status,requested,params) VALUES('p1',?,'paused',5,'{}')", (doc,))
    assert client.post('/api/jobs/p1/cancel').status_code == 200
    assert client.get('/api/jobs', params={'document_id': doc}).json()[0]['status'] == 'cancelled'
    assert client.post('/api/jobs/p1/cancel').status_code == 409


def test_bad_json_from_model_does_not_kill_job(client, doc, candidate, monkeypatch):
    from app.services.llm import ModelFormatError
    model_env(monkeypatch)
    monkeypatch.setattr(LLM, 'json', provider(candidate, failures=[ModelFormatError('bad json')]))
    client.post('/api/generate', json={'document_id': doc, 'count': 1, 'type': 'mcq', 'level': 1})
    job = client.get('/api/jobs', params={'document_id': doc}).json()[0]
    assert job['status'] == 'done' and job['created'] == 1 and job['rejected'] == 1


def test_capacity_estimate(client, doc):
    data = client.get(f'/api/documents/{doc}/capacity').json()
    assert data['fragments'] == 1 and data['estimate'] >= 1


# ------------------------------------------------------------------ student links and grading
def make_open_question(doc, candidate, qid='open1'):
    payload = {**candidate, 'type': 'open', 'options': [], 'correct_index': None, 'requested_level': 2, 'checked_level': 2,
               'difficulty_reason': 'Объяснение', 'language': 'ru', 'validation': {'passed': True, 'reasons': []}}
    payload['text'] = 'Объясните своими словами, зачем исполнителю нужен порядок команд.'
    with connect() as db:
        db.execute("INSERT INTO questions(id,document_id,payload,auto_status,review_status) VALUES(?,?,?,'passed','accepted')", (qid, doc, dumps(payload)))


def test_student_link_flow_hides_answers_and_grades(client, doc, saved, candidate, monkeypatch):
    model_env(monkeypatch)
    monkeypatch.setattr(LLM, 'json', provider(candidate))
    make_open_question(doc, candidate)
    created = client.post('/api/assignments', json={'document_id': doc, 'title': 'Контрольная 1', 'count': 5, 'show_answers': False}).json()
    assert created['question_count'] == 2
    token = created['token']
    info = client.get('/api/s/' + token).json()
    assert info['title'] == 'Контрольная 1' and info['is_open']
    started = client.post(f'/api/s/{token}/start', json={'name': 'Айгерим Сапарова'})
    assert started.status_code == 201
    attempt = started.json()
    text = json.dumps(attempt, ensure_ascii=False)
    assert 'reference_answer' not in text and 'correct_index' not in text
    key = attempt['access_key']
    # Without the key another person cannot read the attempt.
    assert client.get(f'/api/s/{token}/attempts/{attempt["id"]}').status_code == 404
    mcq = next(q for q in attempt['questions'] if q['type'] == 'mcq')
    right = mcq['options'].index(candidate['options'][candidate['correct_index']])
    answers = {mcq['id']: right, 'open1': 'Порядок важен, потому что исполнитель выполняет команды по очереди.'}
    result = client.post(f'/api/s/{token}/attempts/{attempt["id"]}/finish', json={'answers': answers}, headers={'X-Attempt-Key': key}).json()
    assert result['correct'] == 1
    result = client.get(f'/api/s/{token}/attempts/{attempt["id"]}', headers={'X-Attempt-Key': key}).json()
    open_q = next(q for q in result['questions'] if q['type'] == 'open')
    assert open_q['grade']['score'] == 8 and result['percent'] == 90
    assert 'reference_answer' not in json.dumps(result, ensure_ascii=False)
    # Teacher sees the result; closing the link stops new attempts.
    listed = client.get('/api/assignments', params={'document_id': doc}).json()[0]
    assert listed['finished'] == 1 and listed['results'][0]['student_name'] == 'Айгерим Сапарова'
    assert client.patch('/api/assignments/' + created['id'], json={'is_open': False}).status_code == 200
    assert client.post(f'/api/s/{token}/start', json={'name': 'Второй студент'}).status_code == 409
    csv_text = client.get(f'/api/assignments/{created["id"]}/results.csv').content.decode('utf-8-sig')
    assert 'Айгерим Сапарова' in csv_text and '90' in csv_text
    stats = client.get(f'/api/documents/{doc}/stats').json()
    assert stats['attempts'] == 1 and stats['percent'] == 90
    # Teacher's own attempts list does not mix in student attempts.
    assert client.get('/api/attempts', params={'document_id': doc}).json() == []


def test_hosted_mode_student_link_is_public_but_bank_is_not(tmp_path, monkeypatch, doc, saved):
    from app.deployment import create_app
    monkeypatch.setenv('APP_ACCESS_PASSWORD', 'test-only-strong-password-2026')
    with TestClient(create_app(), follow_redirects=False) as hosted:
        with connect() as db:
            from app.main import question_pool, snapshot_of
            db.execute("INSERT INTO assignments(id,token,document_id,title,settings,snapshot) VALUES('a1','tok123',?,'Тест','{}',?)",
                       (doc, dumps([snapshot_of(q) for q in question_pool(db, __import__('app.schemas', fromlist=['TestRequest']).TestRequest(document_id=doc))])))
        assert hosted.get('/api/documents').status_code == 401
        assert hosted.get('/api/s/tok123').status_code == 200
        assert hosted.get('/s/tok123').status_code == 200
        assert hosted.post('/api/s/tok123/start', json={'name': 'Студент'}).status_code == 201
        assert hosted.get('/api/s/wrong').status_code == 404


# ------------------------------------------------------------------ export and backup
def test_export_formats(client, doc, saved):
    docx = client.get(f'/api/documents/{doc}/export', params={'format': 'docx'})
    assert docx.status_code == 200 and docx.content[:2] == b'PK'
    from docx import Document
    text = '\n'.join(p.text for p in Document(io.BytesIO(docx.content)).paragraphs)
    assert 'Что такое алгоритм' in text and 'Правильный ответ: A' in text
    student = client.get(f'/api/documents/{doc}/export', params={'format': 'docx', 'answers': False})
    assert 'Правильный ответ' not in '\n'.join(p.text for p in Document(io.BytesIO(student.content)).paragraphs)
    csv_text = client.get(f'/api/documents/{doc}/export', params={'format': 'csv'}).content.decode('utf-8-sig')
    assert 'Что такое алгоритм' in csv_text
    assert client.get(f'/api/documents/{doc}/export', params={'format': 'json'}).json()['questions'][0]['id'] == saved


def test_backup_roundtrip(client, doc, saved):
    attempt = client.post('/api/attempts', json={'document_id': doc}).json()
    dump = client.get('/api/backup').content
    # Simulate a fresh server.
    with connect() as db:
        for table in ('files', 'attempts', 'assignments', 'questions', 'topics', 'jobs', 'fragments', 'documents'):
            db.execute(f'DELETE FROM {table}')
    result = client.post('/api/backup', files={'file': ('backup.json', dump, 'application/json')}).json()
    assert result['added'] == 1
    q = client.get('/api/questions', params={'document_id': doc}).json()[0]
    with connect() as db:
        fragment_ids = {r[0] for r in db.execute('SELECT id FROM fragments WHERE document_id=?', (doc,))}
    assert q['sources'][0]['fragment_id'] in fragment_ids
    assert client.get('/api/attempts/' + attempt['id']).status_code == 200
    again = client.post('/api/backup', files={'file': ('backup.json', dump, 'application/json')}).json()
    assert again['added'] == 0 and again['skipped'] == 1
    assert client.post('/api/backup', files={'file': ('x.json', b'{"nope":1}', 'application/json')}).status_code == 422


# ------------------------------------------------------------------ Kazakh and language
def test_kazakh_detection_ratio():
    assert detect_language('Қазақстан тарихы және мәдениеті туралы оқулық. Бұл тарауда негізгі ұғымдар қарастырылады.') == 'kk'
    russian = 'Исследование операций изучает модели принятия решений. ' * 20 + 'Термин «Қазақстан» встречается один раз.'
    assert detect_language(russian) == 'ru'
    assert detect_language('Qazaqstan Respýblıkasy — táýelsiz memleket, onyń astanasy Astana.') == 'kk'
    assert obvious_language_mismatch('Что такое алгоритм?', 'kk')
    assert not obvious_language_mismatch('Алгоритм дегеніміз не және ол қалай жұмыс істейді?', 'kk')


def test_legacy_kazakh_font_and_kz1048_text():
    original = 'Қазақ тілі — мемлекеттік тіл. Әліпби, ғылым, өнер, ұлт, үй, һауа, ң әрпі.'
    garbled = original.encode('kz1048').decode('cp1251')
    fixed, repaired = repair_kazakh(garbled)
    assert repaired and fixed == original
    assert decode_legacy(original.encode('kz1048')) == original
    assert decode_legacy('Привет, мир. Обычный русский текст.'.encode('cp1251')).startswith('Привет')
    assert not repair_kazakh('Україна є державою, її столиця Київ.')[1]


def test_upload_kazakh_kz1048_txt_and_docx(client):
    text = 'Алгоритм — белгілі бір мақсатқа жету үшін орындалатын әрекеттер тізбегі. Бағдарлама қадамдардан тұрады.'
    response = client.post('/api/documents', files={'file': ('kk.txt', text.encode('kz1048'), 'text/plain')})
    detail = client.get('/api/documents/' + response.json()['id']).json()
    assert detail['status'] == 'ready' and detail['language'] == 'kk'
    from docx import Document
    buffer = io.BytesIO()
    document = Document()
    document.add_paragraph('Деректер құрылымы — деректерді сақтау және өңдеу тәсілі.')
    document.save(buffer)
    response = client.post('/api/documents', files={'file': ('lesson.docx', buffer.getvalue())})
    assert response.status_code == 201
    detail = client.get('/api/documents/' + response.json()['id']).json()
    assert detail['status'] == 'ready' and detail['language'] == 'kk' and detail['format'] == 'docx'


def test_kazakh_retrieval_matches_word_forms():
    from app.services.retrieval import retrieve
    fragments = [{'id': 1, 'text': 'Алгоритмнің қасиеттері: дискреттілік, анықтылық, нәтижелілік.'},
                 {'id': 2, 'text': 'Мемлекеттік рәміздер: ту, елтаңба, әнұран.'}]
    topic = {'name': 'Алгоритмдер', 'keywords': ['алгоритм қасиеті'], 'fragment_ids': []}
    assert retrieve(fragments, topic, limit=1)[0]['id'] == 1


# ------------------------------------------------------------------ programmatic calculation check
def test_calculation_checker():
    assert calc.evaluate_expression('(12*3+4)/2') == 20
    assert calc.check('2*3+4', 'Ответ: 10 единиц')[0] == 'match'
    assert calc.check('2*3+4', 'Ответ: 12')[0] == 'mismatch'
    assert calc.check('__import__("os")', 'Ответ: 12')[0] == 'skipped'
    assert calc.check('10**1000', '1')[0] == 'skipped'
    assert calc.check('0.25', '25 %')[0] == 'match'
    assert calc.check('2,5*2', '5')[0] == 'match'


def test_wrong_calculation_rejects_mcq(candidate, doc):
    from app.schemas import Candidate
    from app.services.validation import evaluate
    task = candidate | {'text': 'Условный пример: 3 станка по 4 детали в час. Сколько деталей за 2 часа?',
                        'options': ['24', '12', '20', '8'], 'correct_index': 2}

    class Reviewer:
        def json(self, instruction, data, schema):
            if schema is Verdict:
                return Verdict(relevant=True, supported=True, clear=True, single_correct=True, is_duplicate=False,
                               language_matches=True, level=3, difficulty_reason='Применение', reasons=[])
            return IndependentAnswer(has_single_answer=True, correct_index=2, answer='24', reasoning='3*4*2', calculation='3*4*2')
    with connect() as db:
        fragments = [dict(r) for r in db.execute('SELECT * FROM fragments WHERE document_id=?', (doc,))]
    payload, status = evaluate(Reviewer(), Candidate.model_validate(task), fragments, doc, 3, target_language='ru')
    assert status == 'failed' and payload['validation']['calc_check']['status'] == 'mismatch'


# ------------------------------------------------------------------ adaptive testing
def bank_with_levels(doc, candidate, levels=(1, 2, 3, 4, 5), per_level=2):
    n = 0
    with connect() as db:
        for level in levels:
            for k in range(per_level):
                n += 1
                payload = {**candidate, 'text': f'Условный вопрос номер {n} про алгоритм и команды исполнителя {n * 13}?',
                           'requested_level': level, 'checked_level': level, 'difficulty_reason': 'тест', 'language': 'ru',
                           'validation': {'passed': True, 'reasons': []}}
                db.execute("INSERT INTO questions(id,document_id,payload,auto_status,review_status) VALUES(?,?,?,'passed','accepted')",
                           (f'ad{n}', doc, dumps(payload)))


def correct_choice(client_question, candidate):
    return client_question['options'].index(candidate['options'][candidate['correct_index']])


def test_adaptive_staircase_and_estimate(client, doc, candidate):
    bank_with_levels(doc, candidate)
    started = client.post('/api/adaptive', json={'document_id': doc, 'length': 6, 'start_level': 2})
    assert started.status_code == 201
    a = started.json()
    assert a['mode'] == 'adaptive' and a['current']['level'] == 2
    assert 'correct_index' not in json.dumps(a)
    q = a['current']['question']
    levels = []
    for step in range(6):
        levels.append(a['current']['level'] if step == 0 else nxt['level'])
        right = step < 3  # three correct, then wrong
        choice = correct_choice(q, candidate) if right else (correct_choice(q, candidate) + 1) % 4
        r = client.post(f'/api/adaptive/{a["id"]}/answer', json={'question_id': q['id'], 'answer': choice}).json()
        assert r['feedback']['correct'] is right
        if r['finished']:
            break
        nxt = r['next']
        q = nxt['question']
    assert levels[:4] == [2, 3, 4, 5]          # up after each correct answer
    assert levels[4] == 4                       # down after a wrong one
    assert r['finished'] and r['result']['total'] == 6
    assert 3.0 <= r['result']['estimate'] <= 4.5
    done = client.get('/api/attempts/' + a['id']).json()
    assert done['status'] == 'finished' and done['adaptive']['estimate'] == r['result']['estimate']
    # answering twice is rejected
    assert client.post(f'/api/adaptive/{a["id"]}/answer', json={'question_id': q['id'], 'answer': 0}).status_code == 409


def test_adaptive_needs_mcq_pool(client, doc, candidate):
    assert client.post('/api/adaptive', json={'document_id': doc}).status_code == 409


def test_student_adaptive_link(client, doc, candidate):
    bank_with_levels(doc, candidate, levels=(1, 2, 3), per_level=3)
    a = client.post('/api/assignments', json={'document_id': doc, 'title': 'Адаптив', 'adaptive': True,
                                              'adaptive_length': 4, 'show_answers': False}).json()
    info = client.get('/api/s/' + a['token']).json()
    assert info['adaptive'] and info['question_count'] == 4
    s = client.post(f'/api/s/{a["token"]}/start', json={'name': 'Студент А'}).json()
    key = s['access_key']
    assert s['current']['level'] == 2
    q = s['current']['question']
    for _ in range(4):
        r = client.post(f'/api/s/{a["token"]}/attempts/{s["id"]}/answer', json={'question_id': q['id'], 'answer': correct_choice(q, candidate)},
                        headers={'X-Attempt-Key': key}).json()
        assert 'correct_index' not in r['feedback']
        if r['finished']:
            break
        q = r['next']['question']
    assert r['finished'] and r['result']['correct'] == 4
    listed = client.get('/api/assignments', params={'document_id': doc}).json()[0]
    assert listed['results'][0]['estimate'] >= 2.5


# ------------------------------------------------------------------ source viewer and verification statuses
def make_pdf_bytes():
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((60, 100), 'Algorithm is an ordered list of steps.', fontname='helv')
    page.insert_text((60, 130), 'A program is an algorithm written for a computer.', fontname='helv')
    return doc.tobytes()


def test_source_viewer_pdf_page_and_highlight(client, candidate, monkeypatch):
    response = client.post('/api/documents', files={'file': ('lesson.pdf', make_pdf_bytes(), 'application/pdf')})
    doc = response.json()['id']
    with connect() as db:
        fragment = db.execute('SELECT * FROM fragments WHERE document_id=?', (doc,)).fetchone()
        payload = {**candidate, 'text': 'What is an algorithm in this lesson?', 'language': 'en', 'requested_level': 1, 'checked_level': 1,
                   'sources': [{'fragment_id': fragment['id'], 'quote': 'Algorithm is an ordered list of steps.', 'match': 'exact', 'location': 'p1'}],
                   'validation': {'passed': True, 'reasons': []}}
        db.execute("INSERT INTO questions(id,document_id,payload,auto_status) VALUES('src1',?,?,'passed')", (doc, dumps(payload)))
    data = client.get('/api/questions/src1/source').json()
    assert data['pdf_pages'] and data['verification'] == 'confirmed'
    source = data['sources'][0]
    assert source['page'] == 1
    start, end = source['highlight']
    assert source['fragment_text'][start:end] == 'Algorithm is an ordered list of steps.'
    image = client.get(f'/api/documents/{doc}/pages/1.png', params={'q': source['quote']})
    assert image.status_code == 200 and image.content[:4] == b'\x89PNG'
    assert client.get(f'/api/documents/{doc}/pages/9.png').status_code == 404
    assert client.get(f'/api/documents/{doc}/file').content[:4] == b'%PDF'


def test_verification_statuses_and_test_exclusion(client, doc, saved, candidate):
    items = {q['id']: q for q in client.get('/api/questions', params={'document_id': doc}).json()}
    assert items[saved]['verification'] == 'confirmed'
    with connect() as db:
        bad = {**candidate, 'text': 'Сколько лет исполнителю в этом уроке?', 'validation': {'passed': False, 'reasons': ['Ответ не подтверждается источником.']}}
        db.execute("INSERT INTO questions(id,document_id,payload,auto_status,review_status) VALUES('bad1',?,?,'failed','pending')", (doc, dumps(bad)))
        fuzzy = {**candidate, 'text': 'Какой порядок выполнения команд у исполнителя по условию?', 'requested_level': 1, 'checked_level': 1,
                 'sources': [{**candidate['sources'][0], 'match': 'partial'}], 'validation': {'passed': True, 'reasons': []}}
        db.execute("INSERT INTO questions(id,document_id,payload,auto_status,review_status) VALUES('fz1',?,?,'passed','pending')", (doc, dumps(fuzzy)))
    by = {q['id']: q for q in client.get('/api/questions', params={'document_id': doc}).json()}
    assert by['bad1']['verification'] == 'rejected' and by['fz1']['verification'] == 'review'
    assert [q['id'] for q in client.get('/api/questions', params={'document_id': doc, 'verification': 'rejected'}).json()] == ['bad1']
    # an unconfirmed question cannot be accepted and so never reaches a test
    assert client.post('/api/questions/bad1/review', json={'status': 'accepted', 'revision': 1}).status_code == 409
    attempt = client.post('/api/attempts', json={'document_id': doc, 'count': 10}).json()
    assert 'bad1' not in [q['id'] for q in attempt['questions']]


def test_generation_repairs_a_failed_question(client, doc, candidate, monkeypatch):
    model_env(monkeypatch)
    state = {'batch': 0}

    def fake(self, instruction, data, schema):
        if schema is Batch:
            state['batch'] += 1
            if 'previous_question' in data and 'problems' in data:
                return Batch(questions=[candidate | {'text': 'Исправленный вопрос: что такое алгоритм по тексту урока?'}], reason='')
            return Batch(questions=[candidate | {'sources': [{'fragment_id': candidate['sources'][0]['fragment_id'], 'quote': 'Этой цитаты нет в документе вообще нигде'}]}], reason='')
        if schema is Verdict:
            return Verdict(relevant=True, supported=True, clear=True, single_correct=True, is_duplicate=False, language_matches=True,
                           level=1, difficulty_reason='Определение', reasons=[])
        raise AssertionError(schema)
    monkeypatch.setattr(LLM, 'json', fake)
    client.post('/api/generate', json={'document_id': doc, 'count': 1, 'type': 'mcq', 'level': 1})
    job = client.get('/api/jobs', params={'document_id': doc}).json()[0]
    assert job['created'] == 1
    q = client.get('/api/questions', params={'document_id': doc}).json()[0]
    assert q['text'].startswith('Исправленный') and q['auto_fixed']['passed'] and q['verification'] == 'confirmed'


def test_rejected_question_does_not_block_a_new_one(client, doc, candidate, monkeypatch):
    from app.database import connect
    model_env(monkeypatch)
    no_sleep(monkeypatch)
    monkeypatch.setattr(LLM, 'json', provider(candidate, variants=False))
    client.post('/api/generate', json={'document_id': doc, 'count': 1, 'type': 'mcq', 'level': 1})
    assert client.get("/api/jobs", params={"document_id": doc}).json()[0]["created"] == 1
    with connect() as db:
        db.execute("UPDATE questions SET auto_status='failed' WHERE document_id=?", (doc,))
    client.post('/api/generate', json={'document_id': doc, 'count': 1, 'type': 'mcq', 'level': 1})
    assert client.get("/api/jobs", params={"document_id": doc}).json()[0]["created"] == 1
