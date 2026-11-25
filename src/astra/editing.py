"""Versioned, seconds-based working document. Inference caches are never edited."""
from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from uuid import uuid4
import math
import os

from .timing import TempoMap

# Worker-local observation of the authoritative index replace, never global state.
_commit_observer = ContextVar('astra_edit_commit_observer', default=None)
_added_event_observer = ContextVar('astra_added_event_observer', default=None)

def _read(path):
    from .pipeline import read_json
    return read_json(path)

def _save(path, data):
    from .pipeline import save_json
    path = Path(path)
    observer = _commit_observer.get() if path.name == 'current.json' else None
    try:
        save_json(path, data)
    except Exception:
        # A saver can fail after replacement (e.g. during cleanup). Check the
        # intended index while the caller still owns the edit lock. An orphan
        # revision file alone is not a commit.
        if observer is not None:
            try:
                published = _read(path) == data
            except Exception:
                published = False
            if published:
                observer(path, data)
        raise
    if observer is not None:
        observer(path, data)

@contextmanager
def edit_lock(project):
    from contextlib import ExitStack
    from .process_locks import owned_lock, LockUnavailable
    from .project_access import validate_edit
    validate_edit(project)
    folder=Path(project)/'editing'
    folder.mkdir(parents=True,exist_ok=True)
    with ExitStack() as stack:
        try:
            stack.enter_context(owned_lock(folder/'.lock'))
        except LockUnavailable as exc:
            raise ValueError('检测到无法确认的旧编辑锁，已保留工程数据；请关闭旧版程序后检查锁来源。') from exc
        except OSError as exc:
            raise ValueError('工程正在保存，或当前无法取得写入权限，请稍后重试。') from exc
        yield

def current(project, *, expected_index=None):
    from . import editor_checkpoint as cp, live_owner as live
    owner=live.retained(project)
    if owner is not None and expected_index is None:
        return owner.snapshot_view()
    record=cp.read(project)
    if record is None or (expected_index is not None and record.index!=expected_index):
        return None
    return live.view(live.record_document(project,record),record.index,cp.export_fields(project,record.index))

def _revision_file(folder,revision):
    if (not isinstance(revision,str) or not revision or revision in ('.','..')
            or any(c in revision for c in ('/','\\',':','\0'))):
        raise ValueError('无效的编辑版本标识。')
    return folder/'revisions'/f'{revision}.json'

def _read_revision(folder,revision):
    """Read one immutable body; no head, history, or view migration."""
    result=_read(_revision_file(folder,revision))
    if not isinstance(result,dict) or result.get('revision')!=revision:
        raise ValueError('编辑版本文件与历史索引不匹配。')
    return result

def _load_revision(folder,index):
    # Legacy readers may still request an archived v1 body. A v2 working head
    # instead names an inline sealed body; its content ID is not a filename.
    from . import editor_checkpoint as cp, live_owner as live
    project=Path(folder).parent
    record=cp.read(project)
    if record is not None and not record.legacy and record.index['revision']==index['revision']:
        return live.view(live.record_document(project,record),index,cp.export_fields(project,record.index))
    return _revision_view(folder,index,_read_revision(folder,index['revision']))

