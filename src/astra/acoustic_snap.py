"""Explicit acoustic snap suggestions, never edits events or applies a map.

The quarter-beat window / Gaussian / argmax kernel follows statically verified
AUFTAKT KnownDownbeat code in the audited Cubase 16 build. This is not its neural
model or complete decoder. Beat This adaptation, rejection gates, exact bypass,
and clipping around the original centre are Astra policies; see the evidence doc.
"""
from __future__ import annotations

import math
from typing import Sequence


def _positive(value: float, name: str) -> float:
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return value


def _round_positive(value: float) -> int:
    return math.floor(value + .5)


def suggest_acoustic_snap(
    activations: Sequence,
    seconds: float,
    bpm: float,
    *,
    fps: float = 100.,
    source: str = "beat_this",
    snap: bool = True,
    min_probability: float = .1,
    min_contrast: float = .05,
    bounds: tuple[float, float] | None = None,
) -> dict:
    """Propose one nearby pulse; caller must preview/confirm before applying.

    ``beat_this`` accepts its independent beat-probability vector, not a sum of
    beat and downbeat probabilities. ``auf_takt_three_class`` accepts rows in
    [other beat, downbeat, nonbeat] order. No normalization is performed.
    Bounds are inclusive absolute audio seconds and never stretch the curve.
    ``snap=False`` returns the original exact seconds without reading evidence.
    """
    seconds = float(seconds)
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("seconds must be finite and nonnegative")
    result = dict(accepted=False, reason="disabled", original_seconds=seconds,
                  candidate_seconds=seconds, delta_seconds=0., frame=None,
                  source_probability=None, weighted_score=None,
                  diagnostics={"source": source, "version": 1})
    if not snap:
        return result
    fps = _positive(fps, "fps")
    bpm = _positive(bpm, "bpm")
    if source not in {"beat_this", "auf_takt_three_class"}:
        raise ValueError("unsupported probability source")
    for value, name in ((min_probability, "min_probability"), (min_contrast, "min_contrast")):
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"{name} must be in [0, 1]")
    hop_ms = 1000 / fps
    centre = _round_positive(seconds * fps)
    radius = _round_positive(_round_positive(60000 / bpm) * .25 / hop_ms)
    if radius * 2 + 1 > 4097:
        raise ValueError("snap window exceeds 4097 frames")
    low, high = 0., math.inf
    if bounds is not None:
        low, high = (float(v) for v in bounds)
        if not all(math.isfinite(v) for v in (low, high)) or not 0 <= low < high:
            raise ValueError("bounds must be finite, nonnegative and increasing")
        if not low <= seconds <= high:
            raise ValueError("original seconds is outside bounds")
    start = max(0, centre - radius, math.ceil(low * fps - 1e-9))
    end = min(len(activations) - 1, centre + radius)
    if math.isfinite(high):
        end = min(end, math.floor(high * fps + 1e-9))
    sigma = (2 * radius + 1) / 4
    diag = result["diagnostics"]
    diag.update(centre_frame=centre, radius_frames=radius, sigma_frames=sigma,
                window_frames=[start, end], fps=fps,
                min_probability=min_probability, min_contrast=min_contrast,
                gates="Astra policy, not recovered Cubase thresholds",
                adaptation=("independent Beat This beat probability; not AUFTAKT softmax"
                            if source == "beat_this" else "1 - class2; verified three-class ordering"),
                boundary_policy="clip valid frames without shifting the Gaussian centre")
    if end - start < 1:
        result["reason"] = "insufficient_comparison"
        return result
    probabilities = []
    for index in range(start, end + 1):
        item = activations[index]
        if source == "auf_takt_three_class":
            try:
                row = [float(v) for v in item]
            except (TypeError, ValueError) as exc:
                raise ValueError("three-class source needs probability rows") from exc
            if len(row) != 3 or any(not math.isfinite(v) or not 0 <= v <= 1 for v in row):
                raise ValueError("invalid three-class probabilities")
            if abs(sum(row) - 1.) > 1e-4:
                raise ValueError("three-class row must sum to one; independent channels are not softmax")
            probability = 1. - row[2]
        else:
            probability = float(item)
            if not math.isfinite(probability) or not 0 <= probability <= 1:
                raise ValueError("invalid beat probability")
        probabilities.append(probability)
    contrast = max(probabilities) - min(probabilities)
    diag.update(probability_contrast=contrast, maximum_probability=max(probabilities))
    weighted = [p * math.exp(-.5 * ((index - centre) / sigma) ** 2)
                for index, p in enumerate(probabilities, start)]
    best = max(range(len(weighted)), key=weighted.__getitem__)  # earliest tie, native strict >
    frame = start + best
    diag.update(best_frame=frame, best_probability=probabilities[best],
                best_weighted_score=weighted[best])
    if max(probabilities) < min_probability or probabilities[best] < min_probability:
        result["reason"] = "weak_evidence"
        return result
    if contrast < min_contrast:
        result["reason"] = "flat_evidence"
        return result
    candidate = frame / fps
    result.update(accepted=True, reason="candidate", candidate_seconds=candidate,
                  delta_seconds=candidate - seconds, frame=frame,
                  source_probability=probabilities[best], weighted_score=weighted[best])
    return result
