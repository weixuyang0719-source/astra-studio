"""MusicXML creation and local vector PDF export."""

import json
from bisect import bisect_right
from collections import defaultdict
from fractions import Fraction
from heapq import heappop, heappush
from pathlib import Path

from music21 import chord, clef, defaults, duration, instrument as instruments, interval, layout
from music21 import expressions, metadata, meter, note, stream, tempo


def _beat(value):
    # Restore small rational values such as 1/3 after a float/JSON round trip.
    return Fraction(value).limit_denominator(480)


def _spell_mixed_tuplets(container):
    events = list(container.notesAndRests)
    if not any(t.tupletMultiplier() != Fraction(2, 3) for e in events for t in e.duration.tuplets):
        return False

    # A duration between straight and triplet grid points can infer ratios such
    # as 12:7. Express those spans in complete 3:2 groups within quarter beats.
    end = _beat(container.highestTime)
    boundaries = [Fraction(i) for i in range(int(end) + 1)]
    if boundaries[-1] < end:
        start = boundaries[-1]
        for component in duration.Duration(end - start).components:
            start += _beat(component.quarterLength)
            boundaries.append(start)
    spans_by_event = [(_beat(container.elementOffset(event)), event) for event in events]
    mixed_groups = {
        bisect_right(boundaries, boundary) - 1
        for offset, event in spans_by_event
        for boundary in (offset, offset + _beat(event.quarterLength))
        if boundary.denominator % 3 == 0
    }
    transitions = sorted({boundaries[i] for index in mixed_groups for i in (index, index + 1)})
    groups = defaultdict(list)
    for start, event in spans_by_event:
        stop = start + _beat(event.quarterLength)
        cuts = [start, *(b for b in transitions if start < b < stop), stop]
        spans = [right - left for left, right in zip(cuts, cuts[1:])]
        container.remove(event)
        pieces = event.splitByQuarterLengths(spans) if len(spans) > 1 else (event,)
        for offset, piece in zip(cuts, pieces):
            groups[bisect_right(boundaries, offset) - 1].append((offset, piece))

    for index, group in groups.items():
        if index not in mixed_groups:
            for offset, event in group:
                container.insert(offset, event)
            continue
        normal = duration.Duration((boundaries[index + 1] - boundaries[index]) / 2).components[0]
        written = []
        for offset, event in group:
            components = duration.Duration(_beat(event.quarterLength) * Fraction(3, 2)).components
            spans = [_beat(c.quarterLength) * Fraction(2, 3) for c in components]
            pieces = event.splitByQuarterLengths(spans) if len(spans) > 1 else (event,)
            for component, piece, span in zip(components, pieces, spans):
                tuplet = duration.Tuplet(3, 2, normal, normal)
                piece.duration = duration.Duration(components=(component,))
                piece.duration.appendTuplet(tuplet)
                if not piece.isRest:
                    piece.beams.beamsList.clear()
                written.append(tuplet)
                container.insert(offset, piece)
                offset += span
        if len(written) == 1:
            written[0].type = "startStop"
        else:
            written[0].type = "start"
            written[-1].type = "stop"
    return True


def _visible_tempo_indices(marks, starts, end, automatic=False):
    """Separate engraving labels from the complete, unmodified playback map.

    Beat trackers report each beat independently. The weighted median over the
    next two 4/4 bars rejects their isolated short/long intervals for *printing*
    only. User-supplied tempo points keep the existing, finer label threshold.
    """
    visible = set()
    last_bpm = None
    last_measure = None
    for index, (offset, bpm, lead_in) in enumerate(marks):
        if lead_in:
            continue
        measure_index = bisect_right(starts, offset) - 1
        if automatic:
            limit = min(end, offset + 8)
            window = []
            for j in range(index, len(marks)):
                start, value, _ = marks[j]
                if start >= limit:
                    break
                stop = min(limit, marks[j + 1][0] if j + 1 < len(marks) else end)
                if stop > start:
                    window.append((value, float(stop - start)))
            halfway = sum(weight for _, weight in window) / 2
            accumulated = 0
            representative = bpm
            for value, weight in sorted(window):
                accumulated += weight
                if accumulated >= halfway:
                    representative = value
                    break
            if abs(bpm - representative) > max(3, representative * .04):
                continue
            if measure_index == last_measure:
                continue
        threshold = max(8, last_bpm * .1) if automatic and last_bpm else 3
        if last_bpm is None or abs(bpm - last_bpm) >= threshold:
            visible.add(index)
            last_bpm = bpm
            last_measure = measure_index
    return visible