def _revision_view(folder,index,result):
    from .legacy_fixed_points import retire_fixed_points
    result=retire_fixed_points(result,in_place=True)  # caller owns the body
    result['range']=index.get('range',result.get('range'))
    result['can_undo']=bool(index.get('undo'))
    result['can_redo']=bool(index.get('redo'))
    result['dirty']=index.get('exported_revision')!=result['revision']
    result['score_stale']=index.get('score_revision')!=result['revision']
    result['exported_revision']=index.get('exported_revision')
    # Read-only migration: no existing revision or model cache is rewritten.
    if 'rhythm_initial' not in result.get('tempo',{}):
        initial=None;source=None
        export=result.get('source_export')
        if export:
            from .project_access import export_folder, reference
            source_export=export_folder(folder.parent,export)
            candidate=reference(folder.parent,source_export/'tempo_map.json') if source_export else None
            if candidate is not None and candidate.is_file():
                try:
                    initial=_read(candidate);TempoMap(deepcopy(initial))
                    source='model_export' if initial.get('source') in ('beat_this','cubase_local_reconstruction','duet_first_pass') and not initial.get('opening_policy',{}).get('protected') else 'committed_export'
                except (OSError,ValueError,KeyError,TypeError):initial=None
        if initial is None:
            first=(index.get('undo') or [result['revision']])[0]
            try:
                original=_read(folder/'revisions'/f'{first}.json')
                if not original.get('manual'):
                    initial=deepcopy(original['tempo']);source='initial_revision'
            except (OSError,ValueError,KeyError,TypeError):pass
        if initial is not None:
            initial.pop('rhythm_initial',None)
            result['tempo']['rhythm_initial']={'available':True,'source':source,'tempo':initial}
    return result

def _finite(value, minimum=0):
    if isinstance(value,bool):raise ValueError('请输入有效数值。')
    try:value=float(value)
    except (TypeError,ValueError):raise ValueError('请输入有效数值。') from None
    if not math.isfinite(value) or value<minimum:
        raise ValueError('时间和数值必须有限且不小于允许的最小值。')
    return value

def validate(doc):
    from .document_editor import validate as native_validate
    doc['duration'] = native_validate(doc)

def _validate_document(doc):
    clock=TempoMap(deepcopy(doc['tempo']))
    if 'pulse_points' in doc['tempo']:
        from .rhythm_pulses import pulse_state
        pulse_state(doc['tempo'])
    from .score_boundary import confirmed_end_beat
    end_beat = confirmed_end_beat(clock)
    duration=_finite(doc.get('duration',clock.seconds[-1]))
    score_start=doc['tempo'].get('score_start_seconds')
    if score_start is not None and not doc['tempo'].get('opening_policy',{}).get('score_only') and any(n['start']<score_start-1e-8 for n in doc['notes']):
        raise ValueError('首完整小节前存在音符；请改用弱起/前奏或调整开头位置，未丢弃任何音符')
    for group in ('notes','controls'):
        ids=[e['id'] for e in doc[group]]
        if len(ids)!=len(set(ids)):
            raise ValueError('事件标识不能重复。')
    for note in doc['notes']:
        a,b=_finite(note['start']),_finite(note['end'])
        if end_beat is not None and a >= clock.data['score_end']['seconds'] - 1e-8:
            raise ValueError('谱面终点处或之后仍有音符起音，请后移或取消终点；不会隐藏后续音符。')
        if b<=a or type(note['pitch']) is not int or not 0<=note['pitch']<=127:
            raise ValueError('音符需要有效音高与正时长。')
        if type(note['velocity']) is not int or not 1<=note['velocity']<=127:
            raise ValueError('音符力度应为 1–127。')
    for control in doc['controls']:
        _finite(control['seconds'])
        if any(type(control[k]) is not int or not 0<=control[k]<=127 for k in ('number','value')):
            raise ValueError('控制器编号和数值应为 0–127。')
    for event in doc['notes']+doc['controls']:
        if type(event.get('channel',0)) is not int or not 0<=event.get('channel',0)<16:
            raise ValueError('MIDI 通道应为 0–15。')
    extent=max(max((n['end'] for n in doc['notes']),default=0),
               max((c['seconds'] for c in doc['controls']),default=0))
    if extent > duration + 1e-6:
        raise ValueError('编辑事件超出曲目终点；请缩短音符或控制器位置。')
    if duration > clock.seconds[-1]+1e-6:
        raise ValueError('速度图未覆盖完整曲目，请延长速度图后再使用。')
    doc['duration']=duration
    return clock,end_beat

