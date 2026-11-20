import re
from difflib import SequenceMatcher

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from app.schemas import Verdict, IndependentAnswer
from app.services.documents import location
from app.services.llm import RUBRIC
from app.services import calc
from app.services.language import LANGUAGES, obvious_language_mismatch


def whitespace(text):
    return ' '.join(text.split()).casefold().replace('ё', 'е')


def normalized(text):
    return re.sub(r'[^\w]+', ' ', whitespace(text)).strip()


QUESTION_WORDS = set('что такое как какова какой какие каковы чем является называется согласно материалу загруженном загруженному дайте определение назовите основная основную основной основное основные не деген дегеніміз дегенді қалай қандай негізгі бойынша берілген түсіндіріңіз түсіндіріледі аталады ұғымы ұғымын what is are the a an define according to'.split())
QUESTION_WORDS.update('пәнінің теориясының пәні теориясы'.split())

# Thresholds are validated against observed bank pairs and contrast examples in
# tests/data/duplicate_pairs.json. A shared subject alone is not a duplicate.
SEQUENCE_THRESHOLD = .90
CONCEPT_THRESHOLD = .85
CHAR_THRESHOLD = .87
MIN_CORE_OVERLAP = .75


def concept_tokens(text):
    return {word[:5] if len(word) >= 6 else word for word in normalized(text).split() if word not in QUESTION_WORDS}


def duplicate_match(text, other):
    norm = normalized(text)
    other_norm = normalized(other)
    if norm == other_norm:
        return {'method': 'normalized_exact', 'score': 1.0}
    sequence = SequenceMatcher(None, norm, other_norm).ratio()
    # Distinct numerical conditions may be distinct application tasks. Leave
    # those to the semantic reviewer rather than rejecting by surface wording.
    if re.findall(r'\d+(?:[.,]\d+)?', norm) != re.findall(r'\d+(?:[.,]\d+)?', other_norm):
        return None
    tokens = concept_tokens(text)
    other_tokens = concept_tokens(other)
    overlap = len(tokens & other_tokens) / max(1, len(tokens | other_tokens))
    if sequence >= SEQUENCE_THRESHOLD and overlap >= MIN_CORE_OVERLAP:
        return {'method': 'sequence', 'score': round(sequence, 4)}
    if len(tokens) >= 2 and len(other_tokens) >= 2 and overlap >= CONCEPT_THRESHOLD:
        return {'method': 'concept_tokens', 'score': round(overlap, 4)}
    try:
        # Pairwise IDF makes the threshold independent of unrelated bank entries.
        matrix = TfidfVectorizer(analyzer='char_wb', ngram_range=(3, 5), max_features=35000).fit_transform([text, other])
        similarity = float(cosine_similarity(matrix[0], matrix[1])[0, 0])
        if similarity >= CHAR_THRESHOLD and overlap >= MIN_CORE_OVERLAP:
            return {'method': 'char_tfidf', 'score': round(similarity, 4)}
    except ValueError:
        pass
    return None


def duplicate(text, existing):
    return any(duplicate_match(text, other) is not None for other in existing)


def annotate_duplicates(items):
    """Read-only metadata for all candidates, including rejected legacy repeats."""
    for item in items:
        item['possible_duplicate'] = None
        item['duplicate_matches'] = []
    for index, a in enumerate(items):
        for b in items[index + 1:]:
            match = duplicate_match(a['text'], b['text'])
            if match:
                for current, other in ((a, b), (b, a)):
                    current['duplicate_matches'].append({'question_id': other['id'], 'text': other['text'],
                                                          'auto_status': other['auto_status'], **match})
                    current['possible_duplicate'] = current['possible_duplicate'] or other['text']
    return items


def nearest_questions(candidate, existing, limit=12):
    """Rank against every saved question, then bound the independent review's context."""
    scored = []
    candidate_tokens = concept_tokens(candidate.text)
    source_ids = {s.fragment_id for s in candidate.sources}
    for q in existing:
        tokens = concept_tokens(q['text'])
        overlap = len(tokens & candidate_tokens) / max(1, len(tokens | candidate_tokens))
        same_source = bool(source_ids & {s['fragment_id'] for s in q.get('sources', [])})
        score = overlap + .3 * same_source + .2 * (q.get('topic') == candidate.topic)
        scored.append((score, q))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [{'text': q['text'][:600], 'answer': q.get('reference_answer', '')[:400],
             'level': q.get('checked_level'), 'type': q.get('type')} for _, q in scored[:limit]]


