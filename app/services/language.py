import re

LANGUAGES = {'kk': 'қазақ тілі (казахский)', 'ru': 'русский язык', 'en': 'English (английский)'}


def detect_language(text):
    """Small, explicit script heuristic; uncertain Cyrillic defaults to Russian."""
    if re.search('[әғқңөұүһіӘҒҚҢӨҰҮҺІ]', text):
        return 'kk'
    cyrillic = len(re.findall('[а-яА-ЯёЁ]', text))
    latin = len(re.findall('[a-zA-Z]', text))
    return 'en' if latin > cyrillic else 'ru'


def obvious_language_mismatch(text, language):
    if language == 'kk':
        return bool(re.search(r'\b(что такое|какова|какой|какие|что называется|как определяется|what is|which of)\b', text.casefold()))
    if language == 'en':
        return len(re.findall('[а-яА-Яәғқңөұүһі]', text)) > 5
    if language == 'ru':
        return bool(re.search('[әғқңөұүһі]', text.casefold()))
    return False
