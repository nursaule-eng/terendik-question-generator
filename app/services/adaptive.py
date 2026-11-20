"""Adaptive testing (staircase): a correct answer raises the level, a wrong one lowers it.

Only multiple-choice questions are used, so every answer is checked instantly.
The staircase oscillates around the level the student can handle; the estimate
averages the evidence of the last answers (see estimate()).
"""
import json
import random

from app.database import dumps
from app.services.options import shuffle_options

PUBLIC = ('id', 'text', 'type', 'options', 'topic')
LEVEL_NAMES = {1: 'Факт', 2: 'Понимание', 3: 'Применение', 4: 'Анализ', 5: 'Синтез'}


def group(pool):
    by_level = {}
    for q in pool:
        by_level.setdefault(q['checked_level'], []).append(q)
    for items in by_level.values():
        random.shuffle(items)
    return by_level


def pick(by_level, level, used, direction=0):
    """Question at `level`, else the nearest level (prefer the direction of movement)."""
    order = sorted(range(1, 6), key=lambda lv: (abs(lv - level), 0 if (lv - level) * direction >= 0 else 1))
    for lv in order:
        for q in by_level.get(lv, []):
            if q['id'] not in used:
                return q
    return None


SNAPSHOT_KEYS = ('id', 'text', 'type', 'options', 'correct_index', 'reference_answer', 'explanation', 'topic', 'language',
                 'requested_level', 'checked_level', 'difficulty_reason', 'sources')


def new_state(pool, length, start_level):
    pool = [{k: q.get(k) for k in SNAPSHOT_KEYS} for q in pool]
    by_level = group(pool)
    first = pick(by_level, start_level, set())
    state = {'level': first['checked_level'], 'length': min(length, len(pool)), 'start_level': start_level,
             'history': [], 'items': {q['id']: q for q in pool}}
    return state, shuffle_options(first)


def public(question, state):
    return {'question': {k: question[k] for k in PUBLIC if k in question}, 'level': question['checked_level'],
            'index': len(state['history']) + 1, 'length': state['length']}


def estimate(history):
    """Each answer is evidence: correct at level L → can do L; wrong at L → can do at most L-1.
    The estimate averages this evidence over the last four answers."""
    if not history:
        return None
    if not any(h['correct'] for h in history):
        return 1.0
    tail = history[-4:]
    value = sum(h['level'] if h['correct'] else h['level'] - 1 for h in tail) / len(tail)
    return max(1.0, min(5.0, round(value, 1)))


def summary(state):
    history = state['history']
    per_level = {}
    for h in history:
        item = per_level.setdefault(h['level'], {'level': h['level'], 'answered': 0, 'correct': 0})
        item['answered'] += 1
        item['correct'] += h['correct']
    value = estimate(history)
    rounded = int(round(value)) if value else None
    return {'estimate': value, 'estimate_level': rounded, 'estimate_name': LEVEL_NAMES.get(rounded),
            'path': [{'level': h['level'], 'correct': h['correct']} for h in history],
            'per_level': sorted(per_level.values(), key=lambda x: x['level']),
            'correct': sum(h['correct'] for h in history), 'total': len(history)}


def answer(row, question_id, value):
    """Apply one answer. Returns (feedback, next_question_or_None, new_state, snapshot, answers)."""
    state = json.loads(row['adaptive'])
    snapshot = json.loads(row['snapshot'])
    answers = json.loads(row['answers'] or '{}')
    current = snapshot[-1]
    if current['id'] != question_id or question_id in answers:
        raise ValueError('Ответ относится не к текущему вопросу.')
    if value is not None and (type(value) is not int or value not in range(4)):
        raise ValueError('Нужен индекс варианта 0–3 или пропуск.')
    correct = value == current['correct_index']
    answers[question_id] = value
    level = current['checked_level']
    state['history'].append({'id': question_id, 'level': level, 'correct': correct})
    direction = 1 if correct else -1
    state['level'] = max(1, min(5, level + direction))
    feedback = {'correct': correct, 'correct_index': current['correct_index'], 'explanation': current.get('explanation', ''),
                'level': level, 'next_level': state['level']}
    next_question = None
    if len(state['history']) < state['length']:
        used = {q['id'] for q in snapshot}
        by_level = group(list(state['items'].values()))
        candidate = pick(by_level, state['level'], used, direction)
        if candidate:
            next_question = shuffle_options(candidate)
            snapshot.append(next_question)
    return feedback, next_question, state, snapshot, answers


def dump(state, snapshot, answers):
    return dumps(state), dumps(snapshot), dumps(answers)
