"""Compatibility import for archived SMT audits; not a product tempo source."""
from .research.cubase_tempo import PPQ, import_cubase_tempo

__all__ = ['PPQ', 'import_cubase_tempo']
