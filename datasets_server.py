"""Project1B session-protected adapter for the shared dataset viewer."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import http.client
import importlib.util
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from http.cookies import SimpleCookie, CookieError
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, os.environ.get('DATASETS_REPO', str(HERE.parent)))
spec = importlib.util.spec_from_file_location('captioning_data_viewer', HERE / 'viewer/server.py')
datasets = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = datasets
spec.loader.exec_module(datasets)


def checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sync_directory(path: Path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def validate_sources(records: dict, hashes: dict):
    for changes in records.values():
        live = changes[-1]
        if not live.get('revert') and live.get('source_sha256'):
            if hashes.get(live['source']) != live['source_sha256']:
                raise ValueError(f"Source changed since correction: {live['source']}; restore original manifest before continuing")


def export_training(data_root: Path, edits: Path, output: Path, sources=None):
    """Publish a complete training snapshot; never overwrite an existing export."""
    sources = sources or datasets.SOURCES
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f'Export already exists: {output}')
    output.parent.mkdir(parents=True, exist_ok=True)
    # Writers lock the history file for each append. Copy only complete, durable saves.
    with edits.open('rb') as history:
        fcntl.flock(history, fcntl.LOCK_SH)
        snapshot = history.read()
    with tempfile.TemporaryDirectory(prefix='.caption-export-', dir=output.parent) as temporary:
        root = Path(temporary)
        package = root / 'package'
        package.mkdir(mode=0o700)
        audit = package / 'annotation_edits.jsonl'
        audit.write_bytes(snapshot)
        records = datasets.read_edits(audit)  # Corrupt/truncated history aborts export.
        inputs, hashes = root / 'inputs', {}
        for _, source in sources:
            relative = Path(source)
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError('Export source must be relative to data root')
            content = (data_root / relative).read_bytes()
            hashes[source] = checksum(content)
            target = inputs / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        validate_sources(records, hashes)
        original_root = datasets.REPO
        try:
            datasets.REPO = inputs
            datasets.export(sources, records, package)
        finally:
            datasets.REPO = original_root
        files = {str(path.relative_to(package)): checksum(path.read_bytes())
                 for path in package.rglob('*') if path.is_file()}
        metadata = {'created_at': datetime.now(timezone.utc).isoformat(),
                    'history_records': sum(map(len, records.values())),
                    'legacy_history_records': sum(not change.get('source_sha256') for changes in records.values() for change in changes),
                    'source_sha256': hashes, 'file_sha256': files}
        (package / 'snapshot.json').write_text(json.dumps(metadata, indent=2) + '\n')
        for path in package.rglob('*'):
            if path.is_file():
                with path.open('rb') as handle:
                    os.fsync(handle.fileno())
        for path in sorted((p for p in package.rglob('*') if p.is_dir()), reverse=True):
            sync_directory(path)
        sync_directory(package)
        package.rename(output)
        sync_directory(output.parent)
    return metadata


def assets() -> dict[str, tuple[bytes, str]]:
    """Share the existing UI; external scripts keep the site's script CSP intact."""
    html = datasets.UI.read_text()
    style = re.search(r'<style>(.*?)</style>', html, re.S)
    script = re.search(r'<script>(.*?)</script>', html, re.S)
    if not style or not script:
        raise ValueError('Dataset viewer must contain one style and one script block')
    html = html.replace(style[0], '<link rel="stylesheet" href="/captioning_data/viewer.css">')
    html = html.replace(script[0], '<script src="/captioning_data/auth.js"></script><script src="/captioning_data/viewer.js"></script>')
    html = html.replace('<h1>captioning_data</h1>', '<a href="/">Project1B</a><h1>captioning_data</h1>')
    html = html.replace('<title>captioning_data</title>', '<title>Project1B · captioning_data</title>')
    return {
        '/': (html.encode(), 'text/html; charset=utf-8'),
        '/viewer.css': (style[1].encode(), 'text/css; charset=utf-8'),
        '/viewer.js': (script[1].encode(), 'text/javascript; charset=utf-8'),
        '/auth.js': ((Path(__file__).parent / 'web/datasets-auth.js').read_bytes(), 'text/javascript; charset=utf-8'),
    }