def _staff(part, notes, signatures, end, staff_clef, staff_instrument, marks, padding,
           automatic_tempo=False):
    part.insert(0, staff_instrument)
    part.insert(0, staff_clef)
    for offset, (numerator, denominator) in sorted(signatures.items()):
        part.insert(offset + padding if offset else 0, meter.TimeSignature(f"{numerator}/{denominator}"))
    by_span = defaultdict(dict)
    for start, stop, pitch, velocity in notes:
        pitches = by_span[start, stop]
        pitches[pitch] = max(velocity, pitches.get(pitch, 0))
    ends = []
    reduce_voices = False
    for (start, stop), pitches in sorted(by_span.items()):
        while ends and ends[0] <= start:
            heappop(ends)
        heappush(ends, stop)
        reduce_voices |= len(ends) > 4
        tones = [note.Note(pitch) for pitch in sorted(pitches)]
        for tone in tones:
            tone.volume.velocity = pitches[tone.pitch.midi]
        event = chord.Chord(tones) if len(tones) > 1 else tones[0]
        event.quarterLength = stop - start
        part.insert(start + padding, event)

    if reduce_voices:
        # MuseScore keeps at most four voices per staff. Chordify only this staff
        # at note boundaries, with individual ties for pitches held across changes.
        reduction = part.notes.stream().chordify(addTies=True, toSoundingPitch=False)
        part.removeByClass((note.Note, chord.Chord))
        for event in reduction.notes:
            part.insert(reduction.elementOffset(event), event)

    # Establish identical measure boundaries even if a piano hand is silent.
    if padding:
        # Let music21 split ties and beam the pickup at its written beat positions.
        # Keep the virtual beginning of that first bar free of generated rests.
        part.makeVoices(inPlace=True, fillGaps=False)
        part.makeMeasures(inPlace=True, refStreamOrTimeRange=(0, end + padding))
    part.makeNotation(inPlace=True, refStreamOrTimeRange=(0, end + padding))
    measures = list(part.getElementsByClass(stream.Measure))
    if padding:
        pickup = measures[0]
        for container in (pickup, *pickup.voices):
            for event in list(container.notesAndRests):
                container.setElementOffset(event, container.elementOffset(event) - padding)
        pickup.paddingLeft = padding
        pickup.showNumber = stream.enums.ShowNumber.NEVER
        for measure in measures:
            measure.number -= 1
        for measure in measures[1:]:
            part.setElementOffset(measure, part.elementOffset(measure) - padding)
    for measure in measures:
        measure.makeRests(fillGaps=True, timeRangeFromBarDuration=True, inPlace=True)
        # Generated overlap voices are a timing representation, not independent
        # musical lines. Their filler rests otherwise stack over one another and
        # over sounding notes. Keep one staff's rest carrier visible; retain the
        # auxiliary rests in MusicXML so voice cursors and playback do not move.
        for voice in list(measure.voices)[1:]:
            for rest in voice.recurse().getElementsByClass(note.Rest):
                rest.style.hideObjectOnPrint = True
    for measure in measures:
        for container in (measure, *measure.voices):
            if not _spell_mixed_tuplets(container):
                stream.makeNotation.makeTupletBrackets(container, inPlace=True)
    part.streamStatus.tuplets = True
    starts = [part.elementOffset(measure) for measure in measures]
    visible_tempos = _visible_tempo_indices(marks, starts, end, automatic_tempo)
    for mark_index, (offset, bpm, lead_in) in enumerate(marks):
        index = bisect_right(starts, offset) - 1
        show_number = mark_index in visible_tempos
        # MuseScore can merge a visible metronome with a neighboring hidden
        # tracker tempo and lose the label. Keep automatic tracker playback and
        # sparse printed text separate; manual/fitted maps retain normal marks.
        mark = tempo.MetronomeMark(number=bpm, numberImplicit=automatic_tempo or not show_number)
        measures[index].insert(offset - starts[index], mark)
        if automatic_tempo and show_number:
            label = expressions.TextExpression(f"\u2669 = {bpm:g}")
            label.placement = "above"
            label.style.fontWeight = "bold"
            measures[index].insert(offset - starts[index], label)
    return reduce_voices


