"""Keep canonical XML in source time; PDF projection alone hides silent lead-in."""
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
import xml.etree.ElementTree as ET
from .timing import TempoMap

VERSION = 1
PREFIX_ID = 'astra-source-leading-silence'


def is_source_leading_silence(measure):
    # Match only the synthetic hidden transport measure written below.
    if (measure.get('id') != PREFIX_ID or measure.get('number') != '0'
            or measure.get('implicit') != 'yes'
            or [node.tag for node in measure] != ['attributes', 'note']):
        return False
    rest = measure.find('note')
    if (rest.get('print-object') != 'no' or rest.get('print-spacing') != 'yes'
            or [node.tag for node in rest] != ['rest', 'duration']):
        return False
    try:
        return int(rest.findtext('duration')) > 0 and int(measure.findtext('attributes/divisions')) > 0
    except (TypeError, ValueError):
        return False


def score_opening_meter_rows(reference):
    """Read the native opening conductor projection, never invent a remainder."""
    policy = reference.get('opening_policy', {})
    if type(policy.get('keep_before_bar1')) is not bool:
        return reference['time_signatures']
    from .document_editor import timing_projection
    keys = ('anchors', 'time_signatures', 'pickup_beats', 'first_full_bar_beat',
            'opening_mode', 'opening_policy', 'score_start_seconds')
    layout = {key: reference[key] for key in keys if key in reference}
    rows = timing_projection('opening_meter_sections', {'tempo': layout})
    if not isinstance(rows, list) or not rows:
        raise ValueError('Invalid native opening meter boundaries')
    return rows


def manual_opening_span(clock, start, end, onset, stop):
    """Notation only: omit finished prefix notes, carry held tails into bar 1."""
    mark = clock.data.get('score_start_seconds')
    if mark is None or not clock.data.get('opening_policy', {}).get('score_only'):
        return onset, stop
    if end <= mark + 1e-8:
        return None
    origin = clock.to_beat(mark)
    if start < mark - 1e-8:
        return origin, clock.to_beat(end)
    if onset < origin:
        return origin, max(stop, clock.to_beat(end))
    return onset, stop


def restore_source_origin(xml_path, reference):
    """Prepend an exact silent conductor span to a newly engraved local score.

    reference_bars intentionally begins at the first musical bar for normal
    openings. DuET serializes that score relative to zero. All canonical XML
    consumers require source-absolute beats, including CC, harmony and review.
    The existing print-only projection removes this silent transport measure.
    """
    if reference.get('opening_mode') != 'normal' or not reference.get('score_start_seconds'):
        return {'inserted': False, 'origin_beat': 0.}
    clock = TempoMap(reference)
    origin = Fraction(str(clock.to_beat(reference['score_start_seconds']))).limit_denominator(1000000)
    if origin <= 0:
        return {'inserted': False, 'origin_beat': 0.}
    path = Path(xml_path); tree = ET.parse(path); root = tree.getroot()
    parts = root.findall('part')
    if not parts:
        raise ValueError('Cannot restore score origin without parts')
    if any(p.find("measure[@id='" + PREFIX_ID + "']") is not None for p in parts):
        if not all(p.find("measure[@id='" + PREFIX_ID + "']") is not None for p in parts):
            raise ValueError('Inconsistent source origin across score parts')
        return {'inserted': False, 'origin_beat': float(origin)}
    for part in parts:
        first = part.find('measure')
        if first is None:
            raise ValueError('Cannot restore score origin without measures')
        prefix = ET.Element('measure', {'number':'0', 'implicit':'yes', 'id':PREFIX_ID})
        attrs = deepcopy(first.find('attributes'))
        if attrs is None:
            attrs = ET.Element('attributes')
        div = attrs.find('divisions')
        if div is None:
            div = ET.Element('divisions'); attrs.insert(0, div)
        div.text = str(origin.denominator)
        prefix.append(attrs)
        rest = ET.SubElement(prefix, 'note', {'print-object':'no', 'print-spacing':'yes'})
        ET.SubElement(rest, 'rest')
        ET.SubElement(rest, 'duration').text = str(origin.numerator)
        # The original first musical measure must restore its own divisions.
        original = first.find('attributes')
        if original is None:
            original = ET.Element('attributes'); first.insert(0, original)
        if original.find('divisions') is None:
            ET.SubElement(original, 'divisions').text = '1'
        part.insert(list(part).index(first), prefix)
    tree.write(path, encoding='utf-8', xml_declaration=True)
    return {'inserted': True, 'origin_beat':float(origin), 'origin_seconds':reference['score_start_seconds'],
            'playback_source':'canonical_xml', 'print_policy':'omit_silent_lead_in'}
