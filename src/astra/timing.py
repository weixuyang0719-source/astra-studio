"""The only conversion boundary between real seconds and quarter-note beats."""
from bisect import bisect_right
import math
from statistics import median

BEAT_MAPPING_VERSION = 5


class TempoMap:
    def __init__(self, data):
        self.data = data
        self.anchors = data['anchors']
        if len(self.anchors) < 2:
            raise ValueError('Tempo Map 至少需要两个锚点')
        self.seconds = [float(a['seconds']) for a in self.anchors]
        self.beats = [float(a['beat']) for a in self.anchors]
        if self.seconds[0] != 0 or self.beats[0] != 0:
            raise ValueError('首个锚点必须是 seconds=0, beat=0，以保留音频起点')
        for values in (self.seconds, self.beats):
            if not all(math.isfinite(v) for v in values) or any(b <= a for a,b in zip(values, values[1:])):
                raise ValueError('锚点必须有限且严格递增')
        for a,b,c,d in zip(self.seconds,self.seconds[1:],self.beats,self.beats[1:]):
            if not 1 <= (b-a)/(d-c)*1e6 <= 16777215:
                raise ValueError('速度超出 MIDI 可表达范围')
        signatures = data.setdefault('time_signatures', [{'beat':0,'numerator':4,'denominator':4}])
        if not signatures or signatures[0]['beat'] != 0:
            raise ValueError('拍号列表必须从 beat=0 开始')
        previous = -1
        for s in signatures:
            if (not math.isfinite(s['beat']) or s['beat'] <= previous or
                type(s['numerator']) is not int or not 1 <= s['numerator'] <= 32 or
                s['denominator'] not in (1,2,4,8,16,32,64,128)):
                raise ValueError('无效拍号或拍号位置')
            previous = s['beat']
        pickup=float(data.get('pickup_beats',0))
        first_length=signatures[0]['numerator']*4/signatures[0]['denominator']
        if not math.isfinite(pickup) or not 0<=pickup<first_length:
            raise ValueError('pickup_beats 必须为首小节长度以内的非负数')
        origin=float(data.get('grid_origin',pickup))
        if not math.isfinite(origin) or origin < 0:
            raise ValueError('grid_origin 必须是非负有限数')

    @staticmethod
    def _interpolate(value, xs, ys):
        if not math.isfinite(value) or value < 0:
            raise ValueError('时间必须是非负有限数')
        i = max(0, min(len(xs)-2, bisect_right(xs, value)-1))
        return ys[i] + (value-xs[i])*(ys[i+1]-ys[i])/(xs[i+1]-xs[i])

    def to_beat(self, seconds):
        return self._interpolate(seconds, self.seconds, self.beats)

    def to_seconds(self, beat):
        return self._interpolate(beat, self.beats, self.seconds)


def fixed_tempo(bpm, duration, meter='4/4'):
    bpm = float(bpm)
    if not math.isfinite(bpm) or not 10 <= bpm <= 600:
        raise ValueError('BPM 需要在 10–600 之间')
    n,d = map(int, meter.split('/'))
    result = {'version':1, 'source':'manual', 'anchors':[{'seconds':0,'beat':0},
        {'seconds':max(1.,duration),'beat':max(1.,duration)*bpm/60}],
        'time_signatures':[{'beat':0,'numerator':n,'denominator':d}], 'warnings':[]}
    TempoMap(result)
    return result