def _publish_revision(project,doc,index,*,revision=None,_native_prepared=False,make_view=None):
    # Private certified path: only the native transaction adapter and native
    # bootstrap pass this flag. The one file writer below remains unchanged.
    if not _native_prepared:
        validate(doc)
        from .document_editor import release
        if not release(project):
            raise ValueError('保留了未保存的内存编辑；旧版发布器不能覆盖它。')
    return _write_validated_revision(project,doc,index,revision or uuid4().hex,make_view=make_view)

def _write_validated_revision(project,doc,index,revision,*,make_view=None):
    """Shared legacy writer; callers own the edit lock and a validated candidate."""
    from . import editor_checkpoint as cp
    old = cp.read(project)
    if old is not None and not old.legacy:
        raise cp.CheckpointError('Use the working checkpoint writer or export to a new legacy directory; '
                                 'the legacy publisher cannot replace a checkpoint bundle.')
    doc['revision']=revision
    for key in ('can_undo','can_redo','dirty','score_stale','exported_revision','path',
                'preview_path','preview_revision','preview_pending','preview_error'):
        doc.pop(key,None)
    folder=Path(project)/'editing'
    path=_revision_file(folder,revision)
    # Publishers hold the project writer lock. A revision name is never reused,
    # even if an earlier failed transaction left an orphan under that name.
    if path.exists() or path.is_symlink():
        raise ValueError('编辑版本已存在；未改写历史。')
    _save(path,doc)
    index['revision']=revision
    # Same view as current(), but no JSON round-trip under the writer lock.
    # Build it before changing the head. A failure can leave an unreferenced
    # revision, never a partially applied history operation.
    view=(_revision_view if make_view is None else make_view)(folder,index,doc)
    _save(folder/'current.json',index)
    return view

def _bound_inferred_cc64_tail(bundle, duration):
    """Bound only a model sustain span that crosses the real source EOF."""
    pedals=bundle.get('pedals')
    if (not isinstance(pedals,list) or type(duration) not in (int,float)
            or not math.isfinite(duration) or duration<=0):
        return bundle

    def bounded(span):
        if not isinstance(span,dict) or span.get('number')!=64:
            return span
        start,end=span.get('start'),span.get('end')
        # Invalid times and events starting beyond the source remain errors.
        if (type(start) in (int,float) and type(end) in (int,float)
                and 0<=start<duration<end and math.isfinite(end)):
            return dict(span,end=duration)
        return span

    return dict(bundle,pedals=[bounded(span) for span in pedals])


def ensure(project,state=None):
    """Import once from exact inference seconds, never from a quantized display."""
    from .pipeline import _analysis_keys,_notes_bundle,_committed_tempo
    project=Path(project)
    existing=current(project)
    if existing is not None:
        return existing

    with edit_lock(project):
        if current(project) is not None:return current(project)
        state=state or _read(project/'project.json')
        if 'piano' not in state.get('instruments',[]):return None
        key=(state.get('analysis_keys') or _analysis_keys(state.get('config',{})))['piano']
        bundle=_notes_bundle(project,state,'piano',key)
        if bundle is None:raise ValueError('缺少原始秒事件缓存，无法建立可编辑版本。')
        from .document_editor import initialize
        controls=[];other_events=[];imported={}
        selected=state.get('selected_midi') if state.get('source_kind')=='midi' else None
        from . import project_access
        if project_access.approved(project) is not None:
            selected=project_access.reference(project,selected)
        if selected and Path(selected).is_file():
            import mido
            seconds=0.
            for message in mido.MidiFile(selected):
                seconds+=message.time
                if message.type=='control_change':
                    controls.append({'seconds':seconds,'number':message.control,'value':message.value,'channel':message.channel})
                elif message.type not in ('note_on','note_off','set_tempo','time_signature','track_name','end_of_track'):
                    payload=message.dict();payload.pop('time',None)
                    other_events.append({'seconds':seconds,'message':payload})
            imported['midi_controls']=controls
        duration=state['duration']
        if state.get('source_kind') in (None,'audio'):
            # TransKun can release past EOF in padded audio. Match the source
            # boundary here, never a shorter score endpoint or imported MIDI.
            bundle=_bound_inferred_cc64_tail(bundle,duration)
        source_export=state.get('latest_export')
        if project_access.approved(project) is not None:
            source_export=str(project_access.export_folder(project,source_export) or '')
        doc=initialize(bundle=bundle,source_key=key,source_export=source_export,
                       duration=duration,tempo=_committed_tempo(project,state),
                       other_events=other_events,**imported)
        result=_publish_revision(project,doc,{'undo':[],'redo':[],'range':doc['range']},_native_prepared=True)
        # Initial document is exactly the committed performance; its score is not stale.
        index=_read(project/'editing'/'current.json');index['exported_revision']=result['revision']
        if (Path(source_export or '.')/'piano.musicxml').is_file():
            index['score_revision']=result['revision']
        _save(project/'editing'/'current.json',index)
        return current(project)

