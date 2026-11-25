"""Read-only audio device discovery (R12).

Never loads an ASIO driver, never opens or starts a stream, never writes system or driver
settings. Registry presence is reported as "registered", not as "openable": only an explicit,
user-confirmed open can prove that (see device_manager.AudioDeviceManager).

Backend vocabulary (kept distinct on purpose):
  wasapi-shared  Windows shared-mode stream (IAudioClient3). Mixed by the OS; can run next to Cubase.
  wasapi-raw     The same shared stream with the per-stream RAW processing property. NOT exclusive,
                 NOT ASIO. Many USB interfaces (the user's Focusrite) reject it (0x88890027).
  asio           Vendor ASIO driver loaded in-process (pull/callback model). Whether it can be used
                 while another host (e.g. Cubase) holds it depends on the driver; unverified here.
Design reference: JUCE ASIOAudioIODeviceType::scanForDevices (HKLM\\SOFTWARE\\ASIO -> CLSID ->
HKCR\\CLSID\\{..}\\InprocServer32) and its driver blacklist; no JUCE code is copied.
"""
from importlib.util import find_spec

BACKENDS = {
    'auto': dict(label='自动（RAW 优先，否则原生共享）', shared=True, native=True),
    'wasapi-shared': dict(label='WASAPI 共享', shared=True, native=True),
    'wasapi-raw': dict(label='WASAPI 共享 + RAW 属性（非独占）', shared=True, native=True),
    'asio': dict(label='ASIO 驱动', shared=None, native=True),
    # Qt media playback (former session-only "兼容输出" preference): saved like any other choice.
    'compat': dict(label='兼容输出（Qt 媒体播放）', shared=True, native=False),
}
# Same exclusions as JUCE's scan: these wrap the legacy MME/DirectSound path.
BLACKLIST = ('asio directx full duplex', 'asio multimedia driver')
# Not the interface's own driver: wrappers around WASAPI/WDM or virtual mixers.
WRAPPERS = ('asio4all', 'fl studio asio', 'voicemeeter', 'dsdtranscoder', 'dsd transcoder', 'steinberg built-in asio', 'generic low latency')


class WinRegistry:
    """Minimal read-only registry view (64-bit). Injected in tests."""
    def __init__(self):
        import winreg
        self.w = winreg

    def _open(self, root, path):
        w = self.w
        return w.OpenKey(getattr(w, root), path, 0, w.KEY_READ | w.KEY_WOW64_64KEY)

    def subkeys(self, root, path):
        try:
            with self._open(root, path) as key:
                out, i = [], 0
                while True:
                    try: out.append(self.w.EnumKey(key, i)); i += 1
                    except OSError: return out
        except OSError:
            return []

    def value(self, root, path, name=''):
        try:
            with self._open(root, path) as key:
                return self.w.QueryValueEx(key, name)[0]
        except OSError:
            return None


def asio_drivers(registry=None, exists=None):
    """Registered ASIO drivers. Each row: state registered | dll-missing | no-clsid | blacklisted."""
    import os
    reg = registry or WinRegistry()
    exists = exists or os.path.isfile
    return [_asio_row(reg, name, exists) for name in reg.subkeys('HKEY_LOCAL_MACHINE', r'SOFTWARE\ASIO')]


def asio_driver(name, registry=None, exists=None):
    """Resolve only the requested ASIO registry identity, without listing devices."""
    import os
    if not isinstance(name, str) or not name.strip() or '\x00' in name or '\\' in name:
        return None
    reg = registry or WinRegistry()
    row = _asio_row(reg, name, exists or os.path.isfile)
    return None if row['state'] == 'no-clsid' else row


def _asio_row(reg, name, exists):
    path = 'SOFTWARE\\ASIO\\' + name
    clsid = reg.value('HKEY_LOCAL_MACHINE', path, 'CLSID')
    description = reg.value('HKEY_LOCAL_MACHINE', path, 'Description') or name
    low = (name + ' ' + description).lower()
    dll = reg.value('HKEY_CLASSES_ROOT', 'CLSID\\%s\\InprocServer32' % clsid) if clsid else None
    if any(b in low for b in BLACKLIST): state = 'blacklisted'
    elif not clsid: state = 'no-clsid'
    elif not dll or not exists(dll): state = 'dll-missing'
    else: state = 'registered'
    kind = 'wrapper' if any(w in low for w in WRAPPERS) else 'hardware'
    return dict(id='asio:' + name, name=name, description=description, clsid=clsid, dll=dll,
                state=state, kind=kind, verified_open=False)


def wasapi_endpoints(soundcard_module=None):
    """Output endpoints by name/id (enumeration only; no client is activated)."""
    try:
        sc = soundcard_module
        if sc is None:
            import soundcard as sc
        default = sc.default_speaker()
        return [dict(id=s.id, name=s.name, channels=s.channels, default=s.id == default.id) for s in sc.all_speakers()]
    except Exception as exc:  # enumeration failure must not break playback
        return [dict(id=None, name='无法枚举：' + repr(exc)[:120], channels=0, default=False, error=True)]


def asio_runtime():
    """Whether an ASIO-capable PortAudio runtime is installed. Does NOT import it: importing
    sounddevice with SD_ENABLE_ASIO initialises PortAudio, which loads every ASIO driver."""
    try:
        spec = find_spec('sounddevice')
    except Exception:
        spec = None
    return dict(available=spec is not None, package='sounddevice',
                note=None if spec else '未安装 sounddevice（PortAudio ASIO 版）；ASIO 后端不可用')


def catalog(registry=None, soundcard_module=None, exists=None):
    return dict(backends=BACKENDS, wasapi=wasapi_endpoints(soundcard_module),
                asio=asio_drivers(registry, exists), asio_runtime=asio_runtime())
