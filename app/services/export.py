"""Export of the question bank (DOCX / CSV / JSON) and full backup / restore."""
import csv
import io
import json
from datetime import datetime, timezone

from app.database import connect, dumps

LEVEL_NAMES = {1: 'Факт', 2: 'Понимание', 3: 'Применение', 4: 'Анализ', 5: 'Синтез'}
TYPE_NAMES = {'mcq': 'Тестовый', 'open': 'Открытый'}
LETTERS = 'ABCD'


def level_of(q):
    return q.get('checked_level') or q.get('requested_level')


def to_csv(questions):
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=';')
    writer.writerow(['№', 'Уровень', 'Тип', 'Тема', 'Язык', 'Вопрос', 'Вариант A', 'Вариант B', 'Вариант C', 'Вариант D',
                     'Правильный ответ', 'Эталонный ответ', 'Объяснение', 'Источник', 'Цитата', 'AI-проверка', 'Решение преподавателя'])
    for n, q in enumerate(questions, 1):
        options = (q.get('options') or []) + [''] * 4
        correct = LETTERS[q['correct_index']] if q['type'] == 'mcq' and q.get('correct_index') is not None else ''
        source = (q.get('sources') or [{}])[0]
        writer.writerow([n, level_of(q) or '', TYPE_NAMES.get(q['type'], q['type']), q.get('topic', ''), q.get('language', ''),
                         q['text'], *options[:4], correct, q.get('reference_answer', ''), q.get('explanation', ''),
                         source.get('location', ''), source.get('quote', ''), q.get('auto_status', ''), q.get('review_status', '')])
    # BOM so that Excel opens Cyrillic/Kazakh text correctly.
    return ('﻿' + buffer.getvalue()).encode('utf-8')


