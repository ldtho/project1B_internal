"""Run captioning_data locally with SQLite mock accounts and isolated corrections."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import ipaddress
import hmac
import json
import os
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from http.cookies import CookieError, SimpleCookie
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from datasets_server import Handler as DatasetHandler, configure, datasets

HERE = Path(__file__).resolve().parent
PASSWORD = 'preview-password'
ACCOUNTS = {'admin': ['admin', 'worker'], 'worker': ['worker'],
            'caption_manager': ['caption_manager', 'worker'],
            'caption_data_admin': ['caption_data_admin', 'worker'],
            'caption_data_reviewer': ['caption_data_reviewer', 'worker']}
ROLES = ('worker', 'admin', 'record_manager', 'cut_manager', 'caption_manager',
         'caption_data_admin', 'caption_data_reviewer')


@contextmanager
def database(path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f'scrypt${salt.hex()}${digest.hex()}'


def seed(path):
    with database(path) as connection:
        connection.executescript('''
            CREATE TABLE IF NOT EXISTS roles (name TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, full_name TEXT NOT NULL,
                password_hash TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS user_roles (
                user_id TEXT NOT NULL, role TEXT NOT NULL, PRIMARY KEY (user_id, role));
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, csrf_hash TEXT NOT NULL,
                expires_at REAL NOT NULL);
        ''')
        connection.executemany('INSERT OR IGNORE INTO roles VALUES (?)', [(role,) for role in ROLES])
        for name, roles in ACCOUNTS.items():
            result = connection.execute('INSERT OR IGNORE INTO users VALUES (?, ?, ?, ?, 1)',
                                        (f'mock-{name}', name, f'Mock {name.replace("_", " ")}', password_hash(PASSWORD)))
            if result.rowcount:
                connection.executemany('INSERT INTO user_roles VALUES (?, ?)', [(f'mock-{name}', role) for role in roles])


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class MockAPI(datasets.inspect_server.Handler):
    def profile(self, connection, row):
        return {'user_id': row['id'], 'username': row['username'], 'full_name': row['full_name'],
                'roles': [role[0] for role in connection.execute('SELECT role FROM user_roles WHERE user_id = ? ORDER BY role', (row['id'],))]}

    def authenticated(self):
        self.cookies = SimpleCookie()
        try:
            self.cookies.load(self.headers.get('Cookie', ''))
        except CookieError:
            return None
        cookie = self.cookies.get('__Host-rbt_session')
        with database(self.db) as connection:
            row = connection.execute('''SELECT u.*, s.csrf_hash FROM sessions s JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = ? AND s.expires_at > ? AND u.is_active = 1''',
                                     (digest(cookie.value if cookie else ''), time.time())).fetchone()
            return (self.profile(connection, row), row['csrf_hash']) if row else None

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in ('/api/auth/web/login', '/api/auth/logout'):
            return self._json({'detail': 'Not found'}, 404)
        if path == '/api/auth/logout':
            authenticated = self.authenticated()
            if not authenticated:
                return self._json({'detail': 'Not signed in'}, 401)
            cookie = self.cookies.get('__Host-rbt_csrf')
            token = self.headers.get('X-CSRF-Token', '')
            if (self.headers.get('Origin') != self.origin or not cookie or not token
                    or not hmac.compare_digest(cookie.value.encode(), token.encode())
                    or not hmac.compare_digest(digest(token), authenticated[1])):
                return self._json({'detail': 'CSRF token and same-origin request required'}, 403)
            with database(self.db) as connection:
                connection.execute('DELETE FROM sessions WHERE token_hash = ?', (digest(self.cookies['__Host-rbt_session'].value),))
            return self._send(204, b'', 'application/json', [('Set-Cookie', f'{name}=; Path=/; Secure; SameSite=Strict; Max-Age=0')
                                                         for name in ('__Host-rbt_session', '__Host-rbt_csrf')])
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 16384 or self.headers.get_content_type() != 'application/json':
                raise ValueError('JSON login body required')
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict) or not isinstance(body.get('email'), str) or not isinstance(body.get('password'), str):
                raise ValueError('Username and password required')
            with database(self.db) as connection:
                row = connection.execute('SELECT * FROM users WHERE username = ?', (body['email'].strip(),)).fetchone()
                stored = row['password_hash'] if row else self.dummy_hash
                _, salt, _ = stored.split('$')
                valid = hmac.compare_digest(stored, password_hash(body['password'], bytes.fromhex(salt)))
                if not row or not valid or not row['is_active']:
                    return self._json({'detail': 'Invalid credentials'}, 401)
                user = self.profile(connection, row)
                session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                connection.execute('INSERT INTO sessions VALUES (?, ?, ?, ?)',
                                   (digest(session), row['id'], digest(csrf), time.time() + 43200))
            return self._send(200, json.dumps(user).encode(), 'application/json', [
                ('Set-Cookie', f'__Host-rbt_session={session}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=43200'),
                ('Set-Cookie', f'__Host-rbt_csrf={csrf}; Path=/; Secure; SameSite=Strict; Max-Age=43200')])
        except (ValueError, TypeError):
            self.close_connection = True
            return self._json({'detail': 'Invalid JSON login body'}, 400)

    def do_GET(self):
        authenticated = self.authenticated()
        if not authenticated:
            return self._json({'detail': 'Not signed in'}, 401)
        user = authenticated[0]
        path = urlsplit(self.path).path
        if path == '/api/auth/me':
            return self._json(user)
        if 'admin' not in user['roles']:
            return self._json({'detail': 'Admin role required'}, 403)
        episode = self.sample
        item = {'pk': 1, 'video_id': episode['uid'], 'task': episode['instruction'], 'worker': 'Mock recording',
                'status': 'uploaded', 'duration_s': episode['duration'], 'uploaded_at': '2026-10-05T00:00:00Z'}
        if path == '/api/admin/videos':
            return self._json({'videos': [item]})
        if path == '/api/admin/videos/1':
            return self._json(item | {'task': {'name': item['task']}, 'worker': {'name': item['worker']},
                                     'files': [], 'original_bundle_available': False,
                                     'media_roles': [{'role': 'primary', 'label': 'Preview sample'}]})
        if path == '/api/admin/media/1':
            return self._serve_video(Path(episode['video']))
        return self._json({'detail': 'Not found'}, 404)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=9445)
    parser.add_argument('--host', type=ipaddress.IPv4Address, default=ipaddress.IPv4Address('127.0.0.1'),
                        help='IPv4 address to expose the HTTPS preview on')
    parser.add_argument('--state-dir', type=Path, default=HERE / '.preview')
    parser.add_argument('--data-root', type=Path, default=HERE.parent)
    args = parser.parse_args()
    state = args.state_dir.resolve()
    state.mkdir(parents=True, exist_ok=True)
    os.chmod(state, 0o700)
    origin = f'https://{args.host}:{args.port}'
    stopped = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopped.set())
    with (state / 'edits.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        MockAPI.db, MockAPI.origin = state / 'mock.sqlite3', origin
        MockAPI.dummy_hash = password_hash(PASSWORD)
        seed(MockAPI.db)
        edits = state / 'edits.jsonl'
        if not edits.exists():
            initial = args.data_root / datasets.EDIT_LOG
            edits.write_bytes(initial.read_bytes() if initial.is_file() else b'')
        configure(args.data_root.resolve(), edits)
        MockAPI.sample = next(e for e in DatasetHandler.by_uid.values() if Path(e['video']).is_file())
        html, content_type = DatasetHandler.ui['/']
        DatasetHandler.ui['/'] = (html.replace(b'<header>', b'<header><b>Local mock preview</b>', 1), content_type)
        web = state / 'web'
        web.mkdir(exist_ok=True)
        for asset in ('index.html', 'app.js', 'styles.css', 'favicon.svg'):
            shutil.copyfile(HERE / 'web' / asset, web / asset)
        index = web / 'index.html'
        index.write_text(index.read_text().replace('<main>', '<main><p class="notice">Local mock preview. Corrections use isolated local history.</p>', 1))
        certificate, key = state / f'cert-{args.host}.pem', state / f'key-{args.host}.pem'
        if not certificate.is_file():
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '30',
                            '-subj', f'/CN={args.host}', '-addext', f'subjectAltName=DNS:localhost,IP:{args.host}',
                            '-keyout', str(key), '-out', str(certificate)], check=True, capture_output=True)
            os.chmod(key, 0o600)
        with ThreadingHTTPServer(('127.0.0.1', 0), MockAPI) as api, ThreadingHTTPServer(('127.0.0.1', 0), DatasetHandler) as viewer:
            DatasetHandler.auth_api = ('127.0.0.1', api.server_port)
            DatasetHandler.origin = origin
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                http_port = probe.getsockname()[1]
            site = (HERE / 'deploy/nginx.conf').read_text()
            replacements = {
                'listen 80;': f'listen 127.0.0.1:{http_port};', 'listen [::]:80;': '',
                'listen 443 ssl http2;': f'listen {args.host}:{args.port} ssl;', 'listen [::]:443 ssl http2;': '',
                'server_name internal.project1b.space;': f'server_name localhost {args.host};',
                'https://internal.project1b.space': origin,
                '/etc/letsencrypt/live/internal.project1b.space/fullchain.pem': str(certificate),
                '/etc/letsencrypt/live/internal.project1b.space/privkey.pem': str(key),
                'include /etc/letsencrypt/options-ssl-nginx.conf;': 'ssl_protocols TLSv1.2 TLSv1.3;',
                'ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;': '',
                'add_header Strict-Transport-Security "max-age=86400" always;': '',
                '/var/www/project1b-internal/current': str(web),
                '127.0.0.1:8903': f'127.0.0.1:{api.server_port}', '127.0.0.1:8325': f'127.0.0.1:{viewer.server_port}',
            }
            for old, new in replacements.items():
                site = site.replace(old, new)
            config = state / 'nginx.conf'
            config.write_text(f'daemon off; pid {state}/nginx.pid; error_log stderr; events {{}} http {{ '
                              f'include /etc/nginx/mime.types; access_log off; client_body_temp_path {state}/body; '
                              f'proxy_temp_path {state}/proxy; {site} }}')
            nginx_command = ['nginx', '-p', f'{state}/', '-c', str(config)]
            subprocess.run([*nginx_command, '-t'], check=True, capture_output=True)
            for server in (api, viewer):
                threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                with (state / 'nginx.log').open('a') as log:
                    nginx = subprocess.Popen(nginx_command, stdout=log, stderr=log)
                    (state / 'preview.pid').write_text(str(os.getpid()))
                    print(f'PREVIEW {origin}/captioning_data/', flush=True)
                    print(f'MOCK ACCOUNTS {", ".join(ACCOUNTS)}; password: {PASSWORD}', flush=True)
                    print(f'DATABASE {MockAPI.db}; {len(DatasetHandler.by_uid)} samples', flush=True)
                    try:
                        while not stopped.wait(1):
                            if nginx.poll() is not None:
                                raise RuntimeError(f'Local Nginx exited; inspect {state}/nginx.log')
                    finally:
                        nginx.terminate()
                        nginx.wait(timeout=10)
                        (state / 'preview.pid').unlink(missing_ok=True)
            finally:
                api.shutdown()
                viewer.shutdown()


if __name__ == '__main__':
    main()
