"""Navigation checkpoints, not another transport or playback authorization."""
from math import isfinite
from . import vst_saved_restore

_PAGES = ('tracks','notes','score','files')


def _seconds(value):
    return value if type(value) in (int,float) and isfinite(value) and value >= 0 else None


def _current(studio, data):
    p = studio._project
    return (p is not None and p.get('path') == data.get('project')
            and data.get('fromPage') in _PAGES and data.get('toPage') in _PAGES
            and data['fromPage'] != data['toPage'] and type(data.get('serial')) is int
            and 0 < data['serial'] < 2**53)


def _state(studio):
    found = getattr(studio, '_transport_pages', None)
    return found if found is not None and found['project'] is studio._project else None


def _midi_path(studio):
    return (studio._midi_file or {}).get('path')


def _notes(studio, path):
    if not path or path != _midi_path(studio):
        return None, False
    r = studio._vst
    sw = studio._vst_pending_transport()
    choice=sw.get('choice') if sw else None
    bound=choice is not None and choice[0].uri == path and studio._vst_matches(choice[0])
    queued=bool(bound and sw['playing'])
    if bound and 'position' in sw['explicit']:
        return sw['position'], queued
    if (r is not None and not getattr(studio,'_score_transport',None)
            and studio._vst_matches(studio._vst_binding) and studio._vst_binding.uri == path):
        return r.requested_position if getattr(r,'_requested_seek',None) is not None or r.ready else None, r.wants_play or queued
    if studio._mem_goal is not None:
        view, ended = studio._memory_view()
        attempt=vst_saved_restore.lookup(studio)
        queued=queued or bool(attempt and attempt.play_requested)
        return view['position'], queued or bool(studio._mem_goal.playing and not ended)
    intent = studio._output_intent()
    return (_seconds(studio._midi_transport.get('position')),intent.playing) if intent.audition == 'midi' else (None,False)


def _audio(studio):
    intent = studio._output_intent()
    vst_active = studio._vst_active() if hasattr(studio, '_vst_active') else studio._vst
    if (vst_active or studio._mem_goal is not None) or intent.audition != 'audio':
        return None, False
    deferred = getattr(studio,'_deferred_delta',None) or {}
    if 'position' in deferred:
        return _seconds(deferred['position']),bool(deferred.get('playing',intent.playing))
    handoff = studio._navigation_owner()
    if handoff is not None:
        s = handoff.owner.snapshot()
        if s.ready and not s.error:
            target = _seconds(getattr(s,'pending_target',None))
            return (target if target is not None else _seconds(s.position)),handoff.intent.playing
    return _seconds(intent.position),intent.playing


def _save_score(studio, state, scope):
    if scope is None or not studio._vst_matches(scope['binding']):
        return
    r = scope['route']
    revision = scope['revision']
    if r is not studio._vst or revision is None or scope['bridge'] is None:
        return
    pending = getattr(r,'_score_seek',None)
    value = (pending[1] if pending and pending[0] == r.wanted else
             r.requested_position if r.applied is not None and r.applied == r.wanted else None)
    value = _seconds(value)
    if value is not None:
        state['score']=(scope['binding'],scope['bridge'],revision,value)


