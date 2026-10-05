"""Run: python3 -B project1B_internal/tests/storage.py"""
import fcntl
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datasets_server import checksum, configure, datasets, export_training
from local_preview import database, seed


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    source = 'data/splits/train.jsonl'
    manifest = root / source
    manifest.parent.mkdir(parents=True)
    row = {'episode_uid': 'egoverse/e1', 'dataset': 'egoverse', 'split': 'train',
           'instruction': 'Original', 'duration_s': 1, 'video': '/fixture.mp4', 'subtask': '',
           'caption': '00:00.0 - 00:01.0: [right hand] hold cup'}
    manifest.write_text(json.dumps(row) + '\n')
    original = manifest.read_bytes()
    record = {'id': datasets.eid('EgoVerse', row), 'source': source, 'uid': row['episode_uid'],
              'editor': 'Mock reviewer', 'editor_id': 'mock-caption_data_reviewer',
              'editor_roles': ['caption_data_reviewer', 'worker'], 'version': 1,
              'time': '2026-10-05T00:00:00+00:00', 'source_sha256': checksum(original),
              'instruction': 'Corrected instruction', 'subtask': '', 'caption': row['caption']}
    history = root / 'edits.jsonl'
    encoded = (json.dumps(record) + '\n').encode()
    output = root / 'training'
    command = [sys.executable, '-B', str(Path(__file__).resolve().parents[1] / 'datasets_server.py'),
               '--data-root', str(root), '--edits', str(history), '--source', '=data/splits/train.jsonl',
               '--export', str(output)]
    # A concurrent export must wait until a partial append becomes a complete, synced record.
    with history.open('wb') as writer:
        fcntl.flock(writer, fcntl.LOCK_EX)
        writer.write(encoded[:len(encoded) // 2]); writer.flush()
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            process.wait(timeout=0.5)
            raise AssertionError('Export read an uncommitted correction')
        except subprocess.TimeoutExpired:
            pass
        writer.write(encoded[len(encoded) // 2:]); writer.flush(); os.fsync(writer.fileno())
    stdout, stderr = process.communicate(timeout=20)
    assert process.returncode == 0, stderr.decode()
    exported = json.loads((output / source).read_text())
    assert exported['instruction'] == 'Corrected instruction'
    assert exported['before_edit']['instruction'] == 'Original'
    assert exported['edited_by_id'] == record['editor_id'] and exported['edit_version'] == 1
    assert (output / 'annotation_edits.jsonl').read_bytes() == encoded
    metadata = json.loads((output / 'snapshot.json').read_text())
    assert metadata['history_records'] == 1 and metadata['legacy_history_records'] == 0
    assert metadata['source_sha256'][source] == checksum(original)
    for name, digest in metadata['file_sha256'].items():
        assert checksum((output / name).read_bytes()) == digest
    assert manifest.read_bytes() == original
    datasets.REPO = root
    replayed = datasets.load([('', source)], datasets.read_edits(history))[0]
    assert replayed['instruction'] == 'Corrected instruction' and replayed['version'] == 1
    comparison = datasets.detail(replayed, [record], checksum(original))
    assert comparison['original']['instruction'] == 'Original'
    assert comparison['history'][0]['captions']['instruction'] == 'Corrected instruction'
    assert comparison['history'][0]['editor_id'] == record['editor_id']
    revert = record | {'revert': True, 'version': 2}
    restored = datasets.episode('EgoVerse', source, row, [record, revert])
    restored_comparison = datasets.detail(restored, [record, revert], checksum(original))
    assert restored_comparison['instruction'] == 'Original'
    assert restored_comparison['history'][0]['captions']['instruction'] == 'Corrected instruction'
    assert restored_comparison['history'][1]['revert'] and restored_comparison['history'][1]['captions'] == comparison['original']
    unavailable = datasets.detail(replayed, [record | {'source_sha256': 'old source'}], checksum(original))['history'][0]
    assert not unavailable['available'] and 'captions' not in unavailable
    stored = record | {'original': comparison['original'], 'before': comparison['original']}
    archived = datasets.detail(replayed, [stored], 'new source checksum')['history'][0]
    assert archived['available'] and archived['earlier_source']
    assert archived['original']['instruction'] == 'Original' and archived['before']['instruction'] == 'Original'
    assert archived['captions']['instruction'] == 'Corrected instruction'
    try:
        export_training(root, history, output, [('', source)])
        raise AssertionError('Overwrote an existing training snapshot')
    except FileExistsError:
        pass
    manifest.write_text(json.dumps(row | {'instruction': 'Regenerated'}) + '\n')
    try:
        export_training(root, history, root / 'drift', [('', source)])
        raise AssertionError('Accepted changed source manifest')
    except ValueError as error:
        assert 'Source changed' in str(error)
    assert not (root / 'drift').exists()
    try:
        configure(root, history, [('', source)])
        raise AssertionError('Loaded corrections against changed source')
    except ValueError as error:
        assert 'Source changed' in str(error)
    manifest.write_bytes(original)
    history.write_bytes(encoded + b'{"partial":')
    try:
        export_training(root, history, root / 'corrupt', [('', source)])
        raise AssertionError('Accepted truncated correction history')
    except json.JSONDecodeError:
        pass
    assert not (root / 'corrupt').exists()
    db = root / 'mock.sqlite3'
    seed(db); seed(db)
    with database(db) as connection:
        for role in ('caption_data_admin', 'caption_data_reviewer'):
            assert connection.execute('SELECT name FROM roles WHERE name = ?', (role,)).fetchone()
            grants = [x[0] for x in connection.execute('SELECT role FROM user_roles WHERE user_id = ?', (f'mock-{role}',))]
            assert role in grants and 'admin' not in grants
        assert [x[0] for x in connection.execute("SELECT role FROM user_roles WHERE user_id='mock-worker'")] == ['worker']
print('PASS: locked export; replay; comparison/revert history; training captions/provenance/checksums; immutable snapshots; source drift/corruption refusal; new mock roles.')
