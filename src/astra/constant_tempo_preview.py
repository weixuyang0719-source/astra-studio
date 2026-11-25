"""Temporary constant-tempo ruler and explicit constraint comparisons.

No files, active clock, performance events, playback rate or audition mode are
changed here. Adoption must use the ordinary rebar revision transaction.
"""
from copy import deepcopy
import math

from .rebar_edit import apply_rebar, list_boundaries, _number, _sha
from .timing import TempoMap

VERSION = 1
TOLERANCE_SECONDS = 1e-6


def preview_constant_tempo(tempo, request, *, revision, fixed_anchors=None):
    req = deepcopy(request)
    bpm = _number(req.get('reference_bpm'), '参考 BPM')
    if not 10<=bpm<=600:
        raise ValueError('参考 BPM 需要在 10–600 之间')
    phase = _number(req.get('phase_seconds'), '参考第一拍秒数')
    clock=TempoMap(deepcopy(tempo))
    if abs(phase)>max(86400,clock.seconds[-1]*4):
        raise ValueError('参考相位超出可显示范围')
    rows=list_boundaries(tempo,revision)
    if req.get('boundary_revision')!=revision or req.get('tempo_sha256')!=_sha(tempo):
        raise ValueError('参考的小节线已过期，请重新选择')
    known={r['id'] for r in rows}
    if req.get('left_boundary') not in known or req.get('right_boundary') not in known:
        raise ValueError('请选择当前版本的两条已有小节线')
    n,d,bars=req.get('numerator'),req.get('denominator'),req.get('bar_count')
    if type(n) is not int or not 1<=n<=32 or type(d) is not int or d not in (1,2,4,8,16,32,64,128):
        raise ValueError('请输入有效拍号')
    if type(bars) is not int or not 1<=bars<=4096:
        raise ValueError('小节数必须是 1–4096 的整数')
    left,right=_number(req.get('start'),'左定位秒数'),_number(req.get('end'),'右定位秒数')
    if not 0<=left<right<=clock.seconds[-1]:
        raise ValueError('参考选区必须在音频内且严格递增')
    quarter_seconds=60/bpm
    unit=4/d
    bar_quarters=n*unit
    target=bars*bar_quarters
    actual_span=right-left
    expected_span=target*quarter_seconds
    # The selected left line begins an ordinary bar, not merely any beat.
    left_index=round((left-phase)/(quarter_seconds*bar_quarters))*n
    aligned_left=phase+left_index*quarter_seconds*unit
    aligned_right=aligned_left+expected_span
    phase_error=left-aligned_left
    span_error=actual_span-expected_span
    actual_quarters=actual_span/quarter_seconds
    inferred_bars=actual_quarters/bar_quarters
    phases_ok=abs(phase_error)<=TOLERANCE_SECONDS
    count_ok=abs(span_error)<=TOLERANCE_SECONDS
    operation={k:deepcopy(req[k]) for k in ('boundary_revision','tempo_sha256','left_boundary','right_boundary',
        'start','end','numerator','denominator','bar_count','replace_changes') if k in req}
    candidates=[]
    def add(identifier,kind,op,constant,changes):
        candidate=dict(id=identifier,kind=kind,constant_reference_satisfied=constant,
            operation=op,changes=changes,applicable=False)
        try:
            result=apply_rebar(tempo,op,fixed_anchors,revision=revision)
            if constant:
                check=TempoMap(result['tempo']);begin,finish=op['start'],op['end']
                sample=sorted({begin,finish}|{t for t in check.seconds if begin<t<finish})
                if any(abs((check.to_beat(b)-check.to_beat(a))/(b-a)*60-bpm)>1e-6
                       for a,b in zip(sample,sample[1:])):
                    raise ValueError('该候选未能形成要求的局部定速图；不会把保留原速度形状冒称定速')
            candidate.update(applicable=True,impact=result['impact'],summary=result['summary'])
        except ValueError as exc:
            candidate['reason']=str(exc)
            candidate.pop('operation',None)
        candidates.append(candidate)
    fixed=dict(operation,distribution='uniform')
    if phases_ok and count_ok:
        add('fixed_requested','keep_requested_constraints',fixed,True,[])
    else:
        add('fixed_move_endpoints','adjust_endpoints',dict(fixed,start=aligned_left,end=aligned_right),True,
            [dict(field='start',before=left,after=aligned_left,delta_seconds=aligned_left-left),
             dict(field='end',before=right,after=aligned_right,delta_seconds=aligned_right-right)])
        nearest=round(inferred_bars)
        if phases_ok and 1<=nearest<=4096 and abs(actual_span-nearest*bar_quarters*quarter_seconds)<=TOLERANCE_SECONDS:
            add('fixed_change_count','adjust_bar_count',dict(fixed,bar_count=nearest),True,
                [dict(field='bar_count',before=bars,after=nearest)])
    add('release_constant','release_constant_constraint',dict(operation,distribution='preserve'),False,[])
    for c in candidates:
        if c['applicable'] and c['constant_reference_satisfied']:
            c['effective_quarter_bpm']=bpm
        c['playback_rate']=1
        c['performance_export_purpose']='mapped'
    requested_feasible=phases_ok and count_ok and any(c['id']=='fixed_requested' and c['applicable'] for c in candidates)
    return dict(version=VERSION,mode='reference_only',revision=revision,tempo_sha256=_sha(tempo),
        reference_grid=dict(bpm=bpm,phase_seconds=phase,quarter_seconds=quarter_seconds,
            beat_unit_quarters=unit,beat_unit_seconds=unit*quarter_seconds,numerator=n,denominator=d,
            start_seconds=0,end_seconds=clock.seconds[-1],zero_index_at_phase=True,phase_kind='bar_start'),
        constraints=dict(start_seconds=left,end_seconds=right,bar_count=bars,target_quarters=target,
            actual_duration_seconds=actual_span,required_duration_seconds=expected_span,
            duration_error_seconds=span_error,quarter_count_at_reference_bpm=actual_quarters,
            quarter_count_error=actual_quarters-target,left_phase_error_seconds=phase_error,
            implied_bar_count=inferred_bars,phase_consistent=phases_ok,span_consistent=count_ok),
        requested_constraints_feasible=requested_feasible,candidates=candidates,
        playback=dict(rate=1,source='original_unquantized_seconds',reference_grid_drives_playback=False,
            active_clock_changed=False,source_note_or_cc_seconds_changed=False,
            adoption_requires_mapped_tempo_and_note_ticks_together=True,
            quantized_output_must_be_separate=True),
        warnings=[] if requested_feasible else [
            '边界或固定锚点约束阻止采用；请查看候选的具体原因。' if phases_ok and count_ok else
            '参考速度、相位、选区时长与整数小节数不能同时满足；请明确选择调整方案。'])


def reference_grid_points(reference_grid, start_seconds, end_seconds, *, max_points=4096):
    """Generate just the visible ruler; never create a second effective clock."""
    start=_number(start_seconds,'显示开始秒数');end=_number(end_seconds,'显示结束秒数')
    if end<start or type(max_points) is not int or not 1<=max_points<=10000:
        raise ValueError('参考网格范围或数量无效')
    phase=float(reference_grid['phase_seconds']);step=float(reference_grid['beat_unit_seconds'])
    if not math.isfinite(step) or step<=0:raise ValueError('参考拍长无效')
    first=math.ceil((start-phase)/step-1e-9);last=math.floor((end-phase)/step+1e-9)
    if last-first+1>max_points:raise ValueError('参考网格过密，请缩小可见范围')
    n=int(reference_grid['numerator'])
    return [dict(seconds=phase+i*step,index=i,beat_in_bar=i%n+1,bar_offset=i//n)
            for i in range(first,last+1)]
