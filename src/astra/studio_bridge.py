from copy import deepcopy
"""Local WebChannel boundary for the Astra workstation."""

import base64
import json
import math
import sys
import os
from time import perf_counter as monotonic  # QPC (~100 ns); time.monotonic is GetTickCount64 (15.6 ms) on this Windows
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import (QBuffer, QIODevice, QObject, QRunnable, QSize, QThreadPool, QTimer, QStandardPaths,
                            QUrl, Qt, Signal, Slot)
from PySide6.QtGui import QDesktopServices
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import QFileDialog, QMessageBox

from .preview_identity import preview_cache_key
from .analysis_hint_evidence import build_analysis_hint_evidence, export_metadata
from .midi_preview import MidiPreview
from .desktop_jobs import JobController
from .job_progress import JobProgress
from .ncm_tool import NcmTool
from .studio_score_range import ScoreRangeBridge
from .pipeline import ROOT, read_json, save_json
from .release_paths import project_output_root, project_write_path
from . import project_access

OUTPUTS=ROOT/'outputs'  # Legacy projects and non-project outputs stay in place.
PROJECTS=project_output_root()
PREFERENCES=ROOT/'.cache'/'studio.json'
INSTRUMENTS={'piano':'钢琴','guitar':'吉他'}
AUDIO_TYPES={'.wav','.mp3','.flac','.m4a','.aac','.ogg','.aif','.aiff','.opus','.wma'}
AUDIO_IMPORT_TYPES=AUDIO_TYPES|{'.ncm'}
MIDI_TYPES={'.mid','.midi'}
TEMPO_TYPES={'.json','.mid','.midi'}
OUTPUT_TYPES={'.pdf':'pdf','.musicxml':'musicxml','.xml':'musicxml','.mid':'midi','.midi':'midi'}
DEFAULT_OPTIONS={'instruments':['piano','guitar'],'mode':'all','output':str(PROJECTS),'keep_stems':True,
    'quantization':'auto','tempo_path':None,'bpm':None,'meter':'4/4','beat_scale':1.0,'piano_model':'xlance','rhythm_provider':'cubase_local',
    'score_engine':'music21','score_pedal':False,'score_chords':False,'score_voice_mode':'simple',
    'score_duet_device':'cpu'}
PERSISTENT_OPTIONS=('instruments','mode','output','keep_stems','quantization','piano_model','rhythm_provider',
                    'score_engine','score_pedal','score_chords','score_voice_mode','score_duet_device')
JOB_OPTIONS=('mode','tempo_path','bpm','meter','beat_scale')
HARMONY_DISPLAY_FIELDS=('global_key','key_regions','chords','warnings','review','method','notation_review')

def _metadata(path):
    try:
        data=read_json(path)
        return data if isinstance(data,dict) else {}
    except (OSError,ValueError,TypeError):
        return {}

def _harmony_for_display(path):
    data=_metadata(path)
    return {key:data[key] for key in HARMONY_DISPLAY_FIELDS if key in data}

def _export_mode(files):
    current=[file for file in files if not file.get('historical')]
    midi=any(file['kind']=='midi' and not file['name'].endswith(('_score.mid','_reviewed.mid')) for file in current)
    score=any(file['kind'] in ('pdf','musicxml') for file in current)
    return ('all' if midi else 'score') if score else ('midi' if midi else None)

def _local_path(value):
    if not isinstance(value,(str,Path)) or not str(value).strip():
        raise ValueError('请选择一个本地路径。')
    return Path(value).expanduser().resolve()

def _audio_file(value):
    path=_local_path(value)
    if path.is_file() and path.suffix.lower() in AUDIO_TYPES:
        return path
    raise ValueError('请选择存在且受支持的本地音频文件。')

def _number(value, low, high, message):
    if isinstance(value,bool):
        raise ValueError(message)
    number=float(value)
    if not math.isfinite(number) or not low<=number<=high:
        raise ValueError(message)
    return number

def _project_record(path):
    path=project_access.existing_path(path)
    if permit:=project_access.approved(path):
        permit.file(path/'project.json')
    state=_metadata(path/'project.json')
    latest=state.get('latest_export')
    if not isinstance(latest,str) or not latest:
        raise ValueError('此项目没有可打开的成功导出。')
    root=(path/'exports').resolve()
    export=(root/Path(latest).name).resolve()
    if not root.is_relative_to(path) or export.parent!=root or not export.is_dir():
        raise ValueError('找不到项目的最近成功导出。')
    if permit is not None:
        permit.file(export/'report.json')
    report=_metadata(export/'report.json')
    if report.get('status') not in ('complete','complete_with_warnings'):
        raise ValueError('找不到完整的导出报告。')
    return state,export,report

def _shared_project_library():
    """The existing Formal 1 library, discovered read-only by Formal 2."""
    if sys.platform!='win32':
        return None
    path=QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
    return (Path(path) if path else Path.home()/'Documents')/'Astra Studio'/'项目'

def _project_title(path,state):
    source=state.get('source')
    return Path(source).stem if isinstance(source,str) and source else path.name

class _WaveformSignal(QObject):
    done=Signal(object,float,list)

class _InputInspectionSignal(QObject):
    done=Signal(str,object,str)

class _InputInspectionTask(QRunnable):
    def __init__(self,path,token,signal,prepare):
        super().__init__()
        self.path,self.token,self.signal=path,token,signal
        self.prepare=prepare

    def run(self):
        try:
            result,error=self.prepare(self.path),''
        except Exception as exc:
            result,error=None,str(exc) or type(exc).__name__
        try:
            self.signal.done.emit(self.token,result,error)
        except RuntimeError:
            pass

