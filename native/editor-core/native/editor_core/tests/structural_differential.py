"""Compare current Astra Python with the compiled, test-only timing/structural ABI."""
import argparse
from copy import deepcopy
import ctypes
import json
import math
from pathlib import Path
import random

from astra.beat_flags import validate_flag
from astra.boundary_meter import boundary_time_signature
from astra.constant_tempo_preview import preview_constant_tempo, reference_grid_points
from astra.duet_notation import reference_bars
from astra.free_meter import free_ranges
from astra.meter_edit import preview_meter_edit, preview_roll_edit
from astra.rebar_edit import apply_rebar, list_boundaries, _geometry
from astra.score_boundary import confirmed_end_beat, ending_measure
from astra.tempo_edit import apply_tempo_operation
from astra.timing import TempoMap, fixed_tempo, lock_meter, scale_tempo


class Native:
    def __init__(self, library):
        self.library = ctypes.CDLL(str(Path(library).resolve()))
        self.call = self.library.astra_editor_structural_test_call
        self.call.argtypes = [ctypes.c_char_p]
        self.call.restype = ctypes.c_char_p

    def invoke(self, **request):
        response = json.loads(self.call(json.dumps(request, ensure_ascii=False, allow_nan=False).encode('utf-8')))
        if not response['ok']:
            assert response['kind'] == 'invalid_argument', response
            raise ValueError(response['reason'])
        return response['value']

    def query(self, name, **args):
        return self.invoke(entry='query', name=name, args=args)

    def command(self, tempo, operation, *, revision=None, pins=()):
        context = {} if revision is None else {'revision': revision}
        return self.invoke(entry='command', tempo=tempo, operation=operation, context=context, fixed_anchors=list(pins))


def same(native, python, where='$'):
    assert type(native) is type(python), (where, native, python, type(native), type(python))
    if type(python) is dict:
        assert native.keys() == python.keys(), (where, native.keys(), python.keys())
        for k, v in python.items():
            same(native[k], v, f'{where}.{k}')
    elif type(python) in (tuple, list):
        assert len(native) == len(python), (where, native, python)
        for i, (a, b) in enumerate(zip(native, python)):
            same(a, b, f'{where}[{i}]')
    elif type(python) is float:
        assert math.isclose(native, python, rel_tol=2e-14, abs_tol=1e-12), (where, native, python)
        if native == python == 0:
            assert math.copysign(1., native) == math.copysign(1., python), (where, native, python)
    else:
        assert native == python, (where, native, python)


def rejected(call):
    try:
        call()
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return
    raise AssertionError('expected rejection')


def rebar_request(data, left, right, *, revision='版本 😃', **changes):
    rows = list_boundaries(data, revision)
    a, b = rows[left], rows[right]
    return dict(boundary_revision=revision, tempo_sha256=a['tempo_sha256'], left_boundary=a['id'], right_boundary=b['id'],
                start=a['seconds'], end=b['seconds'], numerator=3, denominator=4, bar_count=2) | changes


