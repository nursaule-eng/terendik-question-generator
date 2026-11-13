"""Snapshots are private until completion; this whitelist is the only active-test serializer."""
import json

from fastapi import HTTPException


def present(row):
    snapshot = json.loads(row['snapshot'])
    result = {'id': row['id'], 'document_id': row['document_id'], 'status': row['status'],
              'created_at': row['created_at'], 'finished_at': row['finished_at']}
    if row['status'] == 'active':
        result['questions'] = [{k: q[k] for k in ('id', 'text', 'type', 'options')} for q in snapshot]
        return result
    answers = json.loads(row['answers'])
    for q in snapshot:
        q['user_answer'] = answers.get(q['id'])
        q['correct'] = (q['user_answer'] == q['correct_index']) if q['type'] == 'mcq' else None
    mcq = [q for q in snapshot if q['type'] == 'mcq']
    result.update(questions=snapshot, correct=sum(q['correct'] for q in mcq), total=len(mcq),
                  self_scores=json.loads(row['self_scores']))
    return result


def validate_answers(snapshot, answers):
    by_id = {q['id']: q for q in snapshot}
    if set(answers) - set(by_id):
        raise HTTPException(422, 'В ответах есть вопрос, не входящий в попытку.')
    for key, value in answers.items():
        q = by_id[key]
        if q['type'] == 'mcq':
            if value is not None and (type(value) is not int or value not in range(4)):
                raise HTTPException(422, 'Для тестового вопроса нужен индекс 0–3 или пропуск.')
        elif value is not None and (not isinstance(value, str) or len(value) > 12000):
            raise HTTPException(422, 'Открытый ответ должен быть текстом до 12000 символов.')