def _supported_meters(beats, down_indices, first):
    """Downbeat counts are candidates, not individual meter-change commands.

    Repeated complete groups, locally coherent pulse spacing and bar alignment
    are required. Integer-multiple meter changes remain ambiguous with changing
    accent strength / half-time tracking and need explicit user confirmation.
    """
    intervals = [{'left': a, 'right': b, 'count': b-a,
                  'start_seconds': beats[a], 'end_seconds': beats[b]}
                 for a,b in zip(down_indices, down_indices[1:])]
    runs=[]
    for item in intervals:
        if not runs or runs[-1]['numerator'] != item['count']:
            runs.append({'numerator': item['count'], 'denominator': 4,
                         'left': item['left'], 'right': item['right'], 'support_bars': 1})
        else:
            runs[-1]['right']=item['right'];runs[-1]['support_bars']+=1
    for run in runs:
        # Establish meter from the first three complete groups. Once a meter
        # is supported, a later ritardando or tempo change in the same run
        # must not invalidate the entire song's meter (and default it to 4/4).
        # The preceding bar may use a different tempo: its last pulse is not
        # evidence against a genuine simultaneous meter/tempo change.
        confirmation_right=min(run['right'],run['left']+3*run['numerator'])
        lengths=[beats[i+1]-beats[i] for i in range(run['left'],confirmation_right)]
        ratios=[b/a for a,b in zip(lengths,lengths[1:])]
        center=median(lengths)
        run.update(beat=float(first+run['left']), confirmation_right=confirmation_right,
                   pulse_min_seconds=min(lengths),
                   pulse_max_seconds=max(lengths),
                   coherent_pulse=all(.65<=r<=1.55 for r in ratios)
                       and all(.65<=v/center<=1.55 for v in lengths), applied=False)
    stable=[r for r in runs if 2<=r['numerator']<=7 and r['support_bars']>=3 and r['coherent_pulse']]
    initial=stable[0]['numerator'] if stable else 4
    signatures=[{'beat':0.,'numerator':initial,'denominator':4}]
    # A single spurious opening downbeat must not determine the phase of a
    # later, consistently supported meter run.
    phase_index = stable[0]['left'] if stable else (down_indices[0] if down_indices else 0)
    pickup=(first+phase_index)%initial if down_indices else 0.
    boundary=float(pickup)
    confirmed_end=boundary
    down_positions=[float(first+i) for i in down_indices]
    boundary_repairs=[]
    def on_grid(position,origin,size):
        bars=(position-origin)/size
        return bars>=-1e-8 and abs(bars-round(bars))<1e-8
    for run in runs:
        current=signatures[-1]['numerator'];count=run['numerator']
        if count==current:
            if run['support_bars']>=3 and run['coherent_pulse']:
                confirmed_end=max(confirmed_end,float(first+run['right']))
            run['reason']='same_as_current_meter';continue
        if not 2<=count<=7:
            run['reason']='unsupported_or_missing_downbeat';continue
        if run['support_bars']<3:
            run['reason']='fewer_than_three_complete_groups';continue
        if not run['coherent_pulse']:
            run['reason']='pulse_gap_or_half_time_ambiguity';continue
        if max(current,count)%min(current,count)==0:
            run['reason']='ambiguous_accent_subdivision';continue
        # A few missing/spurious downbeats may delay the *stable run* beyond
        # the real change. Do not let the old meter veto a long stable return.
        # Backtrack at most four new bars, never into confirmed old-meter bars;
        # both meter grids must have a complete bar boundary at the candidate.
        # The bridge itself must favour the new phase, not merely its future.
        earliest=max(confirmed_end,run['beat']-4*count)
        candidates=[p for p in down_positions if earliest-1e-8<=p<run['beat']-1e-8
                    and on_grid(p,boundary,current) and on_grid(run['beat'],p,count)]
        checks=[];chosen=None
        for candidate in candidates:
            observed=[p for p in down_positions if candidate-1e-8<=p<=run['beat']+1e-8]
            new_hits=[p for p in observed if on_grid(p,candidate,count)]
            old_hits=[p for p in observed if on_grid(p,boundary,current)]
            expected=round((run['beat']-candidate)/count)+1
            supported=(len(new_hits)>=3 and len(new_hits)-len(old_hits)>=2
                       and len(new_hits)/expected>=.6 and len(new_hits)/len(observed)>=.6)
            evidence={'candidate_beat':candidate,'stable_run_start_beat':run['beat'],
                'confirmed_previous_meter_end_beat':confirmed_end,'max_lookback_bars':4,
                'observed_downbeat_beats':observed,'new_phase_matches':new_hits,
                'previous_phase_matches':old_hits,'expected_new_downbeats':expected,
                'supported':supported}
            checks.append(evidence)
            if supported and chosen is None:chosen=evidence
        if checks:run['boundary_backtracking']=checks
        change=chosen['candidate_beat'] if chosen else run['beat']
        if not on_grid(change,boundary,current):
            run['reason']='not_on_current_bar_boundary';continue
        signatures.append({'beat':change,'numerator':count,'denominator':4})
        if chosen:
            boundary_repairs.append(dict(chosen,from_numerator=current,to_numerator=count,
                applied_change_beat=change,action='backdate_meter_change_on_complete_bars',
                beat_anchors_changed=False))
        boundary=change;confirmed_end=float(first+run['right'])
        run.update(applied=True,applied_change_beat=change,
                   reason='supported_phase_backtracking_on_complete_bars' if chosen else
                          'repeated_coherent_groups_on_bar_boundary')
    return signatures,pickup,{'method':'stable_downbeat_runs_v3',
        'initial_phase_beat_index':phase_index,
        'initial_meter_supported':bool(stable),'downbeat_intervals':intervals,'candidate_runs':runs,
        'boundary_repairs':boundary_repairs}


