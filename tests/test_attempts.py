import json

from app.database import connect, dumps


def test_attempt_hides_answers_grades_and_uses_snapshot(client, saved, doc):
    response = client.post('/api/attempts', json={'document_id': doc, 'count': 10, 'level': None}).json()
    aid = response['id']
    forbidden = ('correct_index', 'reference_answer', 'explanation', 'sources', 'validation', 'checked_level', 'payload', 'snapshot')
    active = client.get('/api/attempts/' + aid).json()
    for payload in (response, active):
        raw = json.dumps(payload)
        assert all(key not in raw for key in forbidden)
    # Change/delete the bank: the old attempt must still be graded with its private snapshot.
    correct_index = active['questions'][0]['options'].index('Конечная последовательность точных действий')
    client.delete('/api/questions/' + saved)
    result = client.post('/api/attempts/' + aid + '/finish', json={'answers': {'q1': correct_index}}).json()
    assert result['correct'] == result['total'] == 1
    assert result['questions'][0]['correct_index'] == correct_index
    assert result['questions'][0]['reference_answer']
    second = client.post('/api/attempts/' + aid + '/finish', json={'answers': {'q1': 2}}).json()
    assert second['correct'] == 1  # immutable completion


def test_wrong_skipped_invalid_answers(client, saved, doc):
    for skipped in (False, True):
        a = client.post('/api/attempts', json={'document_id': doc}).json()
        aid = a['id']
        index = a['questions'][0]['options'].index('Конечная последовательность точных действий')
        answers = {} if skipped else {'q1': (index + 1) % 4}
        result = client.post('/api/attempts/' + aid + '/finish', json={'answers': answers}).json()
        assert result['total'] == 1 and result['correct'] == 0
    aid = client.post('/api/attempts', json={'document_id': doc}).json()['id']
    for bad in ({'q1': 99}, {'q1': '0'}, {'foreign': 0}):
        assert client.post('/api/attempts/' + aid + '/finish', json={'answers': bad}).status_code == 422
    assert client.get('/api/attempts/' + aid).json()['status'] == 'active'


def test_open_answer_self_scores_and_persistence(client, saved, doc):
    with connect() as db:
        row = db.execute('SELECT payload FROM questions WHERE id=?', (saved,)).fetchone()
        p = json.loads(row[0]) | {'type': 'open', 'options': [], 'correct_index': None}
        db.execute('UPDATE questions SET payload=? WHERE id=?', (dumps(p), saved))
    aid = client.post('/api/attempts', json={'document_id': doc}).json()['id']
    assert client.post('/api/attempts/' + aid + '/self-scores', json={'scores': {'q1': 4}}).status_code == 409
    result = client.post('/api/attempts/' + aid + '/finish', json={'answers': {'q1': 'Мой открытый ответ'}}).json()
    assert result['total'] == 0 and result['questions'][0]['correct'] is None
    assert client.post('/api/attempts/' + aid + '/self-scores', json={'scores': {'q1': 6}}).status_code == 422
    assert client.post('/api/attempts/' + aid + '/self-scores', json={'scores': {'q1': 4}}).status_code == 200
    with connect() as db:
        row = db.execute('SELECT * FROM attempts WHERE id=?', (aid,)).fetchone()
        assert json.loads(row['answers'])['q1'] == 'Мой открытый ответ'
        assert json.loads(row['self_scores'])['q1'] == 4
