"""Revision-bound, read-only editor adapter for the MD research trial.

No adopt function: matching count is necessary, not proof of correct beat units.
"""
from copy import deepcopy
from pathlib import Path
import math
from . import editing, md_beat_trial as trial


def prepare(project, base_revision, request):
    doc=editing.current(project)
    editing._check_base(doc,base_revision)
    if any(key in request for key in ('meter','time_signature','numerator','denominator')):
        raise ValueError('MD trial accepts beat positions, not time signatures')
    data=deepcopy(request)
    if 'start' in data:data['start_seconds']=data.pop('start')
    if 'end' in data:data['end_seconds']=data.pop('end')
    prepared=trial.prepare_project(project,data)
    return {'project':str(Path(project).resolve()),'base_revision':base_revision,'request':prepared}


def run(prepared):
    result=trial.run_trial(prepared['request'])
    result['context_comparison']={'requested':False}
    if prepared['request'].get('compare_context') is True:
        import soundfile as sf
        info=sf.info(prepared['request']['audio_path'])
        req=prepared['request'];start,end=req['start_seconds'],req['end_seconds']
        primary_bounds=trial.audio_context_bounds(start,end,info.samplerate,info.frames,req.get('context_mode','full_window'))
        alternate_bounds=trial.audio_context_bounds(start,end,info.samplerate,info.frames,'symmetric_10s')
        if primary_bounds==alternate_bounds:
            result['context_comparison']={'requested':True,'comparable':False,'reason':'Available audio windows are identical; no second inference was run'}
        else:
            alternate=trial.run_trial(dict(req,context_mode='symmetric_10s',compare_context=False,audit=False))
            result['context_comparison']=compare_results(result,alternate)
    result['can_apply']=False
    result['preview_only']=True
    result['summary']={'known_points':len(result['conditioning']),
                       'requested_intervals':result['requested_interval_count'],
                       'proposed_intervals':result['interval_count'],
                       'time_signature_conditioning':False}
    result['apply_blockers']=candidate_blockers(prepared,result)
    if not result['apply_blockers']:
        result['apply_blockers']=['Preview only: beat-unit mapping and editor apply integration are not enabled']
    return result


def compare_results(primary,alternate):
    if primary.get('source_sha256')!=alternate.get('source_sha256'):
        raise ValueError('Source audio changed between windows; comparison refused')
    a,b=primary['beat_seconds'],alternate['beat_seconds']
    displacement=max([min(abs(x-y) for y in b) for x in a]+[min(abs(y-x) for x in a) for y in b]) if a and b else None
    same_count=len(a)==len(b)
    return {'requested':True,'comparable':True,'same_count':same_count,
            'max_symmetric_displacement_seconds':displacement,
            'window_sensitive':not same_count or displacement is None or displacement>.08,
            'not_accuracy_score':True,'not_ranked_by_requested_count':True,
            'alternate':{k:alternate[k] for k in ('beat_seconds','downbeat_seconds','interval_count','audio_context','elapsed_seconds')},
            'explanation':'Independent window comparison only; agreement does not establish musical correctness'}


def candidate_blockers(prepared,result):
    """Recompute safety checks, never trust a frontend can_apply/count_matches flag."""
    reasons=[]
    request=prepared['request']
    current=trial.prepare_project(prepared['project'],request)
    if current['project_binding']!=request['project_binding']:
        reasons.append('Project version changed; run a new preview')
    source=trial.sha256(current['audio_path'])
    if source!=result.get('source_sha256'):
        reasons.append('Source audio changed; run a new preview')
    if result.get('project_binding')!=request['project_binding']:
        reasons.append('Candidate belongs to another project version')
    if result.get('request')!=trial.validate_request(request,editing.current(prepared['project'])['duration']):
        reasons.append('Candidate request does not match this preview')
    points=result.get('beat_seconds')
    if not isinstance(points,list) or any(type(t) not in (int,float) or not math.isfinite(t) for t in points):
        return reasons+['Invalid candidate beat positions']
    expected=request.get('interval_count')
    if type(expected) is not int or len(points)!=expected+1:
        reasons.append('Beat count does not match; application refused')
    if any(b<=a for a,b in zip(points,points[1:])):
        reasons.append('Beat positions are not strictly increasing')
    known=trial.validate_request(request,editing.current(prepared['project'])['duration'])['known_beats']
    if not points or points[0]!=request['start_seconds'] or points[-1]!=request['end_seconds']:
        reasons.append('Range endpoints changed')
    if any(t not in points for t in known):
        reasons.append('A known beat was lost')
    return reasons


def adopt(*args,**kwargs):
    raise ValueError('MD trial is preview-only; no project changes were made')