def lock_meter(data, meter):
    """Explicitly fix the meter without changing audio seconds or beat anchors."""
    import copy
    try:n,d=map(int,meter.split('/'))
    except (TypeError,AttributeError,ValueError):raise ValueError('拍号格式应为 4/4 或 6/8') from None
    result=copy.deepcopy(data)
    previous=copy.deepcopy(result.get('time_signatures',[]))
    result['time_signatures']=[{'beat':0.,'numerator':n,'denominator':d}]
    result['meter_policy']={'mode':'locked','meter':f'{n}/{d}',
                            'previous_time_signatures':previous}
    TempoMap(result)  # Reject an incompatible pickup instead of silently shifting it.
    return result


def _repair_supported_pulses(beats, downbeats):
    """Repair isolated detector pulse errors only with bar/neighbor evidence.

    All original seconds are retained in diagnostics. We do not flatten tempo:
    a step, gradual change, unmatched pause, or wholesale half-time section is
    left untouched when neighboring pulse and bar duration evidence disagrees.
    """
    indices=sorted(set(min(range(len(beats)),key=lambda i:abs(beats[i]-t)) for t in downbeats))
    if len(indices)<5:
        return list(beats),[]
    proposed,_,support= _supported_meters(beats,indices,0.)
    if not support['initial_meter_supported']:
        return list(beats),[]
    initial=proposed[0]['numerator']
    bars=[]
    for a,b in zip(indices,indices[1:]):
        gaps=[beats[i+1]-beats[i] for i in range(a,b)]
        pulse=median(gaps)
        coherent=all(.85<=v/pulse<=1.15 for v in gaps)
        bars.append({'a':a,'b':b,'count':b-a,'pulse':pulse,'coherent':coherent,
                     'duration':beats[b]-beats[a]})
    good=[i for i,b in enumerate(bars) if b['count']==initial and b['coherent']]
    inserted=[];removed=set();repairs=[]
    for bi,bar in enumerate(bars):
        if bar['count']==initial or not 2<=bar['count']<=initial+2:
            continue
        left=next((j for j in reversed(good) if j<bi),None)
        right=next((j for j in good if j>bi),None)
        if left is None or right is None or bi-left>8 or right-bi>8:
            continue
        l,r=bars[left],bars[right]
        if not .9<=l['pulse']/r['pulse']<=1.1:
            continue
        pulse=(l['pulse']+r['pulse'])/2
        if abs(bar['duration']/(initial*pulse)-1)>.12:
            continue
        a,b=bar['a'],bar['b'];gaps=[beats[i+1]-beats[i] for i in range(a,b)]
        if not any(.85<=v/pulse<=1.15 for v in gaps):
            continue  # all pulses doubled: half-time versus real tempo is ambiguous
        add=[];drop=[];valid=True;k=0
        while k<len(gaps):
            length=gaps[k];ratio=length/pulse
            if .85<=ratio<=1.15:
                k+=1;continue
            multiple=round(ratio)
            if multiple in (2,3) and abs(ratio-multiple)<=.15:
                add.extend(beats[a+k]+length*j/multiple for j in range(1,multiple))
                k+=1;continue
            if (.4<=ratio<=.6 and k+1<len(gaps) and .4<=gaps[k+1]/pulse<=.6
                    and abs((length+gaps[k+1])/pulse-1)<=.15):
                drop.append(beats[a+k+1]);k+=2;continue
            valid=False;break
        if not valid or bar['count']+len(add)-len(drop)!=initial or not (add or drop):
            continue
        inserted.extend(add);removed.update(drop)
        repairs.append({'start_seconds':beats[a],'end_seconds':beats[b],
            'detected_pulses':bar['count'],'corrected_pulses':initial,
            'inserted_seconds':add,'removed_seconds':drop,
            'neighbor_pulse_seconds':[l['pulse'],r['pulse']],
            'neighbor_bar_seconds':[[beats[l['a']],beats[l['b']]],[beats[r['a']],beats[r['b']]]],
            'reason':'isolated_integer_gap_or_split_with_matching_neighbor_bar_duration'})
    return sorted([t for t in beats if t not in removed]+inserted),repairs


