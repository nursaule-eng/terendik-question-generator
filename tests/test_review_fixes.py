import hashlib
import json

from app import database
from app.database import connect, dumps, init_db
from app.schemas import Candidate, Verdict
from app.services.language import detect_language
from app.services.options import shuffle_options
from app.services.validation import duplicate, evaluate


class FixedPermutation:
    def shuffle(self, values):
        values[:] = [1, 2, 3, 0]


def test_option_permutation_keeps_answer_content(candidate):
    original = json.loads(json.dumps(candidate))
    shuffled = shuffle_options(candidate, FixedPermutation())
    assert shuffled['correct_index'] == 3
    assert shuffled['options'][3] == candidate['options'][candidate['correct_index']]
    assert sorted(shuffled['options']) == sorted(candidate['options'])
    assert candidate == original
    opened = candidate | {'type': 'open', 'options': [], 'correct_index': None}
    assert shuffle_options(opened, FixedPermutation()) == opened


def test_legacy_bank_shuffled_once_per_attempt_and_graded(client, saved, doc, monkeypatch):
    monkeypatch.setattr('app.main.shuffle_options', lambda q: shuffle_options(q, FixedPermutation()))
    a = client.post('/api/attempts', json={'document_id': doc}).json()
    assert a['questions'][0]['options'][3] == 'Конечная последовательность точных действий'
    assert 'correct_index' not in a['questions'][0]
    assert client.get('/api/attempts/' + a['id']).json()['questions'] == a['questions']
    result = client.post('/api/attempts/' + a['id'] + '/finish', json={'answers': {saved: 3}}).json()
    assert result['correct'] == 1 and result['questions'][0]['correct_index'] == 3
    assert client.get('/api/questions', params={'document_id': doc}).json()[0]['correct_index'] == 0


def test_kazakh_and_russian_question_paraphrases():
    assert duplicate('Операцияларды зерттеудің негізгі мақсаты қандай?', ['Операцияларды зерттеудің мақсаты қандай?'])
    assert duplicate('Математикалық модель дегеніміз не?', ['Математикалық модель деген не?'])
    assert duplicate('Какова основная цель исследования операций?', ['Назовите цель исследования операций.'])
    assert not duplicate('Математикалық модель дегеніміз не?', ['Тиімділік критерийі дегеніміз не?'])


def test_semantic_duplicate_and_language_are_independent_checks(candidate, doc):
    with connect() as db:
        fragments = [dict(r) for r in db.execute('SELECT * FROM fragments WHERE document_id=?', (doc,))]
    for verdict in ({'is_duplicate': True}, {'language_matches': False}):
        class Reviewer:
            def json(self, instruction, data, schema):
                assert data['existing_questions'][0]['text'] == 'Дайте определение алгоритма.'
                return Verdict(relevant=True, supported=True, clear=True, single_correct=True, level=1,
                               difficulty_reason='Определение из текста', reasons=[], **({'is_duplicate': False, 'language_matches': True} | verdict))
        payload, status = evaluate(Reviewer(), Candidate.model_validate(candidate), fragments, doc, 1,
                                   existing_questions=[{'text': 'Дайте определение алгоритма.'}], target_language='ru')
        assert status == 'failed' and payload['validation']['reasons']


def test_language_detection_and_obvious_wrong_language(candidate, doc):
    assert detect_language('Амалдарды зерттеу пәнінің негізгі ұғымдары') == 'kk'
    assert detect_language('Алгоритм представляет последовательность действий') == 'ru'
    assert detect_language('An algorithm is a sequence of steps') == 'en'
    class Reviewer:
        def json(self, *args):
            return Verdict(relevant=True, supported=True, clear=True, single_correct=True, level=1, is_duplicate=False, language_matches=True,
                           difficulty_reason='Определение из текста', reasons=[])
    with connect() as db:
        fragments = [dict(r) for r in db.execute('SELECT * FROM fragments WHERE document_id=?', (doc,))]
    result, status = evaluate(Reviewer(), Candidate.model_validate(candidate), fragments, doc, 1, target_language='kk')
    assert status == 'failed' and any('Язык' in reason for reason in result['validation']['reasons'])


def test_duplicate_upload_is_same_document_even_with_new_filename(client):
    content = 'Қазақша материал: операцияларды зерттеу пәні.'.encode()
    first = client.post('/api/documents', files={'file': ('lesson.txt', content)})
    second = client.post('/api/documents', files={'file': ('renamed.txt', content)})
    assert first.status_code == 201 and second.status_code == 200
    assert second.json()['already_uploaded'] and first.json()['id'] == second.json()['id']
    assert len(client.get('/api/documents').json()) == 1
    assert len(list((database.DATA / 'uploads').iterdir())) == 1
    changed = client.post('/api/documents', files={'file': ('lesson.txt', content + b' changed')})
    assert changed.status_code == 201 and changed.json()['id'] != first.json()['id']


