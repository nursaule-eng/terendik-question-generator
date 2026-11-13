import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


def retrieve(fragments, topic=None, offset=0, limit=7, budget=11000):
    if not fragments:
        return []
    if topic:
        query = topic['name'] + ' ' + ' '.join(topic['keywords'])
        try:
            matrix = TfidfVectorizer(ngram_range=(1, 2), max_features=40000).fit_transform([f['text'] for f in fragments] + [query])
            scores = cosine_similarity(matrix[-1], matrix[:-1]).ravel()
        except ValueError:
            scores = np.zeros(len(fragments))
        linked = set(topic['fragment_ids'])
        ranked = sorted(range(len(fragments)), key=lambda i: float(scores[i]) + (0.5 if fragments[i]['id'] in linked else 0), reverse=True)
        # Rotate within relevant candidates over batches, preserving their ordering.
        pool = [i for i in ranked if fragments[i]['id'] in linked or float(scores[i]) > 0] or ranked
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
