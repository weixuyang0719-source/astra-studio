"""Native transport replaces the web footer on the audio Tracks view."""
import ctypes as C
import json
import math
from pathlib import Path
from time import perf_counter

from PySide6.QtCore import QObject, QRect, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QRegion
from PySide6.QtWidgets import QWidget

from .native_web_transport import _command


class PanelCommand(C.Structure):
    _fields_ = [('size', C.c_uint32), ('version', C.c_uint32), ('serial', C.c_uint64),
                ('route', C.c_int), ('kind', C.c_int), ('source', C.c_int), ('result', C.c_int),
                ('seconds', C.c_double), ('input_qpc', C.c_int64), ('submit_qpc', C.c_int64),
                ('return_qpc', C.c_int64)]


class PanelPreview(C.Structure):
    _fields_ = [('size', C.c_uint32), ('version', C.c_uint32),
                ('gesture', C.c_int32), ('phase', C.c_int32),
                ('route', C.c_int32), ('sequence', C.c_int32),
                ('seconds', C.c_double), ('displayed', C.c_double),
                ('remote_revision', C.c_int32), ('remote_phase', C.c_int32)]


class PanelMotion(C.Structure):
    _fields_ = [('size', C.c_uint32), ('version', C.c_uint32),
                ('paint_count', C.c_uint64), ('glyph_changes', C.c_uint64),
                ('input_qpc', C.c_int64), ('first_input_paint_qpc', C.c_int64),
                ('glyph_paint_qpc', C.c_int64), ('rail_paint_qpc', C.c_int64),
                ('displayed', C.c_double), ('painted', C.c_double), ('sampled_at', C.c_double),
                ('press', C.c_double), ('playing', C.c_int32), ('moving', C.c_int32)]


_Aux = C.CFUNCTYPE(C.c_int,C.c_int,C.c_int,C.c_double)


class NativeTracksPage(QObject):
    """Connect the two views of the timeline without forwarding audio commands."""
    seekVisual = Signal(int, int, int, int, float, int,
                        arguments=['generation', 'gesture', 'phase', 'route', 'seconds', 'sequence'])
    playbackVisual = Signal(int, int, float, float, float, bool, float, int,
                            arguments=['generation', 'route', 'seconds', 'sampledAt', 'sentAt',
                                       'moving', 'rate', 'heardSequence'])

    def __init__(self, panel):
        super().__init__(panel)
        self._panel = panel

    @Slot(bool,result=bool)
    def setTracks(self, active):
        return self._panel.setTracks(active)

    @Slot(int,int,int,int,int,float,int,result=bool)
    def previewSeek(self,generation,expected_gesture,revision,phase,route,seconds,sequence):
        panel=self._panel
        if not panel.tracks or not panel.handle or generation!=panel._generation:
            return False
        return bool(panel._preview_seek(panel.handle,expected_gesture,revision,phase,route,seconds,sequence))