def exercise(native):
    q = native.query
    for x in [True, False, -.5, .5, -1.5, 2.5, 3.5, 0.0, -0.0, 1e16, float(2**63),
              math.nextafter(2.5, math.inf), math.nextafter(2.5, -math.inf)]:
        same(q('round_even', value=x), round(x))
    for x in [-0.0000000001, 1.0000000005, 2.5000000005, 0.002675, 1e300, 2**53+1]:
        for digits in (6, 9):
            same(q('round_decimal', value=x, digits=digits), round(x, digits))

    plain = fixed_tempo(120, 20)
    variable = fixed_tempo(120, 20)
    variable['anchors'] = [dict(seconds=t, beat=b) for t, b in [(0, 0), (1, 2), (2, 3), (4, 5), (6, 9), (10, 17), (20, 37)]]
    bare = {'anchors': [{'seconds': False, 'beat': '０'}, {'seconds': ' 1_0.0 ', 'beat': 20}], 'pickup_beats': '1e-999'}
    expected = deepcopy(bare)
    TempoMap(expected)
    same(q('validate_tempo', tempo=bare), expected)
    quirky = deepcopy(plain)
    quirky['time_signatures'][0]['denominator'] = 4.0
    same(q('validate_tempo', tempo=quirky), quirky)
    same(q('reference_bars', tempo=quirky), list(reference_bars(TempoMap(quirky), 40.)))
    rejected(lambda: q('boundaries', tempo=quirky, revision='r'))
    rejected(lambda: list_boundaries(quirky, 'r'))
    for bad in ('0x10', '1__0', '1.0 tail', '1e999'):
        altered = deepcopy(plain)
        altered['anchors'][-1]['seconds'] = bad
        rejected(lambda: TempoMap(deepcopy(altered)))
        rejected(lambda: q('validate_tempo', tempo=altered))
    for endpoint in (1, 16777215, .999999, 16777216):
        data = dict(anchors=[dict(seconds=0, beat=0), dict(seconds=endpoint/1e6, beat=1)])
        expected = deepcopy(data)
        try:
            TempoMap(expected)
        except ValueError:
            rejected(lambda: q('validate_tempo', tempo=data))
        else:
            same(q('validate_tempo', tempo=data), expected)

    rng = random.Random(1729)
    for _ in range(24):
        t, b = 0, 0
        data = dict(anchors=[dict(seconds=t, beat=b)], time_signatures=deepcopy(plain['time_signatures']))
        for _ in range(12):
            t += rng.uniform(.1, .9); b += rng.uniform(.2, 1.9)
            data['anchors'].append(dict(seconds=t, beat=b))
        clock = TempoMap(data)
        times = [0., t, t+3] + [a['seconds'] for a in data['anchors']] + [rng.uniform(0, t) for _ in range(40)]
        beats = [clock.to_beat(x) for x in times]
        same(q('to_beats', tempo=data, seconds=times), beats)
        same(q('to_seconds_many', tempo=data, beats=beats), [clock.to_seconds(x) for x in beats])
        same(q('boundaries', tempo=data, revision='r'), list_boundaries(data, 'r'))

    rich = deepcopy(plain)
    rich.update(pickup_beats=.5, first_full_bar_beat=.5)
    rich['future'] = {'安 🎹': ['\x00\x7f\t\\"', -0.0, 1.0, 1e-4, 1e-5, 1e15, 1e16, 1e100, 5e-324, 2**64-1], 'a': {'é': True}}
    same(q('boundaries', tempo=rich, revision='版本 😃'), list_boundaries(rich, '版本 😃'))
    reordered = dict(reversed(list(rich.items())))
    same(q('boundaries', tempo=reordered, revision='版本 😃'), list_boundaries(rich, '版本 😃'))
    picked = deepcopy(plain)
    picked.update(opening_mode='normal', score_start_seconds=2,
                  short_bar_annotations=[dict(start_seconds=4, end_seconds=5)],
                  explicit_partial_measures=[dict(start_seconds=4, end_seconds=5)])
    for data in (plain, rich, picked):
        clock, starts, lengths, meters, last = _geometry(data)
        same(q('geometry', tempo=data), dict(starts=starts, lengths=lengths, meters=meters, last_beat=last,
                                            audio_end_beat=clock.beats[-1], audio_end_seconds=clock.seconds[-1]))
    free = deepcopy(picked)
    free['time_signatures'] = [dict(beat=0, numerator=4, denominator=4), dict(beat=12, numerator=4, denominator=4, free=True),
                               dict(beat=18, numerator=5, denominator=8)]
    same(q('free_ranges', tempo=free), free_ranges(deepcopy(free)))
    same(q('reference_bars', tempo=free, last_beat=42), list(reference_bars(TempoMap(deepcopy(free)), 42)))
    rejected(lambda: q('geometry', tempo=free))
    q('geometry', tempo=free, strict=False)

    pins = [dict(id='retired', seconds=2, beat=999, lock_beat=True)]

    def edit(data, kind, **kwargs):
        saved = deepcopy(data)
        op = dict(type=kind, **kwargs)
        result = native.command(data, dict(type='tempo', action=kind, **kwargs), pins=pins)
        same(result, apply_tempo_operation(data, op, pins))
        same(data, saved)
        return result['tempo']

    def bad_edit(data, kind, **kwargs):
        rejected(lambda: apply_tempo_operation(data, dict(type=kind, **kwargs), pins))
        rejected(lambda: native.command(data, dict(type='tempo', action=kind, **kwargs), pins=pins))

    edit(plain, 'regroup', start=0, end=6, numerator=3, denominator=4, scope=dict(start=0, end=6))
    edit(variable, 'meter', start='0', end='6', numerator=6, denominator=8)
    bad_edit(plain, 'meter', start=0, end=6, numerator=3, scope=dict(start=1, end=6))
    flagged = edit(plain, 'add_meter_event', seconds=2, numerator=3)
    edit(flagged, 'meter_event', seconds=2, numerator=5, denominator=8)
    edit(flagged, 'remove_meter_event', seconds=2)
    bad_edit(plain, 'remove_meter_event', seconds=0)
    bad_edit(plain, 'add_meter_event', seconds=.5, numerator=3)
    for dist in ('uniform', 'preserve'):
        edit(variable, 'recount', start=1, end=4, quarters='6', distribution=dist)
        edit(variable, 'interpret', start=1, end=4, factor='0.5', distribution=dist)
    edit(variable, 'pulse_times', start=1, end=4, beat_seconds=[1+1e-9, 1.9, 3.3, 4-1e-9], analysis={'future': [1, 1.0, False]})
    edit(variable, 'move', seconds=2, to_seconds=2.3)
    inserted = edit(variable, 'insert', seconds=3, beat=4.2)
    edit(inserted, 'delete', seconds=3)
    bad_edit(variable, 'move_point', seconds=2, to_seconds=4.3)
    edit(plain, 'move_beat', seconds=2, to_seconds=2.1, beat=4, scope=dict(start=1.9, end=2.4))
    bad_edit(plain, 'move_beat', seconds=2, to_seconds=2.6, beat=4)
    same(q('validate_flag', tempo=plain, seconds=2, beat=4), validate_flag(plain, 2, 4))
    opening = edit(plain, 'first_bar', seconds=4.5, mode='intro')
    edit(opening, 'first_bar', seconds=.75, mode='pickup')
    ending = edit(plain, 'score_end', seconds=9.25)
    edit(ending, 'clear_score_end')
    bad_edit(plain, 'score_end', seconds=9.251)
    clock = TempoMap(ending)
    same(q('confirmed_end_beat', tempo=ending), confirmed_end_beat(clock))
    same(q('ending_measure', tempo=ending), ending_measure(clock))
    same(q('reference_bars', tempo=ending), list(reference_bars(clock, confirmed_end_beat(clock))))
    same(q('boundaries', tempo=ending, revision='tail'), list_boundaries(ending, 'tail'))
    rejected(lambda: q('reference_bars', tempo=ending, last_beat=24))
    same(q('reference_bars', tempo=ending, last_beat=42, ignore_score_end=True), list(reference_bars(TempoMap(plain), 42)))
    same(q('boundary_time_signature', span_quarters=2, surrounding_denominators=[8]), boundary_time_signature(2, [8]))

    for dist in ('uniform', 'preserve'):
        for data, a, b in ((plain, 2, 4), (variable, 1, 3), (rich, 1, 3), (ending, 1, 3)):
            op = rebar_request(data, a, b, distribution=dist, type='rebar')
            revision = op['boundary_revision']
            changed = native.command(data, op, revision=revision, pins=pins)
            same(changed, apply_rebar(data, op, pins, revision=revision))
            rejected(lambda: native.command(data, op, revision='stale', pins=pins))
            rejected(lambda: native.command(data | {'changed': True}, op, revision=revision, pins=pins))
            rejected(lambda: native.command(data, op | {'start': op['start']+.01}, revision=revision, pins=pins))
            rejected(lambda: native.command(data, op | {'bar_count': 2.0}, revision=revision, pins=pins))
    mixed = deepcopy(plain)
    mixed['time_signatures'].extend([dict(beat=12, numerator=3, denominator=4), dict(beat=18, numerator=4, denominator=4)])
    op = rebar_request(mixed, 2, 5, type='rebar', numerator=5, denominator=8, bar_count=3, replace_changes=True)
    same(native.command(mixed, op, revision=op['boundary_revision']), apply_rebar(mixed, op, revision=op['boundary_revision']))

    for opts in (dict(seconds=0, numerator=3), dict(seconds=2.07, numerator=3), dict(seconds=.5625, previous='pickup'),
                 dict(seconds=1.49, previous=3, numerator=4)):
        same(q('preview_meter_edit', tempo=plain, **opts), preview_meter_edit(plain, **opts))
    for opts in (dict(seconds=2.27, opening=True, numerator=4), dict(seconds=2., opening=False, numerator=2)):
        same(q('preview_roll_edit', tempo=plain, **opts), preview_roll_edit(plain, **opts))
    marks = plain | dict(notation_tempo_marks=[dict(beat=6, label='rit.')], midi_tempo_events=[dict(beat=0)])
    same(q('preview_meter_edit', tempo=marks, seconds=2.07), preview_meter_edit(marks, 2.07))
    for n, bars, phase in ((4, 2, 0.), (4, 3, 0.), (3, 2, 0.), (4, 2, .13)):
        request = rebar_request(plain, 2, 4, numerator=n, bar_count=bars, reference_bpm=120, phase_seconds=phase)
        revision = request['boundary_revision']
        preview = q('preview_constant_tempo', tempo=plain, request=request, revision=revision)
        same(preview, preview_constant_tempo(plain, request, revision=revision, fixed_anchors=pins))
        grid = preview['reference_grid']
        same(q('reference_grid_points', reference_grid=grid, start_seconds=-2, end_seconds=3), reference_grid_points(grid, -2, 3))
        for candidate in preview['candidates']:
            if candidate['applicable']:
                adopted = native.command(plain, {'type': 'rebar', **candidate['operation']}, revision=revision, pins=pins)
                same(adopted['impact'], candidate['impact'])
                same(adopted['summary'], candidate['summary'])
    rejected(lambda: q('reference_grid_points', reference_grid=grid, start_seconds=-2, end_seconds=3, max_points=1))
    same(q('fixed_tempo', bpm='96', duration=12, meter='7/8'), fixed_tempo('96', 12, '7/8'))
    same(q('lock_meter', tempo=rich, meter='6/8'), lock_meter(rich, '6/8'))
    scaled = deepcopy(plain)
    scaled.update(pickup_beats=3, pulse_projection=dict(quarter_span_per_pulse=1.0))
    for f in (.5, 1, 2, 2.0):
        same(q('scale_tempo', tempo=scaled, factor=f), scale_tempo(scaled, f))


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('library', help='path to the compiled structural test ABI shared library')
    exercise(Native(cli.parse_args().library))
