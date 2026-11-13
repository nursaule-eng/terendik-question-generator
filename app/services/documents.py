import re
from pathlib import Path

import pymupdf
from charset_normalizer import from_bytes

NO_TEXT = 'Не удалось извлечь текст. Загрузите PDF с текстовым слоем или TXT'


def extract(path: Path, extension: str):
    if extension == 'pdf':
        try:
            with pymupdf.open(path) as pdf:
                if pdf.needs_pass:
                    raise ValueError('PDF защищён паролем. Загрузите незашифрованный файл.')
                pages = [(i + 1, page.get_text(sort=True)) for i, page in enumerate(pdf)]
        except (pymupdf.FileDataError, RuntimeError) as exc:
            raise ValueError('Не удалось открыть PDF: файл повреждён или имеет другой формат.') from exc
        if not any(text.strip() for _, text in pages):
            raise ValueError(NO_TEXT)
        return pages
    raw = path.read_bytes()
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        raw = raw.decode('utf-16').encode('utf-8')
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        # cp1251 is common for Russian teaching materials; detection also supports UTF-16.
        best = from_bytes(raw).best()
        if best is None:
            raise ValueError('Не распознана кодировка TXT. Сохраните файл в UTF-8.')
        text = str(best)
    if '\x00' in text or sum(ord(c) < 32 and c not in '\n\r\t\f' for c in text) > max(2, len(text) // 100):
        raise ValueError('Файл не похож на текст. Сохраните материал в UTF-8 TXT.')
    if not text.strip():
        raise ValueError('Документ пуст.')
    return [(None, text)]


def chunks(pages, size=1800, overlap=180):
    """Keep every page and tail; pack paragraphs within each physical page."""
    result = []
    for page, text in pages:
        paragraphs = re.split(r'\n\s*\n', text.strip())
        buffer = ''
        for paragraph in paragraphs:
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            if buffer and len(buffer) + len(paragraph) + 2 > size:
                result.append({'text': buffer, 'page_start': page, 'page_end': page})
                buffer = buffer[-overlap:] if overlap else ''
            buffer = (buffer + '\n\n' + paragraph).strip()
            while len(buffer) > size:
                boundary = buffer.rfind(' ', size // 2, size)
                boundary = boundary if boundary > 0 else size
                result.append({'text': buffer[:boundary], 'page_start': page, 'page_end': page})
                buffer = buffer[max(1, boundary - overlap):]
        if buffer:
            result.append({'text': buffer, 'page_start': page, 'page_end': page})
    for index, fragment in enumerate(result, 1):
        fragment['ordinal'] = index
    return result


def location(fragment):
    if fragment['page_start'] is not None:
        return f"Физическая страница PDF {fragment['page_start']}"
    return f"Фрагмент TXT №{fragment['ordinal']}"
