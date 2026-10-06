"""Run: python3 -B project1B_internal/tests/caption_format.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datasets_server import datasets


examples = [
    ('  [left hand]  hold cup.   [right hand] lift lid...  ', '[left hand] hold cup | [right hand] lift lid'),
    ('[Left Hands]hold cup .|| [RIGHT HAND]lift lid . | ', '[left hand] hold cup | [right hand] lift lid'),
    ('[both hand] lift cup. .', '[both hands] lift cup'),
    ('[ego] look. [left hand] hold cup.', '[ego] look | [left hand] hold cup'),
    ('[left hand] grasp cup. Lift it. [right hand] hold a 1.5 kg lid.', '[left hand] grasp cup. Lift it | [right hand] hold a 1.5 kg lid'),
    ('[left hand] hold cup [left hand] turn cup [both hands] lower cup.', '[left hand] hold cup | [left hand] turn cup | [both hands] lower cup'),
    ('  Lift   cup.  Then pour 0.5 L. ', 'Lift cup. Then pour 0.5 L'),
    ('.5 kg cup.', '.5 kg cup'),
    ('[object] cup.', '[object] cup'),
    (' .  . ', ''),
]
for raw, expected in examples:
    assert datasets.format_caption(raw) == expected, raw
    assert datasets.format_caption(expected) == expected, expected

for mode in ('canonical', 'legacy', 'native'):
    row = {'episode_uid': mode, 'dataset': 'egoverse', 'split': 'train', 'video': '/fixture.mp4',
           'duration_s': 10, 'instruction': '  Lift   cup... ', 'subtask': '[0.0 - 10.0] Lift cup.',
           'caption': '[0.0 - 10.0] [left hand] hold cup | [right hand] lift lid'}
    if mode == 'legacy':
        row['caption'] = '[0.0 - 10.0] [ego] look | [left hand] hold cup'
    if mode == 'native':
        row['review_events'] = [
            {'level': 'L3.5', 'start': 1.123456789, 'end': 8.987654321,
             'caption': '[left hand] hold cup. [right hand] lift lid.'},
            {'level': 'L2', 'start': 0.123456789, 'end': 9.123456789, 'caption': 'Lift cup.'}]
        for index, event in enumerate(row['review_events']):
            event.update(annotation_id=str(index), split='train', selection_status='chosen',
                         discard_reason='', selection_reason='', source_splits=['train'])
        row['caption'] = '[1.123456789 - 8.987654321] [left hand] hold cup. [right hand] lift lid.'
        row['subtask'] = '[0.123456789 - 9.123456789] Lift cup.'
    episode = datasets.episode('Fixture', 'fixture.jsonl', row)
    original = datasets.caption_state(episode)
    body = datasets.caption_state(episode)
    body['atomic'][0]['text'] = examples[0][0]
    if mode == 'legacy':
        body['atomic'][0]['text'] = ' [ego] look . [left hand] hold  cup... '
    cleaned = datasets.clean_edit(episode, body)
    assert cleaned['instruction'] == 'Lift cup'
    assert datasets.parse_cues(cleaned['subtask'])[0][2] == 'Lift cup'
    expected = '[ego] look | [left hand] hold cup' if mode == 'legacy' else examples[0][1]
    assert datasets.parse_cues(cleaned['caption'])[0][2] == expected
    assert row['instruction'] == '  Lift   cup... '
    record = cleaned | {'source': 'fixture.jsonl', 'editor': 'Fixture reviewer', 'time': '2026-10-06T00:00:00+00:00',
                        'editor_id': 'mock-reviewer', 'version': 1, 'original': original, 'before': original}
    replayed = datasets.episode('Fixture', 'fixture.jsonl', row, [record])
    assert replayed['qa'] == 'confirmed'
    assert [(c['start'], c['end']) for c in replayed['atomic']] == [(c['start'], c['end']) for c in episode['atomic']]
    comparison = datasets.detail(replayed, [record])
    assert comparison['original'] == original
    assert comparison['history'][0]['before'] == original
    assert comparison['history'][0]['captions']['atomic'][0]['text'] == expected
    # Pre-normalization saves still count as reviewed when only formatting differs.
    legacy_save = record | {key: row[key] for key in datasets.LEVELS}
    assert datasets.episode('Fixture', 'fixture.jsonl', row, [legacy_save])['qa'] == 'confirmed'
    changed_save = record | {'instruction': 'Put cup down'}
    assert datasets.episode('Fixture', 'fixture.jsonl', row, [changed_save])['qa'] == 'corrected'
    body['atomic'][0]['text'] = ' .  . '
    try:
        datasets.clean_edit(episode, body)
    except ValueError as error:
        assert 'empty text' in str(error)
    else:
        raise AssertionError('Punctuation-only caption must be rejected')
print('PASS: separators/whitespace/trailing periods; idempotence; internal punctuation/decimals; canonical/legacy/native saves; timing, original history, and reviewed status preserved.')
