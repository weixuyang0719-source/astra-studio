"""Explicit, undoable adoption of committed transcription caches.

No inference, cache mutation, guessed merge, or replacement of the working clock.
The preview identity binds exact cache bytes to one project and editor revision.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from . import editing, pipeline


def _load(project):
    state = pipeline.read_json(project / 'project.json')
    if state.get('source_kind') == 'midi' or 'piano' not in state.get('instruments', []):
        raise ValueError('此操作仅用于音频工程的新钢琴转录候选。')
    key = (state.get('analysis_keys') or {}).get('piano')
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', key):
        raise ValueError('没有可核验身份的新转录候选；请先完成钢琴分析。')
    path = project / 'notes' / f'piano-{key}.json'
    if not path.is_file():
        raise ValueError('新转录候选缓存缺失；当前人工稿未修改。')
    raw = path.read_bytes()
    bundle = json.loads(raw)
    if not isinstance(bundle, dict) or not isinstance(bundle.get('notes'), list):
        raise ValueError('候选缓存没有有效的秒音符事件。')
    return state, key, hashlib.sha256(raw).hexdigest(), bundle


def _replacement(doc, state, key, digest, bundle):
    if doc.get('source_key') == key:
        raise ValueError('当前工程已经基于此转录候选，没有另一版新候选可采用。')
    result = deepcopy(doc)
    result['notes'] = [dict(
        {k:deepcopy(v) for k,v in n.items() if k not in ('start_beat','end_beat','start_tick','end_tick')},
        id=f'auto-{key}-{i}', origin='transcription', channel=n.get('channel',0))
        for i,n in enumerate(bundle['notes'])]
    if 'controls' in bundle:
        if not isinstance(bundle['controls'], list):
            raise ValueError('候选控制器数据无效。')
        controls = deepcopy(bundle['controls'])
    else:
        controls = []
        for pedal in bundle.get('pedals', []):
            controls.extend({'seconds':seconds,'number':pedal['number'],'value':value,
                             'channel':pedal.get('channel',0)}
                for seconds,value in ((pedal['start'],pedal.get('value',127)),(pedal['end'],0)))
    result['controls'] = [dict(c, id=f'auto-cc-{key}-{i}', origin='transcription',
                               channel=c.get('channel',0)) for i,c in enumerate(controls)]
    # The previous version retains all deletion/edit history. These identifiers
    # describe the replacement, not a merge that suppresses fresh model events.
    result.update(source_key=key, source_export=state.get('latest_export'), deleted_ids=[], manual=True)
    result['operation'] = {'type':'adopt_transcription_candidate', 'candidate_key':key,
                           'candidate_sha256':digest, 'previous_source_key':doc.get('source_key')}
    result['summary'] = '已采用新转录音符与 CC；生效速度图、拍号、固定锚点及选区保留，可撤销。'
    result['edit_warnings'] = []
    editing.validate(result)
    score_end = result['tempo'].get('score_end')
    if score_end and any(n['end'] > score_end['seconds'] + 1e-8 for n in result['notes']):
        raise ValueError('新候选有音符延续到已确认谱面终点之后；请先核对或后移谱面终点，再采用。不会自动裁切。')
    return result


def _native_capability(state,key,digest,bundle):
    return {'adopt_transcription_candidate':dict(source_key=key,source_export=state.get('latest_export'),
                                                 candidate_sha256=digest,bundle=bundle)}


def prepare(project, base_revision):
    project = Path(project).resolve()
    # Same lock order as pipeline export commits; no editing data is changed.
    with pipeline.project_lock(project), editing.edit_lock(project):
        from .document_editor import preview_locked, query_locked
        path=project/'editing'/'current.json'
        index=editing._base_index(path,base_revision)
        state,key,digest,bundle=_load(project)
        trusted=_native_capability(state,key,digest,bundle)
        reply=preview_locked(project,path,index,{'type':'adopt_transcription_candidate'},trusted=trusted)
        old=query_locked(project,path,index,'info',expected=reply['stamp'])
        candidate=reply['delta']['replacement']
        return {'project':str(project), 'base_revision':base_revision, 'editor_ticket':reply['stamp'],
                'candidate_key':key, 'candidate_sha256':digest,
                'source_sha256':state.get('source_sha256'),
                'old_notes':old['notes'], 'new_notes':candidate['notes'],
                'old_controls':old['controls'], 'new_controls':candidate['controls'], 'fixed_anchors':0}


def adopt(project, base_revision, prepared):
    project = Path(project).resolve()
    if prepared.get('project') != str(project) or prepared.get('base_revision') != base_revision:
        raise ValueError('候选确认属于另一工程或旧版本；请重新打开确认。')
    with pipeline.project_lock(project), editing.edit_lock(project):
        path=project/'editing'/'current.json'
        index=editing._base_index(path,base_revision)
        state,key,digest,bundle = _load(project)
        if (key != prepared.get('candidate_key') or digest != prepared.get('candidate_sha256')
                or state.get('source_sha256') != prepared.get('source_sha256')):
            raise ValueError('候选缓存或源分析身份已变化；请重新查看候选后确认。当前编辑未修改。')
        from .document_editor import commit_locked
        return commit_locked(project,path,index,{'type':'adopt_transcription_candidate'},
                             trusted=_native_capability(state,key,digest,bundle),
                             expected=prepared['editor_ticket'])['doc']
