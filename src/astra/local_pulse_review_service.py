"""Revision-bound local review; only explicit adoption creates one editor revision."""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

from . import editing, local_pulse_review, pulse_edit
from .active_pulse_evidence import resolve
from .timing import TempoMap

REASONS={
    'invalid_candidate_count_or_order':'候选拍数或时间顺序无效。',
    'endpoint_not_exact':'候选没有保持选区两端。',
    'confirmed_anchor_changed':'候选移动了已固定的锚点。',
    'abrupt_adjacent_period_change':'相邻拍间距变化过大，需要人工核对。',
    'insufficient_consecutive_supported_pulses':'连续多个拍点的音频支持仍不足。',
    'no_consecutive_multi_pulse_improvement':'只有零散拍点改善，尚无连续改善证据。',
    'insufficient_common_probability_gain':'整段声学支持没有充分改善。',
    'lost_previously_strong_pulse':'候选丢失了原先支持较强的拍点。',
    'changed_count_requires_direct_manual_review':'拍数已改变，需使用手动拍数复核逐段判断。',
}


def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf8')


def _sha(raw):return hashlib.sha256(raw).hexdigest()


def _source(state):
    return {k:state.get(k) for k in ('source','source_sha256','source_kind')}


def _document(doc):
    return _sha(_json({k:doc[k] for k in ('revision','tempo','anchors','duration')}))


def _evidence(evidence):
    return {k:deepcopy(evidence.get(k)) for k in ('provider','cache_key','evidence_ref','result_sha256',
        'activation_sha256','activation_identity','probability_source','fps')}


def prepare(project, base_revision, request):
    """Small metadata/CAS preparation; heavy evidence verification runs off UI."""
    from .pipeline import read_json
    project=Path(project).resolve();doc=editing.current(project)
    editing._check_base(doc,base_revision)
    mode=request.get('mode','discover')
    if mode not in ('discover','propose'):raise ValueError('未知的局部拍点复核方式。')
    options={'mode':mode}
    if mode=='discover':
        scope=request.get('scope')
        if scope is not None:
            if (not isinstance(scope,(list,tuple)) or len(scope)!=2
                    or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in scope)
                    or not 0<=scope[0]<scope[1]<=doc['duration']):
                raise ValueError('复核范围必须位于当前曲目内。')
            options['scope']=list(scope)
    else:
        for name in ('start_seconds','end_seconds','interval_count'):
            if name not in request:raise ValueError('请选择复核范围并明确拍数。')
            options[name]=request[name]
        options['count_confirmed']=request.get('count_confirmed',False)
        # Reuse the existing bounds, count and locked-coordinate validation.
        try:
            pulse_edit.prepare(project,base_revision,{'start':options['start_seconds'],
                'end':options['end_seconds'],'interval_count':options['interval_count']})
        except ValueError as exc:
            if '计算预算' in str(exc):
                raise ValueError('该段超出逐帧复核计算预算。请在卷帘改选较短范围后重新检查拍点；也可进入拍数修正手动划拍。未退回粗粒度候选。') from exc
            raise
        if type(options['count_confirmed']) is not bool:raise ValueError('请明确是否确认了范围拍数。')
        if 'anchors' in request:
            # This entrance uses persisted editor pins only. Arbitrary hidden
            # request anchors must not bypass the established pin workflow.
            raise ValueError('请先在卷帘中固定锚点，再请求局部复核。')
    state=read_json(project/'project.json')
    return dict(project=str(project),base_revision=base_revision,request=deepcopy(options),
        source=_source(state),document_sha256=_document(doc),
        tempo=deepcopy(doc['tempo']),fixed_anchors=[])


def _check(prepared):
    from .pipeline import read_json
    project=Path(prepared['project']);doc=editing.current(project)
    editing._check_base(doc,prepared['base_revision'])
    if _document(doc)!=prepared['document_sha256']:
        raise ValueError('编辑文档发生变化，请重新预览。')
    if _source(read_json(project/'project.json'))!=prepared['source']:
        raise ValueError('项目原曲身份发生变化，请重新复核。')
    return doc


def _preview(prepared, proposal):
    if not proposal.get('candidate_available') or not proposal['review'].get('accepted'):
        raise ValueError('此候选未通过局部证据复核，不能采用。')
    context=dict(prepared,constraints=proposal['constraints'])
    preview=pulse_edit.build_candidate(context,proposal['decoded'])
    preview['operation']['analysis'].update(quality_review_version=local_pulse_review.VERSION,
        quality_review=deepcopy(proposal['review']),count_confirmed=proposal['count_confirmed'],
        acoustic_refinement=deepcopy(proposal['refinement']))
    _outside_unchanged(prepared['tempo'],preview['tempo'],proposal['constraints'])
    return preview


def _outside_unchanged(old,new,constraints):
    """Reject even a valid musical-coordinate shift outside this quality review."""
    start,end=constraints['start_seconds'],constraints['end_seconds']
    before,after=TempoMap(old),TempoMap(new)
    times=sorted(set(before.seconds+after.seconds+[start,end]))
    times+= [(a+b)/2 for a,b in zip(times,times[1:])]
    if any(abs(before.to_beat(t)-after.to_beat(t))>1e-7 for t in times if t<=start or t>=end):
        raise ValueError('候选改变了复核区外的速度图或音乐坐标，未采用。')