def _tempo_marks(tempo_map, end):
    anchors = tempo_map.get("anchors", [])
    hide_lead_in = tempo_map.get("lead_in_alignment") is True
    previous_bpm = None
    for index, (left, right) in enumerate(zip(anchors, anchors[1:])):
        start = _beat(left["beat"])
        beats = right["beat"] - left["beat"]
        seconds = right["seconds"] - left["seconds"]
        if beats > 0 and seconds > 0 and start < end:
            bpm = round(float(beats) * 60 / seconds, 2)
            if bpm > 0 and bpm != previous_bpm:
                lead_in = index == 0 and hide_lead_in
                yield start, bpm, lead_in
                if not lead_in:
                    previous_bpm = bpm


def _number_voices(*parts):
    # music21's generated voices start at 0. MuseScore expects positive IDs
    # and uses those IDs to identify voices across the staves of a part.
    first_voice = 1
    for part in parts:
        last_voice = first_voice
        for measure in part.getElementsByClass(stream.Measure):
            if len(parts) > 1 and not measure.hasVoices():
                voice = stream.Voice(id=0)
                for event in list(measure.notesAndRests):
                    voice.insert(measure.elementOffset(event), event)
                    measure.remove(event)
                measure.insert(0, voice)
            for index, voice in enumerate(measure.voices):
                voice_id = voice.id if voice.id < defaults.minIdNumberToConsiderMemoryLocation else index
                voice.id = first_voice + voice_id
                last_voice = max(last_voice, voice.id)
        first_voice = last_voice + 1


