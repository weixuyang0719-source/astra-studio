"""Compatibility import for private pulse reviews; no automatic adoption."""
from .research.local_pulse_decode import (
    MAX_FRAMES, MAX_INTERVALS, MAX_WORK, decode_boundary_path, decode_evidence,
)

__all__ = ['MAX_FRAMES', 'MAX_INTERVALS', 'MAX_WORK', 'decode_boundary_path', 'decode_evidence']