class _WaveformTask(QRunnable):
    def __init__(self,key,path,signal):
        super().__init__()
        self.key,self.path,self.signal=key,path,signal

    def run(self):
        duration=0.0
        peaks=[]
        try:
            import numpy as np
            import soundfile as sf

            with sf.SoundFile(self.path) as audio:
                frames=len(audio)
                duration=frames/audio.samplerate
                bins=min(1024,frames)
                for index in range(bins):
                    count=((index+1)*frames//bins)-(index*frames//bins)
                    peak=0.0
                    while count:
                        block=audio.read(min(count,131072),dtype='float32',always_2d=True)
                        if not len(block):
                            break
                        peak=max(peak,float(np.max(np.abs(block))))
                        count-=len(block)
                    peaks.append(round(min(peak,1.0),4))
        except (OSError,ValueError,RuntimeError):
            # Qt may still play formats unsupported by the local libsndfile build.
            pass
        try:
            self.signal.done.emit(self.key,duration,peaks)
        except RuntimeError:
            pass  # The window was closed while the local preview was reading.

from .studio_editing import EditingBridge
from .studio_live_editor import EditTransactions, ACTIONS as EDIT_ACTIONS, autosave_settings
from .shared_audio_bridge import SharedAudioBridge
from .studio_ornaments import OrnamentBridge

from .studio_md_trial import MdTrialBridge

from .media_generation import MediaGeneration

from .cancelled_workflow import CancelledWorkflow

from .studio_provisional_score import ProvisionalScoreBridge


# Commands that replace the open project (or start a job that will).
LEAVE_PROJECT_ACTIONS=frozenset(('new','openRecent','chooseAudio','chooseMidi','chooseProject',
                                 'openCancelledJob','openInterruptedJob'))


class StudioBridge(CancelledWorkflow,EditTransactions,MediaGeneration,SharedAudioBridge,EditingBridge,ScoreRangeBridge,ProvisionalScoreBridge,OrnamentBridge,MdTrialBridge,QObject):
    editorTransactionChanged=Signal(str)
    externalMidiChanged=Signal(str)
    editorSaveChanged=Signal(str)
    stateChanged=Signal(str)
    logs=Signal(str)
    playerChanged=Signal(str)
    midiChanged=Signal(str)
    ncmChanged=Signal(str)

    def __init__(self,parent=None):
        super().__init__(parent)
        self._log_lines=[]
        self._job_progress=JobProgress()
        self._review_document=None
        self._review_notice=None
        self._gpu_request=None
        self._gpu_preflight=None
        self._gpu_probe=_InputInspectionSignal(self)
        self._gpu_probe.done.connect(self._gpu_checked,Qt.ConnectionType.QueuedConnection)
        self.jobs=JobController(self)
        self.jobs.log.connect(self._record_log)
        self.jobs.stage.connect(self._stage)
        self.jobs.completed.connect(self._completed)
        self.jobs.provisionalScore.connect(self._notes_score_completed)
        self.jobs.separated.connect(self._separation_completed)
        self._separation_workflow=None
        self.jobs.failed.connect(self._failed)
        self.jobs.cancelled.connect(self._cancelled)
        self.ncm_tool=NcmTool(OUTPUTS/'ncm',ROOT/'.cache'/'imports',self._open_local,self,parent)
        self.ncm_tool.changed.connect(self.ncmChanged)
        self._prefs=_metadata(PREFERENCES) or _metadata(PREFERENCES.with_name('desktop.json'))
        self._persisted_prefs=deepcopy(self._prefs)
        self._options=dict(DEFAULT_OPTIONS,instruments=list(INSTRUMENTS))
        startup_config=_metadata(ROOT/'config.json')
        if startup_config.get('rhythm_provider') in ('beat_this','cubase_local'):
            self._options['rhythm_provider']=startup_config['rhythm_provider']
        score_config=startup_config.get('score',{})
        if score_config.get('piano_engine') in ('music21','duet'):
            self._options['score_engine']=score_config['piano_engine']
        if score_config.get('duet_device') in ('cpu','cuda'):
            self._options['score_duet_device']=score_config['duet_device']
        for key in PERSISTENT_OPTIONS:
            if key in self._prefs:
                try:
                    self._options[key]=self._option(key,self._prefs[key])
                except (TypeError,ValueError,OSError):
                    pass
        # Opening a historical project may temporarily show its legacy source;
        # that is not a new global preference for the next imported song.
        self._new_rhythm_provider=self._options['rhythm_provider']
        self._new_task_options={key:(list(value) if isinstance(value,list) else value)
            for key,value in self._options.items() if key in PERSISTENT_OPTIONS}
        self._phase='等待选择音频'
        self._error=None
        self._request=None
        self._audio=None
        self._audio_inspection=_InputInspectionSignal(self)
        self._audio_inspection.done.connect(self._audio_inspected,Qt.ConnectionType.QueuedConnection)
        self._input_midi=None
        self._midi_inspection=_InputInspectionSignal(self)
        self._midi_inspection.done.connect(self._midi_inspected,Qt.ConnectionType.QueuedConnection)
        self._project=None
        self._project_source=None
        self._recent=[]
        self._approved_output_folders={_local_path(PROJECTS),_local_path(OUTPUTS),_local_path(self._options['output'])}
        self._score_preview_resources=False
        self._current_score_preview=None
        self._pdf_document=QPdfDocument(self)
        self._pdf_buffer=None
        self._pdf_path=None
        self._pdf={'pages':0,'index':0,'dataUrl':'','title':''}
        self._midi_backend=None
        self._midi_file=None
        self._pending_midi_intent=None
        self._preview_midi_token=None
        self._preview_midi_cache={}
        self._preview_midi_active=False
        self._preview_midi_pending=None
        self._preview_midi_signal=_InputInspectionSignal(self)
        self._preview_midi_signal.done.connect(self._midi_prepared,Qt.ConnectionType.QueuedConnection)
        self._preview_pdf_token=None
        self._preview_pdf_active=False
        self._preview_pdf_pending=None
        self._preview_pdf_destination=None
        self._preview_pdf_cache={}
        self._preview_pdf_signal=_InputInspectionSignal(self)
        self._preview_pdf_signal.done.connect(self._pdf_prepared,Qt.ConnectionType.QueuedConnection)
        self._midi_transport={'transport_order':monotonic(),'duration':0.0,'position':0.0,'playing':False,'available':False,'message':''}

        self._closed=False
        from .preview_prefetch import PreviewPrefetch
        self._preview_prefetch=PreviewPrefetch(self)
        self._state_timer=QTimer(self)
        self._state_timer.setSingleShot(True)
        self._state_timer.timeout.connect(self._emit_state)
        self._player_timer=QTimer(self)
        self._player_timer.setSingleShot(True)
        self._player_timer.timeout.connect(self._emit_player)
        self._midi_timer=QTimer(self)
        self._midi_timer.setSingleShot(True)
        self._midi_timer.timeout.connect(self._emit_midi)
        self._volume_save_timer=QTimer(self)
        self._volume_save_timer.setSingleShot(True)
        self._volume_save_timer.timeout.connect(self._save_preferences)
        self._sources_version=0
        self._waveform_cache={}
        self._waveform_pending=set()
        self._waveform=_WaveformSignal(self)
        self._waveform.done.connect(self._waveform_done,Qt.ConnectionType.QueuedConnection)
        self._sources=[]
        self._source=None
        self._pending_audio=None
        self._audio_bank={}
        self._media=QMediaPlayer(self)
        self._audio_output=QAudioOutput(self)
        self._media.setAudioOutput(self._audio_output)
        stored_volume=self._prefs.get('volume',72)
        self._volume=stored_volume if type(stored_volume) is int and 0<=stored_volume<=100 else 72
        self._audio_output.setVolume(self._volume/100)
        self._muted=os.environ.get('ASTRA_STUDIO_MUTE')=='1'
        self._audio_output.setMuted(self._muted)
        self._media_bind_callbacks()
        self._refresh_recent()
        self._edit_init()
        self._edit_identity()

    @staticmethod
    def _json(data):
        return json.dumps(data,ensure_ascii=False,allow_nan=False)

    def _player_state(self,include_sources=True):
        engine=getattr(self,'_shared_audio',None)
        pending=self._pending_audio
        result={'source':self._source,'sources_version':self._sources_version,'transport_order':monotonic(),
            'rate':getattr(self,'_audition_rate',1.0),
            'position':(pending[0] if pending else self._media.position())/1000,
            'duration':self._media.duration()/1000 or next((source.get('duration',0) for source in self._sources if source['key']==self._source),0),
            'playing':pending[1] if pending else self._media.isPlaying(),'volume':self._volume}
        if engine is not None and engine.active:
            result.update(source=engine.source,position=engine.position(),playing=engine.playing,
                          duration=engine.duration,rate=1.0)
        if include_sources:
            result['sources']=self._sources
        return result

    def _playback_states(self, include_sources):
        return self._player_state(include_sources), self._midi_state()

    @Slot(str, result=str)
    def beatCheckMarks(self, raw):
        try:
            from .beat_check_marks import request
            return self._json(request(self._project, json.loads(raw)))
        except (OSError, ValueError, TypeError, KeyError) as error:
            return self._json(dict(ok=False, error=str(error)))

    @Slot(result=str)
    def snapshot(self):
        from .editor_preferences import saved_shortcuts
        from .ui_preferences import saved_ui_preferences
        trace=getattr(self,'_trace',None)
        started=monotonic() if trace is not None and trace.enabled else None
        player,midi=self._playback_states(True)
        serialized=self._json({'audio':self._audio,'input_midi':self._input_midi,'project':self._project,'recent':self._recent,'options':self._options,
            'separation_workflow':self._separation_state(),
            'editor_shortcuts':saved_shortcuts(self._prefs),
            'ui_preferences':saved_ui_preferences(self._prefs),
            'editor_autosave':autosave_settings(self._prefs),'editor_save':self._edit_save_status(),
            'rhythm_candidate_running':bool(getattr(self,'_bar_phase_busy',False) and (getattr(self,'_bar_phase_request',None) or {}).get('kind')=='rhythm_candidate'),
            'job':self._job_state(),
            'job_progress':self._job_progress.snapshot(),
            'notes_score_draft':self._notes_score_status(),
            'project_opening':getattr(self,'_recent_open_status',None),
            'editor_transaction':getattr(self,'_editor_transaction',None),
            'player':player,'pdf':{**self._pdf,'path':str(self._pdf_path) if self._pdf_path else None},
            'review_document':self._review_document,'review_notice':self._review_notice,
            'gpu_preflight':self._gpu_preflight,
            'tempo_estimate':getattr(self,'_tempo_estimate',None),
            'midi':{**(self._midi_file or {'path':None,'name':'','notes':[]}),**midi}})
        if started is not None:trace.add('snapshot',chars=len(serialized),ms=round((monotonic()-started)*1000,3))
        return serialized

    def _job_state(self):
        request=self._request or {}
        pending=getattr(self,'_gpu_request',None) or {}
        return {'running':self.jobs.running or bool(pending.get('workflow_phase')),'phase':self._phase,'error':self._error,
                'editor_export':bool(request.get('editor_revision')),
                'log_path':str(self.jobs.log_path) if self.jobs.log_path else None,
                'kind':request.get('kind'),'separation_only':request.get('workflow_phase')=='separation' or request.get('instruments')==['guitar'],
                'recovery_pending':bool(getattr(self,'_interrupted_open_token',None)),
                **self._cancelled_job_state()}

    def _record_log(self,line):
        if self._phase!='正在取消…':
            self._job_progress.observe(line)
        self._log_lines.append(str(line))
        self._log_lines=self._log_lines[-100:]
        self.logs.emit(str(line))

    @Slot(result=str)
    def poll(self):
        # Lightweight recovery path for background/throttled WebChannel delivery.
        # Large PDF pages and note arrays are sent only with structural snapshots.
        player,midi=self._playback_states(False)
        return self._json({'job':self._job_state(),
            'rhythm_candidate_running':bool(getattr(self,'_bar_phase_busy',False) and (getattr(self,'_bar_phase_request',None) or {}).get('kind')=='rhythm_candidate'),
            'job_progress':self._job_progress.snapshot(),
            'project_opening':getattr(self,'_recent_open_status',None),
            'editor_transaction':getattr(self,'_editor_transaction',None),
            'editor_save':self._edit_save_status(),
            'player':player,'midi':midi,'input_midi':self._input_midi,
            'gpu_preflight':self._gpu_preflight,
            'preview':{'pdf_loading':bool(self._pdf.get('loading')),
                       'midi_path':(self._midi_file or {}).get('path'),
                       'pdf_path':str(self._pdf_path) if self._pdf_path else None,'pdf_index':self._pdf.get('index',0),
                       'midi_loading':midi['loading']},
            'audio_import':self._audio if self._audio and self._audio.get('import_token') else None,'logs':self._log_lines})

    def _publish(self):
        if not self._closed:
            self._state_timer.start(0)

    def _emit_state(self):
        self.stateChanged.emit(self.snapshot())

    def _player_tick(self,*_args):
        self._editor_audio_loop()
        if not self._closed:
            self._player_timer.start(0)

    def _emit_player(self):
        self.playerChanged.emit(self._json(self._player_state(False)))

    def _emit_midi(self):
        self.midiChanged.emit(self._json(self._midi_state()))

    @Slot(str)
    def _problem(self,message):
        self._error=str(message)
        self._publish()

    def _persist_preferences(self,updated):
        from .preference_store import update_preferences
        merged=update_preferences(PREFERENCES,getattr(self,'_persisted_prefs',self._prefs),updated,save_json)
        self._prefs=merged
        self._persisted_prefs=deepcopy(merged)
        return merged

    def _save_preferences(self):
        # Save only changed local preferences; never dump a project's options.
        updated=dict(self._prefs,volume=self._volume)
        try:
            StudioBridge._persist_preferences(self,updated)
            return True
        except (OSError,ValueError,RuntimeError) as exc:
            self.logs.emit(f'未保存工作台设置：{exc}')
            return False

    @Slot(str,result=str)
    def saveUiPreferences(self,message):
        from .ui_preferences import validate_ui_preferences, saved_ui_preferences
        try:
            patch=validate_ui_preferences(json.loads(message))
            existing=self._prefs.get('ui_preferences',{})
            values=dict(existing if isinstance(existing,dict) else {},**patch)
            updated=dict(self._prefs,ui_preferences=values)
            merged=StudioBridge._persist_preferences(self,updated)
            return self._json({'ok':True,'preferences':saved_ui_preferences(merged)})
        except (OSError,TypeError,ValueError,RuntimeError) as exc:
            return self._json({'ok':False,'error':str(exc)})

    @Slot(str,result=str)
    def saveEditorAutosaveSettings(self,message):
        try:
            patch=json.loads(message)
            if not isinstance(patch,dict):raise ValueError('自动保存设置必须是对象。')
            values=autosave_settings(self._prefs,patch)
            merged=self._persist_preferences(dict(self._prefs,editor_autosave=values))
            config=autosave_settings(merged)
            self._edit_autosave_changed(config)
            return self._json(dict(ok=True,settings=config))
        except (OSError,ValueError,TypeError,RuntimeError) as exc:
            return self._json(dict(ok=False,error=str(exc)))

    @Slot(result=str)
    def saveEditor(self):
        return self._json(self._edit_manual_save())

    @Slot(result=str)
    def saveAllEditors(self):
        return self._json(self._edit_save_all_dirty())

    @Slot(result=str)
    def retryEditorSave(self):
        return self._json(self._edit_manual_save(retry=True))

    @Slot(str,result=str)
    def requestEditorSave(self,message):
        try:
            data=json.loads(message)
            if (not isinstance(data,dict) or not isinstance(data.get('project'),str)
                    or type(data.get('generation')) is not int or type(data.get('retry')) is not bool
                    or data.get('kind') not in ('formal','recovery','retention')):
                raise ValueError('无效的保存请求。')
            return self._json(self._edit_manual_save(retry=data['retry'],kind=data['kind'],
                project=data['project'],generation=data['generation']))
        except (TypeError,ValueError) as exc:
            return self._json(dict(ok=False,error=str(exc)))

    @Slot(str,result=str)
    def dismissEditorRecovery(self,message):
        from . import live_owner as live
        try:
            data=json.loads(message)
            project=self._project
            prompt=(project or {}).get('editor_recovery') or {}
            offer=getattr(self,'_editor_recovery_offer',None)
            if (not isinstance(data,dict) or not project or not offer or self._closed
                    or getattr(self,'_editor_closing',False) or self.jobs.running or self._gpu_request
                    or data.get('project')!=project['path'] or data.get('token')!=prompt.get('token')):
                raise ValueError('项目或恢复列表已变化，请重新打开查看。')
            live.dismiss_recovery(project['path'],offer)
            self._editor_recovery_request=self._editor_recovery_offer=None
            project.pop('editor_recovery',None)
            self._publish()
            return self._json(dict(ok=True))
        except (OSError,TypeError,KeyError,ValueError,RuntimeError) as exc:
            self.logs.emit(str(exc))
            return self._json(dict(ok=False,error=str(exc)))

    def _scan_editor_recovery(self):
        from . import live_owner as live
        self._editor_recovery_request=None
        self._editor_recovery_offer=None
        if not self._project or 'piano' not in self._project.get('instruments',[]):return
        self._project.pop('editor_recovery',None)
        token=uuid4().hex
        self._editor_recovery_request=(token,self._project,self._edit_identity())
        if not hasattr(self,'_editor_recovery_signal'):
            self._editor_recovery_signal=_InputInspectionSignal(self)
            self._editor_recovery_signal.done.connect(self._editor_recovery_ready,Qt.ConnectionType.QueuedConnection)
        QThreadPool.globalInstance().start(_InputInspectionTask(self._project['path'],token,
            self._editor_recovery_signal,live.recovery_options))

    @Slot(str,object,str)
    def _editor_recovery_ready(self,token,offer,error):
        req=getattr(self,'_editor_recovery_request',None)
        if (self._closed or not req or req[0]!=token or req[1] is not self._project
                or req[2] is not self._edit_identity()):return
        if error:
            self.logs.emit(error)
            self._project['editor_recovery']=dict(token=token,options=[],error='暂时无法核对恢复副本。请查看日志。')
        elif offer and offer['options']:
            self._editor_recovery_offer=offer
            self._project['editor_recovery']=dict(token=token,formal_available=offer['formal_available'],
                options=[{k:row[k] for k in ('id','kind','created_ms')} for row in offer['options']])
        self._publish()

    @Slot(str,result=str)
    def recoverEditorBackup(self,message):
        from . import editor_recovery as recovery, live_owner as live
        published=False
        try:
            data=json.loads(message)
            project=self._project
            prompt=(project or {}).get('editor_recovery') or {}
            offer=getattr(self,'_editor_recovery_offer',None)
            if (not isinstance(data,dict) or not project or not offer or self._closed
                    or getattr(self,'_editor_closing',False) or self.jobs.running or self._gpu_request
                    or data.get('project')!=project['path'] or data.get('token')!=prompt.get('token')):
                raise ValueError('项目或恢复列表已变化，请重新选择。')
            self._require_no_structure_draft()
            self._cancel_recent_open()
            live.restore_recovery(project['path'],offer,data.get('id'))
            published=True
            self._editor_recovery_request=self._editor_recovery_offer=None
            project.pop('editor_recovery',None)
            self._clear_midi()
            self._audition_range=None
            self._edit_project_ref=None
            self._edit_identity()
            self._refresh_editor()
            if not project.get('editor'):
                raise ValueError('副本已替换正式文件，但编辑器未能重新打开。请重新打开项目。')
            self._review_notice=dict(token=uuid4().hex,text='已恢复所选副本。原正式文件（如有）已另存，导出结果没有改变。')
            self._publish()
            return self._json(dict(ok=True))
        except recovery.PublicationError as exc:
            self.logs.emit(str(exc))
            if exc.outcome!='not_published':
                self._editor_recovery_request=self._editor_recovery_offer=None
                self._project.pop('editor_recovery',None)
                self._project.pop('editor',None)
                self._project['editor_unavailable']='恢复写入未确认。原文件已另存；请先重新打开项目检查。'
                self._clear_midi()
                self._edit_project_ref=None
                self._edit_identity()
                self._publish()
                return self._json(dict(ok=False,error=self._project['editor_unavailable']))
            return self._json(dict(ok=False,error='未替换正式文件。请查看日志后重试。'))
        except (OSError,TypeError,KeyError,ValueError,RuntimeError) as exc:
            self.logs.emit(str(exc))
            self._publish()
            return self._json(dict(ok=False,error=('正式文件已恢复，请重新打开项目检查。' if published else str(exc))))

    def install_editor_audio_factory(self,factory):
        """Native desktop integration only; never a WebChannel slot."""
        self._editor_audio_factory=factory
        if self._edit_preview_selected():
            self._select_live_editor(self._editor_uri,resume=(self._midi_transport.get('position',0.),False))

    @Slot(str,result=str)
    def saveEditorShortcuts(self,message):
        from .editor_preferences import validate_shortcuts
        try:
            data=json.loads(message)
            if not isinstance(data,dict):raise ValueError('快捷键请求必须是对象。')
            bindings=validate_shortcuts(data.get('bindings'))
            if any(chord in ('Ctrl+S','Meta+S') for chord in bindings.values()):
                raise ValueError('Ctrl+S 已用于保存项目，请选择其他快捷键。')
            # Publish only after atomic persistence succeeds; anonymous WebEngine
            # profiles cannot provide cross-launch localStorage persistence.
            updated=dict(self._prefs,editor_shortcuts=bindings)
            merged=StudioBridge._persist_preferences(self,updated)
            return self._json({'ok':True,'bindings':merged['editor_shortcuts']})
        except (OSError,TypeError,ValueError,RuntimeError) as exc:
            return self._json({'ok':False,'error':str(exc)})

    def _refresh_recent(self):
        stored=self._prefs.get('recent',[])
        paths=[Path(item) for item in stored if isinstance(item,str)] if isinstance(stored,list) else []
        # Saved absolute entries can point at an old release or any manually
        # opened location. Discover only direct children of known roots.
        discovered={}
        roots=set()
        isolated=project_access.isolated()
        shared=_shared_project_library() if isolated else None
        configured=getattr(self,'_new_task_options',{}).get('output') if not isolated else None
        for value in (PROJECTS,OUTPUTS,configured,shared):
            try:
                base=_local_path(value)
                if base in roots:continue
                roots.add(base)
                for manifest in base.glob('*/project.json'):
                    try:
                        project=manifest.parent.resolve(strict=True)
                        if project.parent!=base or manifest.resolve(strict=True)!=project/'project.json':
                            continue
                        if manifest.is_file():
                            discovered[project]=manifest.stat().st_mtime_ns
                    except (OSError,ValueError,RuntimeError):
                        continue  # It may have been removed while the list was refreshed.
            except (OSError,ValueError,RuntimeError):
                continue
        paths.extend(sorted(discovered,key=lambda path:discovered[path],reverse=True))
        recent={}
        for item in paths:
            try:
                if isolated and (not item.is_absolute() or item.anchor.startswith(('\\','//'))):continue
                path=item.resolve(strict=True)
                external=isolated and not path.is_relative_to(PROJECTS)
                if external:
                    # A remembered spelling or discovered entry cannot escape
                    # into another directory through a symlink or junction.
                    if path!=item.expanduser():continue
                    state,_,_=project_access.summarize(path)
                else:
                    state,_,_=_project_record(path)
            except (OSError,ValueError,RuntimeError,TypeError):
                continue
            recent.setdefault(path,{'name':_project_title(path,state),'path':str(path)})
            if len(recent)>=18:
                break
        self._recent=list(recent.values())

    @staticmethod
    def _option(key,value):
        if key=='instruments':
            if not isinstance(value,list) or any(item not in INSTRUMENTS for item in value):
                raise ValueError('乐器必须为钢琴或吉他。')
            return [item for item in INSTRUMENTS if item in value]
        if key=='mode' and value in ('all','midi','score'):
            return value
        if key=='quantization' and value in ('auto','straight','triplet','fine'):
            return value
        if key=='piano_model' and value in ('xlance','sw'):
            return value
        if key=='rhythm_provider' and value in ('beat_this','cubase_local'):
            return value
        if key=='score_engine' and value in ('music21','duet'):
            return value
        if key=='score_voice_mode' and value in ('simple','detailed'):
            return value
        if key=='score_duet_device' and value in ('cpu','cuda'):
            return value
        if key in ('keep_stems','score_pedal','score_chords') and type(value) is bool:
            return value
        if key=='output':
            return str(project_write_path(_local_path(value or PROJECTS)))
        if key=='tempo_path':
            if not value:
                return None
            path=_local_path(value)
            if not path.is_file() or path.suffix.lower() not in TEMPO_TYPES:
                raise ValueError('请选择存在的 JSON 或 MIDI 速度图。')
            return str(path)
        if key=='bpm':
            return None if value in (None,'') else _number(value,10,600,'固定 BPM 须在 10 到 600 之间。')
        if key=='meter' and isinstance(value,str):
            numerator,denominator=map(int,value.split('/'))
            if 1<=numerator<=32 and denominator in (1,2,4,8,16,32,64,128):
                return f'{numerator}/{denominator}'
        if key=='beat_scale' and value in (.5,1.,2.) and not isinstance(value,bool):
            return float(value)
        raise ValueError(f'无效的工作台选项：{key}')

    def _update_options(self,payload):
        if not isinstance(payload,dict):
            raise ValueError('工作台选项必须是对象。')
        updated={key:self._option(key,value) for key,value in payload.items() if key in DEFAULT_OPTIONS}
        persistent={key:(list(value) if isinstance(value,list) else value)
            for key,value in updated.items() if key in PERSISTENT_OPTIONS}
        if not self._project and persistent:
            # Commit disk first. On failure neither current nor future options change.
            desired=dict(self._prefs,**persistent)
            try:
                merged=StudioBridge._persist_preferences(self,desired)
            except (OSError,ValueError,RuntimeError) as exc:
                raise ValueError(f'未保存新任务设置，原设置未改变：{exc}') from exc
            for key in PERSISTENT_OPTIONS:
                if key in merged:
                    self._new_task_options[key]=self._option(key,merged[key])
            self._new_rhythm_provider=self._new_task_options['rhythm_provider']
        self._options.update(updated)
        if 'instruments' in updated and self._audio and not getattr(self,'_separation_workflow',None):
            self._audio['requested_instruments']=list(updated['instruments'])

    def _clear_overrides(self):
        self._options.update(tempo_path=None,bpm=None,meter='4/4',beat_scale=1.0)

    def _import_audio_targets(self):
        return list((self._audio or {}).get('requested_instruments',self._options['instruments']))

    def _set_audio(self,path,duration=0.0,original_path=None,*,requested_instruments=None):
        self._audio={'path':str(path),'name':path.name,**self._waveform_preview(path,duration)} if path else None
        if self._audio and requested_instruments is not None:
            self._audio['requested_instruments']=requested_instruments
        if self._audio and original_path:
            self._audio.update(name=Path(original_path).name,original_path=str(original_path))

    def accept_audio(self,path):
        """Accept a native file picker or native Qt drop. This is not a WebChannel slot."""
        if self.jobs.running or self._gpu_request:
            return
        try:
            self._require_no_structure_draft()
            path=_local_path(path)
            if path.suffix.lower()=='.ncm' and path.is_file():
                self._prepare_ncm(path)
                return
            path=_audio_file(path)
            self._input_midi=None
            if not self._audio or self._audio['path']!=str(path):
                self._clear_overrides()
                self._clear_midi()
                self._separation_workflow=None
                self._set_audio(path,requested_instruments=list(self._options['instruments']))
                self._refresh_sources(preserve=False)
            self._phase='音频已就绪'
            self._error=None
            self._publish()
        except (OSError,ValueError,TypeError) as exc:
            self._problem(exc)

    def _prepare_ncm(self,path):
        from .audio_import import prepare_audio_import

        self._clear_overrides()
        self._input_midi=None
        self._clear_midi()
        token=uuid4().hex
        self._separation_workflow=None
        self._audio={'path':str(path),'name':path.name,'original_path':str(path),'duration':0.0,'peaks':[],
                     'import_token':token,'import_loading':True,'requested_instruments':list(self._options['instruments'])}
        self._refresh_sources(preserve=False)
        self._phase='正在本地转换 NCM 音频'
        self._error=None
        self._publish()
        def prepare(source):
            return prepare_audio_import(source,ROOT/'.cache'/'imports',log=lambda _message: None)
        QThreadPool.globalInstance().start(_InputInspectionTask(path,token,self._audio_inspection,prepare))

    def _audio_inspected(self,token,result,error):
        current=self._audio
        if self._closed or not current or current.get('import_token')!=token:
            return
        if error:
            current.update(import_loading=False,import_error=error)
            self._phase='NCM 导入失败'
            self._problem(error)
            return
        self._set_audio(Path(result['path']),result.get('duration',0.0),result['original_path'],
                        requested_instruments=self._import_audio_targets())
        self._audio['import_token']=token
        self._phase='NCM 音频已就绪'
        self._refresh_sources(preserve=False)
        self._kept_audio_ready(token)
        self._publish()

    def accept_project(self,path):
        """Native folder picker and local drop. Not a WebChannel slot."""
        if self.jobs.running or self._gpu_request:
            return
        try:
            self._require_no_structure_draft()
            raw=Path(path).expanduser()
            target=_local_path(raw)
            if project_access.isolated() and not target.is_relative_to(PROJECTS):
                self._cancel_recent_open()
                self._begin_recent_open(raw,external=True)
            else:
                self._show_project(target,restore=True)
        except (OSError,ValueError,RuntimeError,TypeError) as exc:
            self._problem(exc)

    def accept_input(self,path):
        """Route a native drop to the matching local importer."""
        if Path(path).suffix.lower() in MIDI_TYPES:
            self.accept_midi(path)
        else:
            self.accept_audio(path)

    def accept_midi(self,path):
        if self.jobs.running or self._gpu_request:
            return
        try:
            self._require_no_structure_draft()
            from .midi_import import inspect_midi

            path=_local_path(path)
            if path.suffix.lower() not in MIDI_TYPES or not path.is_file():
                raise ValueError('请选择存在的 .mid 或 .midi 文件。')
        except (ImportError,OSError,ValueError,TypeError) as exc:
            self._problem(exc)
            return
        self._clear_overrides()
        self._set_audio(None)
        self._separation_workflow=None
        self._stop_audio()
        self._sources=[]
        self._select_audio(None,preserve=False)
        self._clear_midi()
        token=uuid4().hex
        self._input_midi={'path':str(path),'name':path.name,'loading':True,'token':token}
        self._phase='正在读取 MIDI 轨道'
        self._error=None
        self._publish()
        QThreadPool.globalInstance().start(_InputInspectionTask(path,token,self._midi_inspection,inspect_midi))

    def _midi_inspected(self,token,result,error):
        current=self._input_midi
        if self._closed or not current or current.get('token')!=token:
            return
        if error:
            self._input_midi={**current,'loading':False,'error':error}
            self._phase='MIDI 读取失败'
        else:
            self._input_midi={**result,**current,'loading':False}
            self._phase='请选择要制谱的 MIDI 轨道'
        self._publish()

    @staticmethod
    def _waveform_key(path):
        stat=path.stat()
        return str(path),stat.st_size,stat.st_mtime_ns

    def _waveform_preview(self,path,fallback=0.0):
        duration,peaks=0.0,[]
        try:
            key=self._waveform_key(path)
        except OSError:
            return {'duration':fallback,'peaks':[]}
        if key in self._waveform_cache:
            duration,peaks=self._waveform_cache[key]
        elif key not in self._waveform_pending:
            self._waveform_pending.add(key)
            QThreadPool.globalInstance().start(_WaveformTask(key,path,self._waveform))
        return {'duration':duration or fallback,'peaks':peaks}

    def _waveform_done(self,key,duration,peaks):
        self._waveform_pending.discard(key)
        if self._closed:
            return
        self._waveform_cache[key]=(duration,peaks)
        if len(self._waveform_cache)>32:
            self._waveform_cache.pop(next(iter(self._waveform_cache)))
        rows=[*self._sources,*([self._audio] if self._audio else [])]
        preview=(getattr(self,'_separation_workflow',None) or {}).get('preview') or {}
        aligned_paths={preview.get('original_audio'),*preview.get('stem_paths',{}).values()}
        changed=False
        for row in rows:
            if row['path']==key[0]:
                row.update(self._waveform_preview(Path(row['path']),row['duration']))
                if row['path'] in aligned_paths:
                    row['duration']=preview['duration']
                changed=True
        if changed:
            self._sources_version+=1
            self._publish()

    def _refresh_sources(self,preserve=True):
        self._shared_leave('sources refreshed',preserve=preserve)
        if not preserve:self._stop_audio()
        self._shared_audio_failed=False
        sources=[]
        workflow=getattr(self,'_separation_workflow',None) or {}
        preview=workflow.get('preview') or {}
        aligned=self._audio and preview.get('original_audio')==self._audio['path']
        if self._audio and not self._audio.get('import_loading') and not self._audio.get('import_error'):
            duration=preview['duration'] if aligned else self._audio['duration']
            sources.append({'key':'original','label':'原曲','path':self._audio['path'],
                'duration':duration,'peaks':self._audio['peaks']})
        if aligned:
            for key,path in preview['stem_paths'].items():
                wave=self._waveform_preview(Path(path),preview['duration'])
                sources.append({'key':key,'label':f'{INSTRUMENTS[key]}分轨','path':path,
                                'peaks':wave['peaks'],'duration':preview['duration']})
        elif self._project and (not self._audio or self._project_source==self._audio['path']):
            for item in self._project['files']:
                if item['kind']=='audio':
                    key=item['instrument']
                    sources.append({'key':key,'label':f'{INSTRUMENTS[key]}分轨','path':item['path'],
                        **self._waveform_preview(Path(item['path']))})
        self._sources=sources
        self._sources_version+=1
        selected=self._source if preserve and any(row['key']==self._source for row in sources) else None
        self._select_audio(selected or (sources[0]['key'] if sources else None),preserve=preserve)
        self._preload_audio_sources()
        self._shared_prepare()

    def _audio_signal_pairs(self):
        return [('positionChanged',self._player_tick),('durationChanged',self._duration_changed),
                ('seekableChanged',self._resume_audio),('playbackStateChanged',self._player_tick),
                ('mediaStatusChanged',self._media_status),('errorOccurred',self._media_error)]

    def _preload_audio_sources(self):
        """Keep the few comparison sources decoded; never play inactive media."""
        wanted={row['path'] for row in self._sources}
        bank=getattr(self,'_audio_bank',{})
        for path,(media,output) in list(bank.items()):
            if path not in wanted:
                if media is not self._media:media.stop();media.setSource(QUrl());media.deleteLater();output.deleteLater()
                del bank[path]
        current=next((path for path in wanted if QUrl.fromLocalFile(path)==self._media.source()),None)
        if current:bank[current]=(self._media,self._audio_output)
        for path in wanted:
            if path in bank:continue
            media=QMediaPlayer(self);output=QAudioOutput(self)
            output.setVolume(self._volume/100);output.setMuted(self._muted)
            media.setAudioOutput(output);media.setSource(QUrl.fromLocalFile(path))
            bank[path]=(media,output)
        self._audio_bank=bank

    def _select_audio(self,key,preserve=True):
        item=next((source for source in self._sources if source['key']==key),None)
        if key is not None and item is None:
            raise ValueError('这条试听轨当前不可用。')
        self._cancel_midi_intent()
        engine=getattr(self,'_shared_audio',None)
        if engine is not None and engine.active and item is not None:
            if not preserve:engine.stop()
            engine.select(key);self._source=key;self._player_tick();return
        if item is None:self._shared_leave('source cleared',preserve=False)
        url=QUrl.fromLocalFile(item['path']) if item else QUrl()
        self._source=key
        if url!=self._media.source():
            previous=self._pending_audio or (self._media.position(),self._media.isPlaying())
            self._media_invalidate()
            prepared=getattr(self,'_audio_bank',{}).get(item['path']) if item else None
            if prepared and prepared[0] is not self._media and prepared[0].mediaStatus() in (
                    QMediaPlayer.MediaStatus.LoadedMedia,QMediaPlayer.MediaStatus.BufferingMedia,QMediaPlayer.MediaStatus.BufferedMedia,QMediaPlayer.MediaStatus.EndOfMedia):
                old=self._media
                old.pause()
                self._media,self._audio_output=prepared
                self._media.setPlaybackRate(getattr(self,'_audition_rate',1.0))
                self._audio_output.setVolume(self._volume/100);self._audio_output.setMuted(self._muted)
                self._media_bind_callbacks()
                self._pending_audio=previous if preserve else (0,False)
                self._resume_audio()
            else:
                # Slow first load remains supported. Do not retain a stale bank alias
                # when the active player is reused for another URL.
                if prepared and prepared[0] is not self._media:
                    prepared[0].stop();prepared[0].setSource(QUrl());prepared[0].deleteLater();prepared[1].deleteLater()
                self._audio_bank={path:pair for path,pair in getattr(self,'_audio_bank',{}).items() if pair[0] is not self._media and pair is not prepared}
                self._pending_audio=(previous if preserve else (0,False)) if item else None
                self._media.setSource(url)
        self._preload_audio_sources()
        self._player_tick()

    def _select_audition_source(self,data):
        key=data.get('key')
        position=_number(data.get('position'),0,float('inf'),'试听位置无效。')
        playing=data.get('playing')
        if type(playing) is not bool:raise ValueError('播放状态无效。')
        if key!='midi' and not any(source['key']==key for source in self._sources):
            raise ValueError('这条试听轨当前不可用。')
        selection=data.get('range') or {}
        start=_number(selection.get('start',0),0,float('inf'),'L 位置无效。')
        end=_number(selection.get('end',0),0,float('inf'),'R 位置无效。')
        loop=selection.get('loop',False)
        if type(loop) is not bool:raise ValueError('循环范围无效。')
        if key=='midi':
            self._audition_range={'start':start,'end':end,'loop':loop}
            deferred=self._defer_midi_transport(audition='midi',position=position,playing=playing)
            if deferred or not self._midi_backend or not self._midi_backend.available:
                if not playing and data.get('preservePosition') is not True:
                    engine=getattr(self,'_shared_audio',None)
                    if engine is not None and engine.active:engine.pause()
                    self._pending_audio=None
                    self._media.pause()
                    self._player_tick()
                if deferred:return
            if not self._midi_backend or not self._midi_backend.available:raise ValueError('当前 MIDI 无法试听。')
        else:
            self._cancel_midi_intent()
        self._audition_together=False
        if (self._project or {}).get('editor'):self._project['editor']['audition_together']=False
        self._audition_range={'start':start,'end':end,'loop':loop}
        engine=getattr(self,'_shared_audio',None)
        if key!='midi' and engine is not None and engine.active:
            self._shared_sync()
            if engine.active:
                if self._midi_backend:self._midi_backend.pause()
                engine.select(key);self._source=key
                # Source-only switches preserve the current sample clock even
                # when the UI snapshot arrives late. Explicit seeks stay exact.
                if data.get('preservePosition') is not True:engine.seek(position)
                if data.get('preservePosition') is not True and playing!=engine.playing:
                    engine.play() if playing else engine.pause()
                self._player_tick();return
        if getattr(self,'_shared_audio',None) is not None:
            self._shared_leave('audition mode uses legacy')
        self._media.pause();self._pending_audio=None
        if self._midi_backend:
            self._midi_backend.pause()
            self._midi_backend.set_loop(start,end,loop)
            self._midi_backend.seek(position)
        if key=='midi':
            if playing:self._midi_backend.play()
        else:
            self._select_audio(key,preserve=False)
            self._pending_audio=(round(position*1000),playing)
            self._resume_audio()
        self._player_tick()

    def _media_status(self,status):
        if status in (QMediaPlayer.MediaStatus.LoadedMedia,QMediaPlayer.MediaStatus.BufferedMedia):
            self._resume_audio()

    def _duration_changed(self,duration):
        if self._source=='original' and self._audio and not self._audio['duration'] and duration>0:
            self._audio['duration']=duration/1000
            for source in self._sources:
                if source['key']=='original':
                    source['duration']=duration/1000
            self._publish()
        self._resume_audio()
        self._player_tick()

    def _legacy_playback_position(self, milliseconds):
        """Keep paused positioning exact; callers apply this only when playing."""
        selection=getattr(self,'_audition_range',None) or ((self._project or {}).get('editor') or {}).get('range') or {}
        left,right=selection.get('start',0),selection.get('end',0)
        if selection.get('loop') and right<=milliseconds/1000<left:
            return round(left*1000)
        return milliseconds

    def _resume_audio(self,*_args):
        engine=getattr(self,'_shared_audio',None)
        if engine is not None and engine.active:
            self._pending_audio=None
            return
        if not self._pending_audio or self._media.mediaStatus() not in (
            QMediaPlayer.MediaStatus.LoadedMedia,QMediaPlayer.MediaStatus.BufferingMedia,QMediaPlayer.MediaStatus.BufferedMedia,
            QMediaPlayer.MediaStatus.EndOfMedia,
        ):
            return
        position,playing=self._pending_audio
        if playing:position=self._legacy_playback_position(position)
        if position and (not self._media.isSeekable() or self._media.duration()<=0):
            return
        self._pending_audio=None
        self._media.setPosition(min(position,self._media.duration()))
        if playing:
            self._media.play()
            if getattr(self,'_audition_together',False) and self._midi_backend and not any(self._midi_load_status()):
                self._midi_backend.seek(position/1000)
                self._midi_backend.play()
        self._player_tick()

    def _media_error(self,_error,message):
        if self._source:
            self._pending_audio=None
            self._problem(message or '无法试听这个音频文件。')

    def _play_pause(self):
        self._cancel_midi_intent()
        if self._shared_start():
            engine=self._shared_audio
            if self._midi_backend:self._midi_backend.stop()
            engine.pause() if engine.playing else engine.play()
            self._player_tick();return
        if not self._source:
            raise ValueError('没有可试听的音频。')
        if self._midi_backend:
            self._midi_backend.stop()
        if self._pending_audio is not None:
            self._pending_audio=(self._pending_audio[0],not self._pending_audio[1])
            self._resume_audio()
        elif self._media.isPlaying():
            self._media.pause()
        else:
            target=self._legacy_playback_position(self._media.position())
            if target!=self._media.position():self._media.setPosition(min(target,self._media.duration()))
            self._media.play()
        self._player_tick()

    def _stop_audio(self):
        self._cancel_midi_intent()
        self._media_invalidate()
        engine=getattr(self,'_shared_audio',None)
        if engine is not None and engine.active:engine.stop()
        self._pending_audio=None
        self._media.stop()
        self._player_tick()

    def _seek_audio(self,position):
        target=round(_number(position,0,float('inf'),'试听位置无效。')*1000)
        engine=getattr(self,'_shared_audio',None)
        if engine is not None and engine.active:
            engine.seek(target/1000);self._player_tick();return
        if self._pending_audio is not None:
            self._pending_audio=(target,self._pending_audio[1])
            self._resume_audio()
        elif self._media.isSeekable():
            if self._media.isPlaying():target=self._legacy_playback_position(target)
            self._media.setPosition(min(target,self._media.duration()))
        self._player_tick()

    def _audition(self,key,position):
        """Select, seek and play as one request, including async source loading."""
        target=round(_number(position,0,float('inf'),'试听位置无效。')*1000)
        self._cancel_midi_intent()
        if self._shared_start():
            engine=self._shared_audio
            if self._midi_backend:self._midi_backend.stop()
            engine.select(key);self._source=key;engine.seek(target/1000);engine.play()
            self._player_tick();return
        self._select_audio(key,preserve=False)
        if not self._source:
            raise ValueError('没有可试听的音频。')
        if self._midi_backend:
            self._midi_backend.stop()
        self._pending_audio=(target,True)
        self._resume_audio()
        self._player_tick()

    def _set_volume(self,value):
        self._volume=round(_number(value,0,100,'音量须在 0 到 100 之间。'))
        self._audio_output.setVolume(self._volume/100)
        self._volume_save_timer.start(500)
        self._player_tick()

    @staticmethod
    def _stem_for_export(project,export,state,report,instrument):
        if 'stem_paths' in report:
            paths=report['stem_paths']
        else:
            legacy=state.get('legacy_stem_paths',{})
            paths=legacy.get(str(export)) if isinstance(legacy,dict) else None
            if paths is None and Path(state.get('latest_export') or '').name==export.name:
                paths=state.get('stem_paths')
            if paths is None and 'stem_paths' not in state:
                paths={instrument:str(project/f'{instrument}.wav')}
        value=paths.get(instrument) if isinstance(paths,dict) else None
        if not isinstance(value,str) or not value:
            return None
        try:
            member=project_access.reference(project,value)
            if member is None:return None
            path=member.resolve()
            if path.suffix.lower()=='.wav' and path.is_relative_to(project) and path.is_file():
                return path
        except (OSError,RuntimeError,ValueError):
            pass
        return None

    @staticmethod
    def _reported_pdf_issue(report, files):
        result = report.get('instruments',{}).get('piano',{}).get('pdf_render') or {}
        if result.get('status') != 'failed':
            return None
        message = '本次未生成 PDF。' + (result.get('error') or 'MusicXML 已保留。')
        if any(item['kind']=='pdf' and item['instrument']=='piano' and item.get('historical') for item in files):
            message += '可查看标有修订的上次 PDF。'
        return {'message':message}

    def _result_files(self,project,export,state,report,score_export=None):
        files=[]
        names=report.get('files',[])
        names=[name for name in names if isinstance(name,str)] if isinstance(names,list) else []
        for name in dict.fromkeys([*names,'tempo_map.json','report.json']):
            if Path(name).name!=name:
                continue
            path=(export/name).resolve()
            kind=({'report.json':'report','tempo_map.json':'tempo','tempo.mid':'tempo'}.get(name)
                or ('report' if name.endswith('_harmony.json') else OUTPUT_TYPES.get(path.suffix.lower())))
            if path.parent==export and path.is_file() and kind:
                instrument=name.split('_')[0].split('.')[0]
                files.append({'name':name,'path':str(path),'kind':kind,
                    'instrument':instrument if instrument in INSTRUMENTS else None})
        if score_export is not None and score_export!=export:
            # Keep the current MIDI/tempo files untouched; this separate score
            # snapshot owns only the retained notation and its visible metadata.
            for name,kind in (('piano.musicxml','musicxml'),('piano.pdf','pdf')):
                score_path=(score_export/name).resolve()
                if score_path.parent==score_export and score_path.is_file():
                    files=[item for item in files if not (item['instrument']=='piano' and item['kind']==kind)]
                    files.append({'name':name,'path':str(score_path),'kind':kind,'instrument':'piano',
                                  'retained_score':True,'score_export':str(score_export)})
        if not any(item['kind']=='pdf' and item['instrument']=='piano' for item in files):
            from .score_artifacts import resolve_pdf_export
            retained=resolve_pdf_export(project,state)
            if retained:
                pdf, revision = retained
                label = revision[:8] if isinstance(revision,str) else pdf.parent.name
                files.append({'name':f'上次可用 PDF · {label}', 'path':str(pdf), 'kind':'pdf',
                              'instrument':'piano','historical':True,'retained_score':True,
                              'score_revision':revision,'score_export':str(pdf.parent)})
        for instrument in INSTRUMENTS:
            if instrument in state.get('instruments',[]):
                path=self._stem_for_export(project,export,state,report,instrument)
                if path:
                    files.append({'name':f'{instrument}.wav','path':str(path),'kind':'audio','instrument':instrument})
        if state.get('source_kind')=='midi':
            original=(project/'source.mid').resolve()
            selected=(project/'selected_source.mid').resolve()
            if original.parent==project and original.is_file():
                files.append({'name':original.name,'path':str(original),'kind':'source_midi','instrument':None})
            if (selected.parent==project and selected.is_file()
                    and not any(item['name']=='piano_raw.mid' for item in files)):
                files.append({'name':selected.name,'path':str(selected),'kind':'midi','instrument':'piano'})
        return files

    def _historical_guitar_files(self,project,export):
        root=(project/'exports').resolve()
        from .provisional_score import draft_directory_name
        for folder in sorted(root.iterdir(),reverse=True):
            if draft_directory_name(folder.name) or folder==export or folder.is_symlink() or not folder.is_dir() or folder.resolve().parent!=root:
                continue
            report=_metadata(folder/'report.json')
            if report.get('status') not in ('complete','complete_with_warnings'):
                continue
            files=[dict(file,historical=True) for file in self._result_files(project,folder,{},report)
                   if file['instrument']=='guitar' and file['kind'] in ('midi','musicxml','pdf')]
            if files:
                return files
        return []

    def _external_project_audio(self,project,state):
        raw=project_access.reference(project,state.get('source'))
        media=project_access.reference(project,state.get('source_audio'))
        for member in (media,raw):
            if member is not None and member.suffix.lower() in AUDIO_TYPES:
                return member,str(raw) if raw is not None else None
        linked=getattr(self,'_linked_original',None)
        if linked is not None:
            permit,identity,link=linked
            if permit==project_access.approved(project) and identity==project_access.audio_identity(state):
                try:
                    media,selected=link.verify()
                    return media,str(selected)
                except (OSError,ValueError,RuntimeError):
                    self._linked_original=None
        return None,str(raw) if raw is not None else None

    def _link_project_audio(self,expected_project):
        owner=self._project
        if (not owner or expected_project!=owner['path'] or not owner.get('original_link')
                or owner['original_link']['status']=='pending' or self.jobs.running or self._gpu_request
                or getattr(self,'_recent_open_token',None)):
            return
        path=Path(owner['path'])
        permit=project_access.approved(path)
        if permit is None:return
        filters='音频文件 ('+' '.join('*'+suffix for suffix in sorted(AUDIO_IMPORT_TYPES))+')'
        # The project never supplies the initial folder or the selected file.
        chosen,_=QFileDialog.getOpenFileName(self.parent(),'选择此工程的原曲',str(path),filters)
        if not chosen or self._closed or self._project is not owner or self._audio or self._input_midi:
            return
        state,_,_=_project_record(path)
        if project_access.audio_kind(state)!='audio':return
        identity=project_access.audio_identity(state)
        token=uuid4().hex
        self._original_audio_request=(token,owner,permit,identity)
        owner['original_link']={'status':'pending'}
        if not hasattr(self,'_original_audio_signal'):
            self._original_audio_signal=_InputInspectionSignal(self)
            self._original_audio_signal.done.connect(self._project_audio_ready)
        self._publish()
        def prepare(selected):
            return project_access.prepare_audio_link(selected,identity,AUDIO_TYPES)
        QThreadPool.globalInstance().start(_InputInspectionTask(Path(chosen),token,self._original_audio_signal,prepare))

    @Slot(str,object,str)
    def _project_audio_ready(self,token,result,error):
        current=getattr(self,'_original_audio_request',None)
        if self._closed or current is None or current[0]!=token:return
        self._original_audio_request=None
        _,owner,permit,identity=current
        if self._project is not owner:return
        try:
            if self.jobs.running or self._gpu_request or self._audio or self._input_midi or getattr(self,'_recent_open_token',None):
                owner['original_link']={'status':'missing'}
                return
            if error:
                owner['original_link']={'status':'missing','error':error}
                return
            if self._midi_state().get('playing'):
                owner['original_link']={'status':'missing','error':'请暂停乐谱或 MIDI 后再连接原曲。'}
                return
            state,_,_=_project_record(permit.validate())
            if project_access.audio_kind(state)!='audio' or project_access.audio_identity(state)!=identity:
                raise ValueError('Current project source changed')
            media,selected=result.verify()
            self._set_audio(media,owner.get('source_duration',0),
                str(selected) if selected.suffix.lower()=='.ncm' else None)
        except (ArithmeticError,ImportError,OSError,ValueError,KeyError,RuntimeError,TypeError,AttributeError):
            owner['original_link']={'status':'missing','error':'未能确认原曲连接，请重新选择。'}
        else:
            self._linked_original=(permit,identity,result)
            self._project_source=str(media)
            owner.update(source=str(media),original_source=str(selected))
            owner.pop('original_link',None)
            try:
                self._refresh_sources(preserve=True)
            except (ArithmeticError,ImportError,OSError,ValueError,KeyError,RuntimeError,TypeError):
                self._error='原曲已连接，但暂时无法准备试听。'
        finally:
            self._publish()

    def _open_recent_from_list(self,value,client_request):
        self._cancel_recent_open()
        if not isinstance(value,str) or not any(item['path']==value for item in self._recent):
            raise ValueError('此目录不在最近项目列表中。')
        path=Path(value)
        external=project_access.isolated() and not path.is_relative_to(PROJECTS)
        if external and project_access.approved(path) is None:
            self._require_no_structure_draft()
            # A WebChannel payload is not project authorization. A native
            # confirmation grants this exact folder only for the current session.
            answer=QMessageBox.question(self.parent(),'打开外部工程',
                f'在原位置打开并允许编辑这个工程？\n\n{path}',
                QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if answer!=QMessageBox.StandardButton.Yes or self._closed:
                return
            if self.jobs.running or self._gpu_request:
                raise ValueError('后台任务已启动，请完成后重新打开项目。')
            self._require_no_structure_draft()
            self._begin_recent_open(path,client_request,external=True)
        else:
            self._begin_recent_open(path,client_request)

    def _begin_recent_open(self,path,client_request=None,*,external=False):
        self._require_no_structure_draft()
        def prepare_existing_editor(path):
            if external:
                return project_access.prepare(path)
            _project_record(path)
            return path  # no disk MIDI or speculative editor head to install
        if not hasattr(self,'_recent_open_signal'):
            self._recent_open_signal=_InputInspectionSignal(self)
            self._recent_open_signal.done.connect(self._recent_open_ready)
        self._recent_open_token=uuid4().hex
        # The initiating UI action has already run the synchronous score guard.
        state=getattr(self,'_leave_project_picker_state',None)
        self._recent_score_departure=(self._recent_open_token,state()) if state is not None else None
        self._recent_open_status={'version':getattr(self,'_recent_open_version',0)+1,
            'pending':True,'path':str(path),'request':client_request}
        self._recent_open_version=self._recent_open_status['version']
        self._publish()
        QThreadPool.globalInstance().start(_InputInspectionTask(path,self._recent_open_token,self._recent_open_signal,prepare_existing_editor))

    def _cancel_recent_open(self):
        self._recent_open_token=None
        self._recent_score_departure=None
        if getattr(self,'_recent_open_status',{}).get('pending'):
            self._recent_open_version+=1
            self._recent_open_status=dict(self._recent_open_status,pending=False,version=self._recent_open_version)
            self._publish()

    @Slot(str,object,str)
    def _recent_open_ready(self,token,result,error):
        if self._closed or token!=getattr(self,'_recent_open_token',None):return
        new_permit=None
        try:
            if error:raise ValueError(error)
            if self.jobs.running or self._gpu_request:raise ValueError('后台任务已启动，请完成后重新打开项目。')
            self._require_no_structure_draft()
            state=getattr(self,'_leave_project_picker_state',None)
            receipt=getattr(self,'_recent_score_departure',None)
            if state is not None and receipt != (token,state()):
                raise ValueError('等待打开工程期间，当前工程或右侧乐谱已变化。原工程保留，请重新打开目标。')
            if isinstance(result,project_access.Permit):
                if project_access.register(result) is None:new_permit=result
                result=result.root
            # The token proves liveness. Resolve the current owner at adoption,
            # not the saved revision seen by a previously queued open task.
            self._show_project(result,restore=True)
        except (ArithmeticError,ImportError,OSError,ValueError,KeyError,RuntimeError,TypeError) as exc:
            if new_permit is not None:project_access.forget(new_permit)
            self._error=str(exc)
        finally:
            self._cancel_recent_open()

    def _show_project(self,path,restore=False,prepared_editor=None):
        # Recheck at adoption (a native picker can enter a nested event loop).
        if self._edit_pending():raise ValueError('请等待当前编辑保存后再打开工程。')
        self._cancel_recent_open()
        path=project_access.existing_path(path)
        external=project_access.approved(path) is not None
        if restore or not self._project or self._project['path']!=str(path):
            self._require_no_structure_draft()
        state,export,report=_project_record(path)
        self._input_midi=None
        workflow=getattr(self,'_separation_workflow',None) or {}
        preview=workflow.get('preview') or {}
        promote=not restore and workflow.get('phase')=='scoring' and preview.get('project')==str(path)
        self._review_document=None
        self._review_notice=None
        export_tempo=export_metadata(export,'tempo_map.json')
        tempo=export_tempo or _metadata(path/'tempo_map.json') or None
        if report.get('instruments',{}).get('guitar',{}).get('mode')=='stem_only' and 'piano' not in state.get('instruments',[]):
            tempo=None
        same=self._project is not None and self._project['path']==str(path)
        if not same and getattr(self,'_structure_session',None):self._structure_session.clear()
        midi_resume=None
        if same and not restore and self._midi_file:
            old_editor=self._project.get('editor') or {}
            current_path=self._midi_file['path']
            was_editor=current_path==(old_editor.get('preview_path') or old_editor.get('path'))
            previous_midi=next((item for item in self._project['files']
                if item['kind']=='midi' and item['path']==current_path),None)
            if was_editor or previous_midi:
                transport=(self._midi_backend.state() if self._midi_backend and not any(self._midi_load_status())
                           else self._midi_state())
                midi_resume=(None if was_editor else previous_midi['name'],transport['position'],transport['playing'])
        source=state.get('source')
        try:
            if external:
                audio,source=self._external_project_audio(path,state)
            else:
                audio=_audio_file(state.get('source_audio') or source)
        except (OSError,ValueError):
            audio=None
            if external:source=None
        duration=state.get('duration',0.0)
        duration=float(duration) if isinstance(duration,(int,float)) and math.isfinite(duration) else 0.0
        if not self._audio or self._audio['path']!=str(audio):
            original=source if isinstance(source,str) and Path(source).suffix.lower()=='.ncm' else None
            self._set_audio(audio,duration,original)
        elif duration>0 and not self._audio['duration']:
            self._audio['duration']=duration
        self._project_source=str(audio) if audio else None
        from .score_artifacts import resolve_score_export
        score_export=resolve_score_export(path,state)
        score_report=_metadata(score_export/'report.json') if score_export else report
        score_snapshot=_metadata(score_export/'editor_snapshot.json') if score_export else {}
        harmony_export=score_export or export
        files=self._result_files(path,export,state,report,score_export)
        current_guitar=report.get('instruments',{}).get('guitar',{})
        if current_guitar.get('mode')=='stem_only':
            files.extend(self._historical_guitar_files(path,export))
        # A scoring commit adds metadata and artifacts to the same audition.
        # Refreshing an unchanged catalogue would stop its live output.
        preserve_audition=False
        if promote and audio and preview.get('original_audio')==str(audio):
            committed=[('original',str(audio)),*((item['instrument'],item['path'])
                for item in files if item['kind']=='audio')]
            active={row['key']:row['path'] for row in self._sources}
            preserve_audition=(len(committed)==len(self._sources)==len(active)
                               and dict(committed)==active)
        self._separation_workflow=None
        self._project={'path':str(path),'name':_project_title(path,state),'export':str(export),
            'report':report,'files':files,'tempo':tempo,
            'analysis_hint_evidence':build_analysis_hint_evidence(export,report,export_tempo,duration),
            'score_export':str(score_export) if score_export else None,
            'native_notes_score':state.get('native_notes_score'),
            'score_revision':score_snapshot.get('revision'),'score_report':score_report,
            'instruments':state.get('instruments',[]),
            'piano_analysis_key':(state.get('analysis_keys') or {}).get('piano'),
            'rhythm_provider':state.get('config',{}).get('rhythm_provider','beat_this'),
            'tempo_kind':state.get('tempo_kind'),
            'score_chords':bool(score_report.get('show_chords',state.get('config',{}).get('score',{}).get('show_chords',False))),
            'harmony_engine':score_report.get('harmony_engine') or state.get('config',{}).get('score',{}).get('harmony_engine'),
            'reviews':self._reviews(path),
            'harmony':{instrument:_harmony_for_display((harmony_export if instrument=='piano' else export)/f'{instrument}_harmony.json') for instrument in INSTRUMENTS},
            'source':str(audio) if audio else source if isinstance(source,str) else None,
            'original_source':source if isinstance(source,str) else None,
            'input_kind':project_access.audio_kind(state)}
        if external and self._project['input_kind']=='audio':
            self._project['source_duration']=duration
            if audio is None:self._project['original_link']={'status':'missing'}
        pdf_issue = self._reported_pdf_issue(score_report,files)
        if pdf_issue:
            self._project['score_pdf_issue'] = pdf_issue
        self._edit_identity()
        if restore:
            self._options.update({key:(list(value) if isinstance(value,list) else value)
                for key,value in self._new_task_options.items()})
            self._clear_overrides()
            if not external:
                self._options['output']=str(path.parent)
                self._approved_output_folders.add(path.parent)
            for key in ('instruments','keep_stems'):
                if key in state:
                    self._options[key]=self._option(key,state[key])
            config=state.get('config') or {}
            self._options['rhythm_provider']=self._option('rhythm_provider',config.get('rhythm_provider','beat_this'))
            if isinstance(config,dict) and 'quantization' in config:
                self._options['quantization']=self._option('quantization',config['quantization'])
            if config.get('score',{}).get('piano_engine') in ('music21','duet'):
                self._options['score_engine']=config['score']['piano_engine']
                self._options['score_pedal']=bool(config['score'].get('show_pedal',False))
                self._options['score_chords']=self._project['score_chords']
                voice_mode=config['score'].get('voice_mode','simple')
                self._options['score_voice_mode']=voice_mode if voice_mode in ('simple','detailed') else 'simple'
                if config['score'].get('duet_device') in ('cpu','cuda'):
                    self._options['score_duet_device']=config['score']['duet_device']
            mode=_export_mode([item for item in files if not item.get('retained_score')])
            if mode:
                self._options['mode']=mode
            self._phase='已打开缓存项目'
        self._preview_prefetch.activity()
        if not preserve_audition:
            self._refresh_sources(preserve=same)
        self._clear_midi()
        self._refresh_editor(prepared=prepared_editor)
        if restore or not same:
            self._scan_editor_recovery()
        if midi_resume:
            name,position,playing=midi_resume
            doc=self._project.get('editor') or {}
            midi=(doc.get('preview_path') or doc.get('path')) if name is None else next((item['path'] for item in files
                if item['kind']=='midi' and item['name']==name and not item.get('historical')),None)
            if midi:
                self._select_midi(midi,resume=(position,playing))
        pdf=next((item['path'] for item in files if item['kind']=='pdf' and not item.get('historical')),None)
        if not pdf and pdf_issue:
            pdf=next((item['path'] for item in files if item['kind']=='pdf' and item['instrument']=='piano'),None)
        self._clear_pdf()
        # Audio project navigation opens the stems page; PDF is requested by
        # changeTab('score'). Keep MIDI auto-score and export-refresh behavior.
        from . import native_score_runtime
        native_piano = native_score_runtime.enabled() and 'piano' in state.get('instruments', [])
        if pdf and not native_piano and (not restore and not promote or state.get('source_kind')=='midi'):
            try:
                self._select_pdf(pdf)
            except (OSError,ValueError,RuntimeError) as exc:
                self.logs.emit(str(exc))
        remembered=[item['path'] for item in self._recent if item['path']!=str(path)]
        self._prefs['recent']=[str(path),*remembered[:17]]
        self._refresh_recent()
        self._save_preferences()

    def _authorized_result(self,value,kind=None):
        path=_local_path(value)
        local=getattr(self,'_score_range_request',None)
        if kind in (None,'pdf','musicxml') and local and local.get('status')=='ready' and self._score_range_current(local):
            for field,file_kind in (('pdf','pdf'),('musicxml','musicxml')):
                candidate=local['result'].get(field)
                if (candidate and path==Path(candidate).resolve() and path.is_file()
                        and path.is_relative_to(self._score_range_cache(local['project']))
                        and kind in (None,file_kind)):
                    return path
        editor=(self._project or {}).get('editor') or {}
        if kind in (None,'midi') and str(path)==editor.get('preview_path') and path.is_file():
            return path
        files=self._project['files'] if self._project else []
        if path.is_file() and any(item['path']==str(path) and (kind is None or item['kind']==kind) for item in files):
            return path
        raise ValueError('此文件不在当前结果列表中。')

    def _clear_pdf(self):
        self._current_score_preview=None
        self._preview_pdf_token=None
        self._preview_pdf_pending=None
        self._preview_pdf_destination=None
        if self._pdf_path is not None:
            self._pdf_document.close()
        if self._pdf_buffer is not None:
            self._pdf_buffer.deleteLater()
            self._pdf_buffer=None
        self._pdf_path=None
        self._pdf={'pages':0,'index':0,'dataUrl':'','title':''}

    def _select_pdf(self,value):
        path=self._authorized_result(value,'pdf')
        if self._pdf_path==path:
            return
        self._clear_pdf()
        self._pdf_path=path
        self._pdf={'pages':0,'index':0,'dataUrl':'','title':path.name,'loading':True}
        self._queue_pdf(0)

    def _pdf_page(self,index):
        # Page arrows refer to the retained PDF until its replacement commits.
        # A late click on those arrows must not supersede the new generation.
        if self._preview_pdf_destination is not None:
            return
        if type(index) is not int or not 0<=index<self._pdf['pages']:
            raise ValueError('PDF 页码超出范围。')
        if index==self._pdf['index'] and (self._pdf.get('previewUrl') or self._pdf.get('dataUrl')) and not self._pdf.get('loading'):
            return
        self._queue_pdf(index)

    def _queue_pdf(self,index,*,replacement=None):
        self._preview_prefetch.activity()
        path=replacement or self._pdf_path
        identity=preview_cache_key(path)
        key=(*identity,index) if identity is not None else None
        token=uuid4().hex
        self._preview_pdf_token=token
        self._preview_pdf_destination=(replacement,index) if replacement else None
        if replacement:
            self._pdf.update(loading=True,replacementPath=str(path))
        else:
            self._pdf.pop('replacementPath',None)
            self._current_score_preview=None
            self._pdf.update(index=index,loading=True,dataUrl='',previewUrl='')
        if key is not None and key in self._preview_pdf_cache:
            self._install_score_preview(self._preview_pdf_cache[key])
            self._preview_pdf_pending=None
            return
        self._preview_pdf_pending=(token,path,index,key)
        self._start_pdf_worker()

    def _start_pdf_worker(self):
        if self._closed or self._preview_pdf_active or not self._preview_pdf_pending:
            return
        token,path,index,key=self._preview_pdf_pending
        self._preview_pdf_pending=None
        self._preview_pdf_active=True
        use_resources=getattr(self,'_score_preview_resources',False)
        def render_pdf(source):
            from .preview_worker import prepare_preview
            options={'image_bytes':True} if use_resources else {}
            return {'key':key,'page':prepare_preview('pdf',source,index,**options)}
        QThreadPool.globalInstance().start(_InputInspectionTask(path,token,self._preview_pdf_signal,render_pdf))

    def _install_score_preview(self,page):
        destination=getattr(self,'_preview_pdf_destination',None)
        if destination:
            self._pdf_path=destination[0]
            self._pdf['title']=self._pdf_path.name
            self._pdf.pop('replacementPath',None)
            self._preview_pdf_destination=None
            if self._project:
                self._project.pop('score_pdf_issue',None)
        png=page.get('previewPng')
        if png is not None:
            from .audio_output.score_preview_resources import PREFIX
            url=page.get('previewUrl')
            if not url:
                url=page['previewUrl']=PREFIX+uuid4().hex
            self._current_score_preview=(url,png)
        self._pdf.update({name:value for name,value in page.items() if name!='previewPng'},loading=False)

    def _pdf_prepared(self,token,result,error):
        self._preview_pdf_active=False
        if self._closed:
            return
        if not error and result['key'] is not None:
            try:
                identity=preview_cache_key(Path(result['key'][0]))
                fresh=identity is not None and result['key']==(*identity,result['page']['index'])
            except OSError:fresh=False
            if not fresh:
                if token==self._preview_pdf_token:
                    destination=getattr(self,'_preview_pdf_destination',None)
                    try:
                        if destination:
                            self._queue_pdf(destination[1],replacement=destination[0])
                        else:
                            self._queue_pdf(self._pdf['index'])
                    except (OSError,ValueError) as exc:
                        self._pdf_render_error(str(exc))
                self._start_pdf_worker();return
            self._preview_pdf_cache[result['key']]=result['page']
            while len(self._preview_pdf_cache)>6:
                self._preview_pdf_cache.pop(next(iter(self._preview_pdf_cache)))
        if token==self._preview_pdf_token:
            self._pdf.update(loading=False)
            if error:
                self._pdf_render_error(error)
            else:
                self._install_score_preview(result['page'])
                self._publish()
        self._start_pdf_worker()

    def _pdf_render_error(self,error):
        self._pdf['loading']=False
        self._pdf.pop('replacementPath',None)
        destination=self._preview_pdf_destination
        self._preview_pdf_destination=None
        if destination and self._project:
            retained=bool(self._pdf.get('previewUrl') or self._pdf.get('dataUrl'))
            message='新版 PDF 预览读取失败。' + ('仍显示上一版谱面。' if retained else '')
            self._project['score_pdf_issue']=dict(message=message,retry_path=str(destination[0]))
            self.logs.emit(error)
            self._publish()
        else:
            self._problem(error)

    @Slot(str)
    def _midi_tick(self,data):
        self._midi_transport={**json.loads(data),'transport_order':monotonic()}
        if not self._closed:
            self._midi_timer.start(0)

    def _notify_midi(self):
        self._midi_transport['transport_order']=monotonic()
        if not self._closed:self._midi_timer.start(0)

    def _midi_load_status(self):
        loading,error=self._edit_midi_status()
        return loading or (not error and bool((self._midi_file or {}).get('loading'))),error

    def _midi_state(self):
        loading,error=self._midi_load_status()
        pending=self._pending_midi_intent or {}
        state={**self._midi_transport,'loading':loading,'preparing':loading,
               'play_pending':bool(loading and pending.get('playing'))}
        if pending:state['position']=pending['position']
        if loading or error:
            # Available while loading means the transport accepts requests,
            # not that notes can sound yet. Never report queued Play as playing.
            state.update(playing=False,available=loading,sampled_at=None,
                         message=error or '正在准备 MIDI 试听…')
            if self._edit_preview_selected():state['duration']=self._project['editor']['duration']
        return state

    def _midi_checkpoint(self):
        backend=self._midi_backend
        state=backend.state() if backend else self._midi_transport
        playing=bool(backend and state.get('playing'))
        audio_pending=self._pending_audio or (0,False)
        joint=bool(backend and getattr(self,'_audition_together',False) and (playing or audio_pending[1]))
        return dict(position=audio_pending[0]/1000 if joint and audio_pending[1] else state.get('position',0.),
                    playing=joint or (playing and not (self._media.isPlaying() or audio_pending[1])),
                    audition='joint' if joint else 'midi' if playing else None)

    def _suspend_midi_output(self):
        if not self._midi_backend:return
        if getattr(self,'_audition_together',False):
            self._audition_together=False
            self._pending_audio=None
            self._media.pause()
        self._midi_backend.pause()

    def _hold_editor_midi(self):
        if not self._edit_preview_selected():return
        if self._pending_midi_intent is None:self._pending_midi_intent=self._midi_checkpoint()
        self._suspend_midi_output()
        self._notify_midi()

    def _cancel_midi_intent(self):
        if self._pending_midi_intent is not None:
            self._pending_midi_intent=dict(position=self._pending_midi_intent['position'],playing=False)
            self._notify_midi()

    def _defer_midi_transport(self,*,toggle=None,**delta):
        loading,error=self._midi_load_status()
        if not (loading or error):return False
        current=self._pending_midi_intent or self._midi_checkpoint()
        if toggle is not None:
            delta.update(audition=toggle,playing=not (current.get('audition')==toggle and current.get('playing')))
        if error:delta.update(playing=False,audition=None)
        self._pending_midi_intent={**current,**delta}
        self._notify_midi()
        return True

    def _midi_selection_resume(self,resume):
        if resume is not None:
            return dict(position=resume[0],audition='midi',playing=bool(resume[1]
                        and not self._media.isPlaying() and not (self._pending_audio or (0,False))[1]))

    def _flush_midi_intent(self):
        if self._closed or not self._midi_file or self._pending_midi_intent is None:return
        loading,error=self._midi_load_status()
        if error:self._cancel_midi_intent()
        if loading or error:return
        intent,self._pending_midi_intent=self._pending_midi_intent,None
        try:self._apply_midi_intent(intent)
        except Exception as exc:self._problem(str(exc) or type(exc).__name__)
        finally:self._notify_midi()

    def _apply_midi_intent(self,intent):
        backend=self._midi_backend
        if not backend:return
        position=min(intent['position'],backend.duration)
        selection=getattr(self,'_audition_range',None)
        if selection:backend.set_loop(selection['start'],selection['end'],selection['loop'])
        backend.seek(position)
        # A deferred seek to/past EOF parks there; it must not restart at zero.
        if intent.get('playing') and position<backend.duration:
            if not backend.available:raise ValueError(backend.state().get('message') or '当前 MIDI 无法试听。')
            self._shared_leave('MIDI audition uses legacy')
            if intent.get('audition')=='joint':
                if not self._audio:raise ValueError('此工程没有可叠加试听的音频。')
                self._select_audio('original',preserve=False)
                self._audition_together=True
                self._pending_audio=(round(position*1000),True)
                self._resume_audio()
            else:
                self._audition_together=False
                self._stop_audio()
                backend.play()
        self._midi_transport={**backend.state(),'transport_order':monotonic()}

    def _seek_midi(self,position):
        if self._defer_midi_transport(position=position):return
        if not self._midi_backend:raise ValueError('请先选择一个 MIDI 结果。')
        self._midi_backend.seek(position)

    def _stop_midi(self):
        if self._defer_midi_transport(position=0.,playing=False,audition=None):return
        self._cancel_midi_intent()
        if self._midi_backend:self._midi_backend.stop()

    def _set_midi_volume(self,volume):
        self._midi_volume=volume
        if self._midi_backend:self._midi_backend.set_volume(0 if self._muted else volume)

    def _set_midi_range(self,data):
        if not self._midi_file or data.get('path')!=self._midi_file['path']:
            raise ValueError('MIDI 已切换，请重新设置定位范围')
        state=self._midi_state()
        limit=state['duration'] or (float('inf') if state['loading'] else 0.)
        start=_number(data.get('start'),0,limit,'L 位置超出 MIDI 范围')
        end=_number(data.get('end'),0,limit,'R 位置超出 MIDI 范围')
        if type(data.get('loop')) is not bool:raise ValueError('循环状态无效')
        self._audition_range=dict(start=start,end=end,loop=data['loop'])
        if not any(self._midi_load_status()):self._apply_midi_loop(start,end,data['loop'])

    def _apply_midi_loop(self,start,end,loop):
        if self._midi_backend:self._midi_backend.set_loop(start,end,loop)

    def _clear_midi(self):
        self._midi_installed_key=None
        self._preview_midi_token=None
        self._preview_midi_pending=None
        self._pending_midi_intent=None
        if self._midi_backend:
            self._midi_backend.close()
            self._midi_backend.deleteLater()
            self._midi_backend=None
        self._midi_file=None
        self._midi_transport={'transport_order':monotonic(),'duration':0.0,'position':0.0,'playing':False,'available':False,'message':''}

    def _select_midi(self,value,*,resume=None,prepared=None):
        if isinstance(value,str) and value.startswith('astra-editor:'):
            return self._select_live_editor(value,resume)
        self._preview_prefetch.activity()
        path=self._authorized_result(value,'midi')
        same=bool(self._midi_file and self._midi_file['path']==str(path))
        if same and prepared is None:
            return
        key=preview_cache_key(path)
        if prepared is not None:
            if key is None or prepared['path']!=path or prepared['key']!=key:
                raise ValueError('MIDI 预览文件已变化，未切换试听。')
            if same and not self._midi_file.get('loading') and getattr(self,'_midi_installed_key',None)==key:
                self._sync_editor_transport(editor_swap=True)
                return
        pending=(self._pending_midi_intent if prepared is not None and resume is None
                 else self._midi_selection_resume(resume))
        previous_duration=self._midi_transport.get('duration',0)
        # Preserve Claude's R16 display continuity during revision replacement.
        # This is display-only; the retired audio owner never remains playable.
        previous_display=(self._midi_file if (resume is not None or prepared is not None)
                          and self._midi_file and not self._midi_file.get('loading') else None)
        self._clear_midi()
        self._pending_midi_intent=pending
        if pending is not None:
            self._midi_transport.update(position=pending['position'],duration=previous_duration)
        token=uuid4().hex
        self._preview_midi_token=token
        self._midi_file={**(previous_display or {}),'path':str(path),'name':path.stem,
                         'notes':(previous_display or {}).get('notes',[]),'loading':True}
        self._notify_midi()
        if prepared is not None:
            # The editor lane already parsed this exact asset. No intermediate
            # loading snapshot or unguarded second completion for this swap.
            try:
                self._install_parsed_midi(path,prepared['parsed'])
                self._midi_installed_key=key
                self._remember_parsed_midi(key,prepared['parsed'])
            except Exception:
                # Do not leave a loading row or partially installed player.
                self._clear_midi()
                raise
            return
        if key is not None and key in self._preview_midi_cache:
            self._midi_prepared(token,{'path':path,'key':key,'parsed':self._preview_midi_cache[key]},'',_from_worker=False)
            return
        self._preview_midi_pending=(token,path,key)
        self._start_midi_worker()

    def _start_midi_worker(self):
        if self._closed or self._preview_midi_active or not self._preview_midi_pending:return
        token,path,key=self._preview_midi_pending
        self._preview_midi_pending=None
        self._preview_midi_active=True
        def parse(source):
            from .preview_worker import prepare_preview, unpack_midi
            return {'path':source,'key':key,'parsed':unpack_midi(prepare_preview('midi',source))}
        QThreadPool.globalInstance().start(_InputInspectionTask(path,token,self._preview_midi_signal,parse))

    def _midi_prepared(self,token,result,error,*,_from_worker=True):
        if _from_worker:self._preview_midi_active=False
        if self._closed or token!=self._preview_midi_token:
            self._start_midi_worker()
            return
        try:
            if error:raise ValueError(error)
            path=self._authorized_result(result['path'],'midi')
            key=result['key']
            if key is not None:
                fresh=preview_cache_key(path)
                if fresh!=key:
                    # Retry this selection, not a new selection that would
                    # discard Play/Seek received while this parse was in flight.
                    self._preview_midi_pending=(token,path,fresh)
                    return
            self._install_parsed_midi(path,result['parsed'])
            self._midi_installed_key=key
            self._remember_parsed_midi(key,result['parsed'])
            self._flush_midi_intent()
            self._publish()
        except Exception as exc:
            self._clear_midi()
            self._problem(str(exc) or type(exc).__name__)
        finally:
            self._notify_midi()
            self._start_midi_worker()

    def _remember_parsed_midi(self,key,parsed):
        if key is None:return
        self._preview_midi_cache[key]=parsed
        while len(self._preview_midi_cache)>3:
            self._preview_midi_cache.pop(next(iter(self._preview_midi_cache)))

    def _install_parsed_midi(self,path,parsed):
        if self._midi_backend is None:
            from .midi_preview import MidiPreview

            self._midi_backend=MidiPreview(self,available_hint=parsed[3].get('_synth_available'))
            self._midi_backend.changed.connect(self._midi_tick)
            self._midi_backend.error.connect(self._problem)
            volume=getattr(self,'_midi_volume',None)
            if self._muted or volume is not None:
                self._midi_backend.set_volume(0 if self._muted else volume)
        self._midi_file=self._midi_backend.load_prepared(path,parsed)
        if not self._sync_editor_transport():
            self._midi_backend.set_rate(getattr(self,'_audition_rate',1.0))
        self._midi_transport={**self._midi_backend.state(),'transport_order':monotonic()}

    def _midi_play_pause(self):
        if self._defer_midi_transport(toggle='midi'):return
        self._shared_leave('MIDI audition uses legacy')
        if not self._midi_file or not self._midi_backend:
            raise ValueError('请先选择一个已就绪的 MIDI 结果。')
        if self._midi_backend.state()['playing']:
            self._midi_backend.pause()
        else:
            self._stop_audio()
            self._midi_backend.play()

    def _native(self,action):
        project_picker = action in ('chooseAudio','chooseMidi','chooseProject')
        if project_picker:
            self._require_no_structure_draft()
        state = getattr(self, '_leave_project_picker_state', None) if project_picker else None
        before = state() if state is not None else None

        def can_accept():
            if self._closed or getattr(self, '_editor_closing', False):
                return False
            if state is None:
                return True
            if not before[0] or before[3] or state() != before:
                self._problem('选取文件时当前工程或右侧乐谱已变化。请确认后再试，已保留原工程。')
                return False
            return True

        parent=self.parent()
        output=self._options['output']
        if action=='chooseAudio':
            folder=str(Path(self._audio.get('original_path') or self._audio['path']).parent) if self._audio else ''
            filters='音频文件 ('+' '.join('*'+suffix for suffix in sorted(AUDIO_IMPORT_TYPES))+')'
            path,_=QFileDialog.getOpenFileName(parent,'选择要转谱的音频',folder,filters)
            if path and can_accept():
                self.accept_audio(path)
        elif action=='chooseMidi':
            folder=str(Path(self._input_midi['path']).parent) if self._input_midi else ''
            path,_=QFileDialog.getOpenFileName(parent,'选择要制谱的 MIDI',folder,'MIDI 文件 (*.mid *.midi)')
            if path and can_accept():
                self.accept_midi(path)
        elif action=='chooseOutput':
            path=QFileDialog.getExistingDirectory(parent,'选择输出目录',output)
            if path:
                folder=project_write_path(_local_path(path))
                self._approved_output_folders.add(folder)
                self._update_options({'output':str(folder)})
                self._refresh_recent()
        elif action=='chooseProject':
            path=QFileDialog.getExistingDirectory(parent,'选择含 project.json 的项目文件夹',str(PROJECTS))
            if path and can_accept():
                self.accept_project(path)
        elif action=='chooseTempo':
            path,_=QFileDialog.getOpenFileName(parent,'选择速度图','',
                '速度图 (*.json *.mid *.midi)')
            if path:
                self._options.update(tempo_path=str(_local_path(path)),bpm=None,beat_scale=1.0)
        elif action=='importReview':
            if not self._project:
                raise ValueError('请先打开要接收审核意见的项目。')
            path,_=QFileDialog.getOpenFileName(parent,'导入 Astra 谱面审核意见',self._project['path'],'JSON 文件 (*.json)')
            if path:
                self._import_review(path)

    def _new(self):
        self._require_no_structure_draft()
        self._interrupted_open_token=None
        if getattr(self,'_structure_session',None):self._structure_session.clear()
        self._audition_together=False
        self._audition_range=None
        self._editor_metronome=False
        self._set_audition_rate(1.0)
        # Capture the departing instrument while its project owner is still
        # retained, before changing identity can evict an apparently clean owner.
        self._clear_midi()
        self._set_audio(None)
        self._input_midi=None
        self._separation_workflow=None
        self._project=None
        self._edit_identity()
        self._options.update({key:(list(value) if isinstance(value,list) else value)
            for key,value in self._new_task_options.items()})
        self._review_document=None
        self._review_notice=None
        self._project_source=None
        self._request=None
        self._clear_overrides()
        self._clear_pdf()
        self._refresh_sources(preserve=False)
        self._refresh_recent()
        self._phase='等待选择音频或 MIDI'

    def _request_for(self,kind,*,tempo_override=None):
        if kind=='reexport' and not self._project:
            raise ValueError('请先打开含有成功导出的项目。')
        audio=self._audio or {}
        same_project=bool(self._project and audio.get('path') and audio['path']==self._project_source)
        external=bool(self._project and project_access.approved(self._project['path']) is not None)
        if external and kind=='run' and (same_project or not audio):
            raise ValueError('外部工程可原地编辑和导出。要重新分析原曲，请从「新建转谱」选择音频。')
        if kind=='reexport' or same_project:
            instruments=list(self._project.get('instruments',self._options['instruments']))
        else:
            instruments=self._import_audio_targets()
        if kind=='run' and (not audio or audio.get('import_loading') or audio.get('import_error') or not instruments):
            raise ValueError('请选择有效音频和至少一种乐器。')
        guitar_only='guitar' in instruments and 'piano' not in instruments
        options={'mode':'all'} if guitar_only else {key:self._options[key] for key in JOB_OPTIONS}
        if tempo_override is not None:
            options.update(tempo_path=str(tempo_override),bpm=None,beat_scale=1.0)
        if options.get('tempo_path'):
            tempo=Path(options['tempo_path'])
            if not tempo.is_file() or tempo.suffix.lower() not in TEMPO_TYPES:
                raise ValueError('请选择存在的 JSON 或 MIDI 速度图。')
            if options['bpm'] is not None:
                raise ValueError('请在速度图和固定 BPM 之间选择一项。')
        return {'kind':kind,'audio':(audio.get('original_path') or audio['path']) if audio and not (external and kind=='reexport') else '',
            'output':str(project_write_path(self._options['output'])),
            'project':str(project_access.existing_path(self._project['path'])) if self._project else '',
            'instruments':instruments,'keep_stems':guitar_only or self._options['keep_stems'],
            'quantization':self._options['quantization'],'piano_model':self._options['piano_model'],
            'rhythm_provider':self._options['rhythm_provider'],
            'score_engine':self._options['score_engine'],'score_pedal':self._options['score_pedal'],
            'score_chords':self._options['score_chords'],'score_voice_mode':self._options['score_voice_mode'],
            'score_duet_device':self._options['score_duet_device'],
            'options':options}

    def _midi_request(self,tracks,channels):
        source=self._input_midi
        if not source or source.get('loading') or source.get('error'):
            raise ValueError('请先读取一个有效的 MIDI 文件。')
        if not isinstance(tracks,list) or not tracks or any(type(index) is not int for index in tracks):
            raise ValueError('请至少选择一条包含音符的 MIDI 轨道。')
        rows={track['index']:track for track in source['tracks'] if track['note_count']}
        if any(index not in rows for index in tracks):
            raise ValueError('所选轨道没有可制谱音符。')
        available={channel for index in tracks for channel in rows[index]['channels'] if channel!=9}
        if not available:
            raise ValueError('鼓轨不能用于钢琴制谱。')
        if channels is not None and (not isinstance(channels,list) or not channels or
                any(type(channel) is not int or channel not in available for channel in channels)):
            raise ValueError('请选择所选轨道中的非鼓声道。')
        return {'kind':'midi_import','midi':source['path'],'output':str(project_write_path(self._options['output'])),
            'tracks':list(dict.fromkeys(tracks)),'channels':None if channels is None else list(dict.fromkeys(channels)),
            'quantization':self._options['quantization'],'score_engine':self._options['score_engine'],
            'score_pedal':self._options['score_pedal'],'score_chords':self._options['score_chords'],
            'score_voice_mode':self._options['score_voice_mode'],
            'score_duet_device':self._options['score_duet_device'],
            'options':{'mode':'all'}}

    def _separation_state(self):
        workflow=getattr(self,'_separation_workflow',None)
        if not workflow:return None
        preview=workflow.get('preview') or {}
        return {'phase':workflow['phase'],'can_score':'piano' in preview.get('stem_paths',{}),
                'has_stems':bool(preview.get('stem_paths')),'project':preview.get('project'),
                'pending_score':bool(workflow.get('pending_score'))}

    def _start_separation(self,request):
        self._start_with_gpu({**request,'workflow_phase':'separation'})

    def _separation_completed(self,preview,*,auto_continue=True):
        request=self._request or {}
        if request.get('workflow_phase')!='separation' or not self._audio:
            return
        try:
            project=Path(preview['project']).resolve(strict=True)
            source=Path(preview['source']).resolve(strict=True)
            targets=request['instruments']
            paths={}
            duration=float(preview['duration'])
            if (source!=Path(request['audio']).resolve(strict=True) or preview['instruments']!=targets
                    or project.parent!=Path(request['output']).resolve(strict=True)
                    or not math.isfinite(duration) or duration<=0):
                raise ValueError('分轨任务结果与当前音频不匹配。')
            for key,path in preview['stem_paths'].items():
                path=Path(path).resolve(strict=True)
                if key not in targets or not path.is_relative_to(project/'stems') or not path.is_file():
                    raise ValueError('分轨任务返回了无效音轨。')
                paths[key]=str(path)
            remembered={**preview,'project':str(project),'duration':duration,'stem_paths':paths,
                        'original_audio':self._audio['path']}
            mode=request.get('options',{}).get('mode','all')
            complete=preview.get('complete',True) and set(paths)==set(targets)
            if not paths:
                raise ValueError('没有可恢复的完整分轨。')
            pending_score=remembered['origin'] if complete and auto_continue and 'piano' in paths and mode in ('midi','score','all') else None
            workflow={'phase':'separated' if complete else 'partial','request':request,
                      'preview':remembered,'pending_score':pending_score}
            self._separation_workflow=workflow
            self._audio.update(duration=duration,requested_instruments=list(targets))
            self._refresh_sources(preserve=True)
        except (OSError,ValueError,RuntimeError,TypeError,KeyError) as exc:
            self._failed(str(exc))
            return
        self._job_progress.finish()
        self._phase='分轨完成' if complete else '已恢复完成的分轨'
        self._error=None
        if pending_score and not self._closed:
            # Send the verified waveform catalogue before the next worker can
            # publish any scoring state.
            self._emit_state()
            QTimer.singleShot(0,lambda:self._start_separated_score(workflow,pending_score))
        else:
            self._publish()

    def _start_separated_score(self,workflow,origin):
        if (self._closed or self.jobs.running or self._gpu_request
                or workflow is not self._separation_workflow or workflow.get('pending_score')!=origin):
            return
        workflow['pending_score']=None
        try:
            self._continue_score()
        except (OSError,ValueError,RuntimeError,TypeError) as exc:
            self._failed(str(exc))
        else:
            self._publish()

    def _continue_separation(self):
        workflow=getattr(self,'_separation_workflow',None) or {}
        preview=workflow.get('preview') or {}
        if (self.jobs.running or self._gpu_request or workflow.get('phase')!='partial'
                or not self._audio or preview.get('original_audio')!=self._audio['path']):
            raise ValueError('当前没有可以续做的分轨任务。')
        request={**workflow['request'],'workflow_phase':'separation','workflow_origin':preview['origin']}
        self._job_progress.start()
        self._submit(request)

    def _continue_score(self):
        workflow=getattr(self,'_separation_workflow',None) or {}
        if workflow.get('pending_score'):
            return
        preview=workflow.get('preview') or {}
        if (workflow.get('phase')!='separated' or 'piano' not in preview.get('stem_paths',{})
                or not self._audio or preview.get('original_audio')!=self._audio['path']):
            raise ValueError('当前音频没有可继续制谱的钢琴分轨。')
        request={**workflow['request'],'workflow_phase':'score','workflow_origin':preview['origin']}
        self._submit(request)

    def _submit(self,request):
        if getattr(self,'_ornament_busy',False):
            raise ValueError('装饰候选正在读取或预览，请完成后再生成谱面。')
        if getattr(self,'_bar_phase_busy',False):
            raise ValueError('拍点候选分析尚未结束，请稍后再启动生成。当前工程未修改。')
        if 'tempo_base_export' in request:
            try:
                self._require_tempo_base(request['tempo_base_export'])
            except (OSError,ValueError,RuntimeError,TypeError) as exc:
                self._problem(str(exc))
                return
        request=self._bind_working_export(request)
        self._log_lines=[]
        workflow=getattr(self,'_separation_workflow',None)
        if request.get('workflow_phase')=='separation':
            if not workflow or workflow.get('phase')!='preparing':
                self._job_progress.start()
            preview=(workflow or {}).get('preview') if request.get('workflow_origin') else None
            self._separation_workflow={'phase':'resuming' if preview else 'separating','request':request}
            if preview:
                self._separation_workflow['preview']=preview
        else:
            self._job_progress.start()
            if request.get('workflow_phase')=='score' and workflow:
                workflow['phase']='scoring'
        self._request=request
        self._error=None
        self._phase={'reexport':'正在启动重导出…','midi_import':'正在使用 MIDI 制谱…','harmony_analysis':'正在准备和弦分析…',
                     'chord_visibility':'正在更新和弦记号…'}.get(request['kind'],'正在启动转谱…')
        if request.get('workflow_phase')=='score':
            mode=request.get('options',{}).get('mode','all')
            self._phase={'midi':'正在生成 MIDI…','score':'正在生成乐谱…'}.get(mode,'正在生成 MIDI 和乐谱…')
        if request.get('workflow_phase')=='separation':
            self._phase='正在准备分轨…'
        if request['kind']=='run' and request.get('instruments')==['guitar']:
            self._phase='正在启动吉他分轨…'
        self.jobs.start(request)

    def _start_with_gpu(self,request):
        self._require_no_structure_draft()  # Preserve retired drafts, do not block export.
        if getattr(self,'_ornament_busy',False):
            raise ValueError('装饰候选仍在处理，请稍后再生成谱面。')
        if getattr(self,'_bar_phase_busy',False):
            raise ValueError('拍点候选仍在分析，请等待完成后再启动模型或导出。当前生效图保持。')
        if 'tempo_base_export' in request:
            self._submit(request)
            return
        uses_model=request['kind'] in ('run','harmony_analysis') or (
            request.get('score_engine')=='duet' and request['options'].get('mode')!='midi'
            and (request['kind']=='midi_import' or 'piano' in request.get('instruments',['piano'])))
        project=self._project or {}
        wants_harmony=request.get('score_chords') or project.get('harmony_engine') in ('chordformer','tsumugi')
        check_harmony_cache=(not uses_model and request['kind']=='reexport' and wants_harmony
            and project.get('input_kind')=='audio' and 'piano' in request.get('instruments',[]))
        if not uses_model and not check_harmony_cache:
            self._submit(request)
            return
        token=uuid4().hex
        self._gpu_request=request
        self._gpu_preflight={'token':token,'status':'probing'}
        if request.get('workflow_phase')=='separation':
            self._separation_workflow={'phase':'preparing','request':request}
            self._request=request
            self._phase='正在检查缓存与计算设备…'
            self._job_progress.start()
        self._error=None
        self._publish()

        def probe(_unused):
            if request['kind']=='run' and not request.get('workflow_phase'):
                from .export_readiness import cached_run_ready
                if cached_run_ready(request):return {'cached_run':True}
            if request['kind']=='reexport':
                from .export_readiness import cached_reexport_ready
                if cached_reexport_ready(request):return {'cached_reexport':True}
            if check_harmony_cache:
                from .desktop_worker import chord_inference_needed

                if not chord_inference_needed(request):
                    return {'cached_reexport':True}
            from .device_policy import probe_gpu_status

            return probe_gpu_status()

        QThreadPool.globalInstance().start(_InputInspectionTask(None,token,self._gpu_probe,probe))

    def _gpu_checked(self,token,result,error):
        pending=self._gpu_preflight
        if (self._closed or not pending or pending['token']!=token
                or pending['status']!='probing'):
            return
        # First-pass separation follows the same device choice as other jobs.
        # A busy hint must not silently choose CPU for the user's new task.
        if result and result.get('cached_run'):
            request=self._gpu_request
            self._gpu_request=self._gpu_preflight=None
            self._submit({**request,'cache_only_run':True})
        elif result and result.get('cached_reexport'):
            request=self._gpu_request
            self._gpu_request=self._gpu_preflight=None
            self._submit({**request,'cache_only_reexport':True})
        elif result and result.get('available') and not result.get('busy'):
            request=self._gpu_request
            self._gpu_request=self._gpu_preflight=None
            # Preflight has resolved the preference for this job. The worker
            # still checks GPU availability, but must not reopen the busy
            # decision when utilization changes between these two processes.
            self._submit({**request,'execution_device':'gpu'})
        else:
            details=result or {'available':False,'busy':False,'reason':error or '无法确认 GPU 是否可用。'}
            self._gpu_preflight={**details,'token':token,'status':'choice'}
        self._publish()

    def _choose_execution_device(self,token,device):
        current=self._gpu_preflight
        if not current or token!=current['token']:
            return
        if device=='cancel':
            if self._gpu_request and self._gpu_request.get('workflow_phase')=='separation':
                self._stop_desktop_job()
            else:
                self._gpu_request=self._gpu_preflight=None
            return
        if current['status']!='choice' or device not in ('gpu','cpu') or (device=='gpu' and not current.get('available')):
            raise ValueError('请选择此任务可用的计算设备。')
        request=self._gpu_request
        self._gpu_request=self._gpu_preflight=None
        self._submit({**request,'execution_device':device})

    def _stage(self,phase):
        if self._phase!=phase:
            self._job_progress.stage_changed()
        self._phase=phase
        self._publish()

    def _adopt_editor_score_export(self,path,revision):
        # Updating the published export does not reload the live editor,
        # audition sources, or the mounted VST route.
        state,export,report=_project_record(path)
        project=self._project
        if str(export)==project['export']:
            return '编辑已更新，本次导出保留在历史中'
        if report.get('editor_revision')!=revision:
            raise ValueError('当前发布的谱面与这次更新的编辑版本不一致。')
        from .score_artifacts import resolve_score_export
        editor=project['editor']
        score_export=resolve_score_export(path,state,editor.get('source_key'))
        score_snapshot=export_metadata(export,'editor_snapshot.json')
        if score_export!=export or score_snapshot.get('revision')!=revision:
            raise ValueError('本次生成的乐谱未通过工程来源或编辑版本核验。')
        score_report=report
        export_tempo=export_metadata(export,'tempo_map.json')
        files=self._result_files(path,export,state,report,score_export)
        if report.get('instruments',{}).get('guitar',{}).get('mode')=='stem_only':
            files.extend(self._historical_guitar_files(path,export))
        preserved_kinds=('audio','source_midi')
        files=[item for item in files if item['kind'] not in preserved_kinds]
        files.extend(item for item in project['files'] if item['kind'] in preserved_kinds)
        previous=[item for item in project['files'] if item['kind']=='pdf' and
                  Path(item['path']).is_file() and Path(item['path']).resolve().is_relative_to(path/'exports')]
        all_paths={item['path'] for item in files}
        shown=next((item for item in previous if item['path']==str(self._pdf_path)),None)
        for instrument in INSTRUMENTS:
            retained=shown if shown and shown['instrument']==instrument else next(
                (item for item in previous if item['instrument']==instrument),None)
            if retained and retained['path'] not in all_paths:
                files.append(dict(retained,historical=True))
        fresh_pdf=next((item for item in files if item['kind']=='pdf' and
                        item['instrument']=='piano' and Path(item['path']).parent==export),None)

        from . import live_owner as live
        entry=live.retained(path)
        if entry is not None:
            entry.refresh_marks()
            exported=entry.marks['exported_revision']
            editor.update(exported_revision=exported,dirty=exported!=editor['revision'],
                          score_stale=entry.marks['score_revision']!=editor['revision'])
        from .score_commit import NATIVE_KEY
        project.update(
            export=str(export),report=report,files=files,
            native_notes_score=state.get(NATIVE_KEY),
            analysis_hint_evidence=build_analysis_hint_evidence(export,report,export_tempo,editor['duration']),
            score_export=str(score_export) if score_export else None,
            score_revision=score_snapshot.get('revision'),score_report=score_report,
            score_chords=bool(score_report.get('show_chords',project.get('score_chords'))),
            harmony_engine=score_report.get('harmony_engine') or project.get('harmony_engine'),
            harmony={instrument:_harmony_for_display(((score_export or export) if instrument=='piano' else export)/f'{instrument}_harmony.json')
                     for instrument in INSTRUMENTS})
        project.pop('score_pdf_issue',None)
        if getattr(self,'_preview_pdf_destination',None):
            self._preview_pdf_destination=None
            self._preview_pdf_pending=None
            self._preview_pdf_token=None
            self._pdf['loading']=False
            self._pdf.pop('replacementPath',None)
        if fresh_pdf:
            if self._pdf_path is None or shown and shown['instrument']=='piano':
                self._queue_pdf(0,replacement=Path(fresh_pdf['path']))
            return None
        last_good=any(item['kind']=='pdf' and item['instrument']=='piano' and item.get('historical') for item in files)
        message='本次未生成 PDF。' + ('可查看上一次可用的谱面。' if last_good else '')
        project['score_pdf_issue']=self._reported_pdf_issue(report,files) or dict(message=message)
        return '已完成导出，PDF 未更新'

    def _completed(self,project):
        self._job_progress.finish()
        request=self._request
        current=self._project
        guard=getattr(self,'_leave_project_guard',None)
        pending_target=(current or {}).get('path')
        try:
            switch=bool(pending_target and _local_path(pending_target)!=_local_path(project))
        except (OSError,TypeError,ValueError):
            switch=False  # Normal adoption reports an invalid result.
        if switch and guard is not None:
            def after_save():
                if not self._closed and self._project is current and self._request is request:
                    self._completed(project)
            if not guard(after_save):
                if not self._closed and self._project is current and self._request is request:
                    self._refresh_recent()
                    self._phase='任务已完成，已保留当前工程。可从最近工程打开结果。'
                    self._publish()
                return
            if self._closed or self._project is not current or self._request is not request:
                return
        self._complete_project_adoption(project)

    def _complete_project_adoption(self,project):
        request=self._request or {}
        kind=request.get('kind')
        notice=None
        try:
            path=_local_path(project)
            if (kind=='reexport' and request.get('editor_revision') and request['options'].get('mode') in ('all','score')
                    and self._project and self._project.get('editor') and self._project['path']==str(path)
                    and _local_path(request['project'])==path):
                notice=self._adopt_editor_score_export(path,request['editor_revision'])
            else:
                self._show_project(project)
        except (OSError,ValueError,RuntimeError,TypeError) as exc:
            self._failed(str(exc))
            return
        self._phase=notice or {'chord_visibility':'和弦记号已更新','harmony_analysis':'和弦分析已更新'}.get(kind,'处理完成')
        self._error=None
        if kind in ('chord_visibility','harmony_analysis'):
            self._options['score_chords']=self._project['score_chords']
            self._save_preferences()
        else:
            self._options['beat_scale']=1.0
        if self._request and 'tempo_base_export' in self._request:
            self._options.update(tempo_path=None,bpm=None)
        self._publish()

    def _failed(self,message):
        self._notes_score_terminal()
        self._job_progress.finish()
        self._phase='处理失败'
        workflow=getattr(self,'_separation_workflow',None)
        if workflow and workflow.get('phase')=='scoring':
            workflow['phase']='separated'
        elif workflow and workflow.get('phase') in ('preparing','separating','resuming'):
            workflow['phase']='partial' if workflow.get('preview') else 'failed'
            self._update_cancelled_job_state()
        self._problem(message)

    def _cancelled(self):
        self._notes_score_terminal()
        self._job_progress.finish()
        self._phase='已取消处理'
        workflow=getattr(self,'_separation_workflow',None)
        if workflow and workflow.get('phase')=='scoring':
            workflow['phase']='separated'
        elif workflow and workflow.get('phase')=='resuming':
            workflow['phase']='partial'
        elif workflow and workflow.get('phase') in ('preparing','separating'):
            self._separation_workflow=None
        self._remember_desktop_cancellation()
        self._publish()

    def _require_tempo_base(self,base):
        message='当前项目或导出已变化，请重新打开结果修正。'
        if not self._project or not isinstance(base,str) or base!=self._project['export']:
            raise ValueError(message)
        project=Path(self._project['path'])
        latest=_metadata(project/'project.json').get('latest_export')
        if not isinstance(latest,str) or not latest or (project/'exports'/Path(latest).name).resolve()!=Path(base):
            raise ValueError(message)
        return project

    def _set_chord_visibility(self,base,value):
        if type(value) is not bool:
            raise ValueError('请选择显示或隐藏和弦记号。')
        if not self._project or base!=self._project['export']:
            raise ValueError('当前导出已变化，请在新结果中修改和弦记号。')
        self._require_harmony_ready()
        if 'piano' not in self._project['instruments'] or not any(
                file['kind'] in ('pdf','musicxml','midi') and file.get('instrument')=='piano' for file in self._project['files']):
            raise ValueError('请先生成钢琴 MIDI 或乐谱，再修改和弦记号。')
        if value==self._project['score_chords']:
            return
        self._submit({'kind':'chord_visibility','project':self._project['path'],
                      'base_export':base,'score_chords':value})

    def _require_harmony_ready(self):
        # Fail before GPU selection; the worker separately checks authoritative
        # revision/dirty state again to protect against edits after this check.
        from .editing import current
        editor=current(self._project['path']) or self._project.get('editor') or {}
        if editor.get('dirty'):
            raise ValueError('当前编辑尚未生成结果，请先更新当前结果，再分析或切换和弦。')
        from .score_artifacts import resolve_score_export,current_score_export
        project=self._project['path']
        if resolve_score_export(project,source_key=editor.get('source_key')) and not current_score_export(project,editor=editor):
            raise ValueError('保留的正式谱面尚未包含当前编辑，请先更新谱面，再分析或切换和弦。')

    def _analyze_project_harmony(self,base,engine=None):
        project=self._project
        if not project or base!=project['export']:
            raise ValueError('当前导出已变化，请在新结果中分析和弦。')
        self._require_harmony_ready()
        if project['input_kind']!='audio' or 'piano' not in project['instruments']:
            raise ValueError('音频和弦分析适用于由录音生成的钢琴结果。')
        engine=engine or project.get('harmony_engine') or 'chordformer'
        if engine not in ('chordformer','tsumugi'):
            raise ValueError('请选择支持的和弦模型')
        self._start_with_gpu({'kind':'harmony_analysis','project':project['path'],
                              'base_export':base,'force':True,'harmony_engine':engine})

    def _save_tempo(self,data,base):
        self._require_no_structure_draft()
        project=self._require_tempo_base(base)
        from .desktop_tempo import _validated

        validated=_validated(data)
        override=project/f'tempo_override_{uuid4().hex}.json'
        save_json(override,validated)
        request=self._request_for('reexport',tempo_override=override)
        mode=_export_mode(self._project.get('files',[]))
        if mode:
            request['options']['mode']=mode
        request['quantization']=None
        self._start_with_gpu({**request,'tempo_base_export':base})

    @Slot(str,result=str)
    def previewMeterEdit(self,message):
        """Validate the quick editor's draft without writing a project or job."""
        from .document_editor import timing_projection
        from .desktop_tempo import _validated

        try:
            request=json.loads(message)
            if not isinstance(request,dict):
                raise ValueError('无法读取结果修正草稿。')
            self._require_tempo_base(request.get('export'))
            if self.jobs.running or self._gpu_request:
                raise ValueError('请等待当前任务完成。')
            result=timing_projection('preview_meter_edit',dict(
                tempo=request.get('data'),seconds=request.get('seconds'),
                numerator=request.get('numerator'),previous=request.get('previous')))
            # Export encoding/format boundary only; native remains the musical planner.
            _validated(result['data'])
        except (ArithmeticError,KeyError,TypeError,ValueError,IndexError,OSError,RuntimeError) as exc:
            result={'error':str(exc) or '无法预览节拍修正。'}
        return self._json(result)

    @Slot(str,result=str)
    def previewRollMeterEdit(self,message):
        """Translate the selected MIDI quarter into the bound export clock."""
        from .document_editor import timing_projection
        from .desktop_tempo import _validated
        try:
            request=json.loads(message)
            if not isinstance(request,dict):
                raise ValueError('无法读取结果修正草稿。')
            self._require_tempo_base(request.get('export'))
            if self.jobs.running or self._gpu_request:raise ValueError('请等待当前任务完成。')
            midi=self._midi_file
            if not midi or midi.get('loading') or request.get('midi')!=midi['path']:
                raise ValueError('MIDI 已切换，请重新选择音符。')
            path=self._authorized_result(request['midi'],'midi')
            if path.parent.resolve()!=Path(request['export']).resolve():
                raise ValueError('请使用当前导出的 MIDI 修正拍位。')
            beat=_number(request.get('beat'),0,float(midi['duration_beats']),'拍位无效。')
            tempo=_metadata(Path(request['export'])/'tempo_map.json')
            if not tempo:raise ValueError('此导出没有参考速度图。')
            seconds=timing_projection('to_seconds',dict(tempo=tempo,beat=beat))
            mode=request.get('mode')
            if mode not in ('opening','bar'):raise ValueError('请选择修正方式。')
            result=timing_projection('preview_roll_edit',dict(
                tempo=tempo,seconds=seconds,opening=mode=='opening',numerator=request.get('numerator')))
            # Export encoding/format boundary only; native remains the musical planner.
            _validated(result['data'])
        except (ArithmeticError,KeyError,TypeError,ValueError,IndexError,OSError,RuntimeError) as exc:
            result={'error':str(exc) or '无法预览卷帘修正。'}
        return self._json(result)

    def _estimate_constant_tempo(self):
        self._require_no_structure_draft()
        self._tempo_estimate=None
        if not self._project:
            raise ValueError('请先打开一个已有节拍分析的项目。')
        from .constant_tempo import estimate_constant_tempo
        project=Path(self._project['path'])
        state=read_json(project/'project.json')
        key=(state.get('analysis_keys') or {}).get('beat_this','')
        cached=project/'cache'/f'beats-{key}.json'
        if not cached.is_file():
            raise ValueError('没有原曲节拍缓存，请手动填写已知 BPM 或导入速度图。')
        beats=read_json(cached)
        self._tempo_estimate=estimate_constant_tempo(beats['beats'],beats.get('downbeats',[]),state['duration'])

    @staticmethod
    def _reviews(project):
        root=(project/'reviews').resolve()
        if root.parent!=project:
            return []
        records=[]
        for candidate in root.glob('*/*.json'):
            path=candidate.resolve()
            if not path.is_relative_to(root) or not path.is_file():
                continue
            record=_metadata(path)
            feedback=record.get('feedback')
            if not isinstance(feedback,dict) or feedback.get('source')!='astra-visual-review':
                continue
            groups=feedback.get('groups')
            if not isinstance(groups,list) or any(not isinstance(group,dict) for group in groups):
                continue
            records.append({'path':str(path),'audit_export':path.parent.name,
                'imported_at':str(record.get('imported_at','')),'group_count':len(groups),
                'comment_count':sum(bool(str(group.get('comment','')).strip()) for group in groups)})
        return sorted(records,key=lambda record:record['imported_at'],reverse=True)

    def _import_review(self,value):
        from .review_feedback import import_feedback

        path=_local_path(value)
        if path.stat().st_size>2*1024*1024:
            raise ValueError('审核意见文件过大。')
        project=Path(self._project['path'])
        known_audit=OUTPUTS/'duet_review'/'timing-audit.json'
        known_export=_metadata(known_audit).get('export')
        candidate=_metadata(path).get('audit_export')
        audit_path=None
        if isinstance(known_export,str):
            export=Path(known_export).resolve()
            if export.parent.parent==project and export.name==candidate:
                audit_path=known_audit
        imported=import_feedback(project,path,audit_path=audit_path)
        self._project['reviews']=self._reviews(project)
        text=f"已保存 {imported['group_count']} 组审核意见，其中 {imported['comment_count']} 组有文字。"
        self._review_notice={'token':uuid4().hex,'text':text}
        self._record_log(text)

    def _read_review(self,value):
        path=_local_path(value)
        records=self._project.get('reviews',[]) if self._project else []
        record=next((item for item in records if item['path']==str(path)),None)
        if not record or not path.is_file():
            raise ValueError('此审核意见不属于当前项目。')
        feedback=_metadata(path).get('feedback',{})
        groups=feedback.get('groups',[])
        self._review_document={**record,'groups':groups}

    @staticmethod
    def _open_local(path):
        if not path.exists() or not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            raise ValueError(f'无法打开 {path.name}。请检查本地文件和系统默认程序。')

    def _open_file(self,value):
        if isinstance(value,str) and value.startswith('astra-editor:'):
            from .studio_external_midi import ExternalMidi
            if not hasattr(self,'_external_midi'):
                self._external_midi=ExternalMidi(self,PREFERENCES.parent/'.cache'/'external-midi')
            self._external_midi.request(value)
            return
        path=_local_path(value)
        link=getattr(self,'_linked_original',None)
        if (link is not None and self._project and project_access.approved(self._project['path'])==link[0]
                and path in (link[2].media,link[2].selected)):
            raise ValueError('本次连接的原曲仅供工程试听。')
        if any(source['path']==str(path) for source in self._sources):
            self._open_local(path)
        elif self.jobs.log_path and path==self.jobs.log_path:
            self._open_local(path)
        else:
            self._open_local(self._authorized_result(path))

    def _open_projects_folder(self):
        # No caller-supplied path here. Create only the configured library,
        # and only on the user's explicit Open Folder action.
        PROJECTS.mkdir(parents=True,exist_ok=True)
        self._open_local(PROJECTS)

    def _open_recent_folder(self,value):
        path=_local_path(value)
        if not any(item['path']==str(path) for item in self._recent) or not path.is_dir():
            raise ValueError('此目录不在最近项目列表中。')
        self._open_local(path)

    def _open_folder(self,value):
        folders=set(self._approved_output_folders)
        if self._project:
            folders.update(Path(self._project[key]) for key in ('path','export'))
            folders.update(Path(file['path']).parent for file in self._project['files'] if file['kind']=='audio')
        default=self._project['export'] if self._project else self._options['output']
        path=_local_path(value or default)
        if path not in folders or not path.is_dir():
            raise ValueError('此文件夹不属于当前项目或已选择的输出目录。')
        self._open_local(path)

    @Slot(result=str)
    def ncmSnapshot(self):
        return self.ncm_tool.snapshot()

    @Slot(str,result=str)
    def startOrnamentReview(self,message):
        try:return self._json(self._ornament_start(json.loads(message)))
        except (ValueError,TypeError,KeyError,OSError,RuntimeError) as exc:
            return self._json({'ok':False,'error':str(exc)})

    @Slot(str,result=str)
    def ornamentReviewStatus(self,token):
        try:return self._json(self._ornament_status(token))
        except (ValueError,TypeError,KeyError,OSError,RuntimeError) as exc:
            return self._json({'ok':False,'status':'failed','error':str(exc)})

    @Slot(str,result=str)
    def openScoreRangeWindow(self,message):
        try:
            from .studio_score_window import ScoreWindow
            if os.environ.get('ASTRA_LIVE_SCORE') == '1':
                from .studio_live_score import LiveScoreWindow as ScoreWindow
            data=json.loads(message)
            if not getattr(self,'_score_window',None):self._score_window=ScoreWindow(self)
            self._score_window.show_context(data)
            return self._json({'ok':True})
        except (ValueError,TypeError,KeyError,OSError,RuntimeError) as exc:
            return self._json({'ok':False,'error':str(exc)})

    @Slot(str)
    def syncScoreRangeWindow(self,message):
        if getattr(self,'_score_window',None):
            try:self._score_window.update_context(json.loads(message))
            except (ValueError,TypeError,KeyError):pass

    @Slot(str)
    def cancelScoreRangePreview(self, token):
        self._score_range_cancel(token)

    @Slot(str,result=str)
    def startScoreRangePreview(self,message):
        try:return self._json(self._score_range_preview_start(json.loads(message)))
        except (ValueError,TypeError,KeyError,OSError,RuntimeError) as exc:
            return self._json({'ok':False,'error':str(exc)})

    @Slot(str,result=str)
    def scoreRangePreviewStatus(self,token):
        try:return self._json(self._score_range_status(token))
        except (ValueError,TypeError,KeyError,OSError,RuntimeError) as exc:
            return self._json({'ok':False,'status':'failed','error':str(exc)})

    @Slot(str,result=str)
    def scoreRangePreviewPage(self,message):
        try:return self._json(self._score_range_page_start(json.loads(message)))
        except (ValueError,TypeError,KeyError,OSError,RuntimeError) as exc:
            return self._json({'ok':False,'error':str(exc)})

    @Slot(str,result=str)
    def startBarPhasePreview(self,message):
        try:
            data=json.loads(message)
            if not isinstance(data,dict):raise ValueError('复核请求必须是对象。')
            return self._json(self._bar_phase_preview_start(data))
        except (ValueError,TypeError,KeyError,OSError,RuntimeError) as exc:
            return self._json({'ok':False,'error':str(exc)})

    @Slot(str,result=str)
    def barPhasePreviewStatus(self,token):
        try:return self._json(self._bar_phase_status(token))
        except (ValueError,TypeError,KeyError,OSError,RuntimeError) as exc:
            return self._json({'ok':False,'status':'failed','error':str(exc)})

    @Slot(str,result=str)
    def previewTranscriptionCandidate(self,message):
        from . import editing_candidate
        self._transcription_candidate=None
        try:
            data=json.loads(message)
            if not isinstance(data,dict):raise ValueError('候选确认请求无效。')
            if not self._project:raise ValueError('请先打开钢琴工程。')
            if self.jobs.running or self._gpu_request:raise ValueError('请等待当前分析或导出完成，再采用候选。')
            prepared=editing_candidate.prepare(self._project['path'],data.get('base_revision'))
            token=uuid4().hex
            from .document_editor import edit_ticket
            self._transcription_candidate={'token':token,'prepared':prepared,'generation':self._edit_identity(),
                'stamp':edit_ticket(self._project['path'],data.get('base_revision'))}
            return self._json({'ok':True,'token':token,**{key:prepared[key] for key in (
                'base_revision','candidate_key','candidate_sha256','old_notes','new_notes',
                'old_controls','new_controls','fixed_anchors')}})
        except (ValueError,TypeError,KeyError,OSError,RuntimeError) as exc:
            return self._json({'ok':False,'error':str(exc)})

    def _adopt_transcription_candidate(self,data):
        self._require_no_structure_draft()
        from . import editing_candidate
        candidate=getattr(self,'_transcription_candidate',None)
        from .document_editor import edit_ticket
        if (not self._project or not candidate or candidate['token']!=data.get('token')
                or candidate['generation'] is not self._edit_identity()
                or candidate['stamp']!=edit_ticket(self._project['path'],data.get('base_revision'))):
            raise ValueError('候选确认已失效，请重新查看后确认。')
        if self.jobs.running or self._gpu_request:raise ValueError('请等待当前分析或导出完成，再采用候选。')
        # Consume even on failure: changed caches/revisions require fresh consent.
        self._transcription_candidate=None
        editing_candidate.adopt(self._project['path'],data.get('base_revision'),candidate['prepared'])
        self._refresh_editor(select=True)
        self._phase='已采用新转录候选；拍位图保留，可撤销'

    @Slot(str,result=str)
    def rebarBoundaries(self,message):
        from . import editing
        from .document_editor import query
        try:
            data=json.loads(message)
            if not self._project:raise ValueError('请先打开工程。')
            base=data.get('base_revision')
            editing._check_base(self._project.get('editor'),base)
            return self._json({'ok':True,'revision':base,
                'boundaries':query(self._project['path'],base,'boundaries',expected=self._editor_native_stamp)})
        except (ValueError,TypeError,KeyError,OSError) as exc:
            return self._json({'ok':False,'error':str(exc)})

    @Slot(str,result=str)
    def structureDraft(self,message):
        from .rhythm_structure_service import StructureDraftSession
        try:
            data=json.loads(message)
            if not isinstance(data,dict):raise ValueError('结构编辑请求必须是对象。')
            if not self._project:raise ValueError('请先打开钢琴工程。')
            if self._edit_pending():raise ValueError('请等待当前编辑保存后再修改拍位结构。')
            if self.jobs.running or self._gpu_request:raise ValueError('请等待当前任务完成，再编辑拍位结构。')
            session=getattr(self,'_structure_session',None)
            if session is None:
                session=self._structure_session=StructureDraftSession()
            result=session.handle(self._project['path'],data)
            if result.get('applied'):
                self._refresh_editor(select=True)
                self._phase='拍位结构已更新；谱面待更新，可一次撤销'
                self._publish()
            return self._json(result)
        except (ValueError,TypeError,KeyError,OSError,RuntimeError) as exc:
            return self._json({'ok':False,'error':str(exc)})

    @Slot(str,result=str)
    def previewTempoReference(self,message):
        """Read-only guide: it cannot publish a map or change the transport."""
        from .document_editor import query
        try:
            data=json.loads(message)
            if not isinstance(data,dict):raise ValueError('参考速度请求必须是对象。')
            if not self._project:raise ValueError('请先打开工程。')
            operation=data.get('operation')
            if not isinstance(operation,dict) or operation.get('type')!='rebar':
                raise ValueError('请先框选需要检查的完整小节范围。')
            request={**operation,'reference_bpm':data.get('reference_bpm'),
                     'phase_seconds':data.get('phase_seconds')}
            result=query(self._project['path'],data.get('base_revision'),'tempo_reference',
                         dict(request=request,viewport=data.get('viewport')),expected=self._editor_native_stamp)
            # Candidate mutations deliberately stay out of this display API.
            # Adoption is the ordinary explicit revision-bound rebar command.
            return self._json({'ok':True,'revision':result['revision'],'mode':'reference_only',
                'reference_grid':result['reference_grid'],'points':result['points'],
                'constraints':result['constraints'],
                'feasible':result['requested_constraints_feasible'],
                'warnings':result['warnings'],'playback':result['playback'],
                'reasons':[c['reason'] for c in result['candidates'] if c.get('reason')]})
        except (ValueError,TypeError,KeyError,OSError,RuntimeError) as exc:
            return self._json({'ok':False,'error':str(exc)})

    @Slot(str,result=str)
    def previewProject(self,message):
        from . import editing
        try:
            data=json.loads(message)
            if not self._project:raise ValueError('请先打开工程。')
            from .document_editor import preview
            doc=self._project.get('editor') or {}
            editing._check_base(doc,data.get('base_revision'))
            op=data['operation']
            if isinstance(op,dict) and op.get('type')=='selection_range':
                result=self._preview_selection_range(data,doc)
                return self._json(dict(ok=True,delta=result['delta'],native_stamp=result['stamp']))
            result=preview(self._project['path'],data['base_revision'],op,expected=self._editor_native_stamp)
            candidate=result['delta']
            seq=data.get('review_click_seq')
            if (type(seq) is int and seq > 0 and isinstance(op,dict)
                    and op.get('type')=='rhythm_intent' and op.get('action')=='record_beats'
                    and hasattr(self,'_prepare_review_click')):
                self._prepare_review_click(seq, doc, candidate, op, result['stamp'])
            return self._json({'ok':True,'tempo':candidate.get('tempo',doc['tempo']),
                'anchors':candidate.get('anchors',doc['anchors']),
                'summary':candidate.get('summary','编辑音符或踏板，原始转录保留。'),
                'warnings':candidate.get('edit_warnings',[]),'impact':candidate.get('edit_impact'),
                'delta':candidate,'native_stamp':result['stamp']})
        except (ValueError,TypeError,KeyError,OSError) as exc:
            return self._json({'ok':False,'error':str(exc)})

    @Slot(str)
    def dispatch(self,message):
        try:
            data=json.loads(message)
            if not isinstance(data,dict):
                raise ValueError('工作台命令必须是对象。')
            action=data.get('action')
            if action in EDIT_ACTIONS:
                self._edit_submit(action,data)
                return
            if (action in LEAVE_PROJECT_ACTIONS and self._project and not self._closed
                    and not (action=='openRecent' and data.get('path')==self._project.get('path'))):
                # A window-owned document (the right-hand score) may hold edits
                # that the project save does not cover. Ask before replacing it.
                guard=getattr(self,'_leave_project_guard',None)
                if guard is not None and not guard(lambda:self.dispatch(message)):
                    return
            if action in ('saveEditor','retryEditorSave'):
                self._edit_manual_save(retry=action=='retryEditorSave')
                return
            if self._closed or getattr(self,'_editor_closing',False):
                return
            if action=='linkProjectAudio':
                self._link_project_audio(data.get('project'))
                return
            if action=='openProjectsFolder':
                self._open_projects_folder()
                return
            if action=='openRecentFolder':
                self._open_recent_folder(data.get('path'))
                return
            if action=='recoverEditorCheckpoint':
                raise ValueError('请在项目标题旁选择并确认要恢复的副本。')
            if action not in ('openRecent','playPause','stop','seek','volume','midiPlayPause','midiStop','midiSeek','midiVolume','selectAudio','setAuditionRate'):
                self._cancel_recent_open()
            self._preview_prefetch.activity()
            if action=='adoptTranscriptionCandidate':
                self._error=None
                self._adopt_transcription_candidate(data)
                self._publish()
                return
            if action=='applyBarPhaseCandidate':
                self._error=None
                self._apply_bar_phase_candidate(data)
                self._publish()
                return
            if action in ('editProject','undoProject','redoProject','exportEditor','setEditorRange'):
                self._error=None
                self._editor_action(action,data)
                self._publish()
                return
            if action=='auditionRate':
                self._set_audition_rate(data.get('value',1))
                self._publish()
                return
            if action in ('auditionTogether','metronome'):
                if action=='auditionTogether':
                    raise ValueError('原曲与 MIDI 联合播放已暂时移除；请分别选择原音频或当前 MIDI。')
                else:self._set_metronome(data.get('enabled',False))
                self._publish()
                return
            if action in ('ncmChooseFiles','ncmChooseOutput','ncmRemove','ncmClear','ncmStart',
                          'ncmCancel','ncmOpenOutput'):
                self.ncm_tool.handle(action,data)
                return
            if (self.jobs.running or self._gpu_request) and action in (
                'chooseAudio','chooseMidi','chooseOutput','chooseProject','chooseTempo','openRecent','new','updateOptions',
                'run','continueScore','runMidi','reexport','retry','saveTempo','estimateConstantTempo','importReview','setChordVisibility','analyzeHarmony',
            ):
                return
            if action in ('chooseAudio','chooseMidi','chooseOutput','chooseProject','chooseTempo','openRecent','new',
                'updateOptions','run','continueScore','runMidi','reexport','retry','saveTempo','estimateConstantTempo','importReview','readReview','setChordVisibility','analyzeHarmony'):
                self._error=None
            if action in ('chooseAudio','chooseMidi','chooseOutput','chooseProject','chooseTempo','importReview'):
                self._native(action)
            elif action=='openRecent':
                self._open_recent_from_list(data.get('path'),data.get('open_request'))
            elif action=='new':
                self._new()
            elif action=='updateOptions':
                if 'project_scope' in data and data['project_scope']!=(self._project or {}).get('path'):
                    raise ValueError('当前工程已切换，请重新打开设置。')
                self._update_options(data.get('options',data))
            elif action=='enableExternalEditor':
                if (not self._project or self._project['path']!=data.get('project')
                        or not self._project.get('editor_setup_required') or self.jobs.running or self._gpu_request):
                    return
                self._refresh_editor(select=True,external_setup=True)
            elif action=='continueScore':
                self._continue_score()
            elif action=='continueSeparation':
                self._continue_separation()
            elif action in ('run','reexport'):
                request=self._request_for(action)
                if action=='run' and (not self._project or self._project_source!=self._audio['path']):
                    self._start_separation(request)
                else:
                    self._start_with_gpu(request)
            elif action=='setChordVisibility':
                self._set_chord_visibility(data.get('base_export'),data.get('show_chords'))
            elif action=='analyzeHarmony':
                self._analyze_project_harmony(data.get('base_export'),data.get('engine'))
            elif action=='runMidi':
                self._start_with_gpu(self._midi_request(data.get('tracks'),data.get('channels')))
            elif action=='retry':
                if self._request:
                    if self._request.get('workflow_phase')=='score':
                        self._continue_score()
                    elif (getattr(self,'_separation_workflow',None) or {}).get('phase')=='partial':
                        self._continue_separation()
                    else:
                        log=self.jobs.log_path
                        token='local:'+log.parent.name if log else None
                        recoverable=self._cancelled_job_state().get('interrupted_history',[])
                        if token and any(row['id']==token for row in recoverable):
                            self._open_interrupted_job(token)
                        else:
                            request={key:value for key,value in self._request.items() if key!='execution_device'}
                            self._start_with_gpu(request)
            elif action=='chooseExecutionDevice':
                self._choose_execution_device(data.get('token'),data.get('device'))
            elif action=='cancel':
                self._stop_desktop_job()
            elif action in ('keepCancelledJob','cleanCancelledJob','openCancelledJob','openInterruptedJob'):
                self._cancelled_job_action(action,data)
            elif action=='openFile':
                self._open_file(data.get('path'))
            elif action=='openFolder':
                self._open_folder(data.get('path'))
            elif action=='openLog':
                if not self.jobs.log_path:
                    raise ValueError('暂无本地任务日志。')
                self._open_local(self.jobs.log_path)
            elif action=='readReview':
                self._read_review(data.get('path'))
            elif action=='closeReview':
                self._review_document=None
            elif action=='help':
                self._open_local(ROOT/'README.md')
            elif action=='playPause':
                self._play_pause()
            elif action=='stop':
                self._stop_audio()
            elif action=='seek':
                self._seek_audio(data.get('position'))
                return
            elif action=='volume':
                self._set_volume(data.get('value'))
                return
            elif action=='navigateTransportPage':
                navigate=getattr(self,'_navigate_transport_page',None)
                if navigate:navigate(data)
            elif action=='selectAuditionSource':
                self._select_audition_source(data)
            elif action=='selectAudio':
                self._select_audio(data.get('key'))
            elif action=='audition':
                self._audition(data.get('key'),data.get('position',0))
            elif action=='saveTempo':
                self._save_tempo(data.get('data'),data.get('export'))
            elif action=='estimateConstantTempo':
                self._estimate_constant_tempo()
            elif action=='selectPdf':
                self._select_pdf(data.get('path'))
                if self._edit_preview_selected():
                    self._hold_editor_midi()
                    self._cancel_midi_intent()
                    self._flush_midi_intent()
            elif action=='retryScorePreview':
                issue=(self._project or {}).get('score_pdf_issue') or {}
                path=self._authorized_result(issue.get('retry_path'),'pdf')
                self._queue_pdf(0,replacement=path)
            elif action=='pdfPage':
                self._pdf_page(data.get('index'))
            elif action=='selectMidi':
                self._select_midi(data.get('path'))
            elif action=='midiSetRange':
                self._set_midi_range(data)
                return
            elif action=='midiPlayPause':
                self._midi_play_pause()
            elif action=='midiStop':
                self._stop_midi()
            elif action=='midiSeek':
                self._seek_midi(_number(data.get('position'),0,float('inf'),'试听位置无效。'))
                return
            elif action=='midiVolume':
                self._set_midi_volume(round(_number(data.get('value'),0,100,'音量须在 0 到 100 之间。')))
                return
            else:
                raise ValueError('未知工作台命令。')
            if action in ('playPause','stop','selectAudio','audition','midiPlayPause','midiStop'):
                return  # Transport signals carry everything these actions changed.
            self._publish()
        except (ArithmeticError,ImportError,KeyError,OSError,RuntimeError,TypeError,ValueError) as exc:
            self._problem(str(exc) or '工作台操作失败。')

    def close(self):
        if self._closed:return True
        from PySide6.QtWidgets import QWidget
        parent=self.parent()
        if not self._edit_prepare_close(parent.window() if isinstance(parent,QWidget) else None):
            return False
        self._closed=True
        self._edit_close()
        self._media_invalidate()
        self._preview_prefetch.close()
        self._shared_leave('window closed',preserve=False)
        self._closed=True
        if self._volume_save_timer.isActive():
            self._volume_save_timer.stop()
            self._save_preferences()
        self._state_timer.stop()
        self._player_timer.stop()
        self._midi_timer.stop()
        self.jobs.cancel()
        self._stop_audio()
        self._media.setSource(QUrl())
        for media,output in getattr(self,'_audio_bank',{}).values():
            if media is not self._media:media.stop();media.setSource(QUrl())
        self._audio_bank={}
        self._clear_midi()
        self._clear_pdf()
        return True
