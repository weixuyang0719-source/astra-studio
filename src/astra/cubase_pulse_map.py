"""Pure projection of reconstructed pulse candidates, never an audio decoder.

No pulse repair, smoothing, normalization to constant tempo, note retiming,
madmom, project publication, or inference that a detected downbeat is surely
the first complete musical bar. Production must explicitly choose the prefix
policy and retain the provisional phase provenance.
"""
from copy import deepcopy
from fractions import Fraction
import math
import re

from .timing import TempoMap
from .boundary_meter import boundary_time_signature

MAPPING_VERSION = 1
NOTATIONAL_PREFIX_DIVISIONS = 32  # quarter units; matches current SMF meter cap 128


def _finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{name} must be a finite number')
    return float(value)


def build_pulse_candidate_map(result, *, audio_duration, audio_sha256,
                              original_source_sha256=None, prefix_policy='exact',
                              opening_phase_policy='provisional_first_downbeat',
                              quarters_per_pulse=1.):
    """Return ``{tempo, report}`` without changing result or any source event.

    ``exact`` extends the first measured interval to transport zero.
    ``notational_prefix`` changes only the quarter-coordinate origin to the
    nearest positive 1/32 quarter (zero stays zero), making short MIDI pickup
    geometry representable with the existing maximum denominator of 128.
    Every observed pulse second and every consecutive one-quarter span remains
    intact. This policy is Astra transport/notation projection, not a native
    decision about musical intro length.
    """
    if result.get('decoder') != 'cubase_dynamic_hmm_reconstruction' or result.get('version') != 1:
        raise ValueError('Unsupported pulse candidate identity')
    if prefix_policy not in ('exact', 'notational_prefix'):
        raise ValueError('Unknown prefix policy')
    if opening_phase_policy not in ('provisional_first_downbeat', 'none'):
        raise ValueError('Unknown opening phase policy')
    if isinstance(quarters_per_pulse,bool) or quarters_per_pulse not in (.5,1.,2.):
        raise ValueError('Quarter interpretation must be .5, 1 or 2 per pulse')
    unit=float(quarters_per_pulse);denominator=int(4/unit)
    duration = _finite(audio_duration, 'audio_duration')
    if duration <= 0:
        raise ValueError('Audio duration must be positive')
    for digest in (audio_sha256, original_source_sha256):
        if digest is not None and (not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)):
            raise ValueError('Source identity must be a SHA-256 hex digest')
    if audio_sha256 is None:
        raise ValueError('Decoded original audio SHA-256 required')
    provenance = result.get('observation_provenance', {})
    identities = [result.get('source_sha256'), provenance.get('audio_source_sha256')]
    identities = [value for value in identities if value is not None]
    if not identities or any(value != audio_sha256 for value in identities):
        raise ValueError('Pulse observations belong to a different or unidentified audio source')
    if _finite(result.get('clock_origin_seconds', 0), 'clock_origin_seconds') != 0:
        raise ValueError('A cropped candidate requires an explicit local merge, not whole-source projection')
    if result.get('audio_duration') is not None and abs(_finite(result['audio_duration'], 'candidate duration')-duration)>1e-6:
        raise ValueError('Candidate and source audio durations differ')
    pulses = [_finite(v, 'pulse second') for v in result.get('beat_seconds', [])]
    numbers = list(result.get('beat_numbers', [])); meters = list(result.get('meters', []))
    if len(pulses)<2 or len(pulses)>100000 or len(numbers)!=len(pulses) or len(meters)!=len(pulses):
        raise ValueError('At least two pulses with matching phase and meter arrays required')
    if pulses[0]<0 or any(b<=a for a,b in zip(pulses,pulses[1:])):
        raise ValueError('Pulse seconds must be nonnegative and strictly increasing')
    if any(type(m) is not int or not 1<=m<=10 or type(n) is not int or not 1<=n<=m for m,n in zip(meters,numbers)):
        raise ValueError('Invalid pulse meter or beat number')
    for i in range(1,len(pulses)):
        expected = numbers[i-1]+1 if numbers[i-1]<meters[i-1] else 1
        if numbers[i]!=expected or (meters[i]!=meters[i-1] and numbers[i]!=1):
            raise ValueError('Pulse phase sequence is inconsistent; projection will not insert/delete beats')
    down = [p for p,n in zip(pulses,numbers) if n==1]
    reported_down = result.get('downbeat_seconds', [])
    if len(down)!=len(reported_down) or any(abs(a-_finite(b,'downbeat second'))>1e-7 for a,b in zip(down,reported_down)):
        raise ValueError('Downbeat array conflicts with the decoded phase states')
    hop_ms = _finite(result.get('parameters',{}).get('hop_ms',10), 'hop_ms')
    if hop_ms<=0 or pulses[-1]>duration+2*hop_ms/1000+1e-7:
        raise ValueError('Pulse range exceeds the audio/feature flush boundary')

    first_interval=pulses[1]-pulses[0]
    exact_prefix=pulses[0]/first_interval*unit
    prefix=exact_prefix
    if prefix_policy=='notational_prefix' and pulses[0]>0:
        prefix=max(1,math.floor(exact_prefix*NOTATIONAL_PREFIX_DIVISIONS+.5))/NOTATIONAL_PREFIX_DIVISIONS
    anchors=[{'seconds':p,'beat':prefix+i*unit} for i,p in enumerate(pulses)]
    if pulses[0]>0:
        anchors.insert(0,{'seconds':0.,'beat':0.})
    tail=duration-pulses[-1]
    if tail>1e-9:
        anchors.append({'seconds':duration,'beat':prefix+(len(pulses)-1)*unit+tail/(pulses[-1]-pulses[-2])*unit})
    signatures=[{'beat':0.,'numerator':meters[0],'denominator':denominator}]
    for i in range(1,len(pulses)):
        if meters[i]!=meters[i-1]:
            signatures.append({'beat':prefix+i*unit,'numerator':meters[i],'denominator':denominator,
                               'origin':'decoded_meter_state','confirmed':False})
    first_down_index=next((i for i,n in enumerate(numbers) if n==1),None)
    first_down_q=prefix+first_down_index*unit if first_down_index is not None else None
    phase_selected=opening_phase_policy=='provisional_first_downbeat' and first_down_index is not None
    opening_length=meters[0]*unit
    pickup=first_down_q % opening_length if phase_selected else 0.
    if min(abs(pickup),abs(pickup-opening_length))<1e-9:pickup=0.
    notation_blocks=[]
    rational=Fraction(str(prefix)).limit_denominator(480)
    if abs(float(rational)-prefix)>1e-7:
        notation_blocks.append('prefix_phase_not_supported_by_current_notation_precision')
    if not phase_selected:
        notation_blocks.append('opening_phase_not_selected')
    if pickup:
        try:boundary_time_signature(pickup,(denominator,))
        except ValueError:notation_blocks.append('opening_partial_bar_not_representable_in_current_midi_meter')
    tempo={'version':1,'source':'cubase_local_reconstruction','pulse_mapping_version':MAPPING_VERSION,
        'candidate_only':True,'anchors':anchors,'time_signatures':signatures,
        'grid_origin':prefix,'pickup_beats':pickup,'lead_in_alignment':bool(pulses[0]),
        'warnings':[], 'opening_phase_confirmed':False,
        'pulse_source':{'audio_sha256':audio_sha256,'original_source_sha256':original_source_sha256,
            'decoder':result['decoder'],'decoder_version':result['version'],
            'parameters':deepcopy(result.get('parameters',{})),
            'observation_provenance':deepcopy(provenance)}}
    if phase_selected:
        tempo['first_full_bar_beat']=first_down_q
        tempo['opening_phase']={'origin':'decoded_downbeat_provisional','confirmed':False,
            'seconds':down[0],'beat':first_down_q,'pulse_index':first_down_index,
            'semantic_first_full_bar_confirmed':False}
        tempo['warnings'].append('首小节相位暂取解码出的第一个强拍，尚非人工确认的引子终点。')
    clock=TempoMap(tempo)
    for i,p in enumerate(pulses):
        if abs(clock.to_seconds(prefix+i*unit)-p)>1e-9:
            raise ValueError('Pulse projection failed exact second preservation')
    report={'mapping_version':MAPPING_VERSION,'prefix_policy':prefix_policy,
        'prefix_resolution_quarters':1/NOTATIONAL_PREFIX_DIVISIONS if prefix_policy=='notational_prefix' else None,
        'exact_extrapolated_prefix_quarters':exact_prefix,'chosen_prefix_quarters':prefix,
        'prefix_coordinate_shift_quarters':prefix-exact_prefix,
        'prefix_interval_seconds':pulses[0],
        'exact_prefix_seconds_per_quarter':first_interval/unit,
        'chosen_prefix_seconds_per_quarter':pulses[0]/prefix if prefix else None,
        'prefix_change_scope':'unobserved transport lead-in only; pulse seconds and interpreted interval slopes unchanged',
        'tail_extrapolated':tail>1e-9,'tail_seconds':max(0.,tail),
        'tail_seconds_per_quarter':(pulses[-1]-pulses[-2])/unit,
        'detected_pulses_after_audio_end':[p for p in pulses if p>duration],
        'pulse_count':len(pulses),'all_pulse_seconds_preserved':True,
        'quarter_span_per_pulse':unit,'pulse_ordinal_quarter_origin':prefix,
        'decoded_bars_per_notated_bar':1,'meter_denominator':denominator,
        'meter_interpretation':'same decoded grouping with explicit quarter value; no automatic bar merging or splitting',
        'first_detected_downbeat_seconds':down[0] if down else None,
        'selected_phase_is_provisional':phase_selected,'semantic_first_full_bar_confirmed':False,
        'notation_ready':not notation_blocks,'notation_blocks':notation_blocks,
        'meter_changes_are_decoder_candidates':True,'source_events_modified':False,
        'automatic_metrical_level_selection_solved':False,'effective_map_published':False}
    tempo['pulse_projection']=deepcopy(report)
    return {'tempo':tempo,'report':report}