def refresh_candidate(project,state,tempo,expected_revision=None):
    """Adopt fresh inference only when no working musical edit is protected.

    Viewing, changing the loop or initializing the editor is not a manual edit.
    A genuinely edited document keeps all events/deletions/map; the fresh
    inference remains in the ordinary immutable model caches for comparison.
    """
    from .pipeline import _analysis_keys,_notes_bundle
    project=Path(project)
    with edit_lock(project):
        doc=current(project)
        if expected_revision is not None and doc and doc['revision']!=expected_revision:return doc
        if doc is None or doc.get('manual') or 'piano' not in state.get('instruments',[]):return doc
        key=(state.get('analysis_keys') or _analysis_keys(state.get('config',{})))['piano']
        bundle=_notes_bundle(project,state,'piano',key)
        if bundle is None:raise ValueError('新候选缺少秒事件缓存，保留当前工程。')
        from .document_editor import commit_locked
        path=project/'editing'/'current.json'
        index=_base_index(path,doc['revision'])
        trusted={'refresh_candidate':dict(bundle=bundle,source_key=key,tempo=tempo,duration=state['duration'],
                                          retain_midi_controls=state.get('source_kind')=='midi')}
        return commit_locked(project,path,index,{'type':'refresh_candidate'},trusted=trusted)['doc']

def _check_base(doc,base):
    if not doc or doc['revision']!=base:
        raise ValueError('工程版本已变化，请使用当前版本重新操作；旧修改未覆盖新内容。')

def _resolve_cc64_incoming(doc,result):
    """Resolve only actively edited mapped-clock targets; untouched imports survive.

    All members of a historical same-tick cluster moved together are one unit.
    Otherwise the later original time/list position wins; generated additions
    follow updates and later additions win. Retained events keep input order.
    """
    from .midi import PPQ
    clock=TempoMap(deepcopy(result['tempo']))
    def key(event):
        return event.get('channel',0),round(clock.to_beat(float(event['seconds']))*PPQ)
    old={e['id']:e for e in doc['controls']}
    final={e['id']:e for e in result['controls']}
    order={e['id']:i for i,e in enumerate(doc['controls'])}
    incoming={e['id'] for e in result['controls'] if e['number']==64 and
              (e['id'] not in old or any(e[k]!=old[e['id']][k] for k in ('seconds','value')))}
    if not incoming:return
    clusters={}
    for event in doc['controls']:
        if event['number']==64:clusters.setdefault(key(event),[]).append(event['id'])
    units={};grouped=set()
    for ids in clusters.values():
        if len(ids)>1 and all(i in incoming for i in ids) and len({key(final[i]) for i in ids})==1:
            ident=tuple(ids);rank=(0,max(float(old[i]['seconds']) for i in ids),max(order[i] for i in ids))
            for i in ids:units[i]=(ident,rank)
            grouped.update(ids)
    add_index=0
    for event in result['controls']:
        ident=event['id']
        if ident not in incoming or ident in grouped:continue
        if ident in old:rank=(0,float(old[ident]['seconds']),order[ident])
        else:
            rank=(1,add_index,0);add_index+=1
        units[ident]=((ident,),rank)
    winners={}
    for ident in incoming:
        target=key(final[ident]);unit,rank=units[ident]
        if target not in winners or rank>winners[target][1]:winners[target]=(unit,rank)
    removed=[];kept=[]
    for event in result['controls']:
        winner=winners.get(key(event)) if event['number']==64 else None
        if winner is not None and event['id'] not in winner[0]:removed.append(event['id'])
        else:kept.append(event)
    result['controls']=kept
    result['deleted_ids'].extend(i for i in removed if i not in result['deleted_ids'])

