import itertools
import json
from pathlib import Path

import pytest

from app.database import connect, dumps
from app.schemas import Batch, Generate, TopicBatch
from app.services.llm import LLM, ModelError
from app.services.options import shuffle_options
from app.services.validation import duplicate, annotate_duplicates, duplicate_match
from app.services.validation import evaluate
from app.schemas import Candidate, Verdict, IndependentAnswer

PAIRS = json.loads((Path(__file__).parent / 'data' / 'duplicate_pairs.json').read_text('utf-8'))


@pytest.mark.parametrize('pair', PAIRS, ids=[str(i) + '-' + p['source'] for i, p in enumerate(PAIRS)])
def test_calibrated_duplicate_examples(pair):
    assert duplicate(pair['a'], [pair['b']]) is pair['duplicate']
    assert duplicate(pair['b'], [pair['a']]) is pair['duplicate']
    # Unrelated bank questions must not alter a pair's matching threshold.
    assert duplicate(pair['a'], [pair['b'], 'Какие вещества входят в состав клетки?']) is pair['duplicate']


def test_rejected_legacy_duplicate_also_has_a_visible_flag(client, doc, saved):
    with connect() as db:
        payload = db.execute('SELECT payload FROM questions WHERE id=?', (saved,)).fetchone()[0]
        db.execute("INSERT INTO questions(id,document_id,payload,auto_status) VALUES('rejected_copy',?,?,'failed')", (doc, payload))
    items = client.get('/api/questions', params={'document_id': doc}).json()
    assert all(q['possible_duplicate'] for q in items)
    failed = next(q for q in items if q['auto_status'] == 'failed')
    assert failed['duplicate_matches'][0]['method'] == 'normalized_exact'
    assert client.post('/api/attempts', json={'document_id': doc}).json()['available'] == 1


def test_mixed_levels_do_not_get_stuck_on_failed_level(client, doc, monkeypatch):
    from test_pipeline import stub_provider
    monkeypatch.setenv('LLM_API_KEY', 'TEST_ONLY')
    monkeypatch.setenv('LLM_BASE_URL', 'https://example.invalid')
    monkeypatch.setenv('LLM_MODEL', 'TEST_ONLY')
    requested = []
    def provider(self, instruction, data, schema):
        assert schema is Batch
        assert data['cognitive_task']
        requested.append(data['level'])
        return Batch(questions=[], reason='Материал не поддерживает новое задание')
    monkeypatch.setattr(LLM, 'json', provider)
    client.post('/api/generate', json={'document_id': doc, 'count': 5, 'level': None})
    assert requested == [1, 2, 3, 4, 5, 1, 2, 3, 4, 5]
    job = client.get('/api/jobs', params={'document_id': doc}).json()[0]
    assert job['created'] == 0 and job['status'] == 'done'


def test_duplicate_in_same_generation_job_is_not_admitted(client, doc, candidate, monkeypatch):
    from test_pipeline import stub_provider
    stub_provider(monkeypatch, candidate)
    client.post('/api/generate', json={'document_id': doc, 'count': 2, 'type': 'mcq', 'level': 1})
    items = client.get('/api/questions', params={'document_id': doc}).json()
    assert sum(q['auto_status'] == 'passed' for q in items) == 1
    assert all(q['review_status'] == 'pending' for q in items)
    assert any('похожий вопрос' in str(q['validation']).casefold() for q in items if q['auto_status'] == 'failed')


def test_every_correct_index_survives_every_option_permutation(candidate):
    for correct in range(4):
        for order in itertools.permutations(range(4)):
            class Permutation:
                def shuffle(self, indices):
                    indices[:] = order
            original = candidate | {'correct_index': correct}
            result = shuffle_options(original, Permutation())
            assert result['options'][result['correct_index']] == original['options'][correct]
            assert original['correct_index'] == correct


def test_independent_solution_index_follows_option_shuffle(candidate):
    class Permutation:
        def shuffle(self, indices):
            indices[:] = [1, 2, 3, 0]
    original = candidate | {'validation': {'independent_solution': {'has_single_answer': True, 'correct_index': 0,
                                                                  'answer': candidate['options'][0], 'reasoning': 'Технический пример'}}}
    shuffled = shuffle_options(original, Permutation())
    assert shuffled['correct_index'] == shuffled['validation']['independent_solution']['correct_index'] == 3
    assert original['validation']['independent_solution']['correct_index'] == 0
    again = shuffle_options(shuffled, Permutation())
    assert again['correct_index'] == again['validation']['independent_solution']['correct_index'] == 2