def export_score(
    notes: list[dict],
    tempo_map: dict,
    instrument: str,
    output_dir: Path,
    log=print,
    harmony_options: dict | None = None,
) -> dict:
    """Export quantized notes and return the generated paths and PDF warnings."""
    if instrument not in {"piano", "guitar"}:
        raise ValueError(f"不支持的制谱乐器 {instrument!r}")
    events = []
    for event in notes:
        start = _beat(event["start_beat"])
        stop = _beat(event["end_beat"])
        if start < 0 or stop <= start:
            raise ValueError(f"无效的量化音符时间 {start}–{stop}")
        events.append((start, stop, int(event["pitch"]), int(event["velocity"])))

    end = max((event[1] for event in events), default=Fraction(1))
    signatures = {Fraction(0): (4, 4)}
    for signature in tempo_map.get("time_signatures", []):
        offset = _beat(signature["beat"])
        if 0 <= offset < end:
            signatures[offset] = (signature["numerator"], signature["denominator"])
    first_numerator, first_denominator = signatures[0]
    first_bar = Fraction(4 * first_numerator, first_denominator)
    pickup = _beat(tempo_map.get("pickup_beats", 0))
    if not 0 <= pickup < first_bar:
        raise ValueError("弱起长度必须非负且短于首个拍号的小节长度")
    padding = first_bar - pickup if pickup else Fraction(0)
    actual_signatures = {Fraction(0): signatures[0]}
    previous_start = pickup
    previous_meter = signatures[0]
    for offset, signature in sorted(signatures.items()):
        if not offset or signature == previous_meter:
            continue
        numerator, denominator = previous_meter
        if (offset - previous_start) % Fraction(4 * numerator, denominator):
            raise ValueError(f"第 {offset} 拍的拍号变更不在小节线上，无法按原拍位制谱")
        actual_signatures[offset] = signature
        previous_start, previous_meter = offset, signature
    marks = list(_tempo_marks(tempo_map, end))
    automatic_tempo = tempo_map.get("source") == "beat_this"

    score = stream.Score()
    score.metadata = metadata.Metadata(
        title=f"{instrument.capitalize()} transcription", composer="Astra - automatic draft"
    )
    reduced_staves = []
    if instrument == "piano":
        right = stream.PartStaff(id="PianoRight")
        left = stream.PartStaff(id="PianoLeft")
        # Middle C separates hands. The split stays fixed for held notes.
        if _staff(right, [e for e in events if e[2] >= 60], actual_signatures, end,
                  clef.TrebleClef(), instruments.Piano(), marks, padding, automatic_tempo):
            reduced_staves.append("钢琴右手")
        if _staff(left, [e for e in events if e[2] < 60], actual_signatures, end,
                  clef.BassClef(), instruments.Piano(), (), padding):
            reduced_staves.append("钢琴左手")
        _number_voices(right, left)
        score.insert(0, right)
        score.insert(0, left)
        score.insert(0, layout.StaffGroup(right, left, name="Piano", symbol="brace"))
    else:
        guitar = stream.Part(id="Guitar")
        guitar.atSoundingPitch = True
        guitar_instrument = instruments.AcousticGuitar()
        guitar_instrument.transposition = interval.Interval("P-8")
        if _staff(guitar, events, actual_signatures, end, clef.TrebleClef(), guitar_instrument, marks, padding, automatic_tempo):
            reduced_staves.append("吉他")
        _number_voices(guitar)
        score.insert(0, guitar)

    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    musicxml = output_dir / f"{instrument}.musicxml"
    pdf = output_dir / f"{instrument}.pdf"
    score.write("musicxml", fp=musicxml)

    # Annotate before PDF engraving so the shared switch has the same meaning
    # for the legacy engine, MusicXML, and MIDI marker exports.
    harmony_file = None
    if harmony_options is not None and events:
        from .harmony import analyze_harmony, apply_key_confirmation
        from .notation_readability import sounding_signature
        annotated = output_dir / f"{instrument}.harmony-pending.musicxml"
        original_signature = sounding_signature(musicxml)
        audio_harmony = harmony_options.get('_audio_harmony')
        report = analyze_harmony(musicxml, output_path=annotated, write_modes=True,
                                 write_chords=harmony_options.get('show_chords', False) and audio_harmony is None)
        if audio_harmony is not None:
            from .harmony_export import apply_prediction
            apply_prediction(report,audio_harmony,tempo_map,musicxml=annotated,
                             show_chords=harmony_options.get('show_chords',False))
        if sounding_signature(annotated) != original_signature:
            raise RuntimeError('和声标注改变了音符时间，已停止导出')
        annotated.replace(musicxml)
        report['musicxml_path'] = str(musicxml)
        apply_key_confirmation(report, harmony_options.get('confirmed_key'))
        harmony_file = output_dir / f"{instrument}_harmony.json"
        harmony_file.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')

    warnings = []
    if reduced_staves:
        warnings.append(f"{'、'.join(reduced_staves)}超过四个重叠声部，已用连线和弦保留各音起止。")
    from .osmd_pdf import export_pdf_result
    render = export_pdf_result(musicxml, pdf, warnings)
    for warning in warnings:
        log(warning)
    result = {"musicxml": str(musicxml), "warnings": warnings, **render}
    if harmony_file is not None:
        result['harmony'] = str(harmony_file)
    return result
