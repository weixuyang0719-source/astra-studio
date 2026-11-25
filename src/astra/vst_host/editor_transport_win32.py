"""Route plain Space only inside registered effect editors in this Qt worker."""
import ctypes as C
from ctypes import wintypes as W
from threading import get_native_id

from PySide6.QtCore import QAbstractNativeEventFilter
from PySide6.QtWidgets import (QAbstractButton, QAbstractItemView, QAbstractSlider,
                              QAbstractSpinBox, QApplication, QComboBox, QLineEdit,
                              QPlainTextEdit, QTextEdit)

from . import editor_win32 as win

KEY_DOWN, KEY_UP, SYS_KEY_UP, SPACE = 0x0100, 0x0101, 0x0105, 0x20
PREVIOUSLY_DOWN = 1 << 30
_GUI_INTERACTION = 0x02 | 0x04 | 0x08 | 0x10  # move/size, menu, system menu, popup menu
_MODIFIERS = (0x10, 0x11, 0x12, 0x5b, 0x5c)  # Shift, Control, Alt, left/right Win
_LOCAL_CLASSES = {
    'edit', 'button', 'combobox', 'combolbox', 'listbox', 'scrollbar', '#32768', '#32770',
    'syslistview32', 'systreeview32', 'systabcontrol32', 'sysdatetimepick32', 'sysmonthcal32',
    'directuihwnd', 'ime', 'msctfime ui',
}
_QT_LOCAL = (QAbstractButton, QAbstractItemView, QAbstractSlider, QAbstractSpinBox,
             QComboBox, QLineEdit, QPlainTextEdit, QTextEdit)


class _GuiThreadInfo(C.Structure):
    _fields_ = [('cbSize', W.DWORD), ('flags', W.DWORD), ('active', W.HWND),
                ('focus', W.HWND), ('capture', W.HWND), ('menu', W.HWND),
                ('movesize', W.HWND), ('caret', W.HWND), ('caret_rect', W.RECT)]


_ancestor = win._api(win.u, 'GetAncestor', W.HWND, W.HWND, W.UINT)
_gui_thread_info = win._api(win.u, 'GetGUIThreadInfo', W.BOOL, W.DWORD, C.POINTER(_GuiThreadInfo))
_key_state = win._api(win.u, 'GetKeyState', C.c_short, C.c_int)


def _root(hwnd):
    return _ancestor(hwnd, 2)  # GA_ROOT follows parents, not popup owners.


def _class_is_local(hwnd):
    buf = C.create_unicode_buffer(256)
    if not win._real_class(hwnd, buf, len(buf)):
        return True  # Window/focus was lost; don't take the key.
    name = buf.value.casefold()
    return (name in _LOCAL_CLASSES or name.startswith(('richedit', 'scintilla', 'msctls_'))
            or name.startswith(('windowsforms10.edit.', 'windowsforms10.richedit.',
                                'windowsforms10.combobox.', 'windowsforms10.listbox.')))


def _native_local(target, shell):
    # Text input may be nested inside a combo/control wrapper.
    while target and target != shell:
        if _class_is_local(target):
            return True
        target = _ancestor(target, 1)  # GA_PARENT, no traversal into a popup's owner.
    return target != shell


class EditorTransportFilter(QAbstractNativeEventFilter):
    def __init__(self, app):
        super().__init__()
        self._app = app
        self._thread = get_native_id()
        self._editors = {}
        self._press = None

    def add(self, shell, callback):
        if not self._editors:
            self._app.installNativeEventFilter(self)
        self._editors[shell] = callback

    def remove(self, shell):
        if self._editors.pop(shell, None) is None:
            return
        if self._press and self._press[0] == shell:
            self._press = None
        if not self._editors:
            self._app.removeNativeEventFilter(self)

    def _eligible(self, target, shell):
        if not target or _root(target) != shell:
            return False
        state = _GuiThreadInfo()
        state.cbSize = C.sizeof(state)
        if (not _gui_thread_info(self._thread, C.byref(state)) or state.active != shell
                or state.focus != target or state.flags & _GUI_INTERACTION
                or state.capture or state.menu or state.movesize
                or (state.caret and _root(state.caret) == shell)):
            return False
        if self._app.activePopupWidget() or self._app.activeModalWidget():
            return False
        if isinstance(self._app.focusWidget(), _QT_LOCAL) or _native_local(target, shell):
            return False
        return not any(_key_state(modifier) < 0 for modifier in _MODIFIERS)

    def nativeEventFilter(self, event_type, message):
        # Qt 6.12's Win32 dispatcher offers queued HWND input here before
        # TranslateMessage, including foreign (vendor) child HWNDs. Native
        # menu loops that do their own dispatch never acquire our shortcut.
        if event_type != b'windows_generic_MSG' or not message:
            return False, 0
        msg = W.MSG.from_address(int(message))
        if msg.message not in (KEY_DOWN, KEY_UP, SYS_KEY_UP) or msg.wParam != SPACE:
            return False, 0
        previous = self._press
        repeat = bool(msg.lParam & PREVIOUSLY_DOWN)
        if msg.message != KEY_DOWN or not repeat:
            self._press = None
        if msg.message == SYS_KEY_UP or (previous is None and (msg.message == KEY_UP or repeat)):
            return False, 0
        target, shell = msg.hWnd, win.foreground()
        emit = self._editors.get(shell)
        if not emit or not self._eligible(target, shell):
            self._press = None
            return False, 0
        if repeat or msg.message == KEY_UP:
            owned = previous == (shell, target)
            if not owned:
                self._press = None
            return owned, 0
        if emit(shell):  # enqueue only; no wire waits, renderer or focus changes
            self._press = (shell, target)
            return True, 0
        return False, 0


def editor_transport():
    app = QApplication.instance()
    instance = getattr(app, '_astra_editor_transport', None)
    if instance is None:
        instance = app._astra_editor_transport = EditorTransportFilter(app)
    return instance