def _plain_note_add(operation):
    """The ordinary pen payload; unfamiliar shapes use the general editor."""
    if type(operation) is not dict or operation.keys() != {'type','add'} or operation['type'] != 'notes':
        return None
    rows = operation['add']
    if type(rows) is not list or len(rows) != 1 or type(rows[0]) is not dict:
        return None
    note = rows[0]
    required = {'start','end','pitch','velocity'}
    if (not required <= note.keys() <= required | {'channel'}
            or any(type(note[k]) not in (int,float) for k in ('start','end'))
            or any(type(note.get(k,0)) is not int for k in ('pitch','velocity','channel'))):
        return None
    return note

def _new_event(kind,added_index,event):
    event=deepcopy(event);event.update(id=uuid4().hex,origin='manual')
    observer = _added_event_observer.get()
    if observer is not None: observer(kind, added_index, event['id'])
    event.setdefault('channel',0)
    return event

def apply_operation(doc,operation):
    """Legacy pure reference for differential tests and older offline callers.

    Never used to prepare a publication or by the native preview adapter.
    New interactive callers use document_editor.preview/query instead.
    """
    from .legacy_fixed_points import retire_fixed_points
    # One private copy is enough: retirement only rewrites anchors/legacy pins, and `doc` is read
    # below solely for its events and revision (R16: the second whole-document copy cost ~35 ms).
    result=retire_fixed_points(doc);kind=operation.get('type')
    if kind in ('notes','controls'):
        fields={'start','end','pitch','velocity'} if kind=='notes' else {'seconds','value'}
        events={e['id']:e for e in result[kind]}
        for ident in operation.get('delete',[]):
            if ident not in events:raise ValueError('所选事件不存在。')
            if kind=='controls' and events[ident]['number']!=64:raise ValueError('仅开放 CC64 编辑。')
            del events[ident]
            result['deleted_ids'].append(ident)
        for update in operation.get('updates',[]):
            event=events.get(update.get('id'))
            if event is None:raise ValueError('所选事件不存在。')
            if kind=='controls' and event['number']!=64:raise ValueError('仅开放 CC64 编辑。')
            if set(update)-fields-{'id'}:raise ValueError('不支持的事件编辑字段。')
            event.update({k:v for k,v in update.items() if k!='id'});event['origin']='manual'
        # A batch may contain unchanged members, or several assignments that
        # return one event to its starting value. Preserve their provenance.
        # Full validation below still runs, including for equal-looking values.
        originals={e['id']:e for e in doc[kind]}
        for ident,event in events.items():
            original=originals[ident]
            if ({k:v for k,v in event.items() if k!='origin'} ==
                    {k:v for k,v in original.items() if k!='origin'}):
                if 'origin' in original:event['origin']=original['origin']
                else:event.pop('origin',None)
        for added_index, event in enumerate(operation.get('add',[])):
            event=_new_event(kind,added_index,event)
            if kind=='controls':
                event.setdefault('number',64)
                if event['number']!=64:raise ValueError('仅开放 CC64 编辑。')
            events[event['id']]=event
        result[kind]=list(events.values())
    elif kind=='tempo':
        from .tempo_edit import apply_tempo_operation
        payload={k:v for k,v in operation.items() if k not in ('type','action')}
        payload['type']=operation['action']
        changed=apply_tempo_operation(result['tempo'],payload,fixed_anchors=result['anchors'])
        result['tempo']=changed['tempo'];result['anchors']=changed['fixed_anchors']
        result['summary']=changed['summary'];result['edit_warnings']=changed.get('warnings',[])
    elif kind=='rhythm_intent':
        from .rhythm_intent import apply_intent
        intent=deepcopy(operation)
        if intent.get('action')=='divide_beats':
            # Never trust caller-supplied note evidence; use this exact revision.
            intent['_note_onsets']=[n['start'] for n in result['notes']]
        changed=apply_intent(result['tempo'],intent,result['anchors'])
        result['tempo']=changed['tempo'];result['anchors']=changed['fixed_anchors']
        result['summary']=changed['summary'];result['edit_warnings']=changed['warnings']
        result['edit_impact']=changed['impact']
    elif kind=='rebar':
        from .rebar_edit import apply_rebar
        changed=apply_rebar(result['tempo'],operation,result['anchors'],revision=doc['revision'])
        result['tempo']=changed['tempo'];result['anchors']=changed['fixed_anchors']
        result['summary']=changed['summary'];result['edit_warnings']=changed.get('warnings',[])
        result['edit_impact']=changed['impact']
    elif kind=='rhythm_structure':
        from .rhythm_structure_draft import create_draft, apply_edit, commit_draft
        operations=operation.get('operations')
        if not isinstance(operations,list) or not 1<=len(operations)<=512:
            raise ValueError('结构草稿需要1–512项明确操作。')
        if not any(isinstance(item,dict) and item.get('type')!='preserve_positions' for item in operations):
            raise ValueError('尚未修改拍位或拍号；锁定开关仅作用于当前草稿。')
        enabled=operation.get('preserve_unedited_positions',True)
        if type(enabled) is not bool:raise ValueError('结构锁定设置必须明确。')
        draft=create_draft(result['tempo'],doc['revision'],scope=operation.get('scope'),
                           fixed_anchors=result['anchors'],preserve_unedited_positions=enabled)
        source_sha=draft['source_sha256']
        for item in operations:
            if not isinstance(item,dict):raise ValueError('结构操作必须是对象。')
            draft=apply_edit(draft,item)
        changed=commit_draft(draft,expected_revision=doc['revision'],expected_source_sha256=source_sha)
        result['tempo']=changed['tempo'];result['anchors']=changed['fixed_anchors']
        result['summary']=f'已应用 {len(operations)} 项拍位／拍号修改；原演奏秒数不变，可一次撤销。'
        result['edit_warnings']=[]
    elif kind=='bar_phase_candidate':
        # A confirmed local decoder may alter grouping only. It never receives
        # authority to replace event seconds or the effective clock.
        result['tempo']['time_signatures']=deepcopy(operation['time_signatures'])
        result['tempo']['source']='manual_editor'
        result['tempo']['meter_policy']={'mode':'confirmed_local_bar_decode','protected':True}
        result['tempo']['local_bar_decode']=deepcopy(operation.get('analysis',{}))
        result['summary']='已更新局部分组；速度、音符和踏板实际位置不变。'
    elif kind in ('beat_flag_lock','anchors'):
        raise ValueError('内部固定点功能已移除；请直接编辑拍点或使用选区两端。')
    elif kind=='adopt_rhythm_candidate':
        from .rhythm_candidate import replacement
        result=replacement(doc,operation['tempo'],operation['identity'])
    elif kind=='replace_tempo':
        result['tempo']=deepcopy(operation['tempo'])
    else:
        raise ValueError('未知工程编辑操作。')
    result['operation']=deepcopy(operation);result['manual']=True
    _validate_document(result)
    if kind=='controls':_resolve_cc64_incoming(doc,result)
    return result

