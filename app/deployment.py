"""Password-protected hosted entry point; the local entry point stays unchanged."""
import hashlib
import hmac
import html
import os
import secrets
import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

COOKIE = 'terendik_session'
LIFETIME = 7 * 86400


class HostedAccess(BaseHTTPMiddleware):
    def __init__(self, app, password, hosts, storage_notice=''):
        super().__init__(app)
        if len(password) < 20:
            raise RuntimeError('APP_ACCESS_PASSWORD must contain at least 20 characters.')
        self.key = hashlib.sha256(password.encode()).digest()
        self.hosts = set(hosts)
        self.storage_notice = storage_notice
        self.failures = defaultdict(deque)

    def token(self):
        body = f'{int(time.time())}.{secrets.token_hex(16)}'
        return body + '.' + hmac.new(self.key, body.encode(), 'sha256').hexdigest()

    def valid(self, value):
        try:
            issued, nonce, signature = value.split('.')
            age = time.time() - int(issued)
            expected = hmac.new(self.key, f'{issued}.{nonce}'.encode(), 'sha256').hexdigest()
            return 0 <= age < LIFETIME and hmac.compare_digest(signature, expected)
        except (ValueError, TypeError, AttributeError):
            return False

    def page(self, error='', status=200):
        message = '<p class="error">' + html.escape(error) + '</p>' if error else ''
        notice = '<p class="notice">' + html.escape(self.storage_notice) + '</p>' if self.storage_notice else ''
        return HTMLResponse('''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Тереңдік · вход</title>
<link rel="stylesheet" href="/static/login.css"></head><body><main>
<p class="brand">Тереңдік</p><h1>Вход в учебное пространство</h1>
<p>Вход для преподавателя. Студенты проходят тест по отдельной ссылке от преподавателя, пароль им не нужен.</p>'''
            + message + '''<form action="/login" method="post"><label for="password">Пароль доступа</label>
<input id="password" name="password" type="password" required autocomplete="current-password" maxlength="256">
<button type="submit">Войти</button></form>''' + notice + '</main></body></html>', status_code=status)

    async def dispatch(self, request, call_next):
        if request.url.hostname not in self.hosts:
            response = JSONResponse({'detail': 'Недопустимый адрес сервера.'}, status_code=400)
        elif request.method == 'GET' and request.url.path == '/healthz':
            response = JSONResponse({'status': 'ok'})
        elif request.method == 'GET' and request.url.path.startswith('/static/'):
            # Static files hold no data; every API stays behind the session check.
            response = await call_next(request)
        elif request.url.path.startswith(('/s/', '/api/s/')):
            # Student side: only one test by its unguessable link, without the bank or answers in advance.
            response = await call_next(request)
        elif request.url.path == '/logout':
            response = RedirectResponse('/login', 303)
            response.delete_cookie(COOKIE, path='/')
        elif request.url.path == '/login' and request.method == 'GET':
            response = RedirectResponse('/', 303) if self.valid(request.cookies.get(COOKIE)) else self.page()
        elif request.url.path == '/login' and request.method == 'POST':
            origin = request.headers.get('origin')
            if origin and origin != str(request.base_url).rstrip('/'):
                response = JSONResponse({'detail': 'Недопустимый источник запроса.'}, status_code=403)
            elif int(request.headers.get('content-length', '0')) > 4096:
                response = self.page('Запрос слишком большой.', 413)
            else:
                ip = request.client.host if request.client else 'unknown'
                now = time.monotonic()
                bucket = self.failures[ip]
                while bucket and bucket[0] < now - 60:
                    bucket.popleft()
                if len(bucket) >= 5:
                    response = self.page('Слишком много попыток. Повторите через минуту.', 429)
                    response.headers['Retry-After'] = '60'
                else:
                    form = await request.form(max_fields=2, max_files=0)
                    supplied = str(form.get('password', ''))
                    if len(supplied) <= 256 and hmac.compare_digest(hashlib.sha256(supplied.encode()).digest(), self.key):
                        self.failures.pop(ip, None)
                        response = RedirectResponse('/', 303)
                        response.set_cookie(COOKIE, self.token(), max_age=LIFETIME, httponly=True,
                                            secure=request.url.scheme == 'https', samesite='lax', path='/')
                    else:
                        bucket.append(now)
                        response = self.page('Неверный пароль.', 401)
        elif not self.valid(request.cookies.get(COOKIE)):
            response = JSONResponse({'detail': 'Войдите в учебное пространство.'}, status_code=401) if request.url.path.startswith('/api/') else RedirectResponse('/login', 303)
        else:
            response = await call_next(request)
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                                 'Referrer-Policy': 'same-origin',
                                 'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"})
        return response


def create_app():
    from app.main import app
    hosts = ['localhost', '127.0.0.1', 'testserver'] + [
        h.strip() for h in (os.getenv('APP_ALLOWED_HOSTS', '') + ',' + os.getenv('RENDER_EXTERNAL_HOSTNAME', '')).split(',') if h.strip()
    ]
    from app.database import backend
    notice = '' if backend() == 'postgres' else os.getenv('APP_STORAGE_NOTICE', '')
    return HostedAccess(app, os.getenv('APP_ACCESS_PASSWORD', ''), hosts, notice)