def to_json(questions, document_name):
    keep = ('id', 'text', 'type', 'options', 'correct_index', 'reference_answer', 'explanation', 'topic', 'language',
            'requested_level', 'checked_level', 'difficulty_reason', 'sources', 'auto_status', 'review_status')
    return dumps({'document': document_name, 'exported_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                  'questions': [{k: q.get(k) for k in keep} for q in questions]}).encode('utf-8')


def to_docx(questions, document_name, answers=True):
    from docx import Document
    from docx.shared import Pt, RGBColor

    doc = Document()
    style = doc.styles['Normal']
    style.font.name = 'Times New Roman'
    style.font.size = Pt(12)
    doc.add_heading(document_name, level=1)
    doc.add_paragraph(('Вопросы с ответами' if answers else 'Вопросы для студентов') + f' · {len(questions)} шт.')
    for n, q in enumerate(questions, 1):
        level = level_of(q)
        meta = doc.add_paragraph()
        run = meta.add_run(f'{n}. Уровень {level} · {LEVEL_NAMES.get(level, "")} · {TYPE_NAMES.get(q["type"], "")}'
                           + (f' · {q["topic"]}' if q.get('topic') else ''))
        run.font.size = Pt(9)
        run.font.color.rgb = RGBColor(0x5B, 0x6B, 0x7A)
        doc.add_paragraph().add_run(q['text']).bold = True
        if q['type'] == 'mcq':
            for i, option in enumerate(q.get('options') or []):
                p = doc.add_paragraph(f'{LETTERS[i]}. {option}')
                p.paragraph_format.left_indent = Pt(18)
        elif not answers:
            for _ in range(3):
                doc.add_paragraph('_' * 80)
        if answers:
            if q['type'] == 'mcq' and q.get('correct_index') is not None:
                doc.add_paragraph().add_run('Правильный ответ: ' + LETTERS[q['correct_index']]).italic = True
            doc.add_paragraph('Эталонный ответ: ' + (q.get('reference_answer') or ''))
            if q.get('explanation'):
                doc.add_paragraph('Объяснение: ' + q['explanation'])
            for source in q.get('sources') or []:
                p = doc.add_paragraph(f'Источник ({source.get("location", "")}): «{source.get("quote", "")}»')
                p.runs[0].font.size = Pt(10)
        doc.add_paragraph()
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


# ------------------------------------------------------------------ backup
BACKUP_VERSION = 1


def backup():
    with connect() as db:
        documents = []
        for doc in db.execute('SELECT * FROM documents ORDER BY created_at').fetchall():
            doc_id = doc['id']
            documents.append({
                'document': dict(doc),
                'fragments': [dict(r) for r in db.execute('SELECT * FROM fragments WHERE document_id=? ORDER BY ordinal', (doc_id,))],
                'topics': [dict(r) for r in db.execute('SELECT * FROM topics WHERE document_id=? ORDER BY id', (doc_id,))],
                'questions': [dict(r) for r in db.execute('SELECT * FROM questions WHERE document_id=?', (doc_id,))],
                'assignments': [dict(r) for r in db.execute('SELECT * FROM assignments WHERE document_id=?', (doc_id,))],
                'attempts': [dict(r) for r in db.execute('SELECT * FROM attempts WHERE document_id=?', (doc_id,))],
            })
    return dumps({'app': 'terendik', 'version': BACKUP_VERSION,
                  'exported_at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'documents': documents}).encode('utf-8')


def _remap_sources(items, mapping):
    for item in items or []:
        for source in item.get('sources') or []:
            if source.get('fragment_id') in mapping:
                source['fragment_id'] = mapping[source['fragment_id']]
    return items


def _insert(db, table, row, allowed):
    keys = [k for k in row if k in allowed]
    db.execute(f'INSERT INTO {table}({",".join(keys)}) VALUES({",".join("?" * len(keys))})', tuple(row[k] for k in keys))


def restore(raw):
    from app.database import columns
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError('Файл не является резервной копией Тереңдік (некорректный JSON).') from None
    if not isinstance(data, dict) or data.get('app') != 'terendik' or not isinstance(data.get('documents'), list):
        raise ValueError('Файл не является резервной копией Тереңдік.')
    added = skipped = 0
    with connect() as db:
        allowed = {t: columns(db, t) for t in ('documents', 'fragments', 'topics', 'questions', 'assignments', 'attempts')}
        for item in data['documents']:
            doc = item['document']
            if db.execute('SELECT id FROM documents WHERE id=?', (doc['id'],)).fetchone():
                skipped += 1
                continue
            doc = dict(doc)
            if doc.get('status') == 'processing':
                doc['status'] = 'error'
            if doc.get('topic_status') == 'processing':
                doc['topic_status'] = 'error'
            _insert(db, 'documents', doc, allowed['documents'])
            mapping = {}
            for fragment in item.get('fragments', []):
                old = fragment['id']
                row = {k: v for k, v in fragment.items() if k != 'id'}
                _insert(db, 'fragments', row, allowed['fragments'])
                mapping[old] = db.execute('SELECT id FROM fragments WHERE document_id=? AND ordinal=? ORDER BY id DESC LIMIT 1',
                                          (doc['id'], fragment['ordinal'])).fetchone()[0]
            for topic in item.get('topics', []):
                row = {k: v for k, v in topic.items() if k != 'id'}
                row['fragment_ids'] = dumps([mapping.get(i, i) for i in json.loads(row['fragment_ids'])])
                _insert(db, 'topics', row, allowed['topics'])
            for question in item.get('questions', []):
                row = dict(question)
                payload = json.loads(row['payload'])
                _remap_sources([payload], mapping)
                row['payload'] = dumps(payload)
                if db.execute('SELECT id FROM questions WHERE id=?', (row['id'],)).fetchone():
                    continue
                _insert(db, 'questions', row, allowed['questions'])
            for table in ('assignments', 'attempts'):
                for record in item.get(table, []):
                    row = dict(record)
                    if row.get('snapshot'):
                        row['snapshot'] = dumps(_remap_sources(json.loads(row['snapshot']), mapping))
                    if db.execute(f'SELECT id FROM {table} WHERE id=?', (row['id'],)).fetchone():
                        continue
                    _insert(db, table, row, allowed[table])
            added += 1
    return {'added': added, 'skipped': skipped}
