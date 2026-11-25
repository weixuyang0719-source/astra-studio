"""Modeless native VST editor. All view rectangles are physical pixels."""
import ctypes as C
from ctypes import wintypes as W
from math import ceil

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import QAbstractScrollArea, QApplication, QFrame
from . import editor_win32 as win

OFF, ON = Qt.ScrollBarPolicy.ScrollBarAlwaysOff, Qt.ScrollBarPolicy.ScrollBarAlwaysOn


class PluginEditorWindow(QAbstractScrollArea):
    observed = Signal(bool)

    def __init__(self, plugin, title):
        super().__init__()
        self.plugin = plugin
        self.setWindowTitle(title)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setHorizontalScrollBarPolicy(OFF)
        self.setVerticalScrollBarPolicy(OFF)
        self._canvas = 0
        self._transport_window = None
        self._proc = self._pixels = self._user_start = None
        self._dpi = 0
        self._opened = self._observe_queued = False
        self._close_error = None
        self._window_signals = False
        self._screen = None
        self._work_changed = False
        self._sync_timer = QTimer(self)
        self._sync_timer.setSingleShot(True)
        self._sync_timer.timeout.connect(self._sync)

    def _observe(self):
        self._observe_queued = False
        opened = False
        if self._canvas and self.plugin.handle and self.plugin.editor_open and win.is_window(self._canvas):
            shell = int(self.winId())
            opened = bool(win.visible(shell) or win.minimized(shell))
        if opened != self._opened:
            self._opened = opened
            self.observed.emit(opened)
        return opened

    def _observe_after_change(self):
        # Coalesce native transitions; there is no polling timer for window state.
        if not self._observe_queued:
            self._observe_queued = True
            QTimer.singleShot(0, self, self._observe)

    def _later(self):
        timer = getattr(self, '_sync_timer', None)
        if self._canvas and timer is not None and not timer.isActive():
            timer.start(0)

    def _fit_shell(self, pixels, *, center=False):
        screen = self.screen()
        work = screen.availableGeometry()
        scale = self.devicePixelRatioF()
        m = self.windowHandle().frameMargins()
        w = min(ceil(pixels[0] / scale), max(1, work.width() - m.left() - m.right()))
        h = min(ceil(pixels[1] / scale), max(1, work.height() - m.top() - m.bottom()))
        self.resize(w, h)
        full_w, full_h = w + m.left() + m.right(), h + m.top() + m.bottom()
        x = work.x() + (work.width() - full_w) // 2 if center else self.x()
        y = work.y() + (work.height() - full_h) // 2 if center else self.y()
        self.move(max(work.x(), min(x, work.right() + 1 - full_w)),
                  max(work.y(), min(y, work.bottom() + 1 - full_h)))
        win.constrain_visible_frame(int(self.winId()))

    def _work_area_changed(self, *_):
        self._work_changed = True
        self._later()

    def _screen_changed(self, screen):
        if self._screen is not None:
            try:
                self._screen.availableGeometryChanged.disconnect(self._work_area_changed)
            except (RuntimeError, TypeError):
                pass  # The old monitor may already have been disconnected.
        self._screen = screen
        if screen is not None:
            screen.availableGeometryChanged.connect(self._work_area_changed)
        self._work_area_changed()

    def _bind_editor_transport(self, on_space):
        shell = int(self.winId()) if on_space is not None else None
        previous = self._transport_window
        if previous and previous[1] != shell:
            previous[0].remove(previous[1])
            self._transport_window = None
        if shell:
            from .editor_transport_win32 import editor_transport
            transport = editor_transport()
            transport.add(shell, on_space)
            self._transport_window = transport, shell

    def present(self, gesture=None, *, on_space=None):
        fresh = not self.plugin.editor_open
        if fresh:
            screen_name = win.owner_screen(gesture)
            screen = next((s for s in QApplication.screens() if s.name() == screen_name), None)
            if screen and self.screen() != screen:
                self.setScreen(screen)
            self.winId()
            if not self._window_signals:
                self.windowHandle().screenChanged.connect(self._screen_changed)
                self._screen_changed(self.screen())
                self._window_signals = True
            try:
                self._fit_shell((1, 1), center=True)  # establish the correct monitor/DPI before the plugin attaches
                parent = int(self.viewport().winId())
                self._canvas, self._proc = win.canvas(parent, self._later)
                self._dpi = win.dpi_for(self._canvas)
                size = self.plugin.attach_editor(self._canvas)
                if size is None:
                    self._release()
                    return {'available': False, 'editor_open': False}
                self._pixels = win.client(self._canvas)
                self._fit_shell(self._pixels, center=True)
            except Exception:
                self._release()
                raise
        if self.isMinimized():
            self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self._bind_editor_transport(on_space)
        self.show()  # WA_ShowWithoutActivating: no implicit foreground theft
        win.constrain_visible_frame(int(self.winId()))
        self._sync()
        active = win.focus_on_gesture(int(self.winId()), gesture)
        w, h = self._pixels
        return dict(available=True, editor_open=self._observe(), width=w, height=h, foreground=active)

    def dismiss(self):
        self._close_error = None
        if self._canvas:
            self.close()
        result = dict(available=True, editor_open=self._observe())
        if self._close_error:
            result['error'] = self._close_error
        return result

    def _sync(self):
        if not self._canvas or not self.plugin.editor_open:
            return
        dpi = win.dpi_for(self._canvas)
        if dpi and dpi != self._dpi:
            self._dpi = dpi
            win.forward_dpi(self._canvas, dpi)
        pixels = win.client(self._canvas)
        win.fit_clipped_wrapper(self._canvas, pixels, dpi)
        if self._pixels != pixels:
            self._pixels = pixels
            self._fit_shell(pixels)
        elif self._work_changed and not self._user_start:
            self._fit_shell(win.client(int(self.winId())))
        self._work_changed = self._work_changed and bool(self._user_start)
        w, h = pixels
        full_w, full_h = win.client(int(self.winId()))
        hb, vb = self.horizontalScrollBar(), self.verticalScrollBar()
        scale = self.devicePixelRatioF()
        sbw, sbh = ceil(vb.sizeHint().width() * scale), ceil(hb.sizeHint().height() * scale)
        need_h, need_v = w > full_w, h > full_h
        need_h = need_h or w > full_w - (sbw if need_v else 0)
        need_v = need_v or h > full_h - (sbh if need_h else 0)
        ph, pv = ON if need_h else OFF, ON if need_v else OFF
        if self.horizontalScrollBarPolicy() != ph:
            self.setHorizontalScrollBarPolicy(ph)
        if self.verticalScrollBarPolicy() != pv:
            self.setVerticalScrollBarPolicy(pv)
        vw, vh = win.client(int(self.viewport().winId()))
        for bar, length, visible in ((hb, w, vw), (vb, h, vh)):
            bar.setRange(0, max(0, length - visible))
            bar.setPageStep(max(1, visible))
            bar.setSingleStep(max(1, round(24 * scale)))
        win.offset(self._canvas, hb.value(), vb.value())

    def scrollContentsBy(self, dx, dy):
        if getattr(self, '_canvas', 0):
            win.offset(self._canvas, self.horizontalScrollBar().value(), self.verticalScrollBar().value())

    def viewportEvent(self, event):
        result = super().viewportEvent(event)
        if event.type() == QEvent.Type.Resize and getattr(self, '_canvas', 0):
            self._later()
        return result

    def nativeEvent(self, event_type, message):
        if getattr(self, '_canvas', 0) and self.plugin.editor_open:
            msg = W.MSG.from_address(int(message))
            if msg.message == 0x0082:  # WM_NCDESTROY: do not retain a recyclable HWND.
                self._bind_editor_transport(None)
            if msg.message in (0x0018, 0x0082):  # WM_SHOWWINDOW, WM_NCDESTROY
                self._observe_after_change()
            if msg.message == 0x0231:  # WM_ENTERSIZEMOVE
                self._user_start = win.client(int(self.winId()))
            elif msg.message == 0x0232 and self._user_start:  # WM_EXITSIZEMOVE; do not resize the plugin on a move
                size = win.client(int(self.winId()))
                changed, self._user_start = size != self._user_start, None
                if changed:
                    self.plugin.request_editor_size(*size)  # fixed-size plugins keep their canvas and can scroll
                self._later()
        return super().nativeEvent(event_type, message)

    def _release(self):
        self._sync_timer.stop()
        if self.plugin.handle and self.plugin.editor_open:
            self.plugin.remove_editor()
        self._bind_editor_transport(None)
        if self._canvas:
            win.release_canvas(self._canvas, self._proc)
        self._canvas = 0
        self._proc = self._pixels = self._user_start = None
        self.horizontalScrollBar().setValue(0)
        self.verticalScrollBar().setValue(0)
        self._observe()

    def closeEvent(self, event):
        try:
            self._release()
        except Exception as exc:
            self._close_error = str(exc) or 'VST editor refused to close'
            event.ignore()
            self._observe()
            return
        super().closeEvent(event)