def _base_index(path,base):
    from . import editor_checkpoint as cp, live_owner as live
    project=Path(path).parent.parent
    entry=live.retained(project)
    if entry is not None:
        with entry.lock:
            _check_base(entry.head,base)
            return deepcopy(entry.head)
    record=cp.read(project)
    index=record.index if record else None
    _check_base(index,base)
    return index

def edit(project,base,operation,*,expected=None,request=None):
    from .document_editor import mutate
    if isinstance(operation,dict) and operation.get('type') in ('history','range','draft_apply'):
        raise ValueError('请使用相应的历史、选区或草稿入口。')
    return mutate(project,base,operation,expected=expected,request=request)['doc']

def navigate(project,base,direction,*,expected=None,request=None):
    from .document_editor import mutate
    return mutate(project,base,dict(type='history',direction=direction),expected=expected,request=request)['doc']

def set_range(project,base,selection,*,expected=None,request=None):
    from .document_editor import mutate
    return mutate(project,base,dict(selection,type='range'),expected=expected,request=request)['doc']

def mark_exported(project,revision,export):
    """Compatibility export marker; caller holds edit_lock. Never touches a checkpoint."""
    project=Path(project)
    doc=current(project)
    if not doc or doc['revision']!=revision:return False
    state=_read(project/'project.json')
    state['editor_exported_revision']=revision
    if (Path(export)/'piano.musicxml').is_file():state['editor_score_revision']=revision
    _save(project/'project.json',state)
    from .live_owner import retained
    entry=retained(project)
    if entry is not None:entry.refresh_marks()
    return True

