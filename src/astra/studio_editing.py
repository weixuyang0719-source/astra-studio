"""Qt bridge helpers for the versioned working document."""
from pathlib import Path
from PySide6.QtCore import Slot
from . import editing
from .detected_beats import DetectedBeatCache

def range_grid_view(tempo):
    """Canonical grid origins for gesture painting; never stored in a document."""
    from .document_editor import timing_projection
    fields = ('anchors','time_signatures','pickup_beats','first_full_bar_beat','short_bar_annotations','opening_policy')
    return timing_projection('editor_grid_sections', {'tempo': {k:tempo[k] for k in fields if k in tempo}})

class EditingBridge:
    def _require_no_structure_draft(self):
        """Compatibility hook: preserve legacy work, never gate the new timeline.

        The retired draft is not applied or discarded. Snapshot before a newer
        revision makes it stale, or a navigation path clears its in-memory slot.
        A failed preservation is the only reason to stop the requested action.
        """
        import hashlib
        import json
        project=getattr(self,'_project',None)
        session=getattr(self,'_structure_session',None)
        if not (project and session and session.pending(project['path'])):
            return
        active=getattr(session,'_active',None)
        if not isinstance(active,dict):
            raise ValueError('旧草稿无法读取，未清除或覆盖；请先保存旧草稿。')
        content=json.dumps(dict(format='astra-legacy-structure-recovery-v1',
                                applied=False,session=active),ensure_ascii=False,
                           sort_keys=True,default=str,indent=2)
        digest=hashlib.sha256(content.encode('utf-8')).hexdigest()
        folder=Path(project['path'])/'editing'/'legacy-draft-recovery'
        folder.mkdir(parents=True,exist_ok=True)
        path=folder/f'{digest}.json'
        if not path.exists():
            # Exclusive creation prevents replacing any prior recovery artifact.
            with path.open('x',encoding='utf-8') as stream:
                stream.write(content)
        elif path.read_text(encoding='utf-8')!=content:
            raise ValueError('旧草稿恢复副本校验失败，原草稿保持；尚未执行修改。')
        self._review_notice={'token':digest,'text':'旧版未应用节拍草稿已保留为恢复副本，未自动应用或丢弃。新编辑使用当前内存版本。'}

    def _bar_phase_preview_start(self,data):
        from uuid import uuid4
        from PySide6.QtCore import QThreadPool,Qt
        from .studio_bridge import _InputInspectionTask,_InputInspectionSignal
        from . import bar_phase_edit, pulse_edit, acoustic_snap_edit,rhythm_candidate
        if not self._project:raise ValueError('请先打开工程。')
        if self.jobs.running or self._gpu_request:raise ValueError('请等待当前分析／导出结束后再复核节拍。')
        if getattr(self,'_bar_phase_busy',False):raise ValueError('局部节拍复核仍在运行，请稍后重试。')
        kind=data.get('kind','bar_phase')
        if kind=='rhythm_candidate':
            from .project_access import approved
            if approved(self._project['path']) is not None:
                raise ValueError('外部工程可手工编辑节拍；重新分析音频请从「新建转谱」选择原曲。')
        services={'bar_phase':bar_phase_edit,'pulse_times':pulse_edit,'acoustic_snap':acoustic_snap_edit,
                  'rhythm_candidate':rhythm_candidate}
        if kind=='pulse_quality':
            from . import local_pulse_review_service
            services[kind]=local_pulse_review_service
        if kind not in services:raise ValueError('未知缓存节拍复核方式。')
        service=services[kind]
        from .document_editor import edit_ticket
        ticket=edit_ticket(self._project['path'],data.get('base_revision'))
        prepared=service.prepare(self._project['path'],data.get('base_revision'),data)
        token=uuid4().hex
        self._bar_phase_candidate=None
        self._bar_phase_request={'token':token,'prepared':prepared,'status':'running','kind':kind,'editor_ticket':ticket,
                                 'generation':self._edit_identity()}
        if not hasattr(self,'_bar_phase_signal'):
            self._bar_phase_signal=_InputInspectionSignal(self)
            self._bar_phase_signal.done.connect(self._bar_phase_done,Qt.ConnectionType.QueuedConnection)
        self._bar_phase_busy=True
        getattr(self,'_publish',lambda:None)()
        QThreadPool.globalInstance().start(_InputInspectionTask(prepared,token,self._bar_phase_signal,service.run))
        return {'ok':True,'token':token,'status':'running'}

    def _bar_phase_current(self,request):
        if not self._project or str(Path(self._project['path']).resolve())!=request['prepared']['project']:
            return False
        from .document_editor import edit_ticket
        if request['generation'] is not self._edit_identity():return False
        try:
            return request['editor_ticket']==edit_ticket(self._project['path'],request['prepared']['base_revision'])
        except (OSError,ValueError,KeyError):
            return False

    @Slot(str, object, str)
    def _bar_phase_done(self,token,result,error):
        self._bar_phase_busy=False
        request=getattr(self,'_bar_phase_request',None)
        if self._closed or not request or request['token']!=token:return
        if not self._bar_phase_current(request):
            request.update(status='stale',error='工程已变化，旧节拍候选未应用；请重新预览。')
        elif error:
            request.update(status='failed',error=error)
        else:
            self._bar_phase_candidate=result
            request.update(status='ready')
        getattr(self,'_publish',lambda:None)()

    def _bar_phase_status(self,token):
        request=getattr(self,'_bar_phase_request',None)
        if not request or token!=request['token']:
            return {'ok':True,'status':'stale','error':'此节拍预览已失效，请重新预览。'}
        if not self._bar_phase_current(request):
            return {'ok':True,'status':'stale','error':'工程已变化，请重新预览；旧候选未应用。'}
        result={'ok':True,'status':request['status']}
        if request.get('error'):result['error']=request['error']
        if request['status']=='ready' and self._bar_phase_candidate:
            fields=('candidates','summary','warnings','provenance','needs_device_choice','gpu_status') if request.get('kind')=='rhythm_candidate' else ('tempo','summary','warnings')
            if request.get('kind')=='pulse_quality':
                fields=('ranges','mode','candidate_available','summary','warnings','tempo','review','review_messages','provenance')
            result.update({k:self._bar_phase_candidate[k] for k in fields if k in self._bar_phase_candidate})
            result['candidate_token']=token
        return result

    def _apply_bar_phase_candidate(self,data):
        EditingBridge._require_no_structure_draft(self)
        if getattr(getattr(self,'jobs',None),'running',False) or getattr(self,'_gpu_request',None):
            raise ValueError('请等待当前生成结束后再采用候选；当前生效图保持。')
        request=getattr(self,'_bar_phase_request',None)
        if (not request or data.get('token')!=request['token'] or request['status']!='ready'
                or not self._bar_phase_current(request) or not self._bar_phase_candidate):
            raise ValueError('节拍候选尚未就绪或已过期，请重新预览。')
        base=request['prepared']['base_revision']
        if data.get('base_revision')!=base:raise ValueError('工程版本不匹配，请重新预览。')
        if request.get('kind')=='rhythm_candidate':
            from .rhythm_candidate import adopt
            adopt(request['prepared'],self._bar_phase_candidate,data.get('candidate_id'))
        elif request.get('kind')=='pulse_quality':
            if self._bar_phase_candidate.get('candidate_available') is not True:
                raise ValueError('当前复核未得到足够可靠的拍点候选；可继续手工划拍，当前图未改变。')
            from .local_pulse_review_service import adopt
            adopt(request['prepared'],self._bar_phase_candidate)
        else:
            from .document_editor import commit_reviewed
            commit_reviewed(self._project['path'],base,self._bar_phase_candidate['operation'],
                            expected=request['editor_ticket'])
        request['status']='applied';self._bar_phase_candidate=None
        self._refresh_editor(select=True)

    def _refresh_editor(self,*,select=False,prepared=None,external_setup=False):
        from . import live_owner as live, editor_checkpoint as cp, editor_recovery as recovery, project_access
        if not self._project or 'piano' not in self._project.get('instruments',[]):return
        self._edit_identity()
        project=self._project['path']
        was_editor=self._edit_preview_selected()
        resume=self._midi_checkpoint() if was_editor else None
        if not hasattr(self,'_detected_beats_cache'):
            self._detected_beats_cache=DetectedBeatCache()
        self._project['detected_beats']=self._detected_beats_cache.snapshot(project)
        try:
            entry=live.retained(project)
            if entry is None:
                if cp.read(project) is None:
                    if recovery.registered(project):
                        raise cp.CheckpointError('找不到正式项目文件。可查看恢复副本。')
                    if project_access.approved(project) is not None and not external_setup:
                        self._project.update(editor_setup_required=True,
                            editor_unavailable='首次点击编辑后才生成工作记录，原始音频和导出不会改动。')
                        return
                    self._project.pop('editor_setup_required',None)
                    if editing.ensure(project) is None:return
                entry=live.owner(project)
            entry.refresh_marks()
            frozen=entry.freeze()
            doc=entry.snapshot_view(frozen)
            doc['range_grid_sections']=range_grid_view(doc['tempo'])
        except (OSError,KeyError,ValueError) as exc:
            self._project['editor_unavailable']=str(exc)
            self._project.pop('editor',None)
            if was_editor:self._clear_midi()
            return  # the project-open worker offers only verified, project-bound copies
        path=self._editor_uri
        self._editor_native_stamp=tuple(frozen.head['stamp'])
        doc.update(path=path,preview_path=path,preview_revision=doc['revision'],preview_pending=False)
        self._editor_preview_clock=None
        doc.update(metronome=bool(getattr(self,'_editor_metronome',False)),
                   audition_together=bool(getattr(self,'_audition_together',False)))
        stamp=getattr(self,'_edit_stamp',None)
        if stamp:stamp(doc)
        self._project['editor']=doc
        self._project['tempo']=doc['tempo']
        self._project.pop('editor_unavailable',None)
        self._project.pop('editor_setup_required',None)
        if select or was_editor:
            position=resume['position'] if resume else self._midi_transport.get('position',0.)
            self._select_midi(path,resume=(position,bool(resume and resume['playing'])))
        self._sync_editor_transport(editor_swap=True) if select else self._sync_editor_transport()

    def _sync_editor_transport(self,*,range_only=False,editor_swap=False):
        doc=(self._project or {}).get('editor') or {}
        path=doc.get('preview_path')
        backend=self._midi_backend
        if not backend or not path or (self._midi_file or {}).get('path')!=path:
            return False
        selection=getattr(self,'_audition_range',None) or doc.get('range') or {}
        clock=getattr(self,'_editor_preview_clock',None)
        tempo=None
        if not range_only and clock and clock[0]==path:
            # While a revision is pending, the last good MIDI owns its clock.
            _,tempo,duration=clock
            backend._duration=max(backend.duration,duration)
        backend.set_loop(selection.get('start',0),selection.get('end',0),selection.get('loop',False))
        if not range_only:
            backend.set_rate(getattr(self,'_audition_rate',1.0))
            tempo=doc['tempo'] if path==getattr(self,'_editor_uri',None) else tempo
            if tempo:backend.set_metronome(tempo,getattr(self,'_editor_metronome',False))
        return True

    def _editor_action(self,action,data):
        if not self._project:raise ValueError('请先打开钢琴工程。')
        project=self._project['path'];base=data.get('base_revision')
        changes_revision=action in ('undoProject','redoProject') or (action=='editProject' and data.get('operation',{}).get('type')!='range')
        if changes_revision:EditingBridge._require_no_structure_draft(self)
        # Update Score binds the live owner below, not a possibly stale saved Notes image.
        if action!='exportEditor':
            doc=editing.ensure(project)
            editing._check_base(doc,base)
        if action=='editProject':
            operation=data.get('operation',{})
            if operation.get('type')=='range':
                editing.set_range(project,base,operation)
            else:
                editing.edit(project,base,operation)
            self._refresh_editor(select=operation.get('type')!='range')
        elif action in ('undoProject','redoProject'):
            editing.navigate(project,base,'undo' if action=='undoProject' else 'redo')
            self._refresh_editor(select=True)
        elif action=='setEditorRange':
            editing.set_range(project,base,data.get('range',data))
            self._audition_range=None
            self._refresh_editor()
        elif action=='exportEditor':
            if self.jobs.running or self._gpu_request or self._notes_score_save_in_progress():
                raise ValueError('已有后台任务，请稍后再生成。')
            mode=data.get('mode','all');purpose=data.get('purpose','mapped')
            if mode not in ('all','midi','score') or purpose not in ('mapped','target_grid','seconds_120'):
                raise ValueError('未知导出用途。')
            request=self._request_for('reexport')
            request['options']={'mode':mode,'beat_scale':1.0,'bpm':None,'tempo_path':None}
            request['editor_revision']=base
            if mode in ('all','score'):
                request['_notes_score_update']=True
            request['editor_export_options']={'purpose':purpose,'include_time_signatures':bool(data.get('include_time_signatures',True))}
            self._start_with_gpu(request)
        else:raise ValueError('未知编辑命令。')

    def _editor_audio_loop(self):
        if getattr(self,'_shared_sync',lambda:False)():return
        selection=getattr(self,'_audition_range',None) or ((self._project or {}).get('editor') or {}).get('range') or {}
        left,right=selection.get('start',0),selection.get('end',0)
        position=self._media.position()/1000
        if (selection.get('loop') and self._media.isPlaying() and
                ((left<right and position>=right) or (right<=position<left))):
            self._media.setPosition(round(left*1000))
        if (getattr(self,'_audition_together',False) and self._media.isPlaying() and self._midi_backend
                and abs(self._midi_backend.position-self._media.position()/1000)>.15):
            self._midi_backend.seek(self._media.position()/1000)

    def _set_audition_rate(self,value):
        rate=float(value)
        if not .25<=rate<=2:raise ValueError('试听速度应为 0.25–2 倍。')
        if rate!=1:getattr(self,'_shared_leave',lambda *a,**k:None)('non-1x rate')
        self._audition_rate=rate
        self._media.setPlaybackRate(rate)
        if self._midi_backend:self._midi_backend.set_rate(rate)

    def _toggle_together(self):
        getattr(self,'_shared_leave',lambda *a,**k:None)('joint MIDI audition uses legacy')
        if not self._midi_backend:raise ValueError('请先进入编辑版本。')
        if not self._audio:raise ValueError('此工程没有可叠加试听的音频。')
        playing=self._midi_backend.playing and self._media.isPlaying()
        self._audition_together=not playing
        if playing:
            self._midi_backend.pause();self._media.pause()
        else:
            position=self._midi_backend.position
            self._midi_backend.pause()
            self._media.pause()
            self._select_audio('original',preserve=False)
            self._pending_audio=(round(position*1000),True)
            self._resume_audio()
        if self._project and self._project.get('editor'):
            self._project['editor']['audition_together']=self._audition_together

    def _set_metronome(self,enabled):
        self._editor_metronome=bool(enabled)
        if self._project and self._project.get('editor'):
            self._project['editor']['metronome']=self._editor_metronome
        self._sync_editor_transport()

