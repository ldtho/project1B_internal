"""Run: python3 -B project1B_internal/tests/dataset_sources.py"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datasets_server import Handler, checksum, configure, datasets, export_training


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    external = root / 'external-recordings.jsonl'
    event = {'level': 'L3.5', 'annotation_id': 'one', 'start': 1.123456789, 'end': 4.87654321,
             'caption': 'The right hand holds a cup.', 'split': 'val', 'selection_status': 'chosen',
             'source_splits': ['train'], 'discard_reason': '', 'selection_reason': ''}
    events = [event, event | {'annotation_id': 'two', 'start': 3.5, 'end': 5.5},
              event | {'level': 'L2', 'annotation_id': 'sub', 'start': 7.1, 'end': 10.02},
              event | {'annotation_id': 'drop', 'selection_status': 'discarded', 'split': 'train'}]
    row = {'dataset': 'EgoExoLearn', 'episode_uid': 'ego-one', 'split': 'train', 'view': 'ego',
           'video': '/ego.mp4', 'duration_s': 10, 'instruction': '', 'caption': '', 'subtask': '',
           'full_recording': True, 'review_events': events}
    external.write_text('\n'.join(json.dumps(r) for r in [row, row | {'episode_uid': 'excluded', 'review_events': events[-1:]}]) + '\n')
    source = 'data/pantheon/formatted.jsonl'
    manifest = root / source
    manifest.parent.mkdir(parents=True)
    annotation = root / 'native.json'
    annotation.write_text(json.dumps({'task_label': ['Serve drink'],
                                     'tasks': [{'start_s': 1.123, 'end_s': 3.456, 'task': 'Lift cup'},
                                               {'start_s': 2.123, 'end_s': 4.456, 'task': 'Pour drink'}]}))
    manifest.write_text(json.dumps({'dataset': 'openaoe', 'episode_uid': 'pantheon/openaoe/one', 'split': None,
        'source_annotation': str(annotation), 'video': '/pantheon.mp4', 'duration_s': 10,
        'instruction': 'Serve drink', 'caption': '[0.0 - 10.0] [both hands] hold cup', 'subtask': ''}) + '\n')
    sources = [('EgoExoLearn', str(external)), ('', source)]
    originals = {path: (root / path).read_bytes() for _, path in sources}
    history = root / 'history.jsonl'
    history.write_text('')
    configure(root, history, sources)
    ego = Handler.by_uid['EgoExoLearn:val:ego-one']
    pantheon = Handler.by_uid['OpenAoE:unsplit:pantheon/openaoe/one']
    assert len(Handler.by_uid) == 2 and len(ego['atomic']) == 2
    assert pantheon['row']['subtask_source'] == 'Pantheon task segments'
    assert len(pantheon['subtasks']) == 2
    records = []
    for episode in (ego, pantheon):
        state = datasets.caption_state(episode)
        cleaned = datasets.clean_edit(episode, state)
        metadata = {'id': episode['id'], 'source': episode['source'], 'uid': episode['uid'],
                    'editor': 'Fixture reviewer', 'editor_id': 'mock-reviewer', 'version': 1,
                    'time': '2026-10-06T00:00:00+00:00',
                    'source_sha256': checksum(originals[episode['source']]), 'original': state, 'before': state}
        unchanged = datasets.episode(episode['dataset'], episode['source'], episode['row'], [metadata | cleaned])
        assert unchanged['qa'] == 'confirmed'
        for track in ('subtasks', 'atomic'):
            assert [(c['start'], c['end']) for c in unchanged[track]] == [(c['start'], c['end']) for c in episode[track]]
        state['atomic'][0]['text'] = 'Corrected caption' if episode is ego else '[both hands] pour drink'
        if episode is pantheon:
            state['subtasks'][0]['text'] = 'Corrected subtask'
        records.append(metadata | datasets.clean_edit(episode, state))
    for bad in ({'start': 9, 'end': 10.02}, {'start': 0, 'end': float('inf')}, {'start': float('nan'), 'end': 4}):
        state = datasets.caption_state(ego)
        state['atomic'][0] |= bad
        try:
            datasets.clean_edit(ego, state)
            raise AssertionError('Accepted invalid new native timing')
        except ValueError:
            pass
    history.write_text(''.join(json.dumps(r) + '\n' for r in records))
    configure(root, history, sources)
    for record in records:
        current = Handler.by_uid[record['id']]
        assert current['qa'] == 'corrected' and current['version'] == 1
        comparison = datasets.detail(current, [record], record['source_sha256'])
        assert comparison['original'] == record['original']
        assert comparison['history'][0]['captions']['atomic'][0]['text'] == current['atomic'][0]['text']
    output = root / 'training'
    snapshot = export_training(root, history, output, sources)
    for record in records:
        relative = datasets.export_path(record['source'])
        assert not relative.is_absolute() and (output / relative).is_relative_to(output)
        exported = json.loads((output / relative).read_text())
        assert exported['caption'] == record['caption'] and exported['subtask'] == record['subtask']
        assert exported['split'] == Handler.by_uid[record['id']]['split']
        assert exported['before_edit']['caption'] != exported['caption']
        assert snapshot['source_sha256'][record['source']] == record['source_sha256']
        assert (root / record['source']).read_bytes() == originals[record['source']]
    assert (output / 'annotation_edits.jsonl').read_bytes() == history.read_bytes()
    for name, digest in snapshot['file_sha256'].items():
        assert checksum((output / name).read_bytes()) == digest
    try:
        datasets.export_path('../escape.jsonl')
        raise AssertionError('Accepted export path traversal')
    except ValueError:
        pass
print('PASS: chosen ego subset; native gaps/overlaps/precision; unsafe timing rejection; Pantheon subtasks; correction replay/before-after; contained export and unchanged sources.')
