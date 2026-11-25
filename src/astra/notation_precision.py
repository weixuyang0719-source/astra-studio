"""Exact clocks for reference-aligned DuET engraving, without vendor edits."""
from contextlib import contextmanager
from copy import copy
from dataclasses import replace
from fractions import Fraction as F
from importlib import import_module
from math import lcm


def engraving_divisions(plan, ctx):
    """Represent every event, bar and direction exactly before XML is written."""
    values = [ctx.pickup]
    for m in range(ctx.nbars):
        values.extend((ctx.bars.start(m), ctx.bars.end(m), ctx.bars.length(m)))
    for events in plan.EV.values():
        for event in events:
            values.extend((event['on'], event['end']))
    for directions in plan.dirs.td.values():
        values.extend(item[0] for item in directions)
    divisions = lcm(24, getattr(plan, 'divisions', 24),
                    *(F(value).denominator for value in values))
    if divisions > 2 ** 30:
        raise ValueError('Notation clock requires an unsupported tick resolution')
    return divisions


def sanitize_reference(notes, ctx, origin):
    """Keep zero, exact reference-grid positions and true fractional bar lines."""
    def snap(value):
        if value == 0:
            return value
        phase = value - origin
        if (phase * 8).denominator == 1 or (phase * 12).denominator == 1:
            return value
        base = ctx.bars.start(ctx.bar_of(value)) if ctx.bars else origin
        relative = value - base
        a, b = F(round(relative * 8), 8), F(round(relative * 12), 12)
        return base + (a if abs(a - relative) <= abs(b - relative) else b)

    changed = 0
    for note in notes:
        onset = snap(note['on'])
        if note['grace']:
            note['on'] = onset
            continue
        duration = note['dur']
        legal_duration = duration > 0 and (
            (duration * 24).denominator == 1 or (duration * 16).denominator == 1)
        if onset == note['on'] and legal_duration:
            continue  # Especially a real zero-time attack crossing the lead-in.
        end = snap(note['on'] + duration)
        changed += onset != note['on'] or end != note['on'] + duration
        note['on'] = onset
        note['dur'] = max(end - onset, F(1, 12))
    if changed:
        ctx.log(f'sanitize: {changed} notes aligned to the reference bar grid')


@contextmanager
def reference_rhythm(upstream, grid_origin):
    """Scope native zero-phase heuristics to the reference clock's phase.

    Public positions, revision identities, MIDI and reports remain absolute.
    Only the native voice assignment sees a temporary relative coordinate system.
    """
    origin = F(str(grid_origin)).limit_denominator(480)
    if abs(float(origin) - float(grid_origin)) > 1e-7:
        raise ValueError('Reference grid origin is not on a supported rational grid')
    audit = {'grid_origin': float(origin), 'phase_aware': origin.denominator != 1,
             'quantization_divisions': lcm(24, origin.denominator)}
    if origin.denominator == 1:
        yield audit
        return

    quantizer = import_module('duet.readable.quantize')
    from duet.readable.context import Bars
    original_fraction = quantizer.to_frac
    original_quantize = upstream.quantize
    original_sanitize = upstream.sanitize
    original_assign = upstream.assign

    def exact_fraction(value, den=24):
        resolution = lcm(den, origin.denominator)
        return F(round(value * resolution), resolution)

    def quantize(inp, grid, shift, ctx):
        # These optional heuristics assume phase zero. The onsets have already
        # been aligned to an explicit reference grid before entering this worker.
        original_config = ctx.cfg
        ctx.cfg = replace(original_config, compound_snap=False, triplet_context=False,
                          swing_min_notes=len(inp.perf) + 1)
        try:
            return original_quantize(inp, grid, shift, ctx)
        finally:
            ctx.cfg = original_config

    def assign(notes, staff, ctx):
        relative = copy(ctx)
        relative.bars = Bars([start - origin for start in ctx.bars.starts],
                             ctx.bars.sigs, ctx.bars.lens)
        relative.cross_staff_conflicts = []
        saved = [(note, note['on']) for note in {id(n): n for n in notes}.values()]
        try:
            for note, onset in saved:
                note['on'] = onset - origin
            return original_assign(notes, staff, relative)
        finally:
            for note, onset in saved:
                note['on'] = onset
            conflicts = relative.cross_staff_conflicts
            if conflicts:
                if not hasattr(ctx, 'cross_staff_conflicts'):
                    ctx.cross_staff_conflicts = []
                ctx.cross_staff_conflicts.extend(
                    dict(item, on=item['on'] + origin) for item in conflicts)

    quantizer.to_frac = exact_fraction
    upstream.quantize = quantize
    upstream.sanitize = lambda notes, ctx: sanitize_reference(notes, ctx, origin)
    upstream.assign = assign
    try:
        yield audit
    finally:
        quantizer.to_frac = original_fraction
        upstream.quantize = original_quantize
        upstream.sanitize = original_sanitize
        upstream.assign = original_assign
