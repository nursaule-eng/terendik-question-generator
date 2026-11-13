import pymupdf
import pytest

from app.database import connect
from app.services.documents import NO_TEXT, chunks, extract
from app.services.retrieval import retrieve


def test_pdf_all_physical_pages_and_tail(tmp_path):
    path = tmp_path / 'lesson.pdf'
    pdf = pymupdf.open()
    for index in range(3):
        page = pdf.new_page()
        page.insert_text((50, 50), f'Physical page {index + 1}. Unique tail {index}.')
    pdf.save(path)
    pdf.close()
    pages = extract(path, 'pdf')
    fragments = chunks(pages)
    assert [p for p, _ in pages] == [1, 2, 3]
    assert fragments[-1]['page_start'] == 3
    assert 'Unique tail 2' in fragments[-1]['text']


def test_chunking_keeps_every_word_and_end():
    text = '\n\n'.join(' '.join(f'word{i}_{j}' for j in range(40)) for i in range(50)) + '\n\nLAST_MARKER'
    parts = chunks([(None, text)], size=400, overlap=40)
    assert len(parts) > 50
    assert all(len(f['text']) <= 400 for f in parts)
    joined = '\n'.join(f['text'] for f in parts)
    assert all(word in joined for word in text.split())
    assert parts[-1]['text'].endswith('LAST_MARKER')


def test_empty_scanned_pdf(tmp_path):
    path = tmp_path / 'scan.pdf'
    with pymupdf.open() as pdf:
        pdf.new_page()
        pdf.save(path)
    with pytest.raises(ValueError, match=NO_TEXT):
        extract(path, 'pdf')


def test_upload_status_and_reconnect(client, doc):
    data = client.get('/api/documents/' + doc).json()
    assert data['status'] == 'ready'
    assert data['topic_status'] == 'error'  # no key; extraction still succeeded
    with connect() as db:
        assert 'Алгоритм' in db.execute('SELECT text FROM documents WHERE id=?', (doc,)).fetchone()[0]
    with connect() as db:
        source = db.execute('SELECT * FROM fragments WHERE document_id=?', (doc,)).fetchone()
        assert source['ordinal'] == 1 and source['page_start'] is None


def test_invalid_uploads(client):
    assert client.post('/api/documents', files={'file': ('a.exe', b'data')}).status_code == 415
    assert client.post('/api/documents', files={'file': ('a.txt', b'')}).status_code == 422
    bad = client.post('/api/documents', files={'file': ('a.pdf', b'not pdf')}).json()
    assert client.get('/api/documents/' + bad['id']).json()['status'] == 'error'
    too_big = client.post('/api/documents', files={'file': ('a.txt', b'x' * (20 * 1024 * 1024 + 1))})
    assert too_big.status_code == 413


def test_whole_material_retrieves_tail_and_middle():
    fragments = [{'id': i, 'text': f'материал раздел {i}'} for i in range(100)]
    selected = retrieve(fragments)
    assert selected[0]['id'] == 0 and selected[-1]['id'] == 99
    assert any(35 < f['id'] < 65 for f in selected)


def test_tfidf_respects_topic_link():
    fragments = [{'id': i, 'text': text} for i, text in enumerate(['Геометрия и треугольники', 'Алгоритмы и исполнители', 'Биология и растения'])]
    assert retrieve(fragments, {'name': 'Алгоритмы', 'keywords': ['исполнители'], 'fragment_ids': [1]}, limit=1)[0]['id'] == 1
