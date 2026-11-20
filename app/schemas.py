from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True, strict=True)


class Source(Strict):
    fragment_id: int = Field(gt=0)
    quote: str = Field(min_length=8, max_length=2000)


class Candidate(Strict):
    text: str = Field(min_length=8, max_length=4000)
    type: Literal['mcq', 'open']
    options: list[str] = Field(default_factory=list, max_length=4)
    correct_index: int | None = Field(default=None, ge=0, le=3)
    reference_answer: str = Field(min_length=2, max_length=6000)
    explanation: str = Field(min_length=2, max_length=6000)
    topic: str = Field(min_length=2, max_length=250)
    sources: list[Source] = Field(min_length=1, max_length=8)

    @model_validator(mode='after')
    def check_type(self):
        if self.type == 'mcq':
            if len(self.options) != 4 or self.correct_index is None:
                raise ValueError('Нужны четыре варианта и индекс правильного ответа 0–3')
            if any(not x.strip() for x in self.options) or len({x.strip().casefold() for x in self.options}) != 4:
                raise ValueError('Варианты должны быть непустыми и различаться')
        elif self.options or self.correct_index is not None:
            raise ValueError('У открытого вопроса нет вариантов и индекса')
        return self


class Batch(Strict):
    questions: list[Candidate] = Field(max_length=5)
    reason: str = Field(max_length=2000)


class Topic(Strict):
    name: str = Field(min_length=2, max_length=250)
    keywords: list[str] = Field(min_length=1, max_length=20)
    fragment_ids: list[int] = Field(min_length=1, max_length=100)


class TopicBatch(Strict):
    topics: list[Topic] = Field(max_length=15)


class Verdict(Strict):
    relevant: bool
    supported: bool
    clear: bool
    single_correct: bool
    level: int = Field(ge=1, le=5)
    difficulty_reason: str = Field(min_length=3, max_length=2000)
    reasons: list[str] = Field(max_length=20)
    is_duplicate: bool
    language_matches: bool


class IndependentAnswer(Strict):
    has_single_answer: bool
    correct_index: int | None = Field(default=None, ge=0, le=3)
    answer: str = Field(min_length=1, max_length=4000)
    reasoning: str = Field(min_length=2, max_length=5000)
    calculation: str | None = Field(default=None, max_length=400)


class AnswerAgreement(Strict):
    matches: bool
    reason: str = Field(min_length=2, max_length=2000)


class Grade(Strict):
    score: int = Field(ge=0, le=10)
    strengths: str = Field(default='', max_length=2000)
    missed: str = Field(default='', max_length=2000)
    advice: str = Field(default='', max_length=2000)


class Generate(Strict):
    document_id: str
    topic_id: int | None = None
    count: int = Field(default=5, ge=1, le=50)
    level: int | None = Field(default=1, ge=1, le=5)
    type: Literal['mcq', 'open', 'mixed'] = 'mixed'
    language: Literal['auto', 'kk', 'ru', 'en'] = 'auto'


class Edit(Candidate):
    requested_level: int = Field(ge=1, le=5)
    revision: int = Field(ge=1)
    language: Literal['kk', 'ru', 'en'] = 'ru'


class Review(Strict):
    status: Literal['pending', 'accepted', 'rejected']
    revision: int = Field(ge=1)


class TestRequest(Generate):
    pass


class AssignmentRequest(Strict):
    document_id: str
    title: str = Field(min_length=2, max_length=200)
    topic_id: int | None = None
    count: int = Field(default=10, ge=1, le=100)
    level: int | None = Field(default=None, ge=1, le=5)
    type: Literal['mcq', 'open', 'mixed'] = 'mixed'
    language: Literal['auto', 'kk', 'ru', 'en'] = 'auto'
    show_answers: bool = True
    ui_language: Literal['kk', 'ru', 'en'] = 'ru'
    adaptive: bool = False
    adaptive_length: int = Field(default=10, ge=3, le=30)


class AssignmentUpdate(Strict):
    is_open: bool


class AdaptiveStart(Strict):
    document_id: str
    topic_id: int | None = None
    language: Literal['auto', 'kk', 'ru', 'en'] = 'auto'
    length: int = Field(default=10, ge=3, le=30)
    start_level: int = Field(default=2, ge=1, le=5)


class AdaptiveAnswer(Strict):
    question_id: str
    answer: int | None = None


class StudentStart(Strict):
    name: str = Field(min_length=2, max_length=120)


class Submit(Strict):
    answers: dict[str, int | str | None]


class SelfScores(Strict):
    scores: dict[str, int]  # validated against the attempt's open questions, 0..5
