import json
import uuid
from collections import Counter

from app.database import connect, dumps
from app.schemas import Batch
from app.services.llm import LLM, ModelError, RUBRIC
from app.services.retrieval import retrieve
from app.services.topics import read_topics
from app.services.validation import duplicate, evaluate
from app.services.language import LANGUAGES
from app.services.options import shuffle_options

COGNITIVE_TASKS = {
    1: 'Спросить один факт или определение, явно данное в источнике. Не требовать вычислений или анализа.',
    2: 'Попросить объяснить смысл или простое различие своими словами. Не требовать вычислений, выбора оптимального плана, вывода диапазона параметра или анализа ошибок.',
    3: 'Дать новый, явно условный пример с изменёнными исходными числами и попросить применить один изученный метод. Не спрашивать готовый числовой вывод, уже выписанный в источнике.',
    4: 'Дать новую, явно условную ситуацию или ошибочное решение и потребовать выявить причину ошибки, сравнить допустимые альтернативы и обосновать вывод. Это должен быть самостоятельный анализ, а не повтор готового разбора из источника.',
    5: 'Попросить самостоятельно составить согласованную модель/решение для нового условного случая с несколькими взаимодействующими требованиями, выбрать критерий и обосновать решение и ограничения. Не повторять полностью готовый пример источника. Для mcq варианты должны представлять полные альтернативные решения с обоснованием, иначе верни меньше вопросов.',
}


def summary(requested, created, rejected):
    return f'Запрошено {requested}. Проверено кандидатов: {created + rejected}; прошли проверку: {created}; отклонены: {rejected}.'


