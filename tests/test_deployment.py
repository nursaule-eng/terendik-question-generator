import time

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.deployment import COOKIE, LIFETIME, HostedAccess

PASSWORD = 'test-only-strong-password-2026'


@pytest.fixture
def hosted():
    async def endpoint(request):
        return JSONResponse({'ok': True})
    inner = Starlette(routes=[Route('/', endpoint), Route('/api/documents', endpoint),
                              Route('/static/login.css', endpoint)])
    with TestClient(HostedAccess(inner, PASSWORD, ['testserver'], 'Test storage notice'), follow_redirects=False) as client:
        yield client


def test_hosted_fails_closed():
    with pytest.raises(RuntimeError):
        HostedAccess(Starlette(), '', ['testserver'])


def test_anonymous_cannot_read_bank_or_root(hosted):
    assert hosted.get('/api/documents').status_code == 401
    assert hosted.get('/').headers['location'] == '/login'
    page = hosted.get('/login')
    assert page.status_code == 200 and 'Test storage notice' in page.text
    assert hosted.get('/static/login.css').status_code == 200
    assert hosted.get('/healthz').json() == {'status': 'ok'}


def test_login_cookie_and_tampering(hosted):
    response = hosted.post('/login', data={'password': PASSWORD}, headers={'origin': 'http://testserver'})
    assert response.status_code == 303
    assert 'HttpOnly' in response.headers['set-cookie'] and 'SameSite=lax' in response.headers['set-cookie']
    assert hosted.get('/api/documents').status_code == 200
    token = hosted.cookies.get(COOKIE)
    hosted.cookies.set(COOKIE, token[:-1] + ('a' if token[-1] != 'a' else 'b'), domain='testserver.local', path='/')
    assert hosted.get('/api/documents').status_code == 401


def test_expired_session_and_password_change():
    wrapper = HostedAccess(Starlette(), PASSWORD, ['testserver'])
    valid = wrapper.token()
    assert wrapper.valid(valid)
    assert not HostedAccess(Starlette(), PASSWORD + '-new', ['testserver']).valid(valid)
    from unittest.mock import patch
    with patch('app.deployment.time.time', return_value=time.time() + LIFETIME + 5):
        assert not wrapper.valid(valid)


def test_login_csrf_rate_limit_and_invalid_hosts(hosted):
    assert hosted.post('/login', data={'password': PASSWORD}, headers={'origin': 'https://foreign.example'}).status_code == 403
    assert hosted.get('/healthz', headers={'host': 'foreign.example'}).status_code == 400
    for _ in range(5):
        assert hosted.post('/login', data={'password': 'wrong'}).status_code == 401
    assert hosted.post('/login', data={'password': PASSWORD}).status_code == 429


def test_https_session_is_secure():
    app = HostedAccess(Starlette(), PASSWORD, ['testserver'])
    with TestClient(app, base_url='https://testserver', follow_redirects=False) as client:
        response = client.post('/login', data={'password': PASSWORD})
        assert 'Secure' in response.headers['set-cookie']