def navigate(studio, data):
    if getattr(studio,'_output_shutdown',False) or not _current(studio,data):
        return
    found = _state(studio)
    if found is None:
        found = studio._transport_pages = dict(project=studio._project, positions={}, notes=None,
                                              score=None, step=None, consumed=True, serial=0, wait=None, files_notes_owner=None)
    step = (data['fromPage'],data['toPage'],data.get('midiPath'),data.get('nextMidi'))
    if data['serial'] <= found['serial']:
        return
    source,dest,midi,_ = step
    found.update(step=step,consumed=False,serial=data['serial'],wait=None)
    # A FILES detour is a pause in the same renderer, not a selection.
    if source == 'files':
        if dest != 'score' and getattr(studio,'_score_transport',None):
            studio.leave_score_transport()
        return
    scope = getattr(studio,'_score_transport',None)
    if (source == 'score' and scope is not None and scope['route'] is studio._vst
            and studio._vst_matches(scope['binding'])):
        _save_score(studio,found,scope)
        if dest == 'files':
            if scope['desired'] is not None:
                scope['desired']['play'] = False
            if scope['route'].wants_play:
                scope['route'].transport('pause')
        return  # Real Score→other transports use the existing native leave slot.
    if source == 'notes':
        mark,playing=_notes(studio,midi)
        mark=_seconds(mark)
        if mark is not None:
            found['notes']=(midi,mark)
        elif found['notes'] is not None and found['notes'][0]!=midi:
            found['notes']=None
        if playing:
            # The stock silent Pause resends its earlier Goal locator: keep the
            # latest visible/explicit locator on navigation, not the Play anchor.
            delta={'playing':False}
            if studio._vst is None and studio._mem_goal is not None and mark is not None:
                delta['position']=mark
            studio._output_change(**delta)
        if dest=='files':
            found['files_notes_owner']=studio._vst or studio._mem_goal
        return
    mark,playing=_audio(studio)
    if mark is not None:found['positions'][source]=mark
    # Crossing into a different page pauses the active producer, without
    # creating a cold owner or seeding the destination from this position.
    if playing:
        studio._output_change(playing=False)
    if source=='tracks' and dest in ('notes','score') and getattr(studio,'_kept_device',None) is not None:
        studio._kept_device.set_direct_mode(False)


def early_notes(studio, path):
    found = _state(studio)
    if (found and not found['consumed'] and found['step'][1]=='notes'
            and found['step'][3]==path and found['notes'] and found['notes'][0]==path):
        return found['notes'][1]
    return None


def arrival(studio,data):
    scope=data.get('pageScope')
    if scope is None:
        return data
    if not isinstance(scope,dict) or not _current(studio,scope) or data.get('navigation') is not True or data.get('playing') is not False:
        return None
    found=_state(studio)
    step=(scope['fromPage'],scope['toPage'],scope.get('midiPath'),scope.get('nextMidi'))
    if not found or found['serial']!=scope['serial'] or found['step']!=step or found['consumed']:
        return None
    dest=step[1]
    if dest=='files':
        return None
    pos=early_notes(studio,_midi_path(studio)) if dest=='notes' and data['key']=='midi' else found['positions'].get(dest)
    notes_match=dest=='notes' and data['key']=='midi' and pos is not None
    found['consumed']=True
    if dest=='tracks':
        # Retain native Tracks pause-tail/checkpoint, but never feed it Notes.
        studio._notes_return_position=None
    if pos is None:
        # A never-visited transport starts on its own timeline, not the
        # outgoing page clock. Existing native Notes/Score handoff is retained.
        pos=0.
    data=dict(data,position=pos,separatePageClocks=True)
    if dest=='notes' and data['key']=='midi':
        if getattr(studio._vst,'_restore_notes_seek',None) is not None:
            return dict(data,preservePosition=True)
        if notes_match:
            retained=found['files_notes_owner']
            if step[0]=='files' and retained is not None and retained is (studio._vst or studio._mem_goal):
                return dict(data,preservePosition=True)
            data=dict(data,restoreNotesPosition=True,preservePosition=False)
        elif (studio._vst is not None and getattr(studio,'_vst_page_suspended',False)
              and not getattr(studio._vst,'_score_source',False)):
            # Warm mounted VST already holds Notes' own timeline position; never reset to 0.
            return dict(data,preservePosition=True)
    return data


def hold_score_for_files(studio,path):
    s=_state(studio)
    scope=getattr(studio,'_score_transport',None)
    return bool(s and s['step'][:2]==('score','files') and (studio._project or {}).get('path')==path
                and scope and scope['route'] is studio._vst and studio._vst_matches(scope['binding']))


