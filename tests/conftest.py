import pytest
from fastapi.testclient import TestClient

from app import database
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(database, 'DATA', tmp_path / 'data')
    for name in ('LLM_API_KEY', 'LLM_BASE_URL', 'LLM_MODEL'):
        monkeypatch.delenv(name, raising=False)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def doc(client):
    response = client.post('/api/documents', files={'file': ('lesson.txt', 'Алгоритм — конечная последовательность точных действий для решения задачи.\n\nИсполнитель выполняет команды алгоритма по порядку.'.encode(), 'text/plain')})
    assert response.status_code == 201
    return response.json()['id']


@pytest.fixture
def candidate(doc):
    from app.database import connect
    with connect() as db:
        fragment = dict(db.execute('SELECT * FROM fragments WHERE document_id=?', (doc,)).fetchone())
    return {'text': 'Что такое алгоритм согласно материалу?', 'type': 'mcq',
            'options': ['Конечная последовательность точных действий', 'Случайный набор знаков', 'Только один символ', 'Название компьютера'],
            'correct_index': 0, 'reference_answer': 'Конечная последовательность точных действий для решения задачи.',
            'explanation': 'Определение дано в первом абзаце.', 'topic': 'Алгоритмы',
            'sources': [{'fragment_id': fragment['id'], 'quote': 'конечная последовательность точных действий для решения задачи.'}]}


@pytest.fixture
def saved(candidate, doc):
    from app.database import connect, dumps
    payload = {**candidate, 'requested_level': 1, 'checked_level': 1, 'difficulty_reason': 'Воспроизведение определения',
               'level_mismatch': False, 'needs_recheck': False,
               'validation': {'passed': True, 'reasons': []}}
    payload['sources'] = [{**s, 'location': 'Фрагмент TXT №1'} for s in payload['sources']]
    with connect() as db:
        db.execute("INSERT INTO questions(id,document_id,payload,auto_status,review_status) VALUES('q1',?,?,'passed','accepted')", (doc, dumps(payload)))
    return 'q1'
