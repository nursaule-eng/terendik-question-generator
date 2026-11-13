import httpx
import pytest
from pydantic import BaseModel

from app.services.llm import LLM, ModelError


class Answer(BaseModel):
    ok: bool


@pytest.fixture
def model_env(monkeypatch):
    monkeypatch.setenv('LLM_API_KEY', 'TEST_ONLY_SECRET')
    monkeypatch.setenv('LLM_BASE_URL', 'https://example.invalid/v1')
    monkeypatch.setenv('LLM_MODEL', 'TEST_ONLY_MODEL')


def test_json_repair_is_bounded(model_env):
    calls = []
    def handler(request):
        calls.append(request)
        assert str(request.url) == 'https://example.invalid/v1/chat/completions'
        content = 'broken JSON' if len(calls) == 1 else '{"ok":true}'
        return httpx.Response(200, json={'choices': [{'message': {'content': content}}]})
    model = LLM(httpx.MockTransport(handler), sleeper=lambda _: None)
    assert model.json('test', {}, Answer).ok
    assert len(calls) == 2
    bad = LLM(httpx.MockTransport(lambda r: httpx.Response(200, json={'choices': [{'message': {'content': 'no'}}]})))
    with pytest.raises(ModelError, match='дважды'):
        bad.json('test', {}, Answer)


def test_retry_temporary_errors_and_auth_no_secret(model_env):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(503)
    with pytest.raises(ModelError, match='временно'):
        LLM(httpx.MockTransport(handler), sleeper=lambda _: None).json('test', {}, Answer)
    assert len(calls) == 3
    with pytest.raises(ModelError) as exc:
        LLM(httpx.MockTransport(lambda r: httpx.Response(401, text='TEST_ONLY_SECRET'))).probe()
    assert 'TEST_ONLY_SECRET' not in str(exc.value)


def test_missing_configuration_is_explicit(client):
    assert not client.get('/api/config').json()['configured']
    response = client.post('/api/model/check')
    assert response.status_code == 503
    assert 'LLM_API_KEY' in response.json()['detail']


def test_request_failure_is_bounded(model_env):
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout('TEST_ONLY_SECRET')
    with pytest.raises(ModelError, match='время ожидания'):
        LLM(httpx.MockTransport(handler), sleeper=lambda _: None).probe()
    assert len(calls) == 3


def test_rate_limit_honors_retry_after_then_recovers(model_env):
    calls, waits, messages = [], [], []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={'retry-after': '8.2'}, text='TEST_ONLY_SECRET')
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"ok":true}'}}]})
    model = LLM(httpx.MockTransport(handler), sleeper=waits.append, on_wait=messages.append)
    assert model.json('test', {}, Answer).ok
    assert waits == [10] and len(calls) == 2
    assert 'HTTP 429' in messages[0] and 'TEST_ONLY_SECRET' not in messages[0]


def test_long_rate_limit_is_reported_without_fast_retries(model_env):
    calls, waits = [], []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={'retry-after': '3600'}, text='TEST_ONLY_SECRET')
    with pytest.raises(ModelError) as exc:
        LLM(httpx.MockTransport(handler), sleeper=waits.append).probe()
    assert exc.value.status_code == 429 and len(calls) == 1 and not waits
    assert '3601 сек.' in str(exc.value) and 'TEST_ONLY_SECRET' not in str(exc.value)


def test_rate_limit_uses_token_reset_header(model_env):
    from app.services.llm import retry_delay
    assert retry_delay(httpx.Response(429, headers={'x-ratelimit-reset-tokens': '1m2.3s'}), 0) == 64
    assert retry_delay(httpx.Response(429, headers={'retry-after': 'nan'}), 0) == 31


def test_groq_completion_budget_is_bounded(model_env, monkeypatch):
    import json
    monkeypatch.setenv('LLM_BASE_URL', 'https://api.groq.com/openai/v1')
    def handler(request):
        assert json.loads(request.content)['max_completion_tokens'] == 2048
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"ok":true}'}}]})
    assert LLM(httpx.MockTransport(handler)).json('test', {}, Answer).ok