def _save(prepared,payload):
    raw=_json(payload);digest=_sha(raw)
    folder=Path(prepared['project'])/'cache'/'pulse-quality';folder.mkdir(parents=True,exist_ok=True)
    path=folder/f'{digest}.json'
    try:
        with path.open('xb') as stream:stream.write(raw)
    except FileExistsError:
        if path.read_bytes()!=raw:raise ValueError('复核凭据已损坏，未覆盖原文件。')
    return {'candidate_file':str(path),'candidate_sha256':digest}


def run(prepared):
    """Background worker: at most one explicit local decode, never adoption."""
    _check(prepared)
    evidence=resolve(prepared['project'],prepared['tempo'],load_probabilities=True)
    binding=_evidence(evidence);request=prepared['request']
    if request['mode']=='discover':
        quality=local_pulse_review.discover(evidence,prepared['tempo'],scope=request.get('scope'))
        result=dict(mode='discover',quality=quality,ranges=quality['ranges'],candidate_available=False,
            summary=f"{'选区内' if request.get('scope') is not None else '全曲'}发现 {len(quality['ranges'])} 段可复核拍点；尚未调整速度图。",
            warnings=['范围拍数沿用当前图，未经人工确认；强峰不等于正确拍点。'])
    else:
        proposal=local_pulse_review.propose(evidence,prepared['tempo'],request,
            fixed_anchors=prepared['fixed_anchors'])
        result=dict(mode='propose',proposal=proposal,review=proposal['review'],
            review_messages=[REASONS.get(reason,'局部候选仍需人工复核。') for reason in proposal['review']['reasons']],
            candidate_available=proposal['candidate_available'],
            summary=('局部候选通过证据检查；请试听比较后明确采用。' if proposal['candidate_available']
                else '局部证据不足，候选已拒绝；当前速度图保持。'),
            warnings=([] if proposal['count_confirmed'] else ['拍数沿用当前图，未经人工确认。']))
        if proposal['candidate_available']:
            preview=_preview(prepared,proposal)
            result.update(tempo=preview['tempo'],warnings=result['warnings']+preview['warnings'])
            # The UI receives a preview, never an independently trusted edit.
    _check(prepared)
    fresh=resolve(prepared['project'],prepared['tempo'],load_probabilities=True)
    if _evidence(fresh)!=binding:raise ValueError('复核期间拍点证据变化，请重新预览。')
    payload=dict(version=1,prepared=deepcopy(prepared),evidence=binding,result=deepcopy(result))
    result.update(_save(prepared,payload),base_revision=prepared['base_revision'],
        source_events_modified=False,map_published=False,
        provenance={'provider':evidence['provider'],'activation_sha256':evidence['activation_sha256'],
                    'base_revision':prepared['base_revision'],'automatic_correction':False})
    return result


def adopt(prepared,result):
    """Verify stored candidate, fresh source and revision; publish once with undo."""
    from . import pipeline
    project=Path(prepared['project']).resolve();path=Path(result['candidate_file']).resolve()
    if not path.is_relative_to(project/'cache'/'pulse-quality'):
        raise ValueError('候选凭据不属于当前工程。')
    with pipeline.project_lock(project),editing.edit_lock(project):
        doc=_check(prepared)
        if path.stat().st_size>16*1024*1024:raise ValueError('候选凭据超过读取上限。')
        raw=path.read_bytes()
        if _sha(raw)!=result.get('candidate_sha256'):raise ValueError('候选凭据已变化，请重新预览。')
        saved=json.loads(raw)
        if saved.get('version')!=1 or saved.get('prepared')!=prepared:
            raise ValueError('候选与本次编辑版本或请求不匹配。')
        proposal=saved['result'].get('proposal')
        if not proposal or not proposal.get('candidate_available'):
            raise ValueError('此候选未通过局部证据复核，不能采用。')
        evidence=resolve(project,doc['tempo'],load_probabilities=True)
        if _evidence(evidence)!=saved['evidence']:
            raise ValueError('候选来源已变化，请重新复核。')
        clock=TempoMap(doc['tempo']);constraints=proposal['constraints']
        left=clock.to_beat(constraints['start_seconds']);right=clock.to_beat(constraints['end_seconds'])
        old=[clock.to_seconds(left+i) for i in range(round(right-left)+1)]
        review=local_pulse_review.evaluate(evidence,old,proposal['decoded']['beat_seconds'],constraints)
        if not review['accepted'] or review!=proposal['review']:
            raise ValueError('候选未通过当前来源证据检查，未采用。')
        preview=_preview(prepared,proposal)
        new=editing.apply_operation(doc,preview['operation'])
        for key in ('notes','controls','other_events','duration'):
            if new.get(key)!=doc.get(key):raise ValueError('局部复核意外改变原始秒事件，未采用。')
        _outside_unchanged(doc['tempo'],new['tempo'],constraints)
        index=pipeline.read_json(project/'editing'/'current.json')
        index['undo']=[*index.get('undo',[]),prepared['base_revision']];index['redo']=[]
        return editing._publish_revision(project,new,index)
