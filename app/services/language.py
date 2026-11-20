import re

LANGUAGES = {'kk': 'қазақ тілі (казахский)', 'ru': 'русский язык', 'en': 'English (английский)'}

KK_LETTERS = 'әғқңөұүһӘҒҚҢӨҰҮҺ'
# Kazakh Latin alphabet (2021): á ǵ ı ń ó ú ý ş ç
KK_LATIN = re.compile('[áǵıńóúýşÁǴIŃÓÚÝŞ]')
KK_WORDS = re.compile(r'\b(және|бұл|мен|үшін|деп|болып|болады|немесе|бойынша|арқылы|қандай|дегеніміз|сондықтан|яғни)\b')


def detect_language(text):
    """Script and frequency heuristic for kk / ru / en.

    One Kazakh letter inside a Russian text does not make it Kazakh: Kazakh prose has
    roughly one special letter per 8–20 Cyrillic letters.
    """
    sample = text[:200000]
    cyrillic = len(re.findall('[а-яА-ЯёЁіІ' + KK_LETTERS + ']', sample))
    latin = len(re.findall('[a-zA-Z]', sample))
    special = len(re.findall('[' + KK_LETTERS + ']', sample))
    kk_words = len(KK_WORDS.findall(sample.casefold()))
    if latin > cyrillic:
        latin_kk = len(KK_LATIN.findall(sample))
        return 'kk' if latin_kk and latin_kk * 40 >= latin else 'en'
    if not cyrillic:
        return 'ru'
    if special and (special * 60 >= cyrillic or kk_words >= 3 or cyrillic < 120):
        return 'kk'
    return 'ru'


RU_MARKERS = re.compile(r'\b(что такое|какова|какой|какие|каковы|что называется|как определяется|почему|объясните|является|которые|который)\b')
EN_MARKERS = re.compile(r'\b(what is|which of|explain|why does|the following)\b')


def obvious_language_mismatch(text, language):
    folded = text.casefold()
    if language == 'kk':
        if RU_MARKERS.search(folded) or EN_MARKERS.search(folded):
            return True
        # Long Kazakh sentences practically always contain at least one Kazakh-specific letter.
        cyr = len(re.findall('[а-яё]', folded))
        return cyr > 60 and not re.search('[әғқңөұүһі]', folded) and not KK_LATIN.search(text)
    if language == 'en':
        return len(re.findall('[а-яА-Яәғқңөұүһі]', text)) > 5
    if language == 'ru':
        return len(re.findall('[әғқңөұүһ]', folded)) > 2 or bool(KK_WORDS.search(folded))
    return False
