"""Resolve locally built or packaged native DLLs, never download code."""
from pathlib import Path
import json,re,threading

_DEV_SELECTIONS={}
_DEV_LOCK=threading.Lock()
def _development_runtime(dev):
    # Freeze one coherent set per development root, including legacy selection.
    # An already running process never follows a later pointer replacement.
    key=dev.resolve()
    with _DEV_LOCK:
        if key in _DEV_SELECTIONS:
            result,error=_DEV_SELECTIONS[key]
            if error:raise RuntimeError(error)
            return result
        pointer=dev/'runtime-current.json'
        try:
            if not pointer.exists():result=dev
            else:
                data=json.loads(pointer.read_text(encoding='utf8'))
                if not isinstance(data,dict) or set(data)!={'version'} or not isinstance(data['version'],str) or not re.fullmatch('[0-9a-f]{16}',data['version']):
                    raise ValueError('expected a lowercase 16-hex version')
                result=dev/'runtime'/data['version']/'astra'/'audio_output'/'_native'
                # Resolve symlinks/junctions as well as rejecting traversal in JSON.
                if not result.resolve().is_relative_to((dev/'runtime').resolve()):raise ValueError('runtime path escapes version root')
        except (OSError,ValueError,TypeError) as e:
            message=f'Invalid native runtime pointer: {pointer.name}: {e}'
            _DEV_SELECTIONS[key]=(None,message)
            raise RuntimeError(message) from e
        _DEV_SELECTIONS[key]=(result,None)
        return result


def native_library(name):
    if not name.startswith("astra_") or not name.endswith(".dll") or Path(name).name != name:
        raise ValueError("invalid native library name")
    here=Path(__file__).resolve().parent
    dev=here.parents[2]/"native"/"audio_core"
    # Editable checkouts use their build output, never a stale packaged copy.
    if (dev/"CMakeLists.txt").is_file():
        result=_development_runtime(dev)/name
    else:
        result=here/"_native"/name
    if not result.is_file():
        raise FileNotFoundError(f"Native component missing: {name}; build/install native components first")
    return result


def native_dependencies(need_portaudio=True):
    """Locate already installed x64 dependencies without importing audio hosts."""
    import importlib.util
    import platform
    import sys
    if sys.platform != "win32" or sys.maxsize <= 2**32 or platform.machine().lower() not in ("amd64","x86_64"):
        raise RuntimeError("native components currently require Windows x64")
    def package_root(name):
        spec=importlib.util.find_spec(name)
        if spec is None or not spec.origin:
            raise FileNotFoundError(f"Native dependency package missing: {name}")
        return Path(spec.origin).parent
    paths={
        "sndfile_dll":package_root("_soundfile_data")/"libsndfile_x64.dll",
        "samplerate_dll":package_root("samplerate")/"_samplerate_data"/"libsamplerate-64bit.dll",
        "portaudio_dll":(package_root("_sounddevice_data")/"portaudio-binaries"/"libportaudio64bit-asio.dll") if need_portaudio else None,
    }
    for name,path in paths.items():
        if path is not None and not path.is_file(): raise FileNotFoundError(f"Native dependency missing: {name}")
    return paths
