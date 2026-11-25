"""Apply a temporary Windows performance hint in this private plugin worker."""
import ctypes as C
import os
import sys


def private_wire():
    """Keep protocol descriptors private before Qt, the host or vendor code loads."""
    requests=os.fdopen(os.dup(sys.stdin.fileno()),'r',encoding='utf-8')
    try:
        replies=os.fdopen(os.dup(sys.stdout.fileno()),'w',encoding='utf-8',newline='\n',buffering=1)
    except BaseException:
        requests.close()
        raise
    try:
        os.set_inheritable(requests.fileno(),False)
        os.set_inheritable(replies.fileno(),False)
        # A plugin can use CRT fd 1 or GetStdHandle(STD_OUTPUT_HANDLE).
        # Both point at the diagnostic stream after this. Its reader is
        # independent of our strict UTF-8 request/reply parser.
        with open(os.devnull,'rb') as null:
            os.dup2(null.fileno(),sys.stdin.fileno(),inheritable=True)
        os.dup2(sys.stderr.fileno(),sys.stdout.fileno(),inheritable=True)
        if sys.platform=='win32':
            import msvcrt
            set_handle=C.WinDLL('kernel32',use_last_error=True).SetStdHandle
            set_handle.argtypes,set_handle.restype=[C.c_uint32,C.c_void_p],C.c_int
            for which,fd in ((-10,sys.stdin.fileno()),(-11,sys.stdout.fileno())):
                if not set_handle(which & 0xffffffff,msvcrt.get_osfhandle(fd)):
                    raise C.WinError(C.get_last_error())
        return requests,replies
    except BaseException:
        requests.close()
        replies.close()
        raise


class _PowerState(C.Structure):
    _fields_ = [('version', C.c_uint32), ('control', C.c_uint32), ('state', C.c_uint32)]


class PreparationQoS:
    """Leave scheduler priorities, core affinity and explicit EcoQoS policy alone."""
    def __init__(self):
        self.changed = []
        self.targets = []
        if sys.platform != 'win32':
            return
        api = C.WinDLL('kernel32', use_last_error=True)
        try:
            for kind, info in (('Process', 4), ('Thread', 3)):
                current = getattr(api, 'GetCurrent' + kind)
                current.restype = C.c_void_p
                get = getattr(api, 'Get' + kind + 'Information')
                set_info = getattr(api, 'Set' + kind + 'Information')
                get.argtypes = set_info.argtypes = [C.c_void_p, C.c_int, C.POINTER(_PowerState), C.c_uint32]
                self.targets.append((current(), info, get, set_info))
        except AttributeError:
            self.targets.clear()

    def activate(self):
        if self.changed:
            return
        pending = []
        # Read both initial masks first. Respect explicit efficiency settings,
        # including the case where only the process controls execution speed.
        for handle, info, get, set_info in self.targets:
            before = _PowerState(1, 0, 0)
            if not get(handle, info, C.byref(before), C.sizeof(before)):
                return
            if before.control & 1:
                if before.state & 1:
                    return
                continue
            pending.append((handle, info, set_info, before))
        for handle, info, set_info, before in pending:
            active = _PowerState(1, before.control | 1, before.state & ~1)
            if not set_info(handle, info, C.byref(active), C.sizeof(active)):
                self.restore()
                return
            self.changed.append((handle, info, set_info, before))

    def restore(self):
        while self.changed:
            handle, info, set_info, before = self.changed[-1]
            if not set_info(handle, info, C.byref(before), C.sizeof(before)):
                raise C.WinError(C.get_last_error())
            self.changed.pop()

    def __enter__(self):
        self.activate()
        return self

    def __exit__(self, *_):
        self.restore()


if __name__ == '__main__':
    requests,replies=private_wire()
    with PreparationQoS() as preparation:
        from .session_worker import main
        raise SystemExit(main(preparation,requests,replies))