class Handler(datasets.Handler):
    auth_api = ('127.0.0.1', 8903)
    origin = 'https://internal.project1b.space'
    write_failed = False

    def send_header(self, keyword, value):
        if keyword.lower() != 'cache-control':
            super().send_header(keyword, value)

    def end_headers(self):
        super().send_header('Cache-Control', 'no-store')
        super().end_headers()

    def authenticate(self) -> bool:
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get('Cookie', ''))
        except CookieError:
            return self._denied(401, 'Sign in to Project1B first')
        session = cookies.get('__Host-rbt_session')
        if not session or not session.value:
            return self._denied(401, 'Sign in to Project1B first')
        connection = http.client.HTTPConnection(*self.auth_api, timeout=5)
        try:
            connection.request('GET', '/api/auth/me', headers={'Cookie': session.OutputString()})
            response = connection.getresponse()
            if response.status in (401, 403):
                return self._denied(response.status, 'Project1B session expired or access denied')
            if response.status != 200:
                return self._denied(503, 'Project1B authentication unavailable')
            user = json.loads(response.read())
            if not isinstance(user, dict) or not isinstance(user.get('user_id'), str) or not user['user_id']:
                return self._denied(503, 'Project1B authentication unavailable')
            self.identity = user
            self.cookies = cookies
            return True
        except (OSError, http.client.HTTPException, ValueError):
            return self._denied(503, 'Project1B authentication unavailable')
        finally:
            connection.close()

    def _denied(self, code: int, message: str) -> bool:
        self.close_connection = True
        if code == 401 and urlsplit(self.path).path == '/captioning_data/':
            self._send(302, b'', 'text/plain', [('Location', '/?next=/captioning_data/')])
        else:
            self._json({'error': message}, code)
        return False

    def route(self) -> str | None:
        path = urlsplit(self.path).path
        return path[len('/captioning_data'):] if path.startswith('/captioning_data/') else None

    def do_GET(self):
        if self.path == '/healthz':
            return self._json({'episodes': len(self.by_uid)})
        path = self.route()
        if path not in (*self.ui, '/api/episodes', '/api/search', '/api/episode', '/video', '/vtt'):
            return self._json({'error': 'Not found'}, 404)
        if not self.authenticate():
            return
        if path in self.ui:
            body, content_type = self.ui[path]
            return self._send(200, body, content_type)
        if path == '/api/episodes':
            user = self.identity
            return self._json({'episodes': [datasets.summary(e) for e in self.by_uid.values()],
                               'roster': self.roster,
                               'user': {'name': user.get('full_name') or user.get('email') or user['user_id'],
                                        'roles': user.get('roles', []),
                                        'role': 'admin' if {'admin', 'caption_data_admin'}.intersection(user.get('roles', []))
                                        else 'reviewer' if 'caption_data_reviewer' in user.get('roles', []) else 'member'}})
        self.path = self.path[len('/captioning_data'):]
        return super().do_GET()

    def do_POST(self):
        path = self.route()
        if path not in ('/api/edit', '/api/revert'):
            return self._json({'error': 'Not found'}, 404)
        if not self.authenticate():
            return
        csrf_cookie = self.cookies.get('__Host-rbt_csrf')
        csrf_header = self.headers.get('X-CSRF-Token', '')
        if (self.headers.get('Origin') != self.origin or not csrf_cookie or not csrf_header
                or not hmac.compare_digest(csrf_cookie.value.encode(), csrf_header.encode())):
            return self._denied(403, 'Same-origin request and CSRF token required')
        if self.headers.get_content_type() != 'application/json' or self.headers.get('Transfer-Encoding'):
            return self._denied(400, 'JSON request body required')
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 1 << 20:
                return self._denied(413, 'Request too large or empty')
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict) or not isinstance(body.get('id'), str) or type(body.get('version')) is not int:
                raise ValueError('Episode id and integer version required')
            with self.lock:
                if self.write_failed:
                    return self._json({'error': 'Correction log unavailable; contact administrator'}, 503)
                e = self.by_uid.get(body['id'])
                if e is None:
                    return self._json({'error': 'Unknown episode'}, 404)
                recs = self.edits[e['id']]
                if body['version'] != len(recs):
                    return self._json({'error': 'Someone saved this episode after you opened it; reload it first'}, 409)
                user = self.identity
                rec = {'id': e['id'], 'source': e['source'], 'uid': e['uid'],
                       'editor': user.get('full_name') or user.get('email') or user['user_id'],
                       'editor_id': user['user_id'], 'editor_roles': user.get('roles', []),
                       'version': len(recs) + 1, 'source_sha256': self.source_hashes[e['source']],
                       'before': datasets.caption_state(e),
                       'original': datasets.original_captions(e, recs, self.source_hashes[e['source']]),
                       'time': datetime.now(timezone.utc).isoformat()}
                rec |= {'revert': True} if path == '/api/revert' else datasets.clean_edit(e, body)
                try:
                    with self.edit_log.open('a') as log:
                        fcntl.flock(log, fcntl.LOCK_EX)
                        log.write(json.dumps(rec) + '\n')
                        log.flush()
                        os.fsync(log.fileno())
                        sync_directory(self.edit_log.parent)
                except OSError:
                    type(self).write_failed = True
                    return self._json({'error': 'Correction log write failed; contact administrator before retrying'}, 503)
                recs.append(rec)
                updated = datasets.episode(e['dataset'], e['source'], e['row'], recs,
                                           self.checks.get(e['uid']), e['assignee'])
                self.by_uid[e['id']] = updated
                self.hay[e['id']] = datasets.haystack(updated)
            return self._json(datasets.detail(updated, recs, self.source_hashes[updated['source']]))
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as error:
            return self._json({'error': str(error)}, 400)


