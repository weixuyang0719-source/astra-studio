"""Optional MIDI marker track; never add accompaniment notes or alter timing."""
from pathlib import Path
import math
import mido

TRACK_NAME = 'Astra Chords'


def write_chord_markers(path, chords, *, enabled=True):
    path = Path(path)
    midi = mido.MidiFile(path)
    if midi.type not in (0, 1) or midi.ticks_per_beat <= 0:
        raise ValueError('Chord markers require a synchronous PPQ MIDI')
    retained=[]
    for track in midi.tracks:
        owned=track.name==TRACK_NAME
        pure=owned and all(msg.type in ('track_name','marker','end_of_track') for msg in track)
        if pure: continue
        cleaned=mido.MidiTrack(); tick=previous=0
        for msg in track:
            tick+=msg.time
            if msg.type=='marker' and (owned or msg.text.startswith('ASTRA_CHORD:')):
                continue
            cleaned.append(msg.copy(time=tick-previous));previous=tick
        retained.append(cleaned)
    midi.tracks=retained
    markers = {}
    if enabled:
        for chord in chords:
            if chord.get('uncertain', True):
                continue
            beat = float(chord['absolute_quarters'])
            if not math.isfinite(beat) or beat < 0:
                raise ValueError('Invalid chord marker position')
            label = str(chord['label']).replace('♯', '#').replace('♭', 'b')
            label.encode('latin1')  # Mido/SMF text convention used by our reader.
            markers[round(beat * midi.ticks_per_beat)] = label
        if markers:
            midi.type=1
            track = mido.MidiTrack([mido.MetaMessage('track_name', name=TRACK_NAME)])
            previous = 0
            for tick, label in sorted(markers.items()):
                track.append(mido.MetaMessage('marker', text=label, time=tick - previous))
                previous = tick
            track.append(mido.MetaMessage('end_of_track'))
            midi.tracks.append(track)
    temporary = path.with_suffix('.markers-pending.mid')
    midi.save(temporary)
    temporary.replace(path)
    return len(markers)


def write_export_chord_markers(export, instrument, chords, tempo_data, *, source_kind=None, enabled=True,
                               variants=('raw','quantized','reviewed','score','performance','target_grid','seconds_120')):
    """Mark all available renditions, mapping immutable performance clocks via seconds."""
    from .midi import import_tempo_midi
    from .timing import TempoMap
    reference=TempoMap(tempo_data)
    counts={}
    for variant in variants:
        path=Path(export)/f'{instrument}_{variant}.mid'
        if not path.is_file(): continue
        mapped=chords
        if enabled and variant=='seconds_120':
            mapped=[dict(c,absolute_quarters=2*reference.to_seconds(c['absolute_quarters']))
                    for c in chords if not c.get('uncertain',True)]
        elif enabled and (variant=='reviewed' or (variant=='raw' and source_kind=='midi')):
            source=TempoMap(import_tempo_midi(path))
            mapped=[dict(c,absolute_quarters=source.to_beat(reference.to_seconds(c['absolute_quarters'])))
                    for c in chords if not c.get('uncertain',True)]
        counts[variant]=write_chord_markers(path,mapped,enabled=enabled)
        # These existing audit fields describe the published bytes, not the
        # immutable input identity. Marker-only changes must not leave stale hashes.
        metadata=Path(export)/f'{instrument}_{variant}_midi.json'
        if metadata.is_file():
            import hashlib
            from .pipeline import read_json, save_json
            audit=read_json(metadata)
            for field in ('midi_sha256','output_sha256'):
                if field in audit:audit[field]=hashlib.sha256(path.read_bytes()).hexdigest()
            updated=mido.MidiFile(path)
            if 'track_count' in audit:audit['track_count']=len(updated.tracks)
            if 'midi_type' in audit:audit['midi_type']=updated.type
            save_json(metadata,audit)
    return counts
