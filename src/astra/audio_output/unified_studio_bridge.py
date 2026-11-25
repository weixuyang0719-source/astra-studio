"""StudioBridge routing: proven RAW PauseGuard audio, native realtime MIDI.
ASTRA_NATIVE_PCM=1 opts into the experimental PCM core; MIDI defaults native.
Refresh retires previous owners and confirms release before replacement.
"""
from dataclasses import replace
import threading,time,copy,math
from PySide6.QtMultimedia import QMediaDevices
from PySide6.QtCore import QTimer,Signal,Slot
from pathlib import Path
import json
from astra.studio_bridge import StudioBridge,_number
from astra import live_owner as live
from astra.shared_audio_core import ChunkCache
from .raw_handoff_prototype import Handoff,Intent
from .native_intent_owner import NativeIntentOwner
from .qt_intent_owner import QtIntentOwner
from .device_manager import AudioDeviceManager,DeviceSetup,describe
from . import device_catalog
from .playback_trace import PlaybackTrace
from .playback_status_cache import PlaybackStatusCache
from . import asio_panel_bridge
from .memory_midi_bridge import MemoryMidiBridge
from .vst_studio import VstStudioRoute

_READ_OWNER=object()

class _FileStudioBridge(StudioBridge):
 playbackChanged=Signal(str)
 def __init__(self,*args,raw=False,midi_factory=None,joint_factory=None,experimental_joint=False,allow_output_settings=False,**kwargs):
  import os
  self._native_tracks=os.environ.get('ASTRA_NATIVE_TRACKS')=='1'
  self._allow_output_settings=allow_output_settings
  self._handling_output_command=False;self._output_command_error=None
  self._native_midi_enabled=__import__('os').environ.get('ASTRA_NATIVE_MIDI','1')=='1';self._native_midi_failure=None
  self._native_pcm_enabled=__import__('os').environ.get('ASTRA_NATIVE_PCM','0')=='1';self._native_pcm_failure=None
  self._output_device_notice=False;self._output_device_available=True;self._device_resume_position=None
  self._experimental_joint=experimental_joint;self._joint_factory=joint_factory
  self._output_owners=[];self._pending_midi_intent=None;self._output_midi_factory=midi_factory;self._raw_failure_reason=None;self._native_shared_failure=None;self._reported_output_error=None
  self._playback_stream_epoch=0;self._playback_status_ack=None
  self._devices_mgr=AudioDeviceManager(path=kwargs.pop('device_settings',None) or __import__('astra.studio_bridge',fromlist=['PREFERENCES']).PREFERENCES.with_name('audio-device.json'))
  self._tracks_checkpoint=None;self._tracks_returning=False;self._notes_located=False;self._notes_return_position=None
  self._output_desired=Intent();self._deferred_delta=None;self._output_shutdown=False;self._output=None;self._output_resources=None;self._output_retiring=[];self._output_prepared=None;self._want_raw=raw;self._owned_caches=[]
  super().__init__(*args,**kwargs)
  self._output_timer=QTimer(self);self._output_timer.setInterval(20);self._output_timer.timeout.connect(self._output_poll);self._output_timer.start()
  self._output_devices=QMediaDevices(self);self._output_device_id=bytes(QMediaDevices.defaultAudioOutput().id());self._output_devices.audioOutputsChanged.connect(self._output_device_changed)
  self._output_device_available=True if self._native_tracks else not QMediaDevices.defaultAudioOutput().isNull()
  if not self._output_device_available:self._output_device_notice=True
  self._trace=PlaybackTrace(__import__('astra.studio_bridge',fromlist=['PREFERENCES']).PREFERENCES.parent/'raw-output-session')
 def _output_device_changed(self):
  if self._output_shutdown:return
  # Unified ASIO policy: Windows default WASAPI endpoint changes never own or tear down ASIO.
  if getattr(self,'_native_tracks',False):return
  # A user-chosen endpoint is followed by id (a non-default unplug leaves the default unchanged):
  # gone -> system default, paused; back -> restored, paused.
  if self._dm().preferred.device and self._device_endpoints_changed():
   self._output_device_notice=True;self._player_tick();return
  device=QMediaDevices.defaultAudioOutput();current=bytes(device.id());available=not getattr(device,'isNull',lambda:False)()
  if current==self._output_device_id and available==self._output_device_available:return
  self._output_device_id=current;self._output_device_available=available
  self._output_device_notice=True
  self._output_desired=replace(self._output_desired,playing=False)
  if self._deferred_delta is not None:self._deferred_delta=dict(self._deferred_delta,playing=False)
  self._cancel_midi_intent()
  self._pending_audio=None
  for retiring in self._output_retiring:retiring.intent=replace(retiring.intent,playing=False)
  h=self._output
  if available and h is not None and h.owner_mode in ('device','native-pcm','native-joint','native-audio-click') and self._dm().current.device:
   # The default device changed but ours is explicit: pause only, never re-route it.
   if not (h.blocked or h.closing):h.change(playing=False);h.poll();self._output_desired=h.intent
   self._player_tick();return
  if not available:
   if h is not None:
    if h.owner:self._device_resume_position=h.owner.snapshot().position
    self._output_desired=replace(h.intent,playing=False)
    h.close();self._output_retiring.append(h);self._output=None
   self._deferred_delta=None;self._player_tick();return
  if h is None:self._player_tick();return
  # A changed default must never unexpectedly resume through speakers.
  if h.owner_mode in ('raw','native-shared'):self._raw_failure_reason='Default audio device changed'
  if h.blocked or h.closing:self._player_tick();return
  previous=h.owner
  h.change(playing=False,mode='ordinary' if self._raw_failure_reason else h.intent.mode)
  if h.owner is previous and h.owner and not h.retiring:h._retire(True)
  h.poll();self._output_desired=h.intent
  self._player_tick()
 @Slot(str,result=str)
 def vstPluginManager(self,message):
  return self._plugin_manager(message)

 @Slot(str,result=str)
 def setAudioOutputPreference(self,value):
  if not self._allow_output_settings:return self._json({'ok':False,'error':'当前版本尚未开放输出切换。'})
  if self._output_shutdown:return self._json({'ok':False,'error':'窗口正在关闭。'})
  if value not in ('ordinary','raw'):return self._json({'ok':False,'error':'请选择兼容输出或低延迟输出。'})
  self._output_poll() # consume already-pending failures before an explicit retry
  if any(h.blocked or h.closing for h in ([self._output] if self._output else [])+self._output_retiring):return self._json({'ok':False,'error':'旧音频输出状态尚未确认，暂不能切换。'})
  before=(self._want_raw,self._raw_failure_reason,self._native_shared_failure,self._native_pcm_failure)
  try:
   self._native_pcm_failure=None
   self._want_raw=value=='raw'
   if self._want_raw:self._raw_failure_reason=None;self._native_shared_failure=None;self._reported_output_error=None
   if self._sources:self._output_change()
   self._player_tick()
   return self._json({'ok':True,'preference':value,'outputStatus':self._player_state()['outputStatus']})
  except Exception:
   self._want_raw,self._raw_failure_reason,self._native_shared_failure,self._native_pcm_failure=before
   return self._json({'ok':False,'error':'输出切换未完成，已保留原偏好。'})
 @Slot(result=str)
 def audioDevices(self):
  try:return self._json(dict(ok=True,**self._dm().listing(),control_panel=asio_panel_bridge.status(self)))
  except Exception as exc:return self._json({'ok':False,'error':'无法读取设备列表：'+repr(exc)[:160]})
 @Slot(result=str)
 def audioDeviceStatus(self):
  return self._json(dict(ok=True,**self._dm().status(),control_panel=asio_panel_bridge.status(self)))
 @Slot(str,result=str)
 def openAsioControlPanel(self,driver):
  return self._json(self._vst_open_asio_panel(driver))
 @Slot(str,result=str)
 def setAudioDevice(self,raw):
  if asio_panel_bridge.status(self).get('busy'):return self._json({'ok':False,'error':'请等待控制面板操作完成。'})
  if self._output_shutdown:return self._json({'ok':False,'error':'窗口正在关闭。'})
  try:
   data=json.loads(raw);setup=DeviceSetup(backend=str(data.get('backend','auto')),device=data.get('device') or None,sample_rate=int(data.get('sample_rate',48000)),buffer_frames=int(data['buffer_frames']) if data.get('buffer_frames') else None)
  except Exception:return self._json({'ok':False,'error':'设备设置无效。'})
  self._output_poll()
  queued_asio=(setup.backend=='asio' and getattr(self,'_native_tracks',False)
               and getattr(self,'_vst',None) is None)
  handoffs=([self._output] if self._output else [])+self._output_retiring
  # A failed transport keeps its error latched even after its owner has gone.
  # Physical ASIO switches own a separate release fence, so they can accept a
  # new request without reopening until the old owner genuinely released.
  if not queued_asio and any((h.blocked or h.closing) and h.owner is not None for h in handoffs):
   return self._json({'ok':False,'error':'旧音频输出尚未确认释放，暂不能切换。'})
  manager=self._dm()
  endpoint=getattr(self,'_kept_device',None)
  reusable=getattr(endpoint,'reuse_open_asio',None)
  owner=self._output.owner if self._output else None
  active_vst=getattr(self,'_vst',None)
  vst_lease=(active_vst.device.get('kept_endpoint') or {}) if active_vst is not None else {}
  if (owner is not None and not self._output_shares_kept_endpoint()
      or active_vst is not None and active_vst.ready and vst_lease.get('name')!=getattr(endpoint,'name',None)
      or getattr(self,'_vst_panel_transition',None)
      or any(getattr(old,'departing_endpoint',None) is endpoint for old in getattr(self,'_vst_retiring',()))):
   reusable=None
  if any(h.retiring or h.closing or h.blocked for h in handoffs):
   reusable=None
  from contextlib import nullcontext
  # Existing endpoint teardown takes the same control lock, not the audio callback lock.
  with endpoint._lock if reusable is not None else nullcontext():
   error=manager.request(setup,confirm_open=bool(data.get('confirm_open')),from_settings=True,reuse_asio=reusable)
  if error:return self._json({'ok':False,'error':error})
  self._native_pcm_failure=None
  if getattr(self,'_native_tracks',False):
   self._output_device_available=setup.backend=='asio' or not QMediaDevices.defaultAudioOutput().isNull()
   self._output_device_notice=not self._output_device_available
  if manager.reused_open_asio:
   # The current native owner remains valid for the new preference identity.
   owner=self._output.owner if self._output else None
   if owner is not None and self._output_shares_kept_endpoint():
    owner._device_setup=setup
   self._output_ui_signature=None
   self._player_tick()
  else:
   self._switch_output_device(refresh_catalog=False)
  return self._json({'ok':True,'outputStatus':self._player_state()['outputStatus'],'devices':manager.status()})
 def _switch_output_device(self, *, refresh_catalog=True):
  # Stop the old stream (paused, position kept by Handoff), then let the next owner open the new one.
  if self._output_shutdown:return
  self._cancel_midi_intent()
  self._raw_failure_reason=None;self._native_shared_failure=None
  if self._sources:
   before=self._output.owner if self._output else None
   self._output_change(playing=False)
   h=self._output
   # Same mode (e.g. device -> another device): the old owner must still be stopped and rebuilt.
   # A mode change already retired it; an owner created by this call is the new one: keep it.
   if h and before is not None and h.owner is before and not h.retiring:h._retire(True);h.poll()
  self._player_tick()
 def _device_endpoints_changed(self):
  ids={e.get('id') for e in device_catalog.wasapi_endpoints()}
  target=self._dm().endpoints_changed(ids)
  if target is None:return False
  self._output_desired=replace(self._output_desired,playing=False)
  self._switch_output_device();return True
 @Slot(str,result=str)
 def savePlaybackTrace(self,raw):
  trace=getattr(self,'_trace',None)
  if trace is None or not trace.enabled:return self._json({'ok':False,'enabled':False})
  return self._json(trace.submit_page(raw))
 @Slot()
 def enablePlaybackStream(self):
  if self._output_shutdown:return
  self._playback_stream_epoch+=1
  self._playback_status_ack=None
  self._vst_view_ack=None
 @Slot(int,int)
 def ackVstView(self,epoch,revision):
  if epoch==self._playback_stream_epoch and revision==getattr(self,'_vst_view_revision',None):
   self._vst_view_ack=revision
 @Slot(int,int)
 def ackPlaybackStatus(self,epoch,revision):
  memo=getattr(self,'_playback_status_cache',None)
  if epoch==self._playback_stream_epoch and memo is not None and revision==memo.revision:
   self._playback_status_ack=revision
 @Slot(str,result=str)
 def dispatchPlayback(self,message):
  # The ordinary command path still owns every decision, including pending
  # edits and output errors. This response observes it; it does not retry it.
  self.dispatch(message)
  player,midi=self._playback_states(False)
  payload=dict(player=player,midi=midi)
  if getattr(self,'_beat_recording_active',False):
   payload['recording_clock']=self._recording_clock_view(player,midi)
  return self._json(payload)
 @Slot(result=str)
 def recordingClock(self):
  player,midi=self._playback_states(False)
  return self._json(self._recording_clock_view(player,midi))
 @Slot(str,result=str)
 def setBeatRecording(self,raw):
  try:
   data=json.loads(raw) if raw else {}
  except (ValueError,TypeError):
   data={}
  if not isinstance(data,dict):
   data={}
  if 'review_click_seq' in data:
   seq=data['review_click_seq']
   candidate=getattr(self,'_review_click_prepared',None)
   adopted=type(seq) is int and candidate is not None and candidate['seq']==seq and self._review_click_valid(candidate)
   if adopted:
    self._review_click_active=candidate
    self._sync_review_click()
   return self._json(dict(ok=bool(adopted)))
  active=bool(data.get('active'))
  phase=data.get('phase') if active else 'idle'
  self._beat_review_phase=phase
  self._beat_recording_seek_locked=active and phase=='recording'
  self._beat_recording_active=active
  project=(self._project or {}).get('path')
  self._beat_recording_save_project=live.key(project) if active and project else None
  vst=getattr(self,'_vst',None)
  if vst is not None:
   vst.recording_active=active
  if phase!='review':
   self._clear_review_click()
  player,midi=self._playback_states(False)
  return self._json(dict(ok=True,active=active,recording_clock=self._recording_clock_view(player,midi)))
 def _prepare_review_click(self,seq,doc,candidate,op,stamp):
  if getattr(self,'_beat_review_phase',None)!='review':
   return
  current=getattr(self,'_review_click_prepared',None)
  if current and seq<=current['seq']:
   return
  from .native_metronome import review_click_table
  draft=candidate.get('tempo')
  if not isinstance(draft,dict) or not isinstance(draft.get('pulse_points'),list):
   return
  total=float(doc['duration'])
  try:
   canonical=doc['tempo']
   if not isinstance(canonical.get('pulse_points'),list):
    # Legacy documents may not have materialized their beat flags. Use the
    # same native pulse-state projection instead of synthesizing a BPM grid.
    from astra.document_editor import query
    value=query(self._project['path'],doc['revision'],'pulse_state',expected=stamp)
    pulse=value.get('result',value)
    canonical={**canonical,'pulse_points':pulse['pulse_points'],'time_signatures':pulse.get('time_signatures',canonical['time_signatures'])}
   table=review_click_table(canonical,draft,op,total)
  except (KeyError,TypeError,ValueError,ZeroDivisionError,OSError,RuntimeError):
   # A click-plan error does not reject a valid visual/editor preview.
   self._review_click_prepared=None
   return
  self._review_click_prepared=dict(seq=seq,stamp=tuple(stamp),revision=doc['revision'],
                                    project=live.key(self._project['path']),table=table)
 def _review_click_valid(self,row):
  path=(self._project or {}).get('path')
  doc=(self._project or {}).get('editor') or {}
  return bool(path and getattr(self,'_beat_recording_active',False)
    and getattr(self,'_beat_review_phase',None)=='review' and row['project']==live.key(path)
    and row['revision']==doc.get('revision') and row['stamp']==tuple(getattr(self,'_editor_native_stamp',()) or ()))
 def _current_review_click(self):
  row=getattr(self,'_review_click_active',None)
  return row if row and self._review_click_valid(row) else None
 def _review_click_for_head(self,head):
  row=self._current_review_click()
  if row and getattr(self,'_editor_metronome',False) and head.get('revision')==row['revision'] and tuple(head.get('stamp') or ())==row['stamp']:
   return row
 def _clear_review_click(self):
  old=getattr(self,'_review_click_active',None)
  self._review_click_prepared=self._review_click_active=None
  if old:
   self._sync_review_click()
 def _click_tempo(self,normal):
  row=self._current_review_click()
  return {'_review_click_table':row['table']} if row else normal
 def _sync_review_click(self):
  if self._output_shutdown:
   return
  # Click-only commands must not recalculate mode, seek, gain, loop or Play.
  h=self._output
  row=self._current_review_click()
  owner=h.owner if h and not (h.retiring or h.closing or h.blocked) else None
  prior=getattr(self,'_review_click_pcm',None)
  path=(self._project or {}).get('path')
  project=live.key(path) if path else None
  # A direct PCM owner has no normal in-place metronome. Equip that incumbent
  # only upon an enabled Review, then restore its official map without reopening.
  if prior and (prior['owner'] is not owner or prior['project']!=project or h.intent.audition!='audio'):
   prior['owner'].set_review_click(None,0.)
   prior=self._review_click_pcm=None
  enabled=bool(getattr(self,'_editor_metronome',False))
  if (owner is not None and h.intent.audition=='audio' and hasattr(owner,'set_review_click')
      and not getattr(owner,'inplace_metronome',False)):
   if row and enabled and prior is None:
    prior=self._review_click_pcm=dict(owner=owner,project=project,revision=None,table=None)
   if prior:
    table=None
    if row and enabled:
     table=row['table']
    elif enabled:
     doc=(self._project or {}).get('editor') or {}
     if doc.get('revision')!=prior['revision']:
      from .native_metronome import metronome_beats, review_click_table
      duration=float(doc.get('duration') or 0)
      tempo=doc.get('tempo')
      table=None
      if isinstance(tempo,dict):
       if isinstance(tempo.get('pulse_points'),list):
        table=review_click_table(tempo,tempo,dict(start=0.,end=duration),duration)
       else:
        # Review can project flags for legacy documents without authored
        # pulse points. On exit, restore their normal meter-derived click.
        table=tuple(metronome_beats(tempo,duration))
      prior.update(revision=doc.get('revision'),table=table)
     table=prior['table']
    volume=getattr(self,'_midi_volume',None)
    owner.set_review_click(table,0. if getattr(self,'_muted',False) else (100. if volume is None else volume)/100.)
  elif h is not None and (getattr(owner,'inplace_metronome',False) or self._uses_legacy_audio_click(h.intent)):
   doc=(self._project or {}).get('editor') or {}
   self._stage_legacy_click(metronome=(self._click_tempo(doc.get('tempo')),True) if enabled else (None,False))
 def _recording_clock_view(self,player,midi):
  now_qpc=time.perf_counter()
  project=self._project or {}
  doc=project.get('editor') or {}
  stamp=doc.get('edit_stamp') or {}
  doc_scope=dict(project=project.get('path') or '',generation=stamp.get('generation'),
                 version=stamp.get('version'),source_key=doc.get('source_key') or '',
                 revision=doc.get('revision') or '',
                 native_stamp=list(getattr(self,'_editor_native_stamp',()) or ()))
  r=getattr(self,'_vst_active',lambda:None)()
  audio_heard=bool(player.get('playing') and player.get('audition')=='audio')
  if r is not None and not getattr(r,'_score_source',False) and not audio_heard:
   r.recording_active=bool(getattr(self,'_beat_recording_active',False))
   sr=float(getattr(r,'rate',0) or 0)
   wire=getattr(r,'wire',None) or {}
   plugin_lat=float(wire.get('plugin_latency_samples') or 0)
   insert_lat=float((wire.get('inserts') or {}).get('latency_samples') or 0)
   output_lat=float(wire.get('output_latency_samples') or 0)
   if sr>0:
    breakdown=dict(plugin=plugin_lat/sr,inserts=insert_lat/sr,output=output_lat/sr,ring=0.0)
    lat_wall=breakdown['plugin']+breakdown['inserts']+breakdown['output']
   else:
    breakdown=dict(plugin=0.0,inserts=0.0,output=0.0,ring=0.0)
    lat_wall=None
   seek_active=bool(getattr(r,'_requested_seek',None) is not None or r.motion is not None or (r.wanted is not None and r.applied!=r.wanted))
   cstate='error' if r.error else ('seeking' if seek_active else ('playing' if (r.playing and r.wants_play) else ('preparing' if not r.ready else 'paused')))
   rng=getattr(self,'_audition_range',None) or doc.get('range') or {}
   return dict(
    route_kind='vst',route_id=f'vst:{r.epoch}',audible=True,
    source_key='midi',midi_path=(self._midi_file or {}).get('path'),
    doc_scope=doc_scope,clock_state=cstate,
    position_basis='rendered',locate_generation=getattr(r,'_recording_locate_generation',0),
    rendered_source_sec=float(r.position),
    sampled_qpc_sec=r.sampled_qpc_sec,sent_qpc_sec=now_qpc,
    rate=float(r.playback_rate or 1.0),rate_pending=bool(r.rate_request is not None),
    loop=dict(enabled=bool((wire.get('transport') or {}).get('loop_enabled')),
              start=float(rng.get('start',0.0) or 0.0),end=float(rng.get('end',0.0) or 0.0)),
    underruns=int(wire.get('underruns') or 0),
    latency_wall_sec=lat_wall,latency_breakdown_sec=breakdown)
  # Audio stem or non-VST MIDI route
  h=self._output
  intent=h.intent if h is not None else self._output_desired
  aud=getattr(intent,'audition','audio')
  # The player snapshot names what the transport actually auditions. A MIDI-only
  # project has no audio owner, so its default output intent still says 'audio'
  # while the MIDI is what plays; reporting the idle audio player here kept the
  # recorder at "waiting for clock" and dropped every tap.
  if player.get('audition')=='midi':aud='midi'
  if aud=='midi':
   mem=getattr(self,'_mem_goal',None)
   if mem is not None:
    silent=bool(getattr(self,'_instrument_required',False))
    cstate='playing' if midi.get('playing') else 'paused'
    rng=getattr(self,'_audition_range',None) or doc.get('range') or {}
    return dict(
     route_kind='silent' if silent else 'memory-midi',
     route_id=f"mem:{getattr(self,'_mem_epoch',0)}:{getattr(mem,'gate',0)}:{getattr(mem,'locator',0)}",
     audible=not silent,source_key='midi',midi_path=(self._midi_file or {}).get('path'),
     doc_scope=doc_scope,clock_state=cstate,
     rendered_source_sec=float(midi.get('position',0.0) or 0.0),
     sampled_qpc_sec=midi.get('sampled_at'),sent_qpc_sec=now_qpc,
     rate=float(midi.get('rate',1.0) or 1.0),rate_pending=False,
     loop=dict(enabled=bool(rng.get('loop',False)),start=float(rng.get('start',0.0) or 0.0),end=float(rng.get('end',0.0) or 0.0)),
     underruns=0,latency_wall_sec=0.0 if silent else None,
     latency_breakdown_sec=dict(plugin=0.0,inserts=0.0,output=0.0,ring=0.0))
   cstate='playing' if midi.get('playing') else 'paused'
   rng=getattr(self,'_audition_range',None) or doc.get('range') or {}
   return dict(
    route_kind='native-midi' if (h and h.owner_mode=='native-midi') else 'silent',
    route_id=f"midi:{getattr(h,'owner_generation',0)}",
    audible=not bool(getattr(self,'_instrument_required',False)),
    source_key='midi',midi_path=(self._midi_file or {}).get('path'),
    doc_scope=doc_scope,clock_state=cstate,
    rendered_source_sec=float(midi.get('position',0.0) or 0.0),
    sampled_qpc_sec=midi.get('sampled_at'),sent_qpc_sec=now_qpc,
    rate=float(midi.get('rate',1.0) or 1.0),rate_pending=False,
    loop=dict(enabled=bool(rng.get('loop',False)),start=float(rng.get('start',0.0) or 0.0),end=float(rng.get('end',0.0) or 0.0)),
    underruns=0,latency_wall_sec=None,
    latency_breakdown_sec=dict(plugin=0.0,inserts=0.0,output=0.0,ring=0.0))
  # Audio route (original / piano / guitar)
  seek_req=player.get('seek_requested')
  seek_heard=player.get('seek_heard')
  seeking=bool(seek_req is not None and seek_heard is not None and seek_heard<seek_req)
  cstate='seeking' if seeking else ('preparing' if player.get('preparing') else ('playing' if player.get('playing') else 'paused'))
  actual=(player.get('outputStatus') or {}).get('device',{}).get('actualParameters') or {}
  out_lat=actual.get('output_latency')
  lat_wall=float(out_lat) if isinstance(out_lat,(int,float)) and out_lat>=0 else None
  loop_t=getattr(intent,'loop',(0.0,0.0,False)) or (0.0,0.0,False)
  return dict(
   route_kind=(h.owner_mode if h else 'qt-media'),
   route_id=f"audio:{getattr(h,'owner_generation',0)}:{player.get('cpp_route',0)}:{player.get('source','')}",
   audible=True,source_key=str(player.get('source') or 'original'),
   midi_path=(self._midi_file or {}).get('path'),
   doc_scope=doc_scope,clock_state=cstate,
   position_basis=player.get('recording_position_basis','rendered'),locate_generation=seek_heard,
   rendered_source_sec=float(player.get('position',0.0) or 0.0),
   sampled_qpc_sec=player.get('sampled_at'),sent_qpc_sec=now_qpc,
   rate=float(player.get('rate',1.0) or 1.0),rate_pending=False,
   loop=dict(enabled=bool(loop_t[2]) if len(loop_t)>=3 else False,
             start=float(loop_t[0]) if len(loop_t)>=1 else 0.0,
             end=float(loop_t[1]) if len(loop_t)>=2 else 0.0),
   underruns=0,latency_wall_sec=lat_wall,
   latency_breakdown_sec=dict(plugin=0.0,inserts=0.0,output=lat_wall or 0.0,ring=0.0))
 def _emit_playback(self):
  # Both zero timers can be armed by the same owner observation. Consume both
  # here, without changing the handoff polling cadence or queuing any command.
  self._player_timer.stop();self._midi_timer.stop()
  if self._closed:return
  trace=getattr(self,'_trace',None)
  tracing=trace is not None and trace.enabled
  started=time.perf_counter() if tracing else 0.
  player,midi=self._playback_states(False,compact=True)
  built=time.perf_counter() if tracing else 0.
  payload=dict(player=player,midi=midi,stream_epoch=self._playback_stream_epoch)
  if getattr(self,'_beat_recording_active',False):
   payload['recording_clock']=self._recording_clock_view(player,midi)
  wire=self._json(payload)
  encoded=time.perf_counter() if tracing else 0.
  self.playbackChanged.emit(wire)
  if tracing:
   trace.add('emit',paired=True,chars=len(wire),status_core='outputStatus' in player,
             build_ms=round((built-started)*1000,3),json_ms=round((encoded-built)*1000,3),
             submit_ms=round((time.perf_counter()-encoded)*1000,3))
 def _emit_player(self):
  if getattr(self,'_playback_stream_epoch',0):return self._emit_playback()
  trace=getattr(self,'_trace',None)
  if trace is not None and trace.enabled:trace.add('emit')
  super()._emit_player()
 def _emit_midi(self):
  if getattr(self,'_playback_stream_epoch',0):return self._emit_playback()
  super()._emit_midi()
 def _dm(self):
  # Tolerates bridges built without __init__ (tests): an unsaved default manager = 'auto' routing.
  m=getattr(self,'_devices_mgr',None)
  if m is None:m=self._devices_mgr=AudioDeviceManager()
  return m
 def _tracks_project_path(self):
  preview=((getattr(self,'_separation_workflow',None) or {}).get('preview') or {})
  if preview and preview.get('original_audio')==(self._audio or {}).get('path'):
   return preview.get('project')
  return (self._project or {}).get('path')
 def _retire_output_for_midi(self):
  h=self._output
  if h is None:
   if getattr(self,'_tracks_returning',False):
    self._tracks_returning=self._notes_located=False
   return
  h.sync_web_intent()
  owner=h.owner
  if (getattr(self,'_native_tracks',False) and h.intent.audition=='audio' and h.owner_mode=='native-pcm'
      and owner and getattr(owner,'continuous_sources',False) and not (h.retiring or h.closing or h.blocked)):
   s=owner.snapshot()
   if s.ready and not s.error and math.isfinite(s.position) and s.position>=0:
    self._tracks_checkpoint=(self._output_resources,owner,h.revision+1,h.intent.source,s.position)
    self._tracks_returning=self._notes_located=False
    self._notes_return_position=None
  h.close_after_pause()
  self._output_retiring.append(h);self._output=None
 def _tracks_resume_view(self):
  # Memory and VST own a different clock. Resume the displaced PCM only
  # for the same project's ordered audio sources.
  checkpoint=getattr(self,'_tracks_checkpoint',None)
  if not getattr(self,'_tracks_returning',False) or not checkpoint or self._output is not None:return None
  resources,owner,pause_revision,source,position=checkpoint
  if not resources or (resources[0]!=self._tracks_project_path()
      or resources[1]!=tuple(row['key'] for row in self._sources)
      or tuple(item[0] for item in resources[2])!=tuple(row['path'] for row in self._sources)):
   return None
  s=owner.snapshot()
  if (s.intent_revision>=pause_revision and not s.playing and not s.error
      and getattr(s,'pending_target',None) is None and math.isfinite(s.position) and s.position>=0):
   position=s.position
  if self._notes_return_position is not None:position=self._notes_return_position
  deferred=self._deferred_delta or {}
  source=next((i for i,row in enumerate(self._sources) if row['key']==self._source),source)
  return deferred.get('position',position),deferred.get('source',source)
 def _shared_prepare(self):
  # Unified ASIO policy: warm the single physical ASIO KeptEndpoint and Tracks PCM caches
  # once on project open (silent/paused) so first Seek/Play and page switches are instant.
  if (not getattr(self,'_native_tracks',False) or not self._sources or self._output
      or self._output_retiring or self._deferred_delta is not None
      or not self._uses_native_tracks('audio',1,self._output_desired.metronome)):
   return
  self._preparing_tracks=True
  try:
   h=self._output_ensure(audition='audio')
   if h:h.poll()
  finally:self._preparing_tracks=False
 def _shared_start(self):return False
 def _shared_sync(self):return True
 def _resume_audio(self,*args):self._pending_audio=None
 def _output_poll(self):
  review=getattr(self,'_review_click_active',None)
  if review is not None:
   if self._review_click_valid(review):
    self._sync_review_click()
   else:
    self._clear_review_click()
  elif getattr(self,'_review_click_pcm',None) is not None:
   self._sync_review_click()
  self._output_owners=[o for o in self._output_owners if not ((s:=o.snapshot()).closed and s.output_released and (not hasattr(o,"thread") or o.thread.isFinished()))]
  for h in [*self._output_retiring]+([self._output] if self._output else []):h.poll()
  self._output_retiring=[h for h in self._output_retiring if h.owner is not None]
  for owner,cache in list(self._owned_caches):
   if owner.snapshot().closed:cache.close();self._owned_caches.remove((owner,cache))
  from astra.audio_output import asio_panel_bridge
  asio_panel_bridge.poll_panel(self)
  self._pump_failed_tracks_retry()
  resume=self._tracks_resume_view()
  if resume is not None and resume[0]!=self._output_desired.position:
   self._output_desired=replace(self._output_desired,position=resume[0]);self._player_tick()
  if getattr(self,'_asio_panel',None) is None and self._output_device_available and not self._output_retiring and not getattr(self,'_vst_retiring',()) and self._deferred_delta is not None and not self._output_shutdown:
   delta=self._deferred_delta;self._deferred_delta=None;self._output_change(**delta)
  trace=getattr(self,'_trace',None)
  if trace is not None:trace.heartbeat()
  manager=getattr(self,'_devices_mgr',None)  # (methods here must not rely on other helpers: tests compile them alone)
  if manager is not None and manager.poll() is not None:self._output_ui_signature=None;self._player_tick()  # actual parameters read back: tell the page
  if self._output:
   cursor=getattr(self._output,'_reported_event_cursor',0)
   events=self._output.events[cursor:];self._output._reported_event_cursor=len(self._output.events)
   failures=[e[1] for e in events if e[0]=='raw_fallback']
   if failures:self._raw_failure_reason=failures[-1]
   midi_failures=[e[1] for e in events if e[0]=='native_midi_fallback']
   if midi_failures:self._native_midi_failure=midi_failures[-1]
   pcm_failures=[e[1] for e in events if e[0]=='native_pcm_fallback']
   if pcm_failures:
    self._native_pcm_failure=pcm_failures[-1]
    if manager is not None:
     manager.actual=None;manager.state='failed';manager.message='原生音频核心失败，暂用兼容输出；重新选择输出设备可重试。'
   shared_failures=[e[1] for e in events if e[0]=='native_shared_fallback']
   if shared_failures:self._native_shared_failure=shared_failures[-1]
   for failure in [e[1] for e in events if e[0]=='selected_device_failed']:
    if manager is not None:manager.failed(failure)
    self._player_tick()
   for failure in [e[1] for e in events if e[0]=='device_fallback']:
    if manager is None:break
    target=manager.failed(failure)
    if target.backend!='auto' and not (self._output.blocked or self._output.closing):self._output.change(mode='device',playing=False)
    self._player_tick()
   error=self._output.blocked or self._native_shared_failure or (self._raw_failure_reason if self._output.intent.mode!='native-shared' else None)
   if error and error!=self._reported_output_error:
    # Output failures belong to outputStatus, never the project's job error.
    self._reported_output_error=error
   if not self._output.owner:self._finish_together(None)
   if self._output.owner:
    s=self._output.owner.snapshot();i=self._output.intent
    self._finish_together(s)
    owner=self._output.owner
    if getattr(s,'web_serial',0) and i.audition=='audio' and 0<=i.source<len(self._sources):
     self._source=self._sources[i.source]['key']
    if self._output.owner_mode in ('native-pcm','native-joint','native-audio-click') and s.ready and not s.error and not getattr(owner,'_device_reported',False):
     actual=getattr(s,'output',{});route=getattr(s,'actual_route',{})
     if actual and route and manager is not None and manager.current==getattr(owner,'_device_setup',None):
      meta=dict(backend='asio',device=actual.get('device'),rate=actual.get('sample_rate'),observed_block=actual.get('observed_frames'),output_latency=actual.get('output_latency'),native_core=True,shared=route.get('shared'),raw=route.get('raw'))
      with manager._lock:manager._opened=meta
      owner._device_reported=True
    self._audition_together=i.audition=='joint'
    midi_route=i.audition in ('midi','joint')
    self._midi_transport.update(playing=bool(midi_route and s.playing and s.ready and not s.error),
                                rate=i.rate,transport_order=time.perf_counter())
    if midi_route:self._midi_transport.update(position=s.position,sampled_at=getattr(s,'sampled_at',None))
    trace=getattr(self,'_trace',None)
    if trace is not None:trace.owner(s,i,self._output.owner_mode,generation=self._output.owner_generation)
    click=getattr(s,'midi_diagnostics',{}) or {}
    click_state=tuple(click.get(k) for k in ('click_requested','click_enabled','click_channel','click_gain_cc7','click_unavailable'))
    pcm=getattr(s,'pcm_click',None)
    # Submission counters are available in poll/snapshot, not a reason to emit a packet.
    pcm_state=tuple(getattr(pcm,k,None) for k in ('requested','prepared','preparing','error'))
    signature=(s.position,s.playing,getattr(s,'playhead_moving',None),i.rate,i.audition,i.source,getattr(s,'preparing',False),getattr(s,'device_parked',False),bool(getattr(s,'output_overrange',0)),s.error,s.ready,self._output.owner_mode,self._output.retiring,self._output.blocked,self._output_device_notice,self._output.owner_generation,getattr(s,'intent_revision',None),getattr(s,'web_command',0),getattr(s,'web_heard',0),click_state,pcm_state,((getattr(s,'output',None) or {}).get('asio_driver_parameters')))
    if signature!=getattr(self,'_output_ui_signature',None):
     self._output_ui_signature=signature;self._player_tick()
     if not self._closed:self._midi_timer.start(0)
  if self._output_shutdown:
   endpoint=getattr(self,'_kept_device',None)
   if endpoint is not None and not self._output and not self._output_retiring and not getattr(self,'_vst',None) and not getattr(self,'_vst_retiring',()):
    if endpoint.handle:
     if not endpoint.closing:endpoint.close()
     try:endpoint.poll()
     except Exception:pass
    if not endpoint.handle:self._kept_device=None
  if self._output_shutdown and not self._output_retiring and not self._owned_caches and not self._output_owners and getattr(self,'_asio_panel',None) is None and getattr(getattr(self,'_kept_device',None),'handle',None) is None:self._output_timer.stop()
 def _output_ensure(self, audition=None):
  silent_midi=getattr(self,'_instrument_required',False) and (audition or self._output_intent().audition)=='midi'
  if self._output_shutdown or (not silent_midi and (getattr(self._dm(),'awaiting_asio_confirmation',False) or not self._output_device_available or getattr(self,'_asio_panel',None) is not None)):return
  paths=tuple(row['path'] for row in self._sources)
  project_path=self._tracks_project_path()
  if getattr(self,'_native_tracks',False):
   keys=tuple(row['key'] for row in self._sources)
   seen=(project_path,self._sources_version,keys,paths)
   if seen!=getattr(self,'_tracks_resources_seen',None):
    # Audio identity only. Preview/waveform metadata must not retire the stream.
    identities=[]
    for path in paths:
     stat=Path(path).stat()
     identities.append((path,stat.st_size,stat.st_mtime_ns))
    self._tracks_resource_key=(project_path,keys,tuple(identities))
    self._tracks_resources_seen=seen
   resource_key=self._tracks_resource_key
  else:
   signature=tuple((p,Path(p).stat().st_size,Path(p).stat().st_mtime_ns,row.get('duration',0)) for p,row in zip(paths,self._sources))
   resource_key=(project_path,self._sources_version,signature)
  checkpoint=getattr(self,'_tracks_checkpoint',None)
  if checkpoint and checkpoint[0]!=resource_key:
   self._tracks_checkpoint=None;self._tracks_returning=False
  if self._output_resources!=resource_key:
   if self._output:
    s=self._output.owner.snapshot() if self._output.owner else None
    self._output_desired=replace(self._output.intent,position=(getattr(s,'pending_target',None) if getattr(s,'pending_target',None) is not None else s.position) if s else self._output.intent.position)
    self._output.close();self._output_retiring.append(self._output);self._output=None
   self._output_resources=resource_key
  # The timer reaps owners. Polling here could deliver an older Play before
  # the command that called ensure (Pause, Stop, Seek or a source switch).
  if self._output_retiring:return None
  if not paths and not silent_midi:raise ValueError('No audio sources')
  if self._output is None:
   def factory(i,g,r):
    if getattr(self,'_instrument_required',False):
     if i.audition=='midi':
      from .native_midi import NativeMidiOwner,SilentMidiSession
      o=NativeMidiOwner(self._output_prepared,i,g,r,session_factory=SilentMidiSession,muted=self._muted)
      self._output_owners.append(o);return o
     if i.audition=='joint':
      from .silent_instrument import AudioOnlyJointOwner
      mode=self._native_mode('audio',i.rate,i.metronome)
      return AudioOnlyJointOwner(actual_factory(replace(i,audition='audio',mode=mode),g,r),mode)
    return actual_factory(i,g,r)
   def actual_factory(i,g,r):
    if i.mode in ('native-pcm','native-joint','native-audio-click'):
     from .native_pcm_owner import NativePcmOwner
     from .native_assets import native_dependencies
     setup=self._dm().current
     ensure_kept=getattr(self,'_ensure_kept_endpoint',None)
     endpoint=ensure_kept(direct_mode=True) if (ensure_kept and getattr(self,'_native_tracks',False)) else None
     if endpoint is not None:
      backend='asio'
      dev_name=endpoint.device['native_selector']['exact_device']
      sample_rate=endpoint.device['sample_rate']
      buffer_frames=endpoint.device['buffer_frames'] or 0
     else:
      from .asio_policy import resolve_asio_device_route
      route=getattr(self._dm(),'manual_asio_route',None) or resolve_asio_device_route(setup)
      backend='asio'
      dev_name=route['native_selector']['exact_device']
      sample_rate=route['sample_rate']
      buffer_frames=route['buffer_frames'] or 0
     options=dict(**native_dependencies(need_portaudio=True),sample_rate=sample_rate,backend=backend,device=dev_name,buffer_frames=buffer_frames,timeline_duration=max((s.get('duration',0) for s in self._sources),default=0),muted=self._muted)
     if endpoint is not None:options['kept_endpoint']=endpoint
     midi_native=not getattr(self,'_instrument_required',False) and getattr(self,'_native_midi_enabled',False) and not getattr(self,'_native_midi_failure',None) and not self._output_midi_factory
     tracks=getattr(self,'_native_tracks',False) and self._uses_native_tracks(i.audition,i.rate,i.metronome)
     if tracks:
      options['continuous_sources']=True
     if i.mode=='native-pcm' and (tracks or not midi_native):
      o=NativePcmOwner(paths,i,g,r,**options)
     else:
      from .native_pcm_midi_owner import NativePcmMidiOwner
      prepared=(self._output_prepared if i.audition=='joint' else None) or ('',([],[],i.midi_duration,{}))
      o=NativePcmMidiOwner(paths,prepared,i,g,r,**options)
     o._device_setup=setup;self._output_owners.append(o);return o
    if i.mode=='native-midi':
     from .native_midi import NativeMidiOwner
     o=NativeMidiOwner(self._output_prepared,i,g,r,muted=self._muted)
     self._output_owners.append(o);return o
    if i.mode in ('raw','native-shared','device'):
     cache=ChunkCache(paths)
     if self._experimental_joint and i.audition=='joint':
      from .native_joint_owner import NativeJointOwner
      o=NativeJointOwner(cache,max(s.get('duration',0) for s in self._sources),i,g,r,midi_prepared=self._output_prepared,midi_factory=self._joint_factory,muted=self._muted)
     else:o=NativeIntentOwner(cache,max(s.get('duration',0) for s in self._sources),i,g,r,pause_guard=(i.audition=='audio' and i.rate==1),backend_factory=self._dm().device_factory() if i.mode=='device' else None)
     self._owned_caches.append((o,cache));self._output_owners.append(o)
     def cleanup():
      while not o.transport.wait_closed(60):pass
      cache.close()
     threading.Thread(target=cleanup,name='Astra-cache-retire',daemon=True).start()
     return o
    o=QtIntentOwner(paths,i,g,r,muted=self._muted,midi_prepared=self._output_prepared,midi_factory=self._output_midi_factory)
    self._output_owners.append(o);return o
   source=next((i for i,row in enumerate(self._sources) if row['key']==self._source),0)
   resume=self._tracks_resume_view() if audition!='midi' else None
   if self._device_resume_position is not None:
    initial=replace(self._output_desired,playing=False,position=self._device_resume_position,source=source)
    if initial.audition!='audio' and self._output_prepared is None:initial=replace(initial,audition='audio')
    native=self._want_raw and not self._raw_failure_reason and initial.rate==1 and (initial.audition=='audio' or (self._experimental_joint and initial.audition=='joint'))
    initial=replace(initial,mode=self._native_mode(initial.audition,initial.rate,initial.metronome),gain=0 if self._muted else initial.gain)
   else:initial=Intent(mode=self._native_mode('audio',1,self._output_desired.metronome),source=source,gain=0 if self._muted else self._volume/100,metronome=self._output_desired.metronome,midi_duration=self._output_desired.midi_duration)
   if resume is not None and self._device_resume_position is None:
    position,source=resume
    initial=replace(initial,position=position,source=source)
   if initial.audition=='audio' and initial.mode in ('raw','native-shared','device'):
    # An earlier MIDI/export intent or a retiring source may carry a different grid/gate.
    initial=replace(initial,metronome=copy.deepcopy(self._legacy_click_value()))
   if initial.audition=='audio' and initial.mode in ('native-pcm','native-audio-click','raw','native-shared','device') and getattr(self,'_midi_volume',None) is not None:
    initial=replace(initial,midi_gain=self._midi_volume/100)
   if silent_midi:initial=replace(initial,audition='midi',mode='native-midi')
   self._output=Handoff({'raw':factory,'native-shared':factory,'ordinary':factory,'device':factory,'native-midi':factory,'native-pcm':factory,'native-joint':factory,'native-audio-click':factory},initial,allow_fallback=False)
   self._device_resume_position=None
   if resume is not None:
    self._tracks_checkpoint=None;self._tracks_returning=False
  return self._output
 def _uses_native_tracks(self,audition,rate,metronome):
  return (getattr(self,'_native_tracks',False) and audition=='audio' and rate==1
          and not (metronome and metronome[1]))

 def _native_mode(self,audition,rate,metronome=None):
  eligible=audition=='audio' or (self._experimental_joint and audition=='joint' and self._output_prepared is not None)
  manager=getattr(self,'_devices_mgr',None)
  midi_native=not getattr(self,'_instrument_required',False) and getattr(self,'_native_midi_enabled',False) and not getattr(self,'_native_midi_failure',None) and not self._output_midi_factory
  if audition=='midi' and midi_native and self._output_prepared is not None:return 'native-midi'
  if getattr(self,'_native_tracks',False) and audition=='audio':
   return 'native-audio-click' if (rate!=1 and metronome and metronome[1]) else 'native-pcm'
  if manager is not None and manager.current.backend=='compat':return 'ordinary'
  if audition in ('audio','joint') and getattr(self,'_native_pcm_failure',None):return 'ordinary'
  if audition in ('audio','joint') and .25<=rate<=2 and getattr(self,'_native_pcm_enabled',False):
   backend=manager.current.backend if manager is not None else 'auto'
   if backend in ('auto','asio','wasapi-shared','wasapi-raw'):
    if audition=='joint':
     if midi_native and self._output_prepared is not None:return 'native-joint'
    elif midi_native or not (metronome and metronome[1]):
     # In the native MIDI configuration, audio has one PCM mode for both gate states.
     # Without native MIDI, keep the existing compatibility selection for click-on.
     return 'native-pcm'
  if manager is not None and manager.current.backend!='auto' and audition=='audio' and rate==1:return 'device'
  if not self._want_raw or rate!=1 or not eligible:return 'ordinary'
  if not self._raw_failure_reason:return 'raw'
  unsupported='0x88890027' in str(self._raw_failure_reason).lower() or 'RAW_MODE_UNSUPPORTED' in str(self._raw_failure_reason)
  return 'native-shared' if audition=='audio' and unsupported and not self._native_shared_failure else 'ordinary'
 def _output_intent(self):
  if self._output:self._output.sync_web_intent()
  return self._output.intent if self._output else self._output_desired
 def _output_change(self,**delta):
  if self._output_shutdown:return
  manager=self._dm()
  if getattr(manager,'awaiting_asio_confirmation',False) and delta.get('audition',self._output_intent().audition)!='midi':
   self._deferred_delta=None
   self._output_desired=replace(self._output_desired,**{**delta,'playing':False})
   if delta.get('playing') is True:self._output_command_error=manager.message
   self._player_tick();return
  deferred,self._deferred_delta=self._deferred_delta,None
  if delta.get('audition')=='audio':
   self._cancel_midi_intent()
   if self._output_intent().audition!='audio':
    s=self._output.owner.snapshot() if self._output and self._output.owner else None
    if not (s and s.ready and s.playing and not s.error and not self._output.retiring):delta.setdefault('playing',False)
  if deferred is not None:delta={**deferred,**delta}
  if self._output_device_notice or getattr(self,'_asio_panel',None) is not None:delta=dict(delta,playing=False)
  h=self._output_ensure(audition=delta.get('audition',self._output_intent().audition));candidate=replace(h.intent if h else self._output_desired,**delta)
  if h is None:
   self._output_desired=candidate;self._deferred_delta=dict(vars(candidate))
   if candidate.audition=='audio' and self._tracks_resume_view() is not None:
    # An implicit Play queued during release leaves the final pause-tail
    # position authoritative. An explicit Seek or source command still wins.
    for key in ('position','source'):
     if key not in delta:self._deferred_delta.pop(key)
   self._player_tick();self._notify_midi();return
  if h.blocked:
   # A transport fault belongs to outputStatus. Project adoption can legitimately
   # sync the paused position after a successful transcription; that must not
   # throw and mark the completed job as failed (or authorize another Play).
   self._output_command_error=h.blocked
   self._player_tick();self._notify_midi();return
  if candidate.audition!='audio' and h.owner and not h.retiring and getattr(h.owner,'midi_prepared',None) is not self._output_prepared:h._retire(True)
  if h.owner and not h.retiring and hasattr(h.owner,'pair') and candidate.audition!='joint':h._retire(True)
  if h.owner and not h.retiring and h.owner_mode in ('native-midi','native-pcm','native-joint','native-audio-click') and candidate.midi_duration!=h.intent.midi_duration and not getattr(h.owner,'continuous_sources',False):h._retire(preserve_position='position' not in delta)
  if h.owner and not h.retiring and h.owner_mode in ('native-pcm','native-joint','native-audio-click'):
   if candidate.rate!=h.intent.rate or (candidate.source!=h.intent.source and not getattr(h.owner,'continuous_sources',False)):
    h._retire(preserve_position='position' not in delta)
  delta['mode']=self._native_mode(candidate.audition,candidate.rate,candidate.metronome)
  if candidate.audition=='audio' and delta['mode'] in ('raw','native-shared','device'):
   # Resolve again after merging a deferred intent, or when returning from MIDI.
   # Neither may override the current editor gate or the last click-volume command.
   click=self._legacy_click_value()
   if click!=candidate.metronome:delta['metronome']=copy.deepcopy(click)
   volume=getattr(self,'_midi_volume',None)
   if volume is not None and volume/100!=candidate.midi_gain:delta['midi_gain']=volume/100
  # Keep a live legacy alias of the same capable PCM owner until a real handoff.
  if delta['mode']=='native-pcm' and h.owner_mode=='native-audio-click' and not h.retiring:
   delta['mode']='native-audio-click'
  h.change(**delta);self._output_desired=h.intent;self._player_tick();self._notify_midi()
 def _pump_failed_tracks_retry(self):
  h=getattr(self,'_failed_tracks_retry',None)
  if h is None:return
  if (self._output_shutdown or self._output is not h or h.closing or
      getattr(self,'_settings_asio_handoff',None) is not None or getattr(self,'_asio_panel',None) is not None):
   self._failed_tracks_retry=None;return
  # Handoff removes the old owner only after native output_released.
  if h.retiring or h.owner is not None or self._output_retiring:return
  ep=getattr(self,'_kept_device',None)
  if ep is not None and (ep.closing or getattr(ep,'_open_failure',None)):
   if ep.handle:
    ep.close();ep.poll()
    return
   if ep is self._kept_device:self._kept_device=None
  self._failed_tracks_retry=None
  if not(h.blocked and any(e[0]=='selected_device_failed' for e in h.events)):return
  h.blocked=None;self._output_command_error=None
  self._output_change(playing=True)

 def _play_pause(self):
  if self._output_shutdown:return
  h=self._output
  if (h is not None and h.blocked and not h.closing and
      any(e[0]=='selected_device_failed' for e in h.events)):
   # Only an actual press retries; a second press cancels the pending retry.
   self._failed_tracks_retry=None if getattr(self,'_failed_tracks_retry',None) is h else h
   self._pump_failed_tracks_retry()
   self._player_tick();return
  audition=(self._pending_midi_intent or {}).get('audition')
  if audition in ('midi','joint') and self._defer_midi_transport(toggle=audition):return
  intent=self._output_intent()
  if intent.audition=='audio':self._cancel_midi_intent()
  self._output_change(playing=not intent.playing)
 def _stop_audio(self):
  if self._output_shutdown:return
  if (self._pending_midi_intent or {}).get('audition') in ('midi','joint'):
   self._defer_midi_transport(position=0.,playing=False,audition=None)
  self._cancel_midi_intent()
  self._media_invalidate()
  if self._output or self._output_retiring or self._deferred_delta is not None:self._output_change(playing=False,position=0.)
  elif self._tracks_resume_view() is not None:
   self._notes_return_position=0.
   self._output_desired=replace(self._output_desired,position=0.,playing=False);self._player_tick()
 def _seek_audio(self,position):
  if self._output_shutdown:return
  position=_number(position,0,float('inf'),'试听位置无效。')
  if (self._pending_midi_intent or {}).get('audition') in ('midi','joint') and self._defer_midi_transport(position=position):return
  self._output_change(position=position)
 def _select_audio(self,key,preserve=True):
  if self._output_shutdown:return
  if key is None:
   self._tracks_checkpoint=None;self._tracks_returning=False
   self._cancel_midi_intent();self._deferred_delta=None
   if self._output:self._output.close();self._output_retiring.append(self._output);self._output=None
   self._output_desired=replace(self._output_desired,audition='audio',playing=False)
   self._source=None;return
  idx=next((i for i,s in enumerate(self._sources) if s['key']==key),None)
  if idx is None:raise ValueError('Unknown audio source')
  self._cancel_midi_intent()
  self._source=key
  if self._output or self._output_retiring or self._deferred_delta is not None:
   self._output_change(audition='audio',source=idx,**({} if preserve else {'position':0.,'playing':False}))
 def _audition(self,key,position):
  if self._output_shutdown:return
  self._select_audio(key);idx=next(i for i,s in enumerate(self._sources) if s['key']==key)
  self._output_change(audition='audio',source=idx,position=float(position),playing=True)
 def _navigation_owner(self):
  h=self._output
  if h is None or h.owner is None or h.blocked or h.closing:return None
  h.sync_web_intent()
  return h

 def _pause_native_tracks_for_navigation(self,data):
  if data.get('leaveNativeTracks') is not True or data.get('playing') is not False:return data
  h=self._navigation_owner()
  if h is not None and not h.retiring and h.intent.audition=='audio':
   sample=h.owner.snapshot()
   if data.get('navigation') is not True and h.intent.playing:
    h.change(playing=False);self._output_desired=h.intent
   if sample.ready and not sample.error:data={**data,'position':sample.position}
  return {**data,'leaveNativeTracks':False}

 def _select_audition_source(self,data):
  if self._output_shutdown:return
  data=self._pause_native_tracks_for_navigation(data)
  key=data['key'];delta={'audition':'midi' if key=='midi' else 'audio'}
  navigation=getattr(self,'_native_tracks',False) and data.get('navigation') is True and data.get('playing') is False
  h=self._navigation_owner() if navigation else None
  if navigation and key!='midi' and self._output is None and self._output_desired.audition=='audio':
   checkpoint=getattr(self,'_tracks_checkpoint',None)
   if checkpoint is not None:
    first_return=not self._tracks_returning
    self._tracks_returning=True
    if first_return and self._notes_located and not data.get('separatePageClocks'):
     self._notes_return_position=_number(data['position'],0,float('inf'),'无效播放位置')
    resume=self._tracks_resume_view()
    if resume is not None:
     position,source=resume
     pending,self._deferred_delta=self._deferred_delta,None
     if pending and 'position' in pending and not data.get('separatePageClocks'):self._notes_return_position=position
     if first_return and (not pending or 'source' not in pending):source=checkpoint[3]
     self._source=self._sources[source]['key']
     self._output_desired=replace(self._output_desired,position=position,source=source,playing=False,
                                  loop=(0.,0.,False))
     self._player_tick();self._notify_midi()
     return  # The restored, paused clock needs no device until explicit input.
    self._tracks_returning=False
   if not self._output_retiring and self._deferred_delta is None:
    return  # A cold view change must not initialize the selected ASIO device.
  if (h is not None and key!='midi' and not h.retiring and h.intent.audition=='audio'
      and getattr(h.owner,'continuous_sources',False) and h.owner_mode==h.intent.mode):
   # View-only changes share the existing audio route and source. The tail
   # determines its final paused position; never turn a pre-tail read into seek.
   self._cancel_midi_intent()
   r=data.get('range') or {}
   loop=(r.get('start',0),r.get('end',0),r.get('loop',False))
   delta={key:value for key,value in dict(playing=False,loop=loop).items() if getattr(h.intent,key)!=value}
   if delta:
    h.change(**delta);self._output_desired=h.intent
    self._player_tick();self._notify_midi()
   return
  if key!='midi':
   delta['source']=next(i for i,s in enumerate(self._sources) if s['key']==key);self._source=key
  if not data.get('preservePosition'):
   if type(data['playing']) is not bool:raise ValueError('播放状态无效。')
   delta['playing']=data['playing']
   # Existing handoff can preserve the final acknowledged owner position
   # across modes when there is no explicit seek in the navigation packet.
   if not navigation or h is None:
    delta['position']=_number(data['position'],0,float('inf'),'试听位置无效。')
  r=data.get('range') or {};loop=(r.get('start',0),r.get('end',0),r.get('loop',False))
  if key=='midi':
   self._audition_range=dict(start=loop[0],end=loop[1],loop=loop[2])
   current=self._output_intent()
   s=self._output.owner.snapshot() if self._output and self._output.owner else None
   pending=self._pending_midi_intent or dict(position=s.position if s and s.ready else current.position,playing=current.playing)
   if self._defer_midi_transport(**{**pending,**delta}):
    # A parked MIDI command cannot pause the owner still playing Tracks.
    # Pause that owner in place; the parked target keeps the navigation position.
    if delta.get('playing') is False and current.audition=='audio' and current.playing:
     self._output_change(playing=False)
    return
   if self._output_prepared is None:
    if delta.get('playing') is False and current.audition=='audio' and current.playing:
     self._output_change(playing=False)
    raise ValueError('No MIDI selected')
   delta={**self._midi_controls(),**delta}
  self._output_change(**dict(delta,loop=loop))
 def _midi_play_pause(self):
  self._toggle_midi_output('midi')
 def _toggle_midi_output(self,audition):
  if self._output_shutdown or self._defer_midi_transport(toggle=audition):return
  if self._output_prepared is None:raise ValueError('No MIDI selected')
  intent=self._output_intent()
  delta=dict(audition=audition,playing=not(intent.audition==audition and intent.playing))
  if intent.audition!=audition:delta={**self._midi_controls(),**delta}
  if intent.audition=='audio':delta['position']=self._midi_transport['position']
  self._output_change(**delta)
 def _set_audition_rate(self,value):
  if self._output_shutdown:return
  self._audition_rate=float(value)
  if not self._sources and self._output is None and self._output_prepared is None:
   # New and Restore reset the project clock even before audio is installed.
   self._output_desired=replace(self._output_desired,rate=self._audition_rate)
   return
  self._output_change(rate=self._audition_rate)
 def _set_together(self,enabled):
  # This is a mode toggle, not a Play/Pause command. Validate before retirement.
  if type(enabled) is not bool:raise ValueError('Invalid comparison mode')
  if enabled:
   if not self._sources:raise ValueError('No audio source for comparison')
   if not self._output_device_available:raise ValueError('Selected output is unavailable')
   for row in self._sources:Path(row['path']).stat()
  intent=self._output_intent()
  self._output_change(audition='joint' if enabled else 'midi',playing=enabled or intent.playing,position=intent.position)
  self._together_pending=enabled
 def _finish_together(self,status):
  pending=getattr(self,'_together_pending',None)
  if pending is None:return
  h=self._output
  if h is not None and (h.blocked or (status is not None and status.error)):
   self._together_pending=None
   return
  if h is not None and status is not None and status.ready and not h.retiring and h.intent.audition==('joint' if pending else 'midi'):
   self._together_pending=None
   self._audition_together=pending
   if self._project and self._project.get('editor'):self._project['editor']['audition_together']=pending
   self._publish()
 def _toggle_together(self):
  self._toggle_midi_output('joint')
 def _midi_controls(self):
  doc=self._project['editor'] if self._edit_preview_selected() else {}
  r=getattr(self,'_audition_range',None) or doc.get('range') or {}
  tempo=copy.deepcopy(doc.get('tempo'))
  result=dict(loop=(r.get('start',0),r.get('end',0),r.get('loop',False)),
   metronome=(tempo,bool(tempo is not None and getattr(self,'_editor_metronome',False))),
   midi_duration=self._midi_transport['duration'],rate=getattr(self,'_audition_rate',self._output_desired.rate))
  volume=getattr(self,'_midi_volume',None)
  if volume is not None:result['midi_gain']=volume/100
  return result
 def _uses_legacy_audio_click(self,intent):
  if intent.audition!='audio':return False
  modes=('raw','native-shared','device');h=self._output
  if intent.mode in modes:return True
  if h is not None:return h.owner_mode in modes
  # Before first Play there may be only the default Intent, not the admitted mode.
  return self._native_mode('audio',intent.rate,intent.metronome) in modes
 def _legacy_click_value(self):
  if not getattr(self,'_editor_metronome',False):return (None,False)
  project=self._project or {};doc=project.get('editor') or {}
  # The current project exposes its export tempo before the editor is attached.
  # Once supplied, the editor tempo is authoritative, even if invalid/unavailable.
  return (self._click_tempo(doc.get('tempo',project.get('tempo'))),True)
 def _stage_legacy_click(self,**controls):
  # This lane never ensures or polls an output, computes a new mode, sends
  # locators, or authorizes Play. A handoff in progress keeps its transport intent.
  h=self._output;accepts=h is not None and not (h.blocked or h.closing)
  intent=h.intent if accepts else self._output_desired
  delta={k:v for k,v in controls.items() if getattr(intent,k)!=v}
  if delta:
   delta=copy.deepcopy(delta)
   if accepts:
    h.change(**delta);self._output_desired=h.intent
   else:self._output_desired=replace(intent,**delta)
  pending=self._deferred_delta
  if pending is not None:
   delta={k:v for k,v in controls.items() if pending.get(k)!=v}
   if delta:self._deferred_delta={**pending,**copy.deepcopy(delta)}
 def _sync_editor_transport(self,*,range_only=False,editor_swap=False):
  if self._output_shutdown:return False
  intent=self._output_intent()
  if (not range_only and not editor_swap and getattr(self,'_click_toggle_only',False)
      and (self._current_review_click() is not None or getattr(self,'_review_click_pcm',None) is not None)
      and intent.audition=='audio'):
   self._sync_review_click()
   return True
  if self._uses_legacy_audio_click(intent):
   if range_only:return False
   self._stage_legacy_click(metronome=self._legacy_click_value())
   return not editor_swap
  if range_only or editor_swap or intent.audition!='audio':
   # A CAS receipt or MIDI metadata install must not reconfigure the raw
   # audio owner or put the editor clock onto an export audition.
   if intent.audition=='audio' or not self._edit_preview_selected() or any(self._midi_load_status()):return False
  doc=(self._project or {}).get('editor') or {}
  r=getattr(self,'_audition_range',None) or doc.get('range') or {}
  controls=dict(loop=(r.get('start',0),r.get('end',0),r.get('loop',False)))
  if not range_only:
   tempo=copy.deepcopy(doc.get('tempo'))
   click=(tempo,bool(tempo is not None and getattr(self,'_editor_metronome',False)))
   h=self._output
   if not editor_swap and getattr(self,'_click_toggle_only',False) and h and not h.retiring and getattr(h.owner,'inplace_metronome',False):
    # A button press must not also import an editor loop or resize an already
    # open PCM timeline. Metadata/range sync keeps its existing independent path.
    controls=dict(metronome=click)
   else:controls.update(metronome=click,midi_duration=float(doc.get('duration',0)))
  if self._output or self._deferred_delta is not None:
   delta={k:v for k,v in controls.items() if getattr(intent,k)!=v}
   if delta:self._output_change(**delta)
  elif not range_only:
   self._output_desired=replace(self._output_desired,**controls)
  return True
 def _set_metronome(self,enabled):
  if self._output_shutdown:return
  previous=getattr(self,'_click_toggle_only',False)
  self._click_toggle_only=True
  try:
   super()._set_metronome(enabled)
   # Keep editor validation and state publication. Audio delivery must not
   # depend on whether the editor setter called its MIDI transport sync hook.
   if self._uses_legacy_audio_click(self._output_intent()):
    self._stage_legacy_click(metronome=self._legacy_click_value())
   if self._current_review_click() is not None:
    self._sync_review_click()
  finally:self._click_toggle_only=previous
 def _refresh_audio_tempo_click(self):
  if self._output_shutdown:return
  if self._current_review_click() is not None or getattr(self,'_review_click_pcm',None) is not None:
   self._sync_review_click()
   return
  intent=self._output_intent()
  if intent.audition!='audio':return
  if self._uses_legacy_audio_click(intent):
   self._stage_legacy_click(metronome=self._legacy_click_value())
   return
  h=self._output
  if h and not h.retiring and getattr(h.owner,'inplace_metronome',False):
   doc=(self._project or {}).get('editor') or {}
   tempo=copy.deepcopy(doc.get('tempo'))
   click=(tempo,bool(tempo is not None and getattr(self,'_editor_metronome',False)))
   if getattr(intent,'metronome',None)!=click:
    self._output_change(metronome=click)
 def _editor_audio_loop(self):pass
 def _set_volume(self,value):
  if self._output_shutdown:return
  super()._set_volume(value)
  if self._output:self._output_change(gain=0 if self._muted else self._volume/100)
 def _midi_selection_resume(self,resume):
  if resume is not None:
   audition=self._output_intent().audition
   return dict(position=resume[0],audition=audition,playing=bool(resume[1] and audition in ('midi','joint')))
 def _midi_checkpoint(self,*,sample=_READ_OWNER):
  h=self._output;intent=self._output_intent()
  if intent.audition=='audio':return super()._midi_checkpoint()
  s=(h.owner.snapshot() if sample is _READ_OWNER else sample) if h and h.owner and not h.retiring else None
  target=getattr(s,'pending_target',None)
  position=target if target is not None else s.position if s and s.ready else intent.position
  failed=(h and (h.blocked or h.closing)) or (s and s.error)
  ended=bool(s and s.ready and not s.playing and position>=self._midi_transport['duration']>0)
  return dict(position=position,audition=intent.audition,playing=bool(intent.playing and not failed and not ended))
 def _suspend_midi_output(self):
  h=self._output
  if h and h.intent.audition!='audio':
   self._output_desired=h.intent
   h.close();self._output_retiring.append(h);self._output=None
  if (self._deferred_delta or {}).get('audition') in ('midi','joint'):self._deferred_delta=None
  if self._output_desired.audition!='audio':
   self._output_desired=replace(self._output_desired,playing=False,audition='audio')
   self._audition_together=False
  self._midi_transport.update(playing=False,sampled_at=None)
 def _clear_midi(self):
  self._suspend_midi_output()
  self._output_prepared=None
  super()._clear_midi()
 def _install_parsed_midi(self,path,parsed):
  from astra.midi_preview import prepared_midi_view
  self._midi_file=prepared_midi_view(path,parsed)
  self._output_prepared=(path,parsed)
  duration=parsed[2]
  if self._edit_preview_selected():duration=max(duration,self._project['editor']['duration'])
  # Native availability is determined by its owner, not the WinMM hint.
  self._midi_transport.update(position=0.,playing=False,available=True,message='',duration=duration)
 def _apply_midi_intent(self,intent):
  duration=self._midi_transport['duration'];position=min(intent['position'],duration)
  self._midi_transport.update(position=position,playing=False)
  if intent.get('playing') and position<duration:
   self._output_change(**dict(self._midi_controls(),audition=intent.get('audition') or 'midi',position=position,playing=True))
 def _seek_midi(self,position):
  if self._defer_midi_transport(position=position):return
  if self._output_prepared is None:raise ValueError('No MIDI selected')
  if self._output_intent().audition!='audio':self._output_change(position=position)
  else:
   self._midi_transport['position']=min(position,self._midi_transport['duration']);self._notify_midi()
 def _stop_midi(self):
  if self._defer_midi_transport(position=0.,playing=False,audition=None):return
  self._cancel_midi_intent()
  if self._output_intent().audition!='audio':self._output_change(position=0.,playing=False)
  else:self._midi_transport.update(position=0.,playing=False);self._notify_midi()
 def _set_midi_volume(self,volume):
  self._midi_volume=volume
  h=self._output;intent=self._output_intent()
  if self._uses_legacy_audio_click(intent):
   self._stage_legacy_click(midi_gain=volume/100)
  elif intent.audition!='audio' or (h and getattr(h.owner,'inplace_metronome',False)):
   self._output_change(midi_gain=volume/100)
 def _apply_midi_loop(self,start,end,loop):
  if self._output_intent().audition!='audio':self._output_change(loop=(start,end,loop))
 def _problem(self,message):
  if getattr(self,'_handling_output_command',False):
   self._output_command_error=str(message);self._player_tick();return
  return super()._problem(message)
 @Slot(str)
 def dispatch(self,message):
  if self._output_shutdown:return
  previous=getattr(self,'_handling_output_command',False)
  try:
   data=json.loads(message);action=data.get('action')
   if getattr(self,'_failed_tracks_retry',None) is not None and action in (
       'stop','midiStop','selectAuditionSource','selectAudio','midiPlayPause','selectMidi','selectPdf','setAudioDevice'):
    self._failed_tracks_retry=None
   if getattr(self,'_beat_recording_seek_locked',False) and action in ('seek','midiSeek','audition'):
    return
   if (getattr(self,'_tracks_checkpoint',None) is not None
       and (getattr(self,'_mem_goal',None) is not None or getattr(self,'_vst',None) is not None)
       and action in ('midiPlayPause','midiSeek','midiStop','playPause','seek','stop','auditionTogether')):
    self._notes_located=True
   trace=getattr(self,'_trace',None)
   if action=='metronome':
    self._metronome_commands=getattr(self,'_metronome_commands',0)+1
    enabled=data.get('enabled')
    self._metronome_command_enabled=enabled if type(enabled) is bool else None
    if trace is not None:trace.add('cmd',action=action,enabled=self._metronome_command_enabled)
   elif trace is not None:trace.add('cmd',action=action,position=data.get('position'))
   self._handling_output_command=action in ('playPause','audition','selectAuditionSource','selectAudio','seek','volume','stop','midiPlayPause','midiSeek','midiStop','midiVolume','midiSetRange','auditionRate','auditionTogether','metronome')
   if self._handling_output_command:self._output_command_error=None
   # Only an explicit incoming transport action authorizes resumed sound.
   # Metadata completion, resource reload and deferred internal intents cannot.
   if self._output_device_available and action in ('playPause','midiPlayPause','auditionTogether','audition'):self._output_device_notice=False
   return super().dispatch(message)
  except Exception as exc:self._problem(str(exc))
  finally:self._handling_output_command=previous
 def _midi_state(self,*,sample=_READ_OWNER):
  state=super()._midi_state()
  if getattr(self,'_native_tracks',False):state['transport_order']=time.perf_counter()
  h=self._output;intent=self._output_intent()
  blocked=(h.blocked if h else None) or next((r.blocked for r in self._output_retiring if r.blocked),None)
  if state['loading']:
   if self._edit_preview_selected() and self._output_retiring:
    state['message']=str(blocked or '正在准备 MIDI；等待上一输出停止…')
   return state
  if not state['available'] or intent.audition=='audio':return state
  s=(h.owner.snapshot() if sample is _READ_OWNER else sample) if h and h.owner and not h.retiring else None
  error=blocked or (s.error if s else None)
  playing=bool(s and s.ready and s.playing and not error)
  checkpoint=self._midi_checkpoint(sample=s)
  preparing=bool(not error and (s is None or not s.ready or getattr(s,'preparing',False)
                                 or (checkpoint['playing'] and not playing)))
  state.update(position=checkpoint['position'],playing=playing,preparing=preparing,
               play_pending=bool(preparing and checkpoint['playing']),
               sampled_at=getattr(s,'sampled_at',None) if playing else None,
               message=str(error or ('正在准备 MIDI 输出…' if preparing else '')))
  return state
 def _sample_output(self):
  h=self._output
  return h.owner.snapshot() if h and h.owner else None
 def _playback_states(self,include_sources,*,compact=False):
  # An observation is shared only inside this read, never across a command or
  # before/after Handoff.poll(). Receipts and recovery always sample afresh.
  sample=self._sample_output()
  player=self._player_view(include_sources,sample,compact=compact)
  midi=self._midi_state(sample=sample)
  view=midi.get('vst_instrument')
  if view is not None:
   scope=(getattr(self,'_edit_generation_number',None),view)
   if getattr(self,'_vst_view_snapshot',None)!=scope:
    self._vst_view_snapshot=scope
    self._vst_view_revision=getattr(self,'_vst_view_revision',0)+1
   midi['vst_view_revision']=self._vst_view_revision
   if not include_sources and getattr(self,'_vst_view_ack',None)==self._vst_view_revision:
    del midi['vst_instrument']
  file=self._midi_file or {}
  midi.update(path=file.get('path'),view_token=getattr(self,'_preview_midi_token',None),view_loading=bool(file.get('loading')))
  return player,midi
 def _player_state(self,include_sources=True):
  return self._player_view(include_sources,self._sample_output())
 def _player_view(self,include_sources,s,*,compact=False):
  state=super()._player_state(include_sources)
  state['playhead_moving']=getattr(s,'playhead_moving',None)
  # Native DAC stamps and host packet order use QPC even on Python 3.10.
  if getattr(self,'_native_tracks',False):state['transport_order']=time.perf_counter()
  resume=self._tracks_resume_view() if s is None else None
  if resume is not None:
   position,source=resume
   state.update(position=position,source=self._sources[source]['key'],playing=False,audition='audio')
  if s is not None:
   i=self._output.intent
   state.update(position=s.position,playing=s.playing and s.ready and not s.error and i.audition!='midi',rate=i.rate,audition=i.audition,preparing=getattr(s,'preparing',False),sampled_at=getattr(s,'sampled_at',None))
   if self._output.owner_mode in ('native-pcm','native-audio-click','native-joint') and getattr(s,'sampled_at',None) is not None:
    state['recording_position_basis']='dac'
   if 0<=i.source<len(self._sources):state['source']=self._sources[i.source]['key']
   if i.audition=='audio' and getattr(self._output.owner,'continuous_sources',False):
    state.update(duration=s.duration or state['duration'],native_audition=True,
                 seek_requested=self._output.seek_revision,seek_heard=s.position_revision,
                 seek_target=i.position,play_intent=i.playing,seek_failed=bool(s.error or self._output.blocked))
    web_route=getattr(s,'web_route',0)
    if web_route:
     state.update(cpp_route=web_route,cpp_sources=[row['key'] for row in self._sources],
                  cpp_heard=getattr(s,'web_heard',0),cpp_applied=getattr(s,'applied',0))
  if getattr(self,'_native_tracks',False):state.setdefault('cpp_route',0)
  if getattr(self,'_native_tracks',False) and 'native_audition' not in state:
   state['native_audition']=False
  trace=getattr(self,'_trace',None)
  if trace is not None and trace.enabled:state['trace']=True
  memo=getattr(self,'_playback_status_cache',None)
  if memo is None:memo=self._playback_status_cache=PlaybackStatusCache()
  revision,core=memo.view(self,s)
  diagnostics=self._output_diagnostics(s)
  state['output_status_revision']=revision
  if compact:
   state['output_diagnostics']=diagnostics or None
   if getattr(self,'_playback_status_ack',None)!=revision:state['outputStatus']=core
  else:
   state['outputStatus']={**core,**diagnostics}
  return state
 def _output_status_base(self,s):
  from .output_status_model import output_status
  state={'outputStatus':output_status(self._want_raw,self._output,self._native_shared_failure or self._raw_failure_reason,self._output_device_notice,self._output_device_available,sample=s)}
  manager=getattr(self,'_devices_mgr',None)
  if getattr(manager,'awaiting_asio_confirmation',False):
   state['outputStatus'].update(phase='blocked',actual=None,label=manager.message,requiresResume=True)
  if self._output and self._output.intent.audition=='midi':state['outputStatus'].update(midiEngine='native' if self._output.owner_mode=='native-midi' else 'compat',midiEngineFailure=getattr(self,'_native_midi_failure',None))
  if self._output and self._output.owner_mode in ('native-pcm','native-joint','native-audio-click') and self._output.owner:
   route=getattr(s,'actual_route',{});click=getattr(s,'midi_diagnostics',{}) or {}
   state['outputStatus'].update(nativeCore=True,nativeCoreFailure=getattr(self,'_native_pcm_failure',None),nativeRoute=route)
   if state['outputStatus']['phase'] in ('playing','paused') and route:
    label='原生 ASIO' if route.get('host_type')==3 else ('原生 WASAPI RAW' if route.get('raw') else '原生 WASAPI 共享')
    if self._output.intent.audition=='joint':label+=' + MIDI'
    if click.get('click_enabled'):label+=' + 节拍器'
    state['outputStatus']['label']=label
    fallback=route.get('fallback_error')
    diagnostic=('RAW 不受支持，已使用原生共享输出 (0x%08X)' % (fallback & 0xffffffff)) if fallback else None
    if click.get('click_requested') and click.get('click_unavailable'):
     diagnostic='；'.join(filter(None,(diagnostic,'节拍器：'+click['click_unavailable'])))
    state['outputStatus']['diagnostic']=diagnostic
  if getattr(s,'output_overrange',0):
   state['outputStatus']['diagnostic']=f"PCM overrange: {s.output_overrange} samples (pre-headroom peak {s.pre_headroom_peak:.3f})"
  state['outputStatus'].update(nativeCoreFailure=getattr(self,'_native_pcm_failure',None))
  if getattr(self,'_native_pcm_failure',None) and self._output and self._output.intent.audition in ('audio','joint'):state['outputStatus']['diagnostic']=self._native_pcm_failure
  if self._output and self._output.owner and self._output.intent.audition=='audio' and self._output.owner_mode in ('raw','native-shared','device'):
   pcm_click=getattr(s,'pcm_click',None)
   if pcm_click is not None and pcm_click.requested and pcm_click.error:
    output=state['outputStatus']
    output['diagnostic']='；'.join(filter(None,(output.get('diagnostic'),'节拍器：'+pcm_click.error)))
  state['outputStatus']['control_panel']=dict(getattr(self,'_asio_panel_status',{'phase':'idle'}))
  state['outputStatus'].update(selectable=self._allow_output_settings,scope='session',commandError=getattr(self,'_output_command_error',None))
  m=getattr(self,'_devices_mgr',None)
  b=device_catalog.BACKENDS.get(m.current.backend,{}) if m else {}
  if m is not None:state['outputStatus']['device']=dict(backend=m.current.backend,backendLabel=b.get('label'),shared=b.get('shared'),device=m.current.device,state=m.state,message=m.message,actual=describe(m.actual) if m.actual else None,actualParameters=dict(m.actual) if m.actual else None,requested=dict(backend=m.current.backend,device=m.current.device,sample_rate=m.current.sample_rate,buffer_frames=m.current.buffer_frames))
  if (m is not None and self._output and self._output.owner
      and not (self._output.retiring or self._output.closing or self._output.blocked)
      and m.current==getattr(self._output.owner,'_device_setup',None)
      and s is not None and not s.error and not getattr(s,'closed',False)
      and not getattr(s,'output_released',False)
      and (getattr(s,'actual_route',None) or {}).get('host_type')==3):
   info=(getattr(s,'output',None) or {}).get('asio_driver_parameters')
   if info:state['outputStatus']['device']['driverParameters']=info
  if m is not None and self._output and self._output.owner_mode=='device' and state['outputStatus']['phase'] in ('playing','paused'):state['outputStatus']['label']=(b.get('label') or '所选设备')+('：'+describe(m.actual) if m.actual else '')
  if self._output_command_error and state['outputStatus']['phase'] not in ('blocked','error','unavailable'):state['outputStatus']['label']='播放操作未完成：'+self._output_command_error
  return state['outputStatus']
 def _pcm_click_diagnostics(self,s):
  h=self._output;i=h.intent if h else getattr(self,'_output_desired',None)
  modes=('raw','native-shared','device')
  if i is not None and i.audition=='audio' and (i.mode in modes or (h and h.owner_mode in modes)):
   snap=s if h and h.owner else None
   pcm_click=getattr(snap,'pcm_click',None);resources=getattr(self,'_output_resources',None)
   evidence=dict(engine='pcm-mix',command_sequence=getattr(self,'_metronome_commands',0),
                 command_enabled=getattr(self,'_metronome_command_enabled',None),
                 editor_enabled=bool(getattr(self,'_editor_metronome',False)),
                 intent_enabled=i.metronome[1],tempo_present=i.metronome[0] is not None,loop=i.loop,
                 owner_mode=h.owner_mode if h else None,owner_generation=h.owner_generation if snap is not None else None,
                 resource_sources_version=resources[1] if h and resources else None,
                 source=getattr(snap,'source',None),epoch=getattr(snap,'epoch',None),
                 playback_token=getattr(snap,'playback_token',None),pending_target=getattr(snap,'pending_target',None),
                 owner_revision=getattr(snap,'intent_revision',None),accepted=getattr(snap,'accepted',None),applied=getattr(snap,'applied',None))
   if pcm_click is not None:
    evidence.update(vars(pcm_click))
   return evidence
  return None
 def _output_diagnostics(self,s):
  if s is None:return {}
  h=self._output;result={}
  midi_diagnostics=getattr(s,'midi_diagnostics',None)
  if midi_diagnostics is not None:result.update(midiDiagnostics=midi_diagnostics,midiDiagnosticsGeneration=h.owner_generation)
  if getattr(s,'midi_device',None):result['midiDevice']=s.midi_device
  pcm_evidence=self._pcm_click_diagnostics(s)
  if pcm_evidence is not None:result['metronomeDiagnostics']=pcm_evidence
  return result
 def close(self):
  if super().close() is False:return False
  self._output_shutdown=True;self._deferred_delta=None
  if self._output:self._output.close();self._output_retiring.append(self._output);self._output=None
  # Do not stop cleanup timer until asynchronous owners finish.
  trace=getattr(self,'_trace',None)
  if trace is not None:trace.close()


class UnifiedStudioBridge(VstStudioRoute, MemoryMidiBridge, _FileStudioBridge):
 @Slot()
 def _vst_pump(self):
  return VstStudioRoute._vst_pump(self)
 @Slot(bool)
 def _vst_set_feedback(self, enabled):
  return VstStudioRoute._vst_set_feedback(self, enabled)

 """One output arbiter for both the ordinary and RAW Studio modes."""
 @Slot(str)
 def openVstSettings(self, message):
  self._open_vst_settings(message)
 @Slot(str,result=str)
 def setVstInstrumentGain(self, message):
  try:return self._json(dict(ok=True,gain=self._set_vst_instrument_gain(message)))
  except (ValueError, TypeError, KeyError, RuntimeError) as error:return self._json(dict(ok=False,error=str(error)))
 @Slot(str)
 def vstNoteFeedback(self, message):
  self._vst_note_feedback(message)