def from_beats(beats, downbeats, duration, beat_scale=1.0):
    beats = [float(b) for b in beats]
    if len(beats) < 3 or any(b<=a for a,b in zip(beats,beats[1:])):
        raise ValueError('可靠节拍不足。请设置 BPM 或导入 Tempo Map 后重试；不自动猜固定速度。')
    detected_beats=list(beats)
    beats,pulse_repairs=_repair_supported_pulses(beats,downbeats)
    interval = sorted(b-a for a,b in zip(beats,beats[1:]))[len(beats)//2-1]
    # The audio file start need not be a beat. Avoid squeezing a whole beat into
    # a few milliseconds: DAWs may clamp that artificial tempo and shift audio.
    first = max(.125, round(beats[0]/interval*8)/8) if beats[0] > 1e-8 else 0.
    while first and first/beats[0]*60 > 300 and first > 1/32:
        first /= 2
    anchors = [{'seconds':0.,'beat':0.}]
    for i,t in enumerate(beats):
        if t > 1e-8:
            anchors.append({'seconds':t,'beat':float(first+i)})
    if duration > anchors[-1]['seconds']:
        last_dt = beats[-1]-beats[-2]
        anchors.append({'seconds':duration,'beat':anchors[-1]['beat']+(duration-beats[-1])/last_dt})
    warnings = ['自动拍号需连续稳定强拍组支持；复合拍及半倍速歧义请用明确拍号或导入速度图。']
    down_indices = sorted(set(min(range(len(beats)),key=lambda i:abs(beats[i]-x)) for x in downbeats))
    signatures,pickup,meter_diagnostics=_supported_meters(beats,down_indices,first)
    meter_diagnostics['detected_downbeats_seconds']=[float(t) for t in downbeats]
    meter_diagnostics['detected_beats_seconds']=detected_beats
    meter_diagnostics['pulse_repairs']=pulse_repairs
    meter_diagnostics['automatic_pulse_correction_applied']=bool(pulse_repairs)
    meter_diagnostics['timebase_policy']='local_supported_pulse_repair_then_stable_meter_groups'
    uncertain=sum(not r['applied'] and r['reason']!='same_as_current_meter'
                  for r in meter_diagnostics['candidate_runs'])
    if uncertain:
        warnings.append(f'{uncertain} 段强拍组不足以确认变拍；保留当前拍号，原始检测与局部节拍修复记录已保存，可修改速度图后重导出。')
    if pulse_repairs:
        warnings.append(f'已依据相邻小节时长及局部脉冲证据修复 {len(pulse_repairs)} 处漏拍/多拍；原始检测保留在速度图诊断中。')
    if meter_diagnostics['boundary_repairs']:
        warnings.append(f'已依据稳定新拍号及过渡段强拍相位回填 {len(meter_diagnostics["boundary_repairs"])} 处变拍入口；保留完整小节与原拍秒锚点。')
    for i in range(1,len(beats)-1):
        ratio=(beats[i+1]-beats[i])/(beats[i]-beats[i-1])
        if ratio<0.65 or ratio>1.55:
            warnings.append(f'{beats[i]:.2f}s 附近速度突变，可能为真实变速或半速/倍速误判')
    if not down_indices:
        warnings.append('未检测到强拍：小节起点暂按音频起点，建议手动核对。')
    if first:
        warnings.append('音频开头至首节拍为对齐前导段；该段速度为时间对齐用途，不代表真实演奏 BPM。')
    result={'version':1,'source':'beat_this','anchors':anchors,'time_signatures':signatures,
        'pickup_beats':pickup,'grid_origin':first,'lead_in_alignment':bool(first),'warnings':warnings,
        'beat_mapping_version':BEAT_MAPPING_VERSION,
        'meter_policy':{'mode':'supported_pulse_and_meter_inference'},'meter_diagnostics':meter_diagnostics}
    return scale_tempo(result,beat_scale)


def scale_tempo(data, factor):
    import copy
    if factor not in (0.5,1.,2.):
        raise ValueError('节拍倍率仅支持 0.5 / 1 / 2')
    result=copy.deepcopy(data)
    if factor == 1:
        TempoMap(result)
        return result
    if 'first_full_bar_beat' in result:
        result['first_full_bar_beat'] *= factor
    if isinstance(result.get('opening_phase'),dict) and 'beat' in result['opening_phase']:
        result['opening_phase']['beat'] *= factor
    if isinstance(result.get('pulse_projection'),dict):
        # This report is evidence from the original decoder projection. Scaling
        # the musical coordinate does not rerun that decoder or preserve its
        # provisional bar grouping. Keep its audit values, identify the later
        # interpretation explicitly, and never label it a new pulse detection.
        report=result['pulse_projection']
        applied=report.get('post_projection_beat_scale',1.)*factor
        report['post_projection_beat_scale']=applied
        report['coordinate_scope']='original_decoder_projection_before_explicit_beat_scale'
        if 'quarter_span_per_pulse' in report:
            report['effective_quarter_span_per_pulse']=report['quarter_span_per_pulse']*applied
        report['original_grouping_is_current']=False
    for a in result['anchors']:
        a['beat'] *= factor
    for s in result['time_signatures']:
        s['beat'] *= factor
    # Beat-value interpretation is independent of meter. A former pickup may
    # become a full intro bar: preserve the exact boundary as an explicit event.
    opening=result.get('pickup_beats',0)*factor
    first=result['time_signatures'][0]
    size=first['numerator']*4/first['denominator']
    result['pickup_beats']=opening % size
    if opening:
        result.setdefault('first_full_bar_beat',opening)
        if opening >= size:
            rows={s['beat']:s for s in result['time_signatures']}
            rows.setdefault(opening,dict(first,beat=opening))
            result['time_signatures']=[rows[b] for b in sorted(rows)]
    result['grid_origin']=result.get('grid_origin',0)*factor
    if factor!=1:
        result['allow_partial_measures']=True
        result.setdefault('warnings',[]).append('已改变节拍解释；拍号与演奏秒时间保留，可能产生不完整边界小节。')
    TempoMap(result)
    return result


def quantize_notes(notes, tempo, grid='auto'):
    """Snap each boundary locally; a held note may end in a different rhythm."""
    steps={'straight':0.25,'triplet':1/3,'fine':0.125}
    if grid not in ('auto',*steps):
        raise ValueError('量化应为 auto/straight/triplet/fine')
    if not notes:
        return []
    # Share the score's effective bar geometry instead of restarting every
    # song subdivision at a single global origin. A 5/8 bar is 2.5 quarters:
    # its next bar's triplet lattice is therefore not the old triplet lattice.
    # score_end is a presentation boundary, never permission to truncate or
    # reject the unquantized performance's still-sounding tails.
    from .duet_notation import reference_bars
    geometry_data=dict(tempo.data)
    geometry_data.pop('score_end',None)
    last=max(tempo.to_beat(max(n['start'],n['end'])) for n in notes)
    starts,lengths,_=reference_bars(TempoMap(geometry_data),last)
    def snap(value,step):
        index=max(0,bisect_right(starts,value+1e-8)-1)
        origin=starts[index];end=origin+lengths[index]
        # Same half-up tie convention as the piano roll's Math.round gridBeat.
        return max(0,min(end,origin+math.floor((value-origin)/step+.5)*step))
    def boundary(value):
        if grid!='auto':
            return snap(value,steps[grid]),steps[grid]
        # Prefer simple sixteenths for ambiguous timing. Eighth/sixteenth
        # triplets and thirty-seconds remain available when the evidence is
        # closer. Evaluate the END as well as the onset, independently: a
        # whole-beat onset does not imply a straight note duration.
        candidates=((.25,0.),(1/3,.0004),(1/6,.0006),(.125,.001))
        step,_=min(candidates,key=lambda item:
            (value-snap(value,item[0]))**2+item[1])
        return snap(value,step),step
    result=[]
    for n in notes:
        start,end=tempo.to_beat(n['start']),tempo.to_beat(n['end'])
        quantized_start,start_step=boundary(start)
        quantized_end,end_step=boundary(end)
        quantized_start=max(0,quantized_start)
        if quantized_end<=quantized_start+1e-9:
            quantized_end=quantized_start+min(start_step,end_step)
        result.append(dict(n,start_beat=quantized_start,end_beat=quantized_end,
            quantization_shift_seconds=tempo.to_seconds(quantized_start)-n['start']))
    # MIDI export already allocates channels for overlapping unisons. Snapping
    # must not silently merge attacks or shorten sustained notes. If distinct
    # attacks collide on one grid position, retain their original order/times;
    # notation may spell that as an arpeggio, grace group or finer rhythm.
    groups={}
    for n in result:
        groups.setdefault(round(n['start_beat'],9),[]).append(n)
    for group in groups.values():
        if len({round(n['start'],6) for n in group})>1:
            for n in group:
                n['start_beat']=tempo.to_beat(n['start'])
                n['end_beat']=max(n['end_beat'],tempo.to_beat(n['end']))
                n['quantization_shift_seconds']=0.
    result.sort(key=lambda x:(x['start_beat'],x['pitch']))
    for n in result:
        n['quantization_end_shift_seconds']=tempo.to_seconds(n['end_beat'])-n['end']
    return result