def resume_score(studio,editor,scope):
    s=_state(studio)
    if not s or s['step'][1]!='score':
        return
    s['consumed']=True
    s['wait']=None
    relation=s['score']
    revision=editor._native_revision
    if relation is None:
        return
    binding,process,prior,position=relation
    if not studio._vst_matches(binding) or editor.process is not process or revision!=prior:
        s['score']=None
        if scope['revision'] is not None and (editor.process is not scope['bridge'] or revision!=scope['revision']):
            studio.score_transport_revision(revision)
        return
    if scope['revision'] is None and scope['desired'] is None:
        # Already owned; native snapshot builds the correctly expanded Score
        # timeline. Position is on that played timeline, not Notes seconds.
        studio._prepare_score_transport(editor,scope,False,('seconds',position))


def _same_score(studio, editor, relation):
    return bool(relation is not None and studio._vst_matches(relation[0]) and
                editor.process is relation[1] and editor._native_revision == relation[2] and
                editor._playback_page == relation[0].project['path'])


def defer_score(studio,editor):
    s=_state(studio)
    if not s or s['step'][1]!='score' or s['wait'] is not None or not _same_score(studio,editor,s['score']):
        return
    s['wait']=editor, s['serial']
    if studio._vst is None and studio._vst_pending_transport() is None:
        binding=s['score'][0]
        notes=s['notes']
        start=notes[1] if notes and notes[0]==binding.uri else 0.
        # Standard working-editor preparation, fenced to a previously verified
        # same Score. This never authorizes Play or a different audio backend.
        studio._select_live_editor(binding.uri, resume=(start,False))


def poll_score(studio):
    s=_state(studio)
    if not s or s['wait'] is None:
        return
    editor,serial=s['wait']
    if s['serial']!=serial or s['step'][1]!='score' or not _same_score(studio,editor,s['score']):
        s['wait']=None
        return
    r=studio._vst
    if r is not None and r.ready and not r.error and not r.retiring:
        studio.enter_score_transport(studio._project['path'],editor)



def builtin_score(studio, editor, project):
    """Score page with no instrument at all: MuseScore always plays a score with
    its own sound, so mount the built-in MuseScore piano and enter once ready."""
    if getattr(studio, '_vst_mount_builtin_for_score', None) is None:
        return
    s=_state(studio)
    if s and s.get('wait') is not None:
        return          # a Notes->Score handover is already restoring its instrument
    pending=getattr(studio,'_score_builtin',None)
    if pending and pending['editor'] is editor and pending['project']==project:
        return
    studio._score_builtin=dict(editor=editor,project=project,started=False)


def poll_builtin(studio):
    p=getattr(studio,'_score_builtin',None)
    if not p:
        return
    editor,project=p['editor'],p['project']
    if (studio._project or {}).get('path')!=project or getattr(editor,'_playback_page',None)!=project:
        studio._score_builtin=None   # left the Score page or switched project
        return
    r=studio._vst
    if r is not None:
        if r.ready and not r.error and not r.retiring:
            studio._score_builtin=None
            studio.enter_score_transport(project,editor)
        elif p['started'] and (r.error or r.retiring):
            studio._score_builtin=None
        return
    if p['started']:
        if studio._vst_pending_transport() is None and studio._vst_swap is None:
            studio._score_builtin=None   # admission refused or cancelled; never loop
        return
    try:
        binding=studio._vst_context()
    except (KeyError, ValueError, RuntimeError):
        return
    try:
        result=studio._vst_mount_builtin_for_score(binding)
    except (KeyError, ValueError, RuntimeError, OSError) as exc:
        studio._score_builtin=None
        studio._vst_state_warning=f'内置 MuseScore 钢琴未能连接：{exc}'
        studio.logs.emit(studio._vst_state_warning)
        studio._notify_midi()
        return
    if result is True:
        p['started']=True
    elif result is False:
        studio._score_builtin=None


def files_active(studio):
    s=_state(studio)
    return bool(s and s['step'][1]=='files')



def notes_for_score(studio, binding, fallback):
    s=_state(studio)
    if s and s['step'][1]=='score' and s['notes'] and s['notes'][0]==binding.uri:
        return s['notes'][1]
    return fallback