def dehyphen(text):
    """Join words split by a line-end hyphen in PDFs: «визуализа-\nциялау» → «визуализациялау»."""
    return re.sub(r'(\w)[-\u00ad]\s*\n\s*(\w)', r'\1\2', text)


def find_quote(quote, text):
    """Return the verbatim source span supporting `quote`, or None.

    1) exact match ignoring whitespace; 2) match ignoring punctuation, quotes, dashes
    and PDF hyphenation; 3) near-verbatim match (≥ 92 % similar, at least 5 words)
    to tolerate a changed ending or a typo the model silently fixed. In cases 2–3 the
    stored quote becomes the exact text of the source, so nothing invented is shown.
    """
    if whitespace(quote) in whitespace(text):
        return quote
    source = dehyphen(text)
    tokens = list(re.finditer(r'\w+', source))
    words = [m.group().casefold().replace('ё', 'е') for m in tokens]
    wanted = normalized(dehyphen(quote)).split()
    n = len(wanted)
    if not n or n > len(words):
        return None

    def span(i, length):
        return ' '.join(source[tokens[i].start():tokens[i + length - 1].end()].split())

    for i in range(len(words) - n + 1):
        if words[i:i + n] == wanted:
            return span(i, n)
    if n < 5:
        return None
    target = ' '.join(wanted)
    best, best_ratio = None, 0.0
    for length in (n - 1, n, n + 1):
        if length < 1 or length > len(words):
            continue
        for i in range(len(words) - length + 1):
            if words[i][:3] != wanted[0][:3] and words[i + length - 1][:3] != wanted[-1][:3]:
                continue
            ratio = SequenceMatcher(None, target, ' '.join(words[i:i + length]), autojunk=False).ratio()
            if ratio > best_ratio:
                best, best_ratio = (i, length), ratio
    if best and best_ratio >= 0.92:
        return span(*best)
    return None


def validate_sources(candidate, fragments, document_id):
    by_id = {f['id']: f for f in fragments if f['document_id'] == document_id}
    enriched = []
    for source in candidate.sources:
        fragment = by_id.get(source.fragment_id)
        if fragment is None:
            raise ValueError('Источник отсутствует, не передан модели или принадлежит другому документу.')
        verbatim = find_quote(source.quote, fragment['text'])
        if verbatim is None:
            raise ValueError('Подтверждающая цитата не найдена в указанном фрагменте.')
        enriched.append({**source.model_dump(), 'quote': verbatim, 'location': location(fragment)})
    return enriched


