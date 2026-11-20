from collections import OrderedDict
from threading import Lock

import numpy as np
from scipy.sparse import hstack
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

# Character n-grams match Kazakh/Russian word forms with different endings
# (алгоритм / алгоритмнің / алгоритмдер), word n-grams keep exact terms strong.
_cache = OrderedDict()
_lock = Lock()


def _index(fragments):
    key = (fragments[0]['id'], fragments[-1]['id'], len(fragments), sum(len(f['text']) for f in fragments))
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    texts = [f['text'] for f in fragments]
    words = TfidfVectorizer(ngram_range=(1, 2), max_features=40000, sublinear_tf=True)
    chars = TfidfVectorizer(analyzer='char_wb', ngram_range=(3, 5), max_features=60000, sublinear_tf=True)
    matrix = hstack([words.fit_transform(texts), chars.fit_transform(texts)]).tocsr()
    value = (words, chars, matrix)
    with _lock:
        _cache[key] = value
        while len(_cache) > 8:
            _cache.popitem(last=False)
    return value


def scores_for(fragments, query):
    try:
        words, chars, matrix = _index(fragments)
        vector = hstack([words.transform([query]), chars.transform([query])]).tocsr()
        return cosine_similarity(vector, matrix).ravel()
    except ValueError:
        return np.zeros(len(fragments))


def retrieve(fragments, topic=None, offset=0, limit=7, budget=11000):
    if not fragments:
        return []
    if topic:
        query = topic['name'] + ' ' + ' '.join(topic['keywords'])
        scores = scores_for(fragments, query)
        linked = set(topic['fragment_ids'])
        ranked = sorted(range(len(fragments)), key=lambda i: float(scores[i]) + (0.5 if fragments[i]['id'] in linked else 0), reverse=True)
        # Rotate within relevant candidates over batches, preserving their ordering.
        threshold = 0.05
        pool = [i for i in ranked if fragments[i]['id'] in linked or float(scores[i]) > threshold] or ranked
        shift = offset % len(pool)
        order = pool[shift:] + pool[:shift]
    else:
        # Select from all parts of the document, including its end, then rotate.
        n = len(fragments)
        shift = offset % n
        order = list(dict.fromkeys((round(i * (n - 1) / max(1, limit - 1)) + shift) % n for i in range(limit)))
    chosen, used = [], 0
    for i in order:
        fragment = fragments[i]
        if len(chosen) == limit:
            break
        if used + len(fragment['text']) > budget:
            continue
        chosen.append(fragment)
        used += len(fragment['text'])
    return chosen