def pedal_intervals(controls,duration):
    held={};result=[]
    for event in sorted(controls,key=lambda e:e['seconds']):
        if event['number'] not in (64,66,67):continue
        key=(event.get('channel',0),event['number'])
        if event['value']>=64:held.setdefault(key,event['seconds'])
        elif key in held:
            start=held.pop(key)
            if event['seconds']>start:result.append({'number':key[1],'channel':key[0],'start':start,'end':event['seconds']})
    for (channel,number),start in held.items():
        if duration>start:result.append({'number':number,'channel':channel,'start':start,'end':duration})
    return result

def preview_identity_path(project,doc):
    """Versioned identity of the MIDI writer's inputs, not the current head."""
    from hashlib import sha256
    import json
    # Include the full clock and all event payloads, in event order. Do not
    # assume a projection of tempo metadata is enough to identify MIDI bytes.
    inputs=(doc['notes'],doc['tempo'],doc['controls'],doc.get('other_events'))
    # Keep input ordering, including in mappings. This is a conservative key
    # over the writer inputs, not a claim about which fields are audible.
    payload=json.dumps(inputs,separators=(',',':'),allow_nan=False)
    return Path(project)/'editing'/'previews'/f'v3-{sha256(payload.encode()).hexdigest()}.mid'

def preview_path(project,doc,*,prepared_path=None,_owned=False):
    from tempfile import TemporaryDirectory
    from .midi import write_midi
    # Internal prepared_path is paired with the version-checked document.
    path=Path(prepared_path) if prepared_path is not None else preview_identity_path(project,doc)
    if path.is_file():
        return path
    path.parent.mkdir(parents=True,exist_ok=True)
    # No edit lock or shared temporary filename. Keep the final basename for
    # the writer, even when multiple windows build the same immutable asset.
    with TemporaryDirectory(prefix='.preview-',dir=path.parent) as directory:
        staged=Path(directory)/path.name
        inputs=(doc['notes'],doc['tempo'],doc['controls'],doc.get('other_events'))
        # Only the deferred worker may hand off a private, freshly parsed body.
        notes,tempo,controls,events=inputs if _owned else deepcopy(inputs)
        write_midi(staged,notes,tempo,controls=controls,events=events)
        try:
            os.link(staged,path)  # publish-if-absent; never change an existing asset's key
        except FileExistsError:
            if not path.is_file():
                raise
        except OSError:
            if not path.is_file():
                # Windows rename does not overwrite. POSIX rename does; fail
                # the derived preview rather than race a player or a parser.
                if os.name!='nt':
                    raise
                try:
                    os.rename(staged,path)
                except OSError:
                    if not path.is_file():
                        raise
    return path
