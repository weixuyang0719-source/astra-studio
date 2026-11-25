"""Export explicit performance-note omissions without retiming other events."""
from collections import defaultdict, deque
import hashlib
import io
import math
import os
from pathlib import Path
import tempfile

import mido


def _performance_events(mid):
    """Match DuET's tick/track/order note-on IDs and track-local FIFO pairs.

    Unlike inference's permissive cleanup, editing refuses malformed notes:
    otherwise retained-note IDs could differ from note-on event IDs.
    """
    if mid.type == 2 or mid.ticks_per_beat <= 0:
        raise ValueError('Only synchronous type 0/1 MIDI with positive PPQ is supported')
    events=[]
    for track_id,track in enumerate(mid.tracks):
        tick=0
        for order,message in enumerate(track):
            if type(message.time) is not int or message.time < 0:
                raise ValueError('MIDI delta ticks must be nonnegative integers')
            if message.type == 'end_of_track' and order != len(track)-1:
                raise ValueError('MIDI end_of_track must be the final track event')
            tick+=message.time
            events.append((tick,track_id,order,message))
    events.sort(key=lambda item:item[:3])
    notes=[];active=defaultdict(deque)
    tempo=500000;previous_tick=0;seconds=0.;tempo_at_tick={}
    for tick,track_id,order,message in events:
        seconds+=mido.tick2second(tick-previous_tick,mid.ticks_per_beat,tempo)
        previous_tick=tick
        if message.type == 'set_tempo':
            if message.tempo <= 0 or (tick in tempo_at_tick and tempo_at_tick[tick] != message.tempo):
                raise ValueError(f'Invalid or conflicting MIDI tempo at tick {tick}')
            tempo_at_tick[tick]=tempo=message.tempo
        elif message.type in ('note_on','note_off'):
            key=(track_id,message.channel,message.note)
            if message.type == 'note_on' and message.velocity > 0:
                note_id=len(notes)
                notes.append({'input_id':note_id,'pitch':message.note,'raw_start_seconds':seconds,
                    'raw_end_seconds':None,'on_tick':tick,'off_tick':None,'track':track_id,
                    'channel':message.channel,'on_event':(track_id,order),'off_event':None})
                active[key].append(note_id)
            else:
                if not active[key]:
                    raise ValueError('Unmatched MIDI note-off; input IDs cannot be safely edited')
                note=notes[active[key].popleft()]
                if seconds <= note['raw_start_seconds']:
                    raise ValueError('Nonpositive MIDI note duration; input IDs cannot be safely edited')
                note.update(raw_end_seconds=seconds,off_tick=tick,off_event=(track_id,order))
        elif message.type == 'control_change' and message.control in (120,123):
            if any(queue for key,queue in active.items() if key[1] == message.channel):
                raise ValueError('All-notes-off with active notes is ambiguous; explicit note-offs are required')
    if any(active.values()):
        raise ValueError('Unclosed MIDI note-on; input IDs cannot be safely edited')
    if not notes:
        raise ValueError('MIDI contains no valid notes')
    return notes


def write_reviewed_midi(source, target, revisions):
    """Remove only identity-bound ``action='omit'`` note-on/off pairs.

    Duration revisions (missing action, or ``action='shorten'``) are validated
    but do not edit performance MIDI. A zero-omission result has ``output=None``
    and creates no output. Validation/serialization failures leave target intact.
    """
    source=Path(source).resolve(strict=True)
    target=Path(target).resolve()
    if source == target or (target.exists() and os.path.samefile(source,target)):
        raise ValueError('Reviewed MIDI target must not be the source file')
    raw=source.read_bytes();digest=hashlib.sha256(raw).hexdigest()
    if not isinstance(revisions,dict) or revisions.get('input_sha256') != digest:
        raise ValueError('Reviewed MIDI input SHA256 mismatch')
    changes=revisions.get('notes')
    if not isinstance(changes,list):
        raise ValueError('Reviewed MIDI revisions require a notes list')
    mid=mido.MidiFile(file=io.BytesIO(raw))
    notes=_performance_events(mid)
    seen=set();omitted=[];duration_only=[]
    for change in changes:
        if not isinstance(change,dict):
            raise ValueError('Invalid reviewed MIDI note revision')
        note_id=change.get('input_id')
        if type(note_id) is not int or not 0 <= note_id < len(notes) or note_id in seen:
            raise ValueError('Invalid or duplicate reviewed MIDI input ID')
        seen.add(note_id);note=notes[note_id]
        if type(change.get('pitch')) is not int or change['pitch'] != note['pitch']:
            raise ValueError(f'Reviewed MIDI pitch mismatch for input {note_id}')
        for field in ('raw_start_seconds','raw_end_seconds'):
            value=change.get(field)
            if (type(value) not in (int,float) or not math.isfinite(value)
                    or abs(value-note[field]) > 1e-6):
                raise ValueError(f'Reviewed MIDI timestamp mismatch for input {note_id}')
        action=change.get('action')
        if action == 'omit':
            omitted.append(note)
        elif action in (None,'duration','shorten'):
            duration_only.append(note_id)
        else:
            raise ValueError(f'Unsupported reviewed MIDI action: {action}')
    omitted.sort(key=lambda note:note['input_id'])
    audit={'source':str(source),'source_sha256':digest,'output':None,
           'removed_note_count':len(omitted),'removed_input_ids':[n['input_id'] for n in omitted],
           'removed_event_count':2*len(omitted),'input_note_count':len(notes),
           'output_note_count':len(notes)-len(omitted),'duration_only_input_ids':sorted(duration_only),
           'ticks_per_beat':mid.ticks_per_beat,'midi_type':mid.type,'track_count':len(mid.tracks),
           'source_unchanged':True,
           'removed_notes':[{k:v for k,v in n.items() if k not in ('on_event','off_event')} for n in omitted]}
    if not omitted:
        return audit
    removed={location for n in omitted for location in (n['on_event'],n['off_event'])}
    for track_id,track in enumerate(mid.tracks):
        kept=mido.MidiTrack();absolute=previous=0
        for order,message in enumerate(track):
            absolute+=message.time
            if (track_id,order) in removed:
                continue
            kept.append(message.copy(time=absolute-previous));previous=absolute
        mid.tracks[track_id]=kept
    buffer=io.BytesIO();mid.save(file=buffer);payload=buffer.getvalue()
    if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
        raise ValueError('Source MIDI changed during reviewed export')
    # Only now create an output; replacement is atomic and a failed write never
    # truncates an existing target. Source bytes are never opened for writing.
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent,prefix='.reviewed-midi-',suffix='.tmp',delete=False) as handle:
            temporary=Path(handle.name)
            handle.write(payload);handle.flush();os.fsync(handle.fileno())
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    audit.update(output=str(target),output_sha256=hashlib.sha256(payload).hexdigest())
    return audit