def test_successful_topic_retry_clears_stored_error(client, doc, monkeypatch):
    monkeypatch.setenv('LLM_API_KEY', 'TEST_ONLY')
    monkeypatch.setenv('LLM_BASE_URL', 'https://example.invalid')
    monkeypatch.setenv('LLM_MODEL', 'TEST_ONLY')
    def provider(self, instruction, data, schema):
        return TopicBatch(topics=[{'name': 'Алгоритм', 'keywords': ['алгоритм'], 'fragment_ids': [data['fragments'][0]['fragment_id']]}])
    monkeypatch.setattr(LLM, 'json', provider)
    assert client.get('/api/documents/' + doc).json()['topic_error']
    client.post('/api/documents/' + doc + '/topics')
    after = client.get('/api/documents/' + doc).json()
    assert after['topic_status'] == 'ready' and after['topic_error'] is None


def test_blind_solution_rejects_anchor_answer_and_never_sees_it(candidate, doc):
    with connect() as db:
        fragments = [dict(r) for r in db.execute('SELECT * FROM fragments WHERE document_id=?', (doc,))]
    class Reviewer:
        def json(self, instruction, data, schema):
            if schema is Verdict:
                return Verdict(relevant=True, supported=True, clear=True, single_correct=True, level=3,
                               difficulty_reason='Применение метода', reasons=[], is_duplicate=False, language_matches=True)
            assert schema is IndependentAnswer
            assert set(data) == {'text', 'options'}
            assert 'correct_index' not in data and 'reference_answer' not in data and 'explanation' not in data
            return IndependentAnswer(has_single_answer=True, correct_index=2,
                                     answer='Независимо найден другой вариант', reasoning='Подставлены именно условия вопроса')
    payload, status = evaluate(Reviewer(), Candidate.model_validate(candidate), fragments, doc, 3, target_language='ru')
    assert status == 'failed'
    assert payload['validation']['independent_solution']['correct_index'] == 2
    assert any('Независимое решение' in r for r in payload['validation']['reasons'])


def test_reproduced_numeric_anchor_error_is_rejected(candidate, doc):
    # Observed real-model failure: it copied (1.5,2) from a robust example with
    # time=3.5 although the generated question explicitly used time=4.
    q = candidate | {
        'text': 'При добавлении y<=2 найдите максимум F=3x+4y при x+y<=4, x+2y<=6, x>=0, y>=0.',
        'options': ['(2,2)', '(1.5,2)', '(1,3)', '(0,3)'], 'correct_index': 1,
        'reference_answer': '(1.5,2)', 'explanation': 'Готовый ответ взят из другого примера.'}
    vertices = [(0, 0), (4, 0), (2, 2), (0, 2)]
    feasible = [(x, y) for x, y in vertices if x+y <= 4 and x+2*y <= 6 and y <= 2]
    optimum = max(feasible, key=lambda p: 3*p[0] + 4*p[1])
    assert optimum == (2, 2)
    assert 3*optimum[0]+4*optimum[1] == 14 > 3*1.5+4*2
    class Reviewer:
        def json(self, instruction, data, schema):
            if schema is Verdict:
                return Verdict(relevant=True, supported=True, clear=True, single_correct=True, level=3,
                               difficulty_reason='Применение метода вершин', reasons=[], is_duplicate=False, language_matches=True)
            assert 'correct_index' not in data
            assert 'fragments' not in data
            return IndependentAnswer(has_single_answer=True, correct_index=0, answer='(2,2), значение 14',
                                     reasoning='Для условий вопроса это допустимая вершина с наибольшим значением; (1.5,2) даёт лишь 12.5.')
    with connect() as db:
        fragments = [dict(r) for r in db.execute('SELECT * FROM fragments WHERE document_id=?', (doc,))]
    payload, status = evaluate(Reviewer(), Candidate.model_validate(q), fragments, doc, 3, target_language='ru')
    assert status == 'failed' and not payload['validation']['passed']
