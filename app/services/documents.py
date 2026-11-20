import re
import zipfile
from pathlib import Path

import pymupdf
from charset_normalizer import from_bytes

NO_TEXT = 'Не удалось извлечь текст. Загрузите PDF с текстовым слоем, DOCX или TXT'

# Old Kazakh fonts (KZ Times, Arial KZ …) and KZ-1048 text put Kazakh letters into
# the cp1251 slots of Serbian/Ukrainian letters. A PDF built with such a font gives
# "ќазаќ тілі" instead of "қазақ тілі". The table is the exact cp1251 → KZ-1048 difference.
LEGACY_KZ = str.maketrans({
    'Ќ': 'Қ', 'ќ': 'қ', 'Ћ': 'Һ', 'ћ': 'һ', 'Ў': 'Ұ', 'ў': 'ұ', 'Ј': 'Ә', 'ј': 'ә',
    'Ґ': 'Ө', 'ґ': 'ө', 'Є': 'Ғ', 'є': 'ғ', 'Ї': 'Ү', 'ї': 'ү', 'Ѕ': 'Ң', 'ѕ': 'ң'})
LEGACY_CHARS = re.compile('[ЌќЋћЎўЈјҐґЄєЇїЅѕ]')
KAZAKH_CHARS = re.compile('[әғқңөұүһӘҒҚҢӨҰҮҺ]')


def repair_kazakh(text):
    """Return (text, repaired?) — fixes legacy-font Kazakh, leaves other text unchanged."""
    legacy = len(LEGACY_CHARS.findall(text))
    if legacy < 3:
        return text, False
    cyrillic = len(re.findall('[а-яА-ЯёЁіІ]', text))
    proper = len(KAZAKH_CHARS.findall(text))
    # Kazakh text has ~1 special letter per 8–15 Cyrillic letters; Ukrainian has є/ї but not ќ/ј/ѕ/ў.
    distinctive = len(re.findall('[ЌќЈјЅѕЎўЋћ]', text))
    if distinctive >= 2 and legacy > proper and cyrillic > legacy * 3:
        return text.translate(LEGACY_KZ), True
    return text, False


def docx_paragraphs(path):
    from docx import Document
    try:
        document = Document(str(path))
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise ValueError('Не удалось открыть DOCX: файл повреждён или имеет другой формат.') from exc
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            parts.append(' | '.join(dict.fromkeys(c for c in cells if c)))
    return '\n\n'.join(p.strip() for p in parts if p.strip())


def extract(path: Path, extension: str):
    pages = _extract(path, extension)
    repaired = [repair_kazakh(text) for _, text in pages]
    return [(page, text) for (page, _), (text, _) in zip(pages, repaired)]


def legacy_font_used(path: Path, extension: str):
    return any(repair_kazakh(text)[1] for _, text in _extract(path, extension))


def _extract(path: Path, extension: str):
    if extension == 'docx':
        text = docx_paragraphs(path)
        if not text.strip():
            raise ValueError(NO_TEXT)
        return [(None, text)]
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
        text = decode_legacy(raw)
    if '\x00' in text or sum(ord(c) < 32 and c not in '\n\r\t\f' for c in text) > max(2, len(text) // 100):
        raise ValueError('Файл не похож на текст. Сохраните материал в UTF-8 TXT.')
    if not text.strip():
        raise ValueError('Документ пуст.')
    return [(None, text)]


def decode_legacy(raw):
    """cp1251 is common for Russian materials, KZ-1048 for old Kazakh ones."""
    candidates = []
    for encoding in ('cp1251', 'kz1048'):
        try:
            candidates.append((encoding, raw.decode(encoding)))
        except UnicodeDecodeError:
            pass
    if candidates:
        kz = dict(candidates).get('kz1048')
        ru = dict(candidates).get('cp1251')
        if kz and ru and len(KAZAKH_CHARS.findall(kz)) > len(re.findall('[ЌќЋћЎўЈјҐґЄєЇїЅѕ]', ru)) // 2 and KAZAKH_CHARS.search(kz):
            return kz
        best = from_bytes(raw).best()
        if best is not None and best.encoding not in ('cp1251', 'windows_1251', 'kz1048', 'kz_1048'):
            return str(best)
        return ru or kz
    best = from_bytes(raw).best()
    if best is None:
        raise ValueError('Не распознана кодировка TXT. Сохраните файл в UTF-8.')
    return str(best)


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
    return f"Фрагмент текста №{fragment['ordinal']}"
