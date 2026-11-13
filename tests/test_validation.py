import copy

import pytest
from pydantic import ValidationError

from app.database import connect
from app.schemas import Candidate, Verdict
from app.services.validation import duplicate, evaluate, validate_sources


def test_mcq_and_open_schema(candidate):
    assert Candidate.model_validate(candidate).correct_index == 0
    opened = candidate | {'type': 'open', 'options': [], 'correct_index': None}
    assert Candidate.model_validate(opened).type == 'open'
    for change in ({'options': ['a', 'b']}, {'correct_index': 4}, {'options': ['a', 'a', 'b', 'c']}, {'reference_answer': ''}, {'type': 'open'}):
        with pytest.raises(ValidationError):
            Candidate.model_validate(candidate | change)


def test_sources_reject_missing_foreign_and_wrong_quote(candidate, doc, client):
    other = client.post('/api/documents', files={'file': ('other.txt', 'Другая книга описывает растения и фотосинтез.'.encode())}).json()['id']
    with connect() as db:
        all_fragments = [dict(r) for r in db.execute('SELECT * FROM fragments')]
    validate_sources(Candidate.model_validate(candidate), all_fragments, doc)
    foreign = next(f['id'] for f in all_fragments if f['document_id'] == other)
    for source in ({'fragment_id': 999999, 'quote': 'Такой цитаты нет'}, {'fragment_id': foreign, 'quote': 'Другая книга описывает растения'}, {'fragment_id': candidate['sources'][0]['fragment_id'], 'quote': 'Несуществующая цитата из учебника'}):
        bad = candidate | {'sources': [source]}
        with pytest.raises(ValueError):
            validate_sources(Candidate.model_validate(bad), all_fragments, doc)


def test_semantic_check_separate_from_quote_and_level(candidate, doc):
    class StubReviewer:
        # Explicit test double, never used by the application.
        def json(self, instruction, data, schema):
            assert 'requested_level' not in data
            return Verdict(relevant=True, supported=False, clear=True, single_correct=False, is_duplicate=False, language_matches=True,
                           level=2, difficulty_reason='Нужно объяснить значение', reasons=['Ответ не подтверждён смыслом материала'])
    with connect() as db:
        fragments = [dict(r) for r in db.execute('SELECT * FROM fragments WHERE document_id=?', (doc,))]
    result, status = evaluate(StubReviewer(), Candidate.model_validate(candidate), fragments, doc, 5)
    assert status == 'failed'
    assert result['checked_level'] == 2 and result['level_mismatch']
    assert result['sources'][0]['location'] == 'Фрагмент TXT №1'


def test_duplicates():
    assert duplicate('Что такое алгоритм?', ['Что такое АЛГОРИТМ!'])
    assert duplicate('Объясните назначение алгоритма сортировки массива.', ['Объясните назначение алгоритма сортировки массивов.'])
    assert not duplicate('Как растения получают энергию?', ['Что такое алгоритм?'])


def test_edit_resets_acceptance_and_blocks_test(client, saved, candidate, doc):
    edited = copy.deepcopy(candidate)
    edited.update(text='Дайте определение алгоритма из материала.', requested_level=2, revision=1)
    assert client.put('/api/questions/' + saved, json=edited).status_code == 200
    q = client.get('/api/questions', params={'document_id': doc}).json()[0]
    assert q['review_status'] == 'pending' and q['auto_status'] == 'stale' and q['needs_recheck']
    assert client.post('/api/questions/' + saved + '/review', json={'status': 'accepted', 'revision': q['revision']}).status_code == 409
    assert client.post('/api/attempts', json={'document_id': doc}).status_code == 409
    assert client.put('/api/questions/' + saved, json=edited).status_code == 409


def test_rejected_question_never_enters_attempt(client, saved, doc):
    assert client.post('/api/questions/' + saved + '/review', json={'status': 'rejected', 'revision': 1}).status_code == 200
    assert client.post('/api/attempts', json={'document_id': doc}).status_code == 409
