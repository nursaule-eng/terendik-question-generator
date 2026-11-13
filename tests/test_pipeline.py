import json

from app.database import connect, dumps, init_db
from app.schemas import Batch, TopicBatch, Verdict
from app.services.llm import LLM, ModelError


def stub_provider(monkeypatch, candidate, supported=True):
    """TEST ONLY. No demo/fake provider is shipped in the application."""
    monkeypatch.setenv('LLM_API_KEY', 'TEST_ONLY')
    monkeypatch.setenv('LLM_BASE_URL', 'https://example.invalid/v1')
    monkeypatch.setenv('LLM_MODEL', 'TEST_ONLY')
    calls = []
    def fake_json(self, instruction, data, schema):
        calls.append((schema, data))
        if schema is TopicBatch:
            return TopicBatch(topics=[{'name': 'Алгоритмы', 'keywords': ['алгоритм'], 'fragment_ids': [f['fragment_id'] for f in data['fragments']]}])
        if schema is Batch:
            return Batch(questions=[candidate], reason='')
        if schema is Verdict:
            return Verdict(relevant=True, supported=supported, clear=True, single_correct=supported, is_duplicate=False, language_matches=True,
                           level=1, difficulty_reason='Воспроизведение определения', reasons=[] if supported else ['Ответ не подтверждён'])
        raise AssertionError('Unexpected model operation')
    monkeypatch.setattr(LLM, 'json', fake_json)
    return calls


def test_full_api_pipeline_with_explicit_test_double(client, candidate, doc, monkeypatch):
    calls = stub_provider(monkeypatch, candidate)
    assert client.post('/api/documents/' + doc + '/topics').status_code == 202
    topic = client.get('/api/documents/' + doc).json()['topics'][0]
    assert topic['fragment_ids'] == [candidate['sources'][0]['fragment_id']]
    params = {'document_id': doc, 'topic_id': topic['id'], 'count': 1, 'type': 'mcq', 'level': 1}
    response = client.post('/api/generate', json=params)
    assert response.status_code == 202
    job = client.get('/api/jobs', params={'document_id': doc}).json()[0]
    assert job['status'] == 'done' and job['created'] == 1
    q = client.get('/api/questions', params={'document_id': doc}).json()[0]
    assert q['review_status'] == 'pending' and q['auto_status'] == 'passed'
    assert client.post('/api/attempts', json=params).status_code == 409
    assert client.post('/api/questions/' + q['id'] + '/review', json={'status': 'accepted', 'revision': q['revision']}).status_code == 200
    a = client.post('/api/attempts', json=params).json()
    assert 'reference_answer' not in json.dumps(a)
    answer_index = a['questions'][0]['options'].index(candidate['options'][candidate['correct_index']])
    result = client.post('/api/attempts/' + a['id'] + '/finish', json={'answers': {q['id']: answer_index}}).json()
    assert result['correct'] == 1
    edited = candidate | {'text': 'Как материал определяет алгоритм и его назначение?', 'requested_level': 1, 'revision': q['revision'] + 1}
    assert client.put('/api/questions/' + q['id'], json=edited).status_code == 200
    assert client.post('/api/questions/' + q['id'] + '/recheck').status_code == 200
    revised = client.get('/api/questions', params={'document_id': doc}).json()[0]
    assert revised['auto_status'] == 'passed' and revised['review_status'] == 'pending'
    assert len([s for s, _ in calls if s is Verdict]) == 2
    # Close/open connections and simulate restart; questions and completed attempts survive.
    init_db()
    assert client.get('/api/attempts/' + a['id']).json()['correct'] == 1
    assert client.get('/api/questions', params={'document_id': doc}).json()[0]['text'] == edited['text']


def test_semantic_rejection_and_shortfall_reported(client, candidate, doc, monkeypatch):
    stub_provider(monkeypatch, candidate, supported=False)
    client.post('/api/generate', json={'document_id': doc, 'count': 1, 'type': 'mcq'})
    job = client.get('/api/jobs', params={'document_id': doc}).json()[0]
    assert job['created'] == 0 and job['rejected'] == 2 and 'недостаточно' in job['message']
    items = client.get('/api/questions', params={'document_id': doc}).json()
    assert all(q['auto_status'] == 'failed' for q in items)
    assert any('Ответ не подтверждён' in str(q['validation']) for q in items)


def test_model_failure_keeps_document_and_job_error(client, doc, monkeypatch):
    monkeypatch.setenv('LLM_API_KEY', 'TEST_ONLY')
    monkeypatch.setenv('LLM_BASE_URL', 'https://example.invalid/v1')
    monkeypatch.setenv('LLM_MODEL', 'TEST_ONLY')
    def failing(*args, **kwargs):
        raise ModelError('Тестовая недоступность модели')
    monkeypatch.setattr(LLM, 'json', failing)
    client.post('/api/generate', json={'document_id': doc, 'count': 1})
    assert client.get('/api/documents/' + doc).json()['status'] == 'ready'
    job = client.get('/api/jobs', params={'document_id': doc}).json()[0]
    assert job['status'] == 'error' and 'Тестовая недоступность модели' in job['message']
    assert 'сохранено допустимых 0' in job['message']


def test_long_document_topics_examine_all_fragments(client, doc, candidate, monkeypatch):
    with connect() as db:
        for n in range(2, 20):
            db.execute('INSERT INTO fragments(document_id,ordinal,text) VALUES(?,?,?)', (doc, n, f'Алгоритм раздела {n}'))
        expected = {r[0] for r in db.execute('SELECT id FROM fragments WHERE document_id=?', (doc,))}
    calls = stub_provider(monkeypatch, candidate)
    client.post('/api/documents/' + doc + '/topics')
    examined = {f['fragment_id'] for schema, data in calls if schema is TopicBatch for f in data['fragments']}
    assert examined == expected
    topics = client.get('/api/documents/' + doc).json()['topics']
    assert len(topics) == 1 and set(topics[0]['fragment_ids']) == expected


def test_duplicate_jobs_and_foreign_topics_are_blocked(client, candidate, doc, monkeypatch):
    stub_provider(monkeypatch, candidate)
    with connect() as db:
        db.execute("INSERT INTO jobs(id,document_id,status,requested) VALUES('busy',?,'queued',1)", (doc,))
    assert client.post('/api/generate', json={'document_id': doc}).status_code == 409
    assert client.post('/api/generate', json={'document_id': doc, 'topic_id': 99999}).status_code == 422


def test_cross_origin_mutations_and_secrets(client):
    response = client.post('/api/model/check', headers={'Origin': 'https://example.invalid'})
    assert response.status_code == 403
    home = client.get('/')
    assert 'Content-Security-Policy' in home.headers
    assert 'LLM_API_KEY=' not in home.text
