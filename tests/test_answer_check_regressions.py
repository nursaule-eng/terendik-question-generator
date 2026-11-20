import pytest

from app.database import connect
from app.schemas import AnswerAgreement, Candidate, IndependentAnswer, Verdict
from app.services import calc
from app.services.validation import evaluate


@pytest.mark.parametrize('expression,expected,status', [
    ('2+2', '400 единиц', 'mismatch'),
    ('400', '4 единицы', 'mismatch'),
    ('10+5', 'Взяли 15 кг, итог 100 кг', 'skipped'),
    ('0.25', '25 %', 'match'),
    ('0.25', '25 единиц', 'mismatch'),
    ('0', '0.01', 'mismatch'),
    ('max(2,3)', '3', 'match'),
    ('min(2,3)', '2', 'match'),
    ('2,5*2', '5', 'match'),
])
def test_only_confirm_unambiguous_numeric_answers(expression, expected, status):
    assert calc.check(expression, expected)[0] == status


@pytest.mark.parametrize('single,calculation,agreement,expected', [
    (False, None, None, 'failed'),
    (False, '2+2', None, 'failed'),
    (True, None, False, 'failed'),
    (True, None, True, 'passed'),
    (True, '2+3', None, 'failed'),
    (True, '2+2', None, 'passed'),
])
def test_independent_open_solution_must_be_confirmed(candidate, doc, single, calculation, agreement, expected):
    task = Candidate.model_validate(candidate | {
        'text': 'Найдите сумму двух чисел 2 и 2.', 'type': 'open',
        'options': [], 'correct_index': None, 'reference_answer': '4 единицы',
    })
    calls = []

    class Reviewer:
        def json(self, instruction, data, schema):
            calls.append(schema)
            if schema is Verdict:
                return Verdict(relevant=True, supported=True, clear=True, single_correct=True,
                               level=3, difficulty_reason='Применение метода', reasons=[],
                               is_duplicate=False, language_matches=True)
            if schema is IndependentAnswer:
                assert set(data) == {'text', 'options'}  # No author answer leaks to the solver.
                return IndependentAnswer(has_single_answer=single, answer='4 единицы',
                                         reasoning='Два плюс два равно четырём.', calculation=calculation)
            assert schema is AnswerAgreement and agreement is not None
            return AnswerAgreement(matches=agreement, reason='Сравнены итоговые ответы и единицы.')

    with connect() as db:
        fragments = [dict(r) for r in db.execute('SELECT * FROM fragments WHERE document_id=?', (doc,))]
    payload, status = evaluate(Reviewer(), task, fragments, doc, 3, target_language='ru')
    assert status == expected
    assert payload['validation']['passed'] is (expected == 'passed')
    assert (AnswerAgreement in calls) is (agreement is not None)
