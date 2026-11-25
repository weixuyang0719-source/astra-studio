"""Matched synthetic disk-edit timings; deliberately no Qt or preview worker."""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch

from astra import editing, native_editor
from astra.timing import fixed_tempo


def fixture(project, count, anchor_count):
    duration = (anchor_count - 1) / 4
    tempo = fixed_tempo(120, duration)
    tempo['anchors'] = [dict(seconds=i / 4, beat=i / 2) for i in range(anchor_count)]
    tempo['rhythm_initial'] = dict(available=True, source='benchmark', tempo=deepcopy(tempo))
    notes = [dict(id=f'auto-bench-{i}', origin='transcription', start=(duration - 1) * i / count,
                  end=(duration - 1) * i / count + .2, pitch=45 + i % 40, velocity=80, channel=0,
                  confidence=.85) for i in range(count)]
    selection = dict(start=0., end=min(10., duration), loop=False)
    body = dict(version=1, tempo=tempo, notes=notes, controls=[], other_events=[], anchors=[], duration=duration,
                source_key='bench', source_export=None, deleted_ids=[], range=selection,
                operation=dict(type='initialize'), manual=False)
    with editing.edit_lock(project):
        return editing._publish_revision(project, body, dict(undo=[], redo=[], range=selection))


def run(project, library, native, args):
    total, body_ms, head_ms = [], [], []
    accepts = 0
    save, accept = editing._save, native_editor._Session.accept

    def timed_save(path, data):
        start = perf_counter()
        save(path, data)
        (head_ms if Path(path).name == 'current.json' else body_ms).append(1000 * (perf_counter() - start))

    def counted_accept(self, token):
        nonlocal accepts
        accept(self, token)
        accepts += 1

    with patch.dict(os.environ, {'ASTRA_EDITOR_CORE': '1' if native else '0',
                                  'ASTRA_EDITOR_CORE_LIBRARY': str(library)}):
        doc = fixture(project, args.notes, args.anchors)
        with patch.object(editing, '_save', timed_save), patch.object(native_editor._Session, 'accept', counted_accept):
            for i in range(args.samples + 1):
                t = .125 + (i % 16) / 32
                start = perf_counter()
                doc = editing.edit(project, doc['revision'], dict(type='notes', add=[
                    dict(start=t, end=t + .125, pitch=65, velocity=77, channel=0)]))
                total.append(1000 * (perf_counter() - start))
        assert accepts == (args.samples + 1 if native else 0), 'unexpected fallback or missing acceptance'
    return dict(cold_edit_ms=total[0], cold_body_ms=body_ms[0], cold_head_ms=head_ms[0],
                warm_edit_median_ms=median(total[1:]), warm_body_median_ms=median(body_ms[1:]),
                warm_head_median_ms=median(head_ms[1:]), warm_edit_samples_ms=total[1:], native_accepts=accepts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=Path, required=True)
    parser.add_argument('--notes', type=int, default=3597)
    parser.add_argument('--anchors', type=int, default=919)
    parser.add_argument('--samples', type=int, default=12)
    parser.add_argument('--directory', type=Path, help='parent for temporary projects; select a representative disk')
    args = parser.parse_args()
    args.library = args.library.resolve()
    if args.anchors < 5 or args.notes < 1 or args.samples < 1 or not args.library.is_file():
        parser.error('need an existing library, at least 5 anchors, 1 note, and 1 warm sample')
    with TemporaryDirectory(dir=args.directory) as directory:
        root = Path(directory)
        print(json.dumps({'notes': args.notes, 'anchors': args.anchors, 'samples': args.samples,
                          'python': run(root / 'python', args.library, False, args),
                          'native': run(root / 'native', args.library, True, args)}, indent=2))


if __name__ == '__main__':
    main()
