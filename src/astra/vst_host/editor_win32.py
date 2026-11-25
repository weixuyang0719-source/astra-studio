"""Win32 plumbing for an explicit editor gesture. Never join input queues or pin windows."""
import ctypes as C
from ctypes import wintypes as W
import os
from functools import lru_cache

u = C.WinDLL('user32', use_last_error=True)
cc = C.WinDLL('comctl32', use_last_error=True)
STUDIO_PEER = None  # bound once on worker launch; old direct launches use their OS parent


def _studio_peer():
    return os.getppid() if STUDIO_PEER is None else STUDIO_PEER
PTR = C.c_size_t
LRESULT = C.c_ssize_t
Proc = C.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM, PTR, PTR)


def _api(lib, name, result, *args):
    f = getattr(lib, name)
    f.restype, f.argtypes = result, args
    return f


foreground = _api(u, 'GetForegroundWindow', W.HWND)
window_process = _api(u, 'GetWindowThreadProcessId', W.DWORD, W.HWND, C.POINTER(W.DWORD))
allow = _api(u, 'AllowSetForegroundWindow', W.BOOL, W.DWORD)
activate = _api(u, 'SetForegroundWindow', W.BOOL, W.HWND)
is_window = _api(u, 'IsWindow', W.BOOL, W.HWND)
visible = _api(u, 'IsWindowVisible', W.BOOL, W.HWND)
minimized = _api(u, 'IsIconic', W.BOOL, W.HWND)
rect = _api(u, 'GetClientRect', W.BOOL, W.HWND, C.POINTER(W.RECT))
dpi_for = _api(u, 'GetDpiForWindow', W.UINT, W.HWND)
related = _api(u, 'GetWindow', W.HWND, W.HWND, W.UINT)
map_points = _api(u, 'MapWindowPoints', C.c_int, W.HWND, W.HWND, C.POINTER(W.POINT), W.UINT)
create = _api(u, 'CreateWindowExW', W.HWND, W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD,
              C.c_int, C.c_int, C.c_int, C.c_int, W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID)