def run_generation(job_id, params):
    created = rejected = 0
    notes = []
    try:
        with connect() as db:
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
            fragments = [dict(r) for r in db.execute('SELECT * FROM fragments WHERE document_id=? ORDER BY ordinal', (params.document_id,))]
            topics = read_topics(db, params.document_id)
            doc_language = db.execute('SELECT language FROM documents WHERE id=?', (params.document_id,)).fetchone()[0]
            existing_questions = [json.loads(r[0]) for r in db.execute('SELECT payload FROM questions WHERE document_id=?', (params.document_id,))]
            existing = [q['text'] for q in existing_questions]
        target_language = doc_language if params.language == 'auto' else params.language
        schedule = [params.level or (slot % 5 + 1) for slot in range(params.count)]
        targets = Counter(schedule)
        produced = Counter()
        selected = next((t for t in topics if t['id'] == params.topic_id), None)
        stage = ''
        def on_wait(message):
            with connect() as db:
                db.execute('UPDATE jobs SET message=? WHERE id=?',
                           (f'Создано {created} из {params.count}; отклонено {rejected}. ' + stage + ' ' + message, job_id))
        def set_stage(message):
            nonlocal stage
            stage = message
            on_wait('')
        llm = LLM(on_wait=on_wait)
        # A single logical batch per question provides balanced topic/level/type allocation.
        # At most one replacement round if there is insufficient material/duplicates.
        for index in range(params.count * 2):
            if created >= params.count:
                break
            topic = selected or (topics[index % len(topics)] if topics else None)
            level = schedule[index % len(schedule)]
            if produced[level] >= targets[level]:
                continue
            kind = params.type if params.type != 'mixed' else ('mcq' if index % 2 == 0 else 'open')
            offset = index * 3 if selected else round(index * max(0, len(fragments) - 1) / max(1, params.count - 1))
            context = retrieve(fragments, topic, offset=offset)
            set_stage(f'Генерируется вопрос уровня {level}.')
            response = llm.json(
                'Создай один новый вопрос с ответом, объяснением и дословной подтверждающей цитатой. '
                'Для mcq ровно четыре варианта и correct_index от 0 до 3; для open options=[] и correct_index=null. '
                'В вопросе и объяснении не ссылайся на буквы, номера или порядок вариантов: сервер перемешивает их. '
                'Варианты должны сохранять смысл при любом порядке. '
                'Для условной задачи явно укажи в самом вопросе все необходимые числа, ограничения и критерий: ученик не видит контекст запроса к модели. '
                'Не повторяй существующие вопросы. Все source.fragment_id берутся из контекста. '
                'Поле quote скопируй дословно из text выбранного фрагмента: не исправляй грамматику, окончания слов, '
                'дефисы и опечатки исходника. Допустима короткая цитата вместо целого предложения. '
                'Не заменяй в quote знаки <= на ≤ и наоборот. Для нового условного примера цитируй исходный метод решения, а не новые числа или несуществующий готовый ответ. '
                'Не придумывай номера страниц. Не добавляй вопросы при нехватке материала. ' + RUBRIC,
                {'count': 1, 'level': level, 'cognitive_task': COGNITIVE_TASKS[level], 'type': kind, 'target_language': LANGUAGES[target_language], 'topic': topic['name'] if topic else 'Весь материал',
                 'avoid_questions': [text[:400] for text in existing[-40:]],
                 'fragments': [{'fragment_id': f['id'], 'text': ' '.join(f['text'].split())} for f in context]}, Batch)
            if response.reason:
                notes.append(response.reason)
            for candidate in response.questions[:1]:
                if topic:
                    candidate = candidate.model_copy(update={'topic': topic['name']})
                base = {**candidate.model_dump(), 'requested_level': level, 'checked_level': None,
                        'language': target_language,
                        'difficulty_reason': '', 'needs_recheck': False, 'level_mismatch': False}
                try:
                    if candidate.type != kind:
                        raise ValueError('Модель вернула другой тип вопроса.')
                    with connect() as db:
                        existing_questions = [json.loads(r[0]) for r in db.execute('SELECT payload FROM questions WHERE document_id=?', (params.document_id,))]
                    if duplicate(candidate.text, [q['text'] for q in existing_questions]):
                        raise ValueError('Обнаружен похожий вопрос в документе.')
                    set_stage(f'Проверяется вопрос уровня {level}.')
                    payload, status = evaluate(llm, candidate, context, params.document_id, level, topic['name'] if topic else None,
                                               existing_questions=existing_questions, target_language=target_language)
                except ValueError as exc:
                    payload = {**base, 'validation': {'passed': False, 'reasons': [str(exc)]}}
                    status = 'failed'
                with connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    current_texts = [json.loads(r[0])['text'] for r in db.execute('SELECT payload FROM questions WHERE document_id=?', (params.document_id,))]
                    if status == 'passed' and duplicate(candidate.text, current_texts):
                        status = 'failed'
                        payload['validation'].update(passed=False, reasons=['Похожий вопрос появился в банке во время генерации.'])
                    if status == 'passed':
                        payload = shuffle_options(payload)
                    db.execute('INSERT INTO questions(id,document_id,payload,auto_status) VALUES(?,?,?,?)',
                               (uuid.uuid4().hex, params.document_id, dumps(payload), status))
                existing.append(candidate.text)
                if status == 'passed':
                    created += 1
                    produced[level] += 1
                else:
                    rejected += 1
                    notes.extend(payload['validation']['reasons'])
            with connect() as db:
                db.execute('UPDATE jobs SET created=?,rejected=?,message=? WHERE id=?',
                           (created, rejected, f'Создано {created} из {params.count}; отклонено {rejected}.', job_id))
        message = summary(params.count, created, rejected)
        if created < params.count:
            message += ' Лимит попыток исчерпан: недостаточно неповторяющихся подтверждённых вопросов.'
        with connect() as db:
            db.execute("UPDATE jobs SET status='done',message=?,details=? WHERE id=?", (message, dumps(list(dict.fromkeys(notes))[:100]), job_id))
    except Exception as exc:
        message = str(exc) if isinstance(exc, ModelError) else 'Ошибка генерации. Уже созданные вопросы сохранены; повторите запрос.'
        message = f'Запрошено {params.count}, сохранено допустимых {created}, отклонено {rejected}. ' + message
        with connect() as db:
            db.execute("UPDATE jobs SET status='error',created=?,rejected=?,message=?,details=? WHERE id=?", (created, rejected, message, dumps(list(dict.fromkeys(notes))[:100]), job_id))