def test_hash_and_language_backfilled_for_existing_document(client):
    content = 'Негізгі ұғымдар мен құралдар'.encode()
    doc = client.post('/api/documents', files={'file': ('lesson.txt', content)}).json()['id']
    with connect() as db:
        db.execute("UPDATE documents SET file_hash=NULL,language='ru' WHERE id=?", (doc,))
    init_db()
    with connect() as db:
        row = db.execute('SELECT file_hash,language FROM documents WHERE id=?', (doc,)).fetchone()
    assert row['file_hash'] == hashlib.sha256(content).hexdigest() and row['language'] == 'kk'


def test_document_trash_restore_preserves_questions_and_attempt(client, doc, saved):
    attempt = client.post('/api/attempts', json={'document_id': doc}).json()
    assert client.delete('/api/documents/' + doc).status_code == 200
    assert client.get('/api/documents/' + doc).status_code == 404
    assert not client.get('/api/documents').json()
    assert client.get('/api/documents?archived=true').json()[0]['id'] == doc
    assert client.get('/api/attempts/' + attempt['id']).json()['questions'] == attempt['questions']
    assert client.post('/api/documents/' + doc + '/restore').status_code == 200
    assert client.get('/api/questions', params={'document_id': doc}).json()[0]['id'] == saved
    assert client.get('/api/documents/' + doc + '/bank-status').json()['accepted_for_test'] == 1


def test_null_difficulty_cannot_be_accepted_or_tested(client, doc, saved):
    with connect() as db:
        payload = json.loads(db.execute('SELECT payload FROM questions WHERE id=?', (saved,)).fetchone()[0])
        payload['checked_level'] = None
        db.execute('UPDATE questions SET payload=? WHERE id=?', (dumps(payload), saved))
    assert client.post('/api/questions/' + saved + '/review', json={'status': 'accepted', 'revision': 1}).status_code == 409
    assert client.post('/api/attempts', json={'document_id': doc}).status_code == 409
    assert client.get('/api/documents/' + doc + '/bank-status').json()['unchecked_level'] == 1


def test_existing_duplicate_visible_and_cannot_be_accepted_twice(client, doc, saved):
    with connect() as db:
        payload = db.execute('SELECT payload FROM questions WHERE id=?', (saved,)).fetchone()[0]
        db.execute("INSERT INTO questions(id,document_id,payload,auto_status) VALUES('copy',?,?,'passed')", (doc, payload))
    items = client.get('/api/questions', params={'document_id': doc}).json()
    copied = next(q for q in items if q['id'] == 'copy')
    assert copied['possible_duplicate']
    assert client.post('/api/questions/copy/review', json={'status': 'accepted', 'revision': 1}).status_code == 409
    assert client.get('/api/documents/' + doc + '/bank-status').json()['accepted_for_test'] == 1


def test_generated_language_and_shuffled_bank_answer(client, doc, candidate, monkeypatch):
    from app.schemas import Batch
    from app.services.llm import LLM
    for name, value in [('LLM_API_KEY', 'TEST_ONLY'), ('LLM_BASE_URL', 'https://example.invalid'), ('LLM_MODEL', 'TEST_ONLY')]:
        monkeypatch.setenv(name, value)
    kk = candidate | {'text': 'Алгоритм дегеніміз не?', 'options': ['Мақсатқа жететін нақты әрекеттер тізбегі', 'Кездейсоқ таңбалар', 'Бір ғана әріп', 'Компьютер атауы'],
                      'reference_answer': 'Нақты әрекеттер тізбегі', 'explanation': 'Материалда алгоритмнің анықтамасы берілген.'}
    def provider(self, instruction, data, schema):
        assert data['target_language'].startswith('қазақ')
        if schema is Batch:
            return Batch(questions=[kk], reason='Enough material')
        return Verdict(relevant=True, supported=True, clear=True, single_correct=True, level=1, is_duplicate=False, language_matches=True,
                       difficulty_reason='Анықтаманы есте сақтау', reasons=[])
    monkeypatch.setattr(LLM, 'json', provider)
    monkeypatch.setattr('app.services.generation.shuffle_options', lambda q: shuffle_options(q, FixedPermutation()))
    assert client.post('/api/generate', json={'document_id': doc, 'count': 1, 'type': 'mcq', 'language': 'kk'}).status_code == 202
    q = client.get('/api/questions', params={'document_id': doc}).json()[0]
    assert q['language'] == 'kk' and q['auto_status'] == 'passed'
    assert q['correct_index'] == 3 and q['options'][3] == kk['options'][0]
    job = client.get('/api/jobs', params={'document_id': doc}).json()[0]
    assert 'Enough material' not in job['message'] and job['details'] == ['Enough material']


def test_reviewer_must_explicitly_check_language_and_semantic_duplicates():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError) as exc:
        Verdict.model_validate({'relevant': True, 'supported': True, 'clear': True, 'single_correct': True,
                                'level': 1, 'difficulty_reason': 'Определение из текста', 'reasons': []})
    assert {error['loc'][0] for error in exc.value.errors()} == {'is_duplicate', 'language_matches'}