destroy = _api(u, 'DestroyWindow', W.BOOL, W.HWND)
set_pos = _api(u, 'SetWindowPos', W.BOOL, W.HWND, W.HWND, C.c_int, C.c_int, C.c_int, C.c_int, W.UINT)
send = _api(u, 'SendMessageW', LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
set_sub = _api(cc, 'SetWindowSubclass', W.BOOL, W.HWND, Proc, PTR, PTR)
del_sub = _api(cc, 'RemoveWindowSubclass', W.BOOL, W.HWND, Proc, PTR)
default_sub = _api(cc, 'DefSubclassProc', LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
monitor_from = _api(u, 'MonitorFromWindow', W.HANDLE, W.HWND, W.DWORD)


class MonitorInfo(C.Structure):
    _fields_ = [('cb', W.DWORD), ('monitor', W.RECT), ('work', W.RECT), ('flags', W.DWORD), ('device', W.WCHAR * 32)]


monitor_info = _api(u, 'GetMonitorInfoW', W.BOOL, W.HANDLE, C.POINTER(MonitorInfo))
window_rect = _api(u, 'GetWindowRect', W.BOOL, W.HWND, C.POINTER(W.RECT))
extended_frame = _api(C.WinDLL('dwmapi'), 'DwmGetWindowAttribute', C.c_long,
                      W.HWND, W.DWORD, C.POINTER(W.RECT), W.DWORD)
_class_module = _api(u, 'GetClassLongPtrW', PTR, W.HWND, C.c_int)
_module_path = _api(C.windll.kernel32, 'GetModuleFileNameW', W.DWORD,
                    W.HMODULE, W.LPWSTR, W.DWORD)
_system_dir = _api(C.windll.kernel32, 'GetSystemDirectoryW', W.UINT, W.LPWSTR, W.UINT)
_real_class = _api(u, 'RealGetWindowClassW', W.UINT, W.HWND, W.LPWSTR, W.UINT)
_standard_controls = frozenset(('button', 'static', 'edit', 'combobox', 'combolbox',
                                'listbox', 'scrollbar', 'mdiclient', '#32770'))


def constrain_visible_frame(hwnd):
    """Fit the visible native frame after Qt rounds logical units and frame margins."""
    if not visible(hwnd) or minimized(hwnd):
        return
    info, outer, shown = MonitorInfo(), W.RECT(), W.RECT()
    info.cb = C.sizeof(info)
    if not monitor_info(monitor_from(hwnd, 2), C.byref(info)) or not window_rect(hwnd, C.byref(outer)):
        return
    if extended_frame(hwnd, 9, C.byref(shown), C.sizeof(shown)) != 0:
        shown = outer
    work = info.work
    width, height = shown.right - shown.left, shown.bottom - shown.top
    w, h = min(width, work.right - work.left), min(height, work.bottom - work.top)
    x = max(work.left, min(shown.left, work.right - w))
    y = max(work.top, min(shown.top, work.bottom - h))
    if (x, y, w, h) != (shown.left, shown.top, width, height):
        set_pos(hwnd, None, outer.left + x - shown.left, outer.top + y - shown.top,
                outer.right - outer.left + w - width, outer.bottom - outer.top + h - height, 0x14)


def pid_of(hwnd):
    pid = W.DWORD()
    if hwnd:
        window_process(hwnd, C.byref(pid))
    return pid.value


def gesture_for(worker_pid):
    # This code runs in the actual Studio process immediately before explicit open IPC.
    hwnd = foreground()
    if not hwnd or pid_of(hwnd) != os.getpid():
        return {}
    return {'owner': int(hwnd), 'allowed': bool(allow(worker_pid))}


def valid_owner(gesture):
    hwnd = gesture.get('owner') if isinstance(gesture, dict) else None
    peer = _studio_peer()
    if not peer or type(hwnd) is not int or not 0 < hwnd <= PTR(-1).value:return None
    pid=pid_of(hwnd)
    if pid==peer:return hwnd
    if gesture.get('via')==peer and STUDIO_PEER:
        from .editor_peer import verified_parent
        if verified_parent(peer,pid):return hwnd
    return None


def relay_gesture_for(worker_pid,gesture):
    """Only the validated instrument host can relay a Studio click to its own insert."""
    owner=valid_owner(gesture)
    if not owner or not gesture.get('allowed') or gesture.get('via') or foreground()!=owner:return {}
    return dict(owner=int(owner),via=os.getpid(),allowed=bool(allow(worker_pid)))


def owner_screen(gesture):
    hwnd = valid_owner(gesture)
    if hwnd:
        info = MonitorInfo()
        info.cb = C.sizeof(info)
        if monitor_info(monitor_from(hwnd, 2), C.byref(info)):
            return info.device


def focus_on_gesture(hwnd, gesture):
    if not valid_owner(gesture) or gesture.get('allowed') is not True:
        return False
    current = foreground()
    # The musician may already have moved to another application while the plugin loaded.
    if not current or (current!=valid_owner(gesture) and pid_of(current)!=os.getpid()):
        return False
    if current != hwnd:
        activate(hwnd)
    return foreground() == hwnd


def client(hwnd):
    r = W.RECT()
    if not rect(hwnd, C.byref(r)):
        raise C.WinError(C.get_last_error())
    return r.right, r.bottom


def _only_child_at_origin(parent):
    child = related(parent, 5)  # GW_CHILD
    if not child or related(child, 2) or not visible(child) or pid_of(child) != os.getpid():
        return None
    origin = W.POINT()
    map_points(child, parent, C.byref(origin), 1)
    return child if (origin.x, origin.y) == (0, 0) else None


@lru_cache(maxsize=1)
def _system_prefix():
    path = C.create_unicode_buffer(32768)
    length = _system_dir(path, len(path))
    return os.path.normcase(path.value).rstrip('\\') + '\\' if 0 < length < len(path) else None


def _custom_surface(hwnd):
    # Built-in controls can legitimately occupy only part of a plugin's view.
    name = C.create_unicode_buffer(256)
    if not _real_class(hwnd, name, len(name)) or name.value.casefold() in _standard_controls:
        return False
    owner = _class_module(hwnd, -16)  # GCLP_HMODULE
    path = C.create_unicode_buffer(32768)
    count = _module_path(owner, path, len(path)) if owner else 0
    prefix = _system_prefix()
    return bool(prefix and 0 < count < len(path) and
                not os.path.normcase(path.value).startswith(prefix))


def fit_clipped_wrapper(canvas_hwnd, pixels, dpi):
    """Reconcile an unambiguous native surface chain with its VST3 pixel rectangle."""
    if dpi <= 96:
        return
    parent = canvas_hwnd
    clipped = []
    while True:
        child = _only_child_at_origin(parent)
        if not child:
            return
        size = client(child)
        frame = W.RECT()
        if (not window_rect(child, C.byref(frame)) or
                (frame.right-frame.left, frame.bottom-frame.top) != size):
            return
        if size == pixels:
            if clipped:
                if not related(child, 5) and not _custom_surface(child):
                    return
                break  # Full-size content inside one or more clipping wrappers.
        elif all(abs(n * dpi / 96 - p) <= 2 for n, p in zip(size, pixels)):
            clipped.append((parent, child))
        else:
            return
        if not related(child, 5):
            if not clipped or not _custom_surface(child):
                return
            break
        parent = child
    # Resizing a parent may synchronously lay out or replace its children.
    for parent, child in clipped:
        if _only_child_at_origin(parent) != child:
            return
        if client(child) != pixels:
            set_pos(child, None, 0, 0, *pixels, 0x16)


def canvas(parent, changed):
    # Qt owns only the surrounding viewport; it must never reset this plugin-sized HWND.
    hwnd = create(0, 'STATIC', '', 0x56000000, 0, 0, 1, 1, parent, None, None, None)
    if not hwnd:
        raise C.WinError(C.get_last_error())

    @Proc
    def proc(w, msg, wp, lp, ident, ref):
        result = default_sub(w, msg, wp, lp)
        if msg in (0x0005, 0x02e3):  # WM_SIZE, WM_DPICHANGED_AFTERPARENT
            changed()
        return result

    if not set_sub(hwnd, proc, 0x4153, 0):
        destroy(hwnd)
        raise RuntimeError('Cannot observe VST editor size')
    return hwnd, proc


def release_canvas(hwnd, proc):
    del_sub(hwnd, proc, 0x4153)
    destroy(hwnd)


def offset(hwnd, x, y):
    # SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE.
    set_pos(hwnd, None, -x, -y, 0, 0, 0x15)


def forward_dpi(hwnd, dpi):
    # Child HWNDs receive AFTERPARENT, not the top-level WM_DPICHANGED used by IPlugFrame.
    # A child has no top-level suggested placement. Keep its existing client rectangle.
    w, h = client(hwnd)
    suggested = W.RECT(0, 0, w, h)
    send(hwnd, 0x02e0, dpi | (dpi << 16), C.addressof(suggested))