def configure(data_root: Path, edits: Path, sources=None):
    datasets.REPO = data_root
    sources = sources or datasets.SOURCES
    Handler.source_hashes = {source: checksum((data_root / source).read_bytes()) for _, source in sources}
    Handler.edit_log = edits
    Handler.edits = datasets.read_edits(edits)
    validate_sources(Handler.edits, Handler.source_hashes)
    qa = data_root / datasets.QA_DIR
    Handler.roster = datasets.read_json(qa / 'roster.json', {'reviewers': [], 'admins': []})
    Handler.assigned = datasets.read_json(qa / 'assignment.json', {}).get('episodes', {})
    Handler.checks = {uid: check for directory in datasets.CHECKS for uid, check in datasets.read_check(directory).items()}
    episodes = datasets.load(sources, Handler.edits, Handler.checks, Handler.assigned)
    Handler.by_uid = {e['id']: e for e in episodes}
    Handler.hay = {e['id']: datasets.haystack(e) for e in episodes}
    Handler.ui = assets()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, default=HERE.parent)
    parser.add_argument('--edits', type=Path, required=True)
    parser.add_argument('--source', action='append', metavar='DATASET=MANIFEST')
    parser.add_argument('--export', type=Path, metavar='DIR', help='Export a durable training snapshot, then exit')
    parser.add_argument('--port', type=int, default=8325)
    parser.add_argument('--auth-port', type=int, default=8903)
    parser.add_argument('--origin', default=Handler.origin)
    args = parser.parse_args()
    sources = [tuple(s.split('=', 1)) for s in args.source] if args.source else None
    if args.export:
        export_training(args.data_root.resolve(), args.edits, args.export, sources)
        print(f'Training snapshot: {args.export.resolve()}')
        return
    args.edits.parent.mkdir(parents=True, exist_ok=True)
    # Only one process may write this history; the thread lock handles concurrent requests.
    with args.edits.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        configure(args.data_root.resolve(), args.edits, sources)
        Handler.auth_api = ('127.0.0.1', args.auth_port)
        Handler.origin = args.origin
        print(f'{len(Handler.by_uid)} episodes; http://127.0.0.1:{args.port}/captioning_data/', flush=True)
        ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