def evaluate(llm, candidate, fragments, document_id, requested_level, target_topic=None, existing_questions=None, target_language=None):
    sources = validate_sources(candidate, fragments, document_id)
    ids = {s.fragment_id for s in candidate.sources}
    verdict = llm.json(
        'Ты независимый рецензент, а не автор вопроса. Проверь смысл ответа по источникам, '
        'соответствие выбранной теме, ясность, достаточность основания для решения. '
        'Для mcq реши все четыре варианта и проверь, что верен только один, именно correct_index. '
        'Для open single_correct=true, если эталон корректен. Совпадение цитаты не доказывает смысловую верность. '
        'Независимо определи реальную сложность, не подгоняя под пожелание автора. '
        'Не приписывай ученику вычисления, анализ или построение модели, которых вопрос не требует. '
        'Если вопрос лишь просит воспроизвести готовый вывод/ответ из источника, не повышай уровень из-за сложности вычислений, уже выполненных в источнике. '
        'Для уровней 3–5 укажи в difficulty_reason, что именно ученик должен сделать самостоятельно: применить метод в новых условиях, проанализировать новую ситуацию или синтезировать решение. '
        'Сравни со списком existing_questions: is_duplicate=true, если проверяется то же знание тем же действием, '
        'даже при перефразировании, переводе или другом типе вопроса. Само совпадение темы не означает повтор. '
        'Проверь язык вопроса, вариантов, эталона и объяснения: language_matches. Цитаты остаются на языке источника. '
        'Причины сомнений/отказа перечисли в reasons. ' + RUBRIC,
        {'question': candidate.model_dump(), 'selected_topic': target_topic or 'Весь материал',
         'existing_questions': nearest_questions(candidate, existing_questions or []),
         'target_language': LANGUAGES.get(target_language, 'язык исходного вопроса'),
         'fragments': [{'fragment_id': f['id'], 'text': f['text']} for f in fragments if f['id'] in ids]}, Verdict)
    language_ok = verdict.language_matches and not obvious_language_mismatch(candidate.text, target_language)
    passed = all((verdict.relevant, verdict.supported, verdict.clear, verdict.single_correct, language_ok, not verdict.is_duplicate))
    reasons = verdict.reasons.copy()
    independent = None
    calc_check = None
    level = max(requested_level, verdict.level)
    numeric_open = candidate.type == 'open' and level >= 3 and calc.numbers_in(candidate.reference_answer)
    if passed and ((candidate.type == 'mcq' and level >= 3) or numeric_open):
        independent = llm.json(
            ('Реши тестовый вопрос независимо. ' if candidate.type == 'mcq' else 'Реши открытую задачу независимо. ') +
            'Ответ автора и его объяснение намеренно скрыты. '
            'Готовые примеры источника тоже скрыты, чтобы не подменить ими условия вопроса. '
            'Вычисли или логически выведи ответ именно из данных вопроса: чисел, ограничений, критерия, единиц. '
            'Не добавляй недостающие факты. Если вопрос не самодостаточен и требует неуказанных условий, '
            'has_single_answer=false и correct_index=null. '
            + ('Проверь допустимость и все варианты. Верни индекс единственного правильного ответа 0–3, '
               'самостоятельное решение и обоснование. Если правильного варианта нет или их несколько, '
               'has_single_answer=false и correct_index=null. ' if candidate.type == 'mcq' else
               'Для открытой задачи correct_index=null, в answer дай итоговый ответ. ') +
            'Если итоговый ответ — одно число, в calculation запиши одно арифметическое выражение из чисел условия '
            '(только числа, + - * / ** скобки, sqrt, min, max, round, log), значение которого равно этому числу; '
            'иначе calculation=null. Сервер вычислит выражение программно.',
            {'text': candidate.text, 'options': candidate.options}, IndependentAnswer)
        if candidate.type == 'mcq' and (not independent.has_single_answer or independent.correct_index != candidate.correct_index):
            passed = False
            reasons.append('Независимое решение не подтвердило указанный правильный вариант. Проверьте условия, вычисления и варианты ответа.')
        expected = candidate.options[candidate.correct_index] if candidate.type == 'mcq' else candidate.reference_answer
        status, value = calc.check(independent.calculation, expected)
        calc_check = {'expression': independent.calculation, 'value': value, 'status': status}
        if status == 'mismatch':
            passed = False
            reasons.append(f'Программная проверка вычисления: выражение «{independent.calculation}» = {value:g}, '
                           'но это число не найдено в правильном ответе. Проверьте расчёт.')
        elif status == 'match':
            reasons.append(f'Вычисление проверено программно: {independent.calculation} = {value:g}.')
    if verdict.is_duplicate:
        reasons.append('AI-проверка обнаружила смысловой повтор сохранённого вопроса.')
    if not language_ok:
        reasons.append('Язык вопроса или ответа не соответствует выбранному языку.')
    mismatch = verdict.level != requested_level
    if mismatch:
        reasons.append(f'Запрошен уровень {requested_level}, AI-проверка определила {verdict.level}. Нужна проверка преподавателя.')
    return {
        **candidate.model_dump(), 'sources': sources, 'requested_level': requested_level,
        'language': target_language,
        'checked_level': verdict.level, 'difficulty_reason': verdict.difficulty_reason,
        'level_mismatch': mismatch, 'needs_recheck': False,
        'validation': {**verdict.model_dump(), 'passed': passed, 'reasons': reasons,
                       'independent_solution': independent.model_dump() if independent else None,
                       'calc_check': calc_check},
    }, 'passed' if passed else 'failed'
