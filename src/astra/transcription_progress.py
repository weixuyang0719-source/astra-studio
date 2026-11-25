"""Observe completed upstream TransKun windows without replacing inference."""
from contextlib import contextmanager
import json
import math
import time


@contextmanager
def transkun_progress(model, samples, segment, hop, emit=print, clock=time.monotonic):
    # Pinned TransKun 2.x transcribe() pads both ends, rounds the step to its
    # feature hop, and calls transcribeFrames exactly once per resulting window.
    fs, frame_hop = model.fs, model.hopSize
    padding = math.ceil((segment-hop)*fs)
    step = math.ceil(hop*fs/frame_hop)*frame_hop
    total = math.ceil((samples+2*padding)/step)
    original = model.transcribeFrames
    owned = 'transcribeFrames' in vars(model)
    started = clock(); completed = 0
    def progress():
        emit(json.dumps({'completed_windows':completed,'total_windows':total,
                         'elapsed_seconds':max(0.,clock()-started)},allow_nan=False))
    def observed(*args, **kwargs):
        nonlocal completed
        result = original(*args, **kwargs)
        completed += 1
        # An unexpected upstream call count must not report >100% or invent ETA.
        if completed <= total:
            progress()
        return result
    model.transcribeFrames = observed
    try:
        progress()
        yield
    finally:
        if owned:
            model.transcribeFrames = original
        else:
            delattr(model,'transcribeFrames')
