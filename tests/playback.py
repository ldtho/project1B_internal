"""Run: python3 -B project1B_internal/tests/playback.py (requires ffmpeg/ffprobe)."""
import hashlib
import http.client
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datasets_server import Handler, configure, datasets


def frame_times(path):
    result = subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
        '-show_entries', 'frame=best_effort_timestamp_time', '-of', 'json', str(path)])
    return [frame['best_effort_timestamp_time'] for frame in json.loads(result)['frames']]


class Auth(BaseHTTPRequestHandler):
    calls = 0

    def do_GET(self):
        type(self).calls += 1
        authorized = self.headers.get('Cookie') == '__Host-rbt_session=valid'
        body = json.dumps({'user_id': 'mock-reviewer', 'roles': ['caption_data_reviewer']}
                          if authorized else {'error': 'expired'}).encode()
        self.send_response(200 if authorized else 401)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    source = root / 'source.mp4'
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
        'testsrc2=size=128x96:rate=30:duration=2', '-c:v', 'libx265', '-preset', 'ultrafast',
        '-x265-params', 'pools=1:frame-threads=1:log-level=error', str(source)], check=True)
    checksum = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest = root / 'data/splits/train.jsonl'
    manifest.parent.mkdir(parents=True)
    row = {'episode_uid': 'egoverse/fixture', 'dataset': 'egoverse', 'split': 'train',
           'video': str(source), 'duration_s': 2, 'instruction': 'Inspect hands', 'subtask': '',
           'caption': '[0.0 - 2.0] [both hands] move object'}
    manifest.write_text(json.dumps(row) + '\n')
    edits = root / 'state/edits.jsonl'
    edits.parent.mkdir(mode=0o700)
    configure(root, edits, [('', 'data/splits/train.jsonl')])
    episode = next(iter(Handler.by_uid.values()))
    assert datasets.playback_file(episode | {'dataset': 'EgoDex'}) == source
    with patch.object(datasets, 'video_info', wraps=datasets.video_info) as probe:
        with ThreadPoolExecutor(max_workers=4) as workers:
            copies = list(workers.map(datasets.playback_file, [episode] * 4))
        assert probe.call_count == 2, 'Concurrent requests encoded the same source twice'
    target = copies[0]
    assert copies == [target] * 4 and target != source
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700
    assert datasets.video_info(target)['codec_name'] == 'h264'
    assert frame_times(source) == frame_times(target), 'Caption/video frame timestamps shifted'
    atoms = []
    with target.open('rb') as file:
        position = 0
        while position < target.stat().st_size:
            file.seek(position)
            size, kind = struct.unpack('>I4s', file.read(8))
            atoms.append(kind)
            assert size >= 8
            position += size
    assert atoms.index(b'moov') < atoms.index(b'mdat'), 'Playback metadata must precede video bytes'
    stamp = target.stat().st_mtime_ns
    assert datasets.playback_file(episode).stat().st_mtime_ns == stamp
    clip = datasets.playback_file(episode | {'dataset': 'Molmo', 'clip_start': 0.5, 'clip_end': 1.5})
    assert abs(float(datasets.video_info(clip)['duration']) - 1) < 0.001
    assert datasets.video_info(clip)['width'] == 480
    failed_source = root / 'failed.mp4'
    failed_source.write_bytes(source.read_bytes())
    before = set(target.parent.glob('*.mp4'))
    original = datasets.video_info(source)
    with patch.object(datasets, 'video_info', side_effect=[original, original | {'codec_name': 'h264', 'nb_frames': '61'}]):
        try:
            datasets.playback_file(episode | {'video': str(failed_source)})
        except OSError as error:
            assert 'frames or timing' in str(error)
        else:
            raise AssertionError('Invalid playback copy was published')
    assert set(target.parent.glob('*.mp4')) == before
    assert not list(target.parent.glob('*.tmp.mp4'))
    # Preloading does not read or write caption history, even while reviewers append it.
    edits.write_text('untouched history\n')
    result = subprocess.check_output([sys.executable, '-B', str(Path(__file__).resolve().parents[1] / 'datasets_server.py'),
        '--data-root', str(root), '--edits', str(edits), '--source', '=data/splits/train.jsonl', '--prepare-playback'])
    assert b'Playback ready: 1/1' in result and edits.read_text() == 'untouched history\n'

    auth = ThreadingHTTPServer(('127.0.0.1', 0), Auth)
    Handler.auth_api = auth.server_address
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    for service in (auth, server):
        threading.Thread(target=service.serve_forever, daemon=True).start()

    def request(path, headers=None, session='valid'):
        connection = http.client.HTTPConnection(*server.server_address, timeout=10)
        values = {'Cookie': f'__Host-rbt_session={session}'} if session else {}
        values.update(headers or {})
        connection.request('GET', path, headers=values)
        response = connection.getresponse()
        status, headers, body = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return status, headers, body

    try:
        path = '/captioning_data/video?p=' + quote(episode['id'])
        status, headers, body = request(path)
        assert status == 200 and body == target.read_bytes()
        assert headers['Cache-Control'] == 'private, no-cache, must-revalidate'
        etag = headers['ETag']
        calls = Auth.calls
        status, headers, body = request(path, {'If-None-Match': etag})
        assert status == 304 and body == b'' and Auth.calls == calls + 1
        assert headers['ETag'] == etag
        status, headers, body = request(path, {'Range': 'bytes=8-31', 'If-Range': etag})
        assert status == 206 and body == target.read_bytes()[8:32]
        status, headers, body = request(path, {'Range': 'bytes=8-31', 'If-Range': '"old"'})
        assert status == 200 and body == target.read_bytes()
        assert request(path, {'Range': 'bytes=999999999-'} )[0] == 416
        for session in (None, 'expired'):
            status, headers, body = request(path, {'If-None-Match': etag}, session)
            assert status == 401 and headers['Cache-Control'] == 'no-store' and 'ETag' not in headers
        for endpoint in ('/captioning_data/', '/captioning_data/api/episodes'):
            status, headers, body = request(endpoint, {'If-None-Match': '*'})
            assert status == 200 and headers['Cache-Control'] == 'no-store'
        assert request('/captioning_data/video?p=unknown', {'If-None-Match': '*'})[0] == 404
        stat = source.stat()
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000))
        fresh = datasets.playback_file(episode)
        assert fresh != target
        status, headers, body = request(path, {'If-None-Match': etag})
        assert status == 200 and headers['ETag'] != etag and body == fresh.read_bytes()
        assert hashlib.sha256(source.read_bytes()).hexdigest() == checksum
        assert json.loads(manifest.read_text()) == row and edits.read_text() == 'untouched history\n'
    finally:
        for service in (server, auth):
            service.shutdown()
            service.server_close()
print('PASS: HEVC→H.264, faststart, identical frame times/count/rate/resolution; cache reuse/invalidation/concurrency; failed conversion cleanup; clip playback; preload without history changes; authenticated 304/ranges; expired sessions refused; UI/API no-store.')
