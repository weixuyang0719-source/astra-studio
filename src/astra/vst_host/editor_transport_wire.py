"""The private worker pipe carries a bounded, scoped Space edge, never key input."""
from dataclasses import dataclass
from time import monotonic_ns

EVENT = 'editor_space'
_FIELDS = {'event', 'hwnd', 'pid', 'sent_ns', 'scope'}
_HEX = frozenset('0123456789abcdef')


def valid_scope(value):
    return isinstance(value, str) and len(value) == 32 and all(c in _HEX for c in value)


@dataclass(frozen=True)
class EditorSpace:
    hwnd: int
    pid: int
    sent_ns: int
    scope: str

    @classmethod
    def decode(cls, value):
        if (isinstance(value, dict) and set(value) == _FIELDS and value['event'] == EVENT
                and type(value['hwnd']) is int and 0 < value['hwnd'] < 2**64
                and type(value['pid']) is int and 0 < value['pid'] < 2**32
                and type(value['sent_ns']) is int and 0 < value['sent_ns'] < 2**64
                and valid_scope(value['scope'])):
            return cls(value['hwnd'], value['pid'], value['sent_ns'], value['scope'])
        return None

    def encode(self, scope=None):
        return dict(event=EVENT, hwnd=self.hwnd, pid=self.pid, sent_ns=self.sent_ns,
                    scope=self.scope if scope is None else scope)

    def current(self):
        # Python >=3.10 uses a common Windows monotonic clock across processes.
        # Never replay a queued edge after a blocked relay/Studio catches up.
        if not 0 <= monotonic_ns() - self.sent_ns <= 500_000_000:
            return False
        from . import editor_win32 as win
        return (win.foreground() == self.hwnd and win.pid_of(self.hwnd) == self.pid
                and win.visible(self.hwnd) and not win.minimized(self.hwnd))
