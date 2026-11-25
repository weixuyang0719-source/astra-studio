"""Printed bar labels are separate from DuET review/edit ordinals."""
from pathlib import Path
import json
import re
import xml.etree.ElementTree as ET

from .score_tempo import _measure_layout

VERSION = 2


def apply_measure_numbering(xml_path, reference, review_path=None):
    """Number the first complete bar 1 without changing time or review keys.

    Review ``id``, ``bars`` and edit-option payloads remain upstream ordinals.
    ``display_bars`` and the explicit number map are presentation-only. Harmony
    already uses measure_index for insertion, so new labels cannot shift it.
    """
    path = Path(xml_path)
    tree = ET.parse(path)
    root = tree.getroot()
    layout = _measure_layout(root)
    first = float(reference.get('first_full_bar_beat', reference.get('pickup_beats', 0)) or 0)
    mode = reference.get('opening_mode', 'pickup' if reference.get('pickup_beats') else 'normal')
    from .score_origin import score_opening_meter_rows
    keep = reference.get('opening_policy', {}).get('keep_before_bar1')
    opening_rows = score_opening_meter_rows(reference) if keep is True else ()
    review_file = Path(review_path) if review_path is not None else None
    review = (json.loads(review_file.read_text(encoding='utf-8'))
              if review_file is not None and review_file.exists() else None)
    previous = {(row['part_id'], row['measure_index']): row['source_number']
                for row in (review or {}).get('measure_number_map', [])}
    maps = []
    for part in root.findall('part'):
        rows = [row for row in layout if row[0] is part]
        if not rows:
            continue
        full = next((i for i, row in enumerate(rows) if abs(float(row[2]) - first) < 1e-6), None)
        if full is None:
            raise ValueError('First complete bar does not match an engraved measure boundary')
        for index, (_, measure, start, length, _) in enumerate(rows):
            number = index - full + 1
            original = previous.get((part.get('id'), index), measure.get('number', str(index + 1)))
            measure.set('number', str(number))
            kind = ('pickup' if mode == 'pickup' else 'intro' if mode == 'intro' else 'leading_silence') if index < full else 'bar'
            if index < full and opening_rows:
                conductor = next(r for r in reversed(opening_rows) if r['beat'] <= float(start)+1e-7)
                kind = 'prelude_free' if conductor.get('free') else 'prelude_fixed'
            if kind in ('pickup', 'leading_silence', 'prelude_free', 'prelude_fixed'):
                measure.set('implicit', 'yes')
            maps.append(dict(part_id=part.get('id'), measure_index=index,
                             source_number=original, display_number=number,
                             kind=kind, start_quarters=float(start), length_quarters=float(length)))
    if review is not None:
        first_part = root.find('part')
        labels = {row['source_number']: str(row['display_number']) for row in maps
                  if row['part_id'] == first_part.get('id')}
        for item in review.get('items', []):
            # Signed integer labels may occur on repeated application. Never
            # mutate the source key or options used by upstream edits.
            item['display_bars'] = re.sub(r'\d+', lambda m: labels.get(m.group(), m.group()), str(item.get('bars', '')))
        review['measure_number_map'] = maps
        review['measure_numbering_version'] = VERSION
    tree.write(path, encoding='utf-8', xml_declaration=True)
    if review is not None:
        review_file.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding='utf-8')
    return dict(version=VERSION, first_complete_bar_beat=first, opening_mode=mode, measures=maps)