class NativeTracksPanel(QWidget):
    sidebar_width = 72

    def __init__(self, parent, bridge, viewport):
        self.handle = None
        super().__init__(parent)
        self.bridge = bridge
        self.viewport = viewport
        self.tracks = False
        self._generation = 0
        self._seek_visual = None
        self._clock_visual = None
        self._labels = ()
        self._volume = None
        self.setFixedHeight(86)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.hide()
        self.dll = C.CDLL(str(Path(__file__).resolve().parent / '_native' / 'native_transport_panel.dll'))
        signatures = {
            'create': (C.c_void_p, C.c_void_p, C.c_void_p),
            'destroy': (C.c_int, C.c_void_p),
            'fit': (C.c_int, C.c_void_p),
            'focus_keyboard': (C.c_int, C.c_void_p, C.c_int),
            'sync': (C.c_int, C.c_void_p, C.c_int, C.c_double, C.c_double,
                     C.c_int, C.c_int, C.c_int, C.c_int),
            'sync_clock': (C.c_int, C.c_void_p, C.c_int, C.c_double, C.c_double,
                           C.c_int, C.c_int, C.c_int, C.c_int, C.c_double, C.c_double, C.c_int),
            'motion': (C.c_int, C.c_void_p, C.POINTER(PanelMotion), C.c_uint32),
            'ack': (C.c_int, C.c_void_p, C.c_int),
            'preview_state': (C.c_int, C.c_void_p, C.POINTER(PanelPreview), C.c_uint32),
            'preview_seek': (C.c_int, C.c_void_p, C.c_int, C.c_int, C.c_int, C.c_int, C.c_double, C.c_int),
            'last': (C.c_int, C.c_void_p, C.POINTER(PanelCommand), C.c_uint32),
            'set_aux': (C.c_int, C.c_void_p, _Aux),
            'set_sources': (C.c_int, C.c_void_p, C.POINTER(C.c_wchar_p), C.c_int),
            'set_volume': (C.c_int,C.c_void_p,C.c_int),
        }
        for name, (result,*arguments) in signatures.items():
            entry = getattr(self.dll, 'astra_transport_panel_' + name)
            entry.restype, entry.argtypes = result, arguments
            setattr(self, '_' + name, entry)
        user32 = C.WinDLL('user32')
        self._show_child = user32.ShowWindow
        self._show_child.argtypes, self._show_child.restype = (C.c_void_p, C.c_int), C.c_int
        self._focused_child = user32.GetFocus
        self._focused_child.argtypes, self._focused_child.restype = (), C.c_void_p
        self._aux = _Aux(self._post_action)
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self.refresh)
        self.web_page = NativeTracksPage(self)

    def place_over(self, viewport_geometry):
        # Reserve the matching bottom band in the HTML without resizing the
        # Chromium surface. Keep the web navigation dock out of the child mask.
        height = self.height()
        geometry = QRect(viewport_geometry.x(), viewport_geometry.bottom() + 1 - height,
                         viewport_geometry.width(), height)
        self.setGeometry(geometry)
        self.setMask(QRegion(self.sidebar_width, 0, max(0, geometry.width() - self.sidebar_width), height))

    def _release_child(self):
        if self.handle:
            self._destroy(self.handle)
            self.handle = None
        self.destroy(True, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, False)

    def setTracks(self, active):
        active = bool(active)
        if active == self.tracks:
            return True
        self._generation += 1
        self._seek_visual = None
        self._clock_visual = None
        if not active:
            self.tracks = False
            self.timer.stop()
            owned_focus = self.handle and self._focused_child() == self.handle
            if self.handle:
                # WM_SHOWWINDOW retires the old native gesture/capture on the
                # child itself. A hidden parent alone is not this notification.
                self._show_child(self.handle, 0)  # SW_HIDE
                self._sync(self.handle,0,0.,0.,0,0,0,0)
            self.hide()
            if owned_focus:
                self.viewport.setFocus(Qt.FocusReason.OtherFocusReason)
            return True

        # Create lazily on the first Tracks visit, then reuse across pages.
        if not self.handle:
            self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
            self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
            self.handle = self._create(int(self.winId()), C.cast(_command,C.c_void_p))
            if not self.handle or not self._set_aux(self.handle,self._aux):
                self._release_child()
                return False
            self._show_child(self.handle, 0)  # SW_HIDE until initialized
            self._labels = ()
            self._volume = None
        self.place_over(self.viewport.geometry())
        self.tracks = True
        self.refresh()
        self.raise_()
        self.show()
        self._fit(self.handle)
        self._show_child(self.handle, 8)  # SW_SHOWNA
        self.timer.start()
        return True

    def _post_action(self,kind,index,number):
        # Cold/secondary actions use the existing GUI-thread lifecycle. Live
        # play, seek and source changes enter C++ without using this callback.
        if not self.tracks or self.bridge._output_shutdown or not math.isfinite(number):
            return 0
        if kind==1:
            command={'action':'playPause'}
        elif kind==2:
            command={'action':'seek','position':max(0.,number)}
        elif kind==3 and 0<=index<len(self.bridge._sources):
            command={'action':'selectAudio','key':self.bridge._sources[index]['key']}
        elif kind==5:
            command={'action':'stop'}
        elif kind==6:
            command={'action':'volume','value':round(max(0.,min(100.,number)))}
        else:
            return 0
        generation=self._generation
        serialized=json.dumps(command)
        def deliver():
            if self.tracks and self._generation==generation and not self.bridge._output_shutdown:
                self.bridge.dispatch(serialized)
        QTimer.singleShot(0,self,deliver)
        return 1

    def _opening_position(self, output, sample, rows):
        if not rows:
            return 0.
        if output is None:
            # Notes may have displaced the PCM owner altogether.
            resume = self.bridge._tracks_resume_view()
            return resume[0] if resume is not None else 0.
        if output.blocked or output.closing:
            return 0.
        intent = output.intent
        if intent.audition != 'audio' or intent.mode != 'native-pcm':
            return 0.
        # Only use the current canvas's audio identity. Source keys can be the
        # same across projects and a retiring owner can still publish samples.
        resources = self.bridge._output_resources
        if not resources or resources != getattr(self.bridge, '_tracks_resource_key', None):
            return 0.
        preview = ((getattr(self.bridge, '_separation_workflow', None) or {}).get('preview') or {})
        project = (self.bridge._project or {}).get('path')
        if preview and preview.get('original_audio') == (self.bridge._audio or {}).get('path'):
            project = preview.get('project')
        if (project != resources[0] or tuple(row['key'] for row in rows) != resources[1]
                or tuple(row['path'] for row in rows) != tuple(identity[0] for identity in resources[2])):
            return 0.
        position = intent.position
        if (sample and sample.ready and not sample.error and output.retiring
                and output._preserve_retired_position and output.seek_revision == output._retire_seek_revision
                and getattr(sample, 'position_revision', 0) >= output.seek_revision):
            # The retiring owner's final heard position is the checkpoint the
            # handoff will pass to the new audio owner, unless a seek superseded it.
            position = sample.position
        return position if math.isfinite(position) and position >= 0. else 0.

    def refresh(self):
        if not self.handle:
            return
        output = self.bridge._output
        sample = self.bridge._sample_output()
        rows = self.bridge._sources
        intent = output.intent if output is not None else None
        ready = bool(self.tracks and sample and sample.ready and not sample.error and rows
                     and output and not output.retiring and not output.closing and not output.blocked
                     and intent.audition=='audio')
        route = getattr(sample,'web_route',0) if ready and intent.rate==1. else 0
        enabled = bool(self.tracks and rows and not (output and (output.closing or output.blocked)))
        labels=tuple(str(row.get('label') or row['key']) for row in rows)
        if labels!=self._labels:
            self._set_sources(self.handle,(C.c_wchar_p*len(labels))(*labels),len(labels))
            self._labels=labels
        volume=int(self.bridge._volume)
        if volume!=self._volume:
            self._set_volume(self.handle,volume)
            self._volume=volume
        source=next((i for i,row in enumerate(rows) if row['key']==self.bridge._source),0)
        if ready:
            source=getattr(sample,'source',source)
        fallback=max((row.get('duration',0.) or 0. for row in rows),default=0.)
        duration=(sample.duration if ready else 0.) or fallback
        position=sample.position if ready else self._opening_position(output,sample,rows)
        sampled_at=(getattr(sample,'sampled_at',None) or 0.) if ready else 0.
        playing=bool(ready and sample.playing)
        moving_hint=getattr(sample,'playhead_moving',None) if ready else None
        moving=playing and (bool(moving_hint) if moving_hint is not None else True)
        rate=intent.rate if ready else 1.
        sequence=getattr(sample,'web_heard',0) if route else 0
        self._sync_clock(self.handle,route,duration,position,int(playing),source,len(rows),int(enabled),
                         sampled_at,rate,int(moving))
        if ready:
            self._ack(self.handle,sequence)
        visual=self.preview_state()
        signature=(self._generation,visual.gesture,visual.phase,visual.route,
                   visual.seconds if visual.phase else None,visual.sequence)
        if signature!=self._seek_visual:
            self._seek_visual=signature
            self.web_page.seekVisual.emit(self._generation,visual.gesture,visual.phase,visual.route,
                                          visual.seconds,visual.sequence)
        if ready and sampled_at > 0:
            self._publish_clock(route, source, position, sampled_at, moving, rate, sequence)
        else:
            self._clock_visual=None

    def _publish_clock(self, route, source, position, sampled_at, moving, rate, sequence):
        # Send one compact sample at most every 50 ms during uninterrupted
        # movement. State/source/seek changes stay immediate.
        key=(route,source,moving,rate,sequence)
        previous=self._clock_visual
        now=perf_counter()
        if previous and previous[0]==key:
            _, prior_position, prior_sample, prior_send=previous
            if position==prior_position and sampled_at==prior_sample:
                return
            if not moving and position==prior_position:
                return
            predicted=prior_position+max(0.,sampled_at-prior_sample)*rate
            if moving and now-prior_send<.05 and abs(position-predicted)<.04:
                return
        self._clock_visual=(key,position,sampled_at,now)
        self.web_page.playbackVisual.emit(self._generation,route,position,sampled_at,now,moving,rate,sequence)

    def visual_metrics(self):
        state=PanelMotion()
        if self.handle:
            self._motion(self.handle,C.byref(state),C.sizeof(state))
        return state

    def preview_state(self):
        state = PanelPreview()
        if self.handle:
            self._preview_state(self.handle,C.byref(state),C.sizeof(state))
        return state

    def focusInEvent(self,event):
        super().focusInEvent(event)
        reason=event.reason()
        if self.tracks and self.handle and reason in (Qt.FocusReason.TabFocusReason,Qt.FocusReason.BacktabFocusReason):
            self._focus_keyboard(self.handle,int(reason==Qt.FocusReason.BacktabFocusReason))

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if self.handle:
            self._fit(self.handle)

    def last_command(self):
        command = PanelCommand()
        if self.handle:
            self._last(self.handle,C.byref(command),C.sizeof(command))
        return command

    def shutdown(self):
        self.setTracks(False)
        if self.handle:
            self._release_child()
