"""Application-approved instruments, catalog endpoint identity, project state.

The project can remember an instrument key/class, but cannot nominate a DLL
or executable plugin module. Local config or installed VST3 metadata can.
"""
import base64
from dataclasses import dataclass
import json
from hashlib import sha256
import os
from pathlib import Path
import re
import tempfile
from threading import Lock
from uuid import uuid4

from astra.vst_host.state_store import MAX_STATE, load_state, save_state
from . import device_catalog


@dataclass(frozen=True)
class Instrument:
    key: str
    name: str
    class_id: str
    module: str
    vendor: str | None = None
    category: str | None = None


@dataclass(frozen=True)
class Libraries:
    library: str
    bridge_library: str
    device_library: str
    portaudio_library: str

    def open_arguments(self):
        return {key: getattr(self, key) for key in ('bridge_library', 'device_library', 'portaudio_library')}


_name_cache_lock = Lock()


def _instrument_names_path():
    from astra.release_paths import data_root
    return data_root() / '.cache' / 'vst-instrument-names.json'


def _read_instrument_names(path):
    from astra.vst_host.rack_state import plugin_display_name
    try:
        if path.stat().st_size > 512 * 1024:
            return {}
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('names'), list):
        return {}
    names = {}
    for row in value['names'][-1024:]:
        if not isinstance(row, dict):
            continue
        key, cid = row.get('key'), row.get('class_id')
        name = plugin_display_name(row.get('name'))
        if (name and isinstance(key, str) and re.fullmatch(r'[A-Za-z0-9._-]{1,80}', key)
                and isinstance(cid, str) and re.fullmatch(r'[a-f0-9]{32}', cid)):
            names[(key, cid)] = name
    return names


def read_instrument_names():
    """Load display-only names on the control pool."""
    return _read_instrument_names(_instrument_names_path())


def remember_instrument_name(instrument):
    """Record a validated catalog selection after it connects, independent of patch size."""
    from astra.vst_host.rack_state import plugin_display_name
    key, cid = instrument.key, instrument.class_id.lower()
    name = plugin_display_name(instrument.name)
    if not (name and re.fullmatch(r'[A-Za-z0-9._-]{1,80}', key) and re.fullmatch(r'[a-f0-9]{32}', cid)):
        return
    path = _instrument_names_path()
    with _name_cache_lock:
        names = _read_instrument_names(path)
        identity = key, cid
        if names.get(identity) == name:
            return
        names.pop(identity, None)
        names[identity] = name
        rows = [dict(key=k, class_id=c, name=n) for (k, c), n in list(names.items())[-1024:]]
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile('w', dir=path.parent, encoding='utf-8', delete=False) as file:
            tmp = Path(file.name)
        try:
            tmp.write_text(json.dumps(dict(version=1, names=rows), ensure_ascii=False), encoding='utf-8')
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


def _application_data(path):
    path = Path(path)
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8-sig'))
    # First-run defaults are code assets, not a copied trial/user configuration.
    # Installed plugin classes come only from the metadata snapshot below.
    from .native_assets import native_library, native_dependencies
    native = Path(__file__).resolve().parents[1] / 'vst_host' / '_native'
    libraries = dict(library=str(native / 'astra_vst_isolated_fx_host.dll'),
                     bridge_library=str(native / 'astra_vst_rt_bridge.dll'),
                     device_library=str(native_library('astra_portaudio_host.dll')),
                     portaudio_library=str(native_dependencies()['portaudio_dll']))
    for value in libraries.values():
        if not Path(value).is_file():
            raise FileNotFoundError('Packaged VST dependency missing: ' + value)
    return dict(version=1, instruments=[], effects=[], **libraries)


def application_config(path):
    path = Path(path)
    data = _application_data(path)
    if data.get('version') != 1:
        raise ValueError('不支持的 VST 应用配置。')

    def local(value, suffix):
        if not isinstance(value, str) or not value.strip():
            raise ValueError('缺少 VST 应用路径。')
        result = (path.parent / Path(value).expanduser()).resolve()
        if result.suffix.lower() != suffix:
            raise ValueError(f'VST 应用配置需要 {suffix} 路径。')
        return str(result)

    libraries = Libraries(*(local(data[k], '.dll') for k in Libraries.__annotations__))
    instruments = {}
    for row in data['instruments']:
        key, name, cid = row['key'], row['name'], row['class_id']
        if (not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,80}', key)
                or key in instruments or not isinstance(name, str) or not name.strip()
                or not isinstance(cid, str) or not re.fullmatch(r'[A-Fa-f0-9]{32}', cid)):
            raise ValueError('无效或重复的 VST 乐器配置。')
        instruments[key] = Instrument(key, name, cid, local(row['module'], '.vst3'),
            row.get('vendor') if isinstance(row.get('vendor'),str) else None,
            row.get('category') if isinstance(row.get('category'),str) else None)
    from .vst_metadata import additional_classes, static_catalog
    for key, row in additional_classes({k: v.class_id for k, v in instruments.items()},
                                       static_catalog()[0], 'instrument'):
        instruments[key] = Instrument(key, row.name, row.cid, row.module, row.vendor, row.category)
    builtin = builtin_instrument()
    if builtin is not None and builtin.key not in instruments and all(
            v.class_id.lower() != builtin.class_id for v in instruments.values()):
        instruments[builtin.key] = builtin
    return libraries, instruments


# MuseScore Studio 4.6.5's own piano: its FluidSynth engine and MS Basic.sf3,
# packaged as a VST3 instrument so the score plays through this same host
# (scheduler, ASIO/WASAPI endpoint, metronome, loop) with no user plug-in.
BUILTIN_KEY = 'astra-musescore-basic'
BUILTIN_CLASS = '6a2f4c118e3b4d279c15a0e231b7d845'


def builtin_instrument():
    module = Path(__file__).resolve().parents[1] / 'vst_host' / 'builtin' / 'AstraMuseScoreBasic.vst3'
    if not (module / 'Contents' / 'x86_64-win' / 'AstraMuseScoreBasic.vst3').is_file():
        return None
    return Instrument(BUILTIN_KEY, 'MuseScore Basic 钢琴（内置）', BUILTIN_CLASS, str(module),
                      'MuseScore 4.6.5 · FluidSynth', 'Instrument|Piano')


def endpoint_choices(catalog):
    # Use only explicit catalog identities, never a friendly-name search or
    # whatever happens to be system default at the time the worker opens.
    choices = [(backend, row) for backend, rows in (
        ('wasapi-shared', catalog['wasapi']), ('asio', catalog['asio']))
        for row in rows if isinstance(row.get('id'), str) and row['id'] and not row.get('error')
        and (backend != 'asio' or row.get('state') == 'registered' and catalog['asio_runtime']['available'])]
    # The owned host resolves a complete unique name within one host API.
    # Keep catalog ID for UI identity; do not substitute a default or truncate.
    return [(backend, dict(row, native_name_ambiguous=sum(
        b == backend and r.get('name') == row.get('name') for b,r in choices) != 1))
        for backend,row in choices]


def device_route(choice, sample_rate, buffer_frames, channels):
    backend, row = choice
    if backend not in ('asio', 'wasapi-shared') or not row.get('id'):
        raise ValueError('请选择明确的 VST 输出设备。')
    if (type(sample_rate) is not int or not 8000 <= sample_rate <= 384000 or
            type(buffer_frames) is not int or not 0 <= buffer_frames <= 16384):
        raise ValueError('VST 采样率或缓冲帧数无效。')
    maximum = row.get('channels') if backend == 'wasapi-shared' else None
    if (len(channels) != 2 or channels[0] == channels[1] or
            any(type(c) is not int or c < 0 or (maximum is not None and c >= maximum) for c in channels)):
        raise ValueError('请选择两个不同且有效的输出通道。')
    result = dict(backend=backend, device_id=row['id'], sample_rate=sample_rate,
                  buffer_frames=buffer_frames, output_channels=list(channels))
    name = row.get('name')
    if not isinstance(name,str) or not name.strip() or '\0' in name:
        raise ValueError('音频设备目录缺少可验证的完整名称。请重新选择设备。')
    if row.get('native_name_ambiguous'):
        raise ValueError('当前后端存在同名音频设备，无法确定输出目标。')
    try:
        encoded = name.encode('utf-8')
    except UnicodeError as exc:
        raise ValueError('音频设备名称编码无效。') from exc
    if len(encoded) >= 255:
        raise ValueError('音频设备名称过长，不能安全截断后选择。')
    if list(channels) != [0,1]:
        raise ValueError('当前 VST 输出仅支持设备的首对立体声通道 0 / 1。')
    result['native_selector'] = dict(exact_device=name)
    return result


def _paths(project):
    root = Path(project).resolve()
    folder = root / 'vst-instrument'
    if folder.resolve() != folder:
        raise ValueError('VST 状态目录不能重定向到其他位置。')
    ref = folder / 'instrument.json'
    if ref.resolve() != ref:
        raise ValueError('VST 状态引用不能重定向到其他位置。')
    return folder, ref


def instrument_ref(project):
    _, ref = _paths(project)
    try:
        with ref.open(encoding='utf-8') as f:
            data = json.loads(f.read(8193))
    except FileNotFoundError:
        return None
    if (not isinstance(data, dict) or data.get('version') != 1 or
            not isinstance(data.get('instrument'), str) or not isinstance(data.get('class_id'), str) or
            not isinstance(data.get('state'), str) or not re.fullmatch(r'[0-9a-f]{32}\.bin', data['state'])):
        raise ValueError('无效的项目 VST 状态引用。')
    return data


def restore_initial_state(session, state):
    from astra.vst_host.session import PluginSession
    encoded = base64.b64encode(state).decode('ascii')
    if isinstance(session, PluginSession):
        token = uuid4().hex
        session.request('state_set', state=encoded, initial_token=token)
        session.initial_image = (token, state)
    else:
        session.request('state_set', state=encoded)


def restore_instrument(session, project, instrument):
    ref = instrument_ref(project)
    if not ref or ref['instrument'] != instrument.key:
        return
    if ref['class_id'] != instrument.class_id:
        raise ValueError('已保存状态与当前 VST 乐器类不符。')
    folder, _ = _paths(project)
    path = folder / ref['state']
    if path.resolve() != path:
        raise ValueError('VST 状态文件不能重定向到其他位置。')
    state = load_state(path, class_id=instrument.class_id)
    restore_initial_state(session, state)
    return dict(instrument=instrument.key, class_id=instrument.class_id.lower(), state=state, sha256=sha256(state).hexdigest())


def capture_state(session, instrument, *, initial=False, prior_attachment=None, prior_rack=None):
    """Capture opaque state only; formal/recovery publication belongs to Owner."""
    image = getattr(session, 'initial_image', None) if initial else None
    if image is not None:
        session.initial_image = None
    arguments={'newly_prepared_inserts':True} if initial else {}
    if image:arguments['initial_token']=image[0]
    reply = session.request('state_capture', **arguments)
    generation = reply['state_generation']
    if type(generation) is not int or generation < 0:
        raise ValueError('Invalid VST state capture')
    confirmed = image and reply.get('initial_token') == image[0]
    if confirmed:
        state = image[1]
    else:
        encoded = reply['state']
        if not isinstance(encoded, str) or len(encoded) > 4*((MAX_STATE+2)//3):
            raise ValueError('Invalid VST state capture')
        state = base64.b64decode(encoded, validate=True)
    if not 0 < len(state) <= MAX_STATE:
        raise ValueError('Invalid VST state size')
    from astra.live_owner import prepare_instrument
    value = dict(instrument=instrument.key, class_id=instrument.class_id.lower(), state=state)
    if 'inserts' in reply:
        from astra.vst_host.rack_state import decode, encode, plugin_display_name, RACK_KEY, RACK_CLASS
        rack = decode(base64.b64decode(reply['inserts'],validate=True))
        plugin = dict(key=instrument.key, class_id=instrument.class_id.lower(), state=state)
        rack['instrument'] = plugin
        if prior_rack and prior_rack.get('instrument') and 'name' in prior_rack['instrument']:
            plugin['name'] = prior_rack['instrument']['name']
        # The vendor confirmed this exact restored image, and all current
        # slot states were checked above. Keep the original project bytes
        # instead of encoding a large sampler image back to base64 and JSON.
        if (confirmed and prior_attachment and prior_attachment.get('instrument') == RACK_KEY
                and prior_attachment.get('class_id') == RACK_CLASS and prior_rack == rack):
            value = prior_attachment
        else:
            plugin.pop('name', None)
            name = plugin_display_name(instrument.name)
            if name:
                plugin['name'] = name
            # prepare_instrument computes the SHA once for the whole capture.
            value = dict(instrument=RACK_KEY, class_id=RACK_CLASS, state=encode(rack))
    return dict(capture=prepare_instrument(value), generation=generation)


def capture_instrument(session, project, instrument):
    encoded = session.request('state_get')
    if not isinstance(encoded, str) or len(encoded) > 4*((MAX_STATE+2)//3):
        raise ValueError('无效的 VST 状态。')
    state = base64.b64decode(encoded, validate=True)
    folder, target = _paths(project)
    name = uuid4().hex + '.bin'
    save_state(folder / name, class_id=instrument.class_id, state=state)
    # Publish the reference last. Failure leaves the old reference and blob
    # valid; no mutation of project.json, editor checkpoint, or vendor preset.
    fd, temporary = tempfile.mkstemp(prefix='instrument-', suffix='.tmp', dir=folder)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(dict(version=1, instrument=instrument.key, class_id=instrument.class_id, state=name), f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def default_patch_file(instrument):
    from astra.release_paths import data_root
    cid = instrument.class_id.lower()
    if not re.fullmatch(r'[a-f0-9]{32}', cid):
        raise ValueError('无效的插件标识。')
    return data_root() / 'instrument-defaults' / (cid + '.bin')


def save_default_patch(session, instrument):
    encoded = session.request('state_get')
    if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_STATE + 2) // 3):
        raise ValueError('无效的乐器音色。')
    save_state(default_patch_file(instrument), class_id=instrument.class_id.lower(),
               state=base64.b64decode(encoded, validate=True))


def restore_default_patch(session, instrument):
    path = default_patch_file(instrument)
    if path.is_file():
        blob = load_state(path, class_id=instrument.class_id.lower())
        restore_initial_state(session, blob)


def _asio_only_choices():
    runtime = device_catalog.asio_runtime()
    if not runtime['available']:
        return []
    return endpoint_choices(dict(wasapi=[], asio=device_catalog.asio_drivers(), asio_runtime=runtime))


def setup_values(config_path, project):
    libraries, instruments, choices, saved = None, {}, [], None
    errors = []
    read_devices = _asio_only_choices if os.environ.get('ASTRA_NATIVE_TRACKS') == '1' else (lambda: endpoint_choices(device_catalog.catalog()))
    for read, install in (
        (lambda: application_config(config_path), 'config'),
        (read_devices, 'devices'),
        (lambda: instrument_ref(project), 'reference'),
    ):
        try:
            result = read()
            if install == 'config':
                libraries, instruments = result
            elif install == 'devices':
                choices = result
            else:
                saved = result
        except (OSError, ValueError, RuntimeError, KeyError, AttributeError, TypeError, ImportError) as exc:
            errors.append(str(exc))
    return libraries, instruments, choices, saved, '；'.join(errors)


def global_device_route(setup, choices, active_endpoint=None):
    from .asio_policy import resolve_asio_device_route
    return resolve_asio_device_route(setup, choices, active_endpoint=active_endpoint)


def effect_config(path):
    """Resolve effect keys using local config and x64 VST3 static metadata."""
    path=Path(path)
    data=_application_data(path)
    effects={}
    for row in data.get('effects',[]):
        key,cid=row['key'],row['class_id']
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,80}',key) or key in effects:
            raise ValueError('Invalid effect key')
        if not isinstance(cid,str) or not re.fullmatch(r'[A-Fa-f0-9]{32}',cid):
            raise ValueError('Invalid effect class')
        module=(path.parent/Path(row['module']).expanduser()).resolve()
        if module.suffix.lower()!='.vst3':raise ValueError('Effect must be a VST3 module')
        effects[key]=dict(row,module=str(module),class_id=cid.lower(),available=module.exists(),error=None if module.exists() else 'Plugin module is missing')
    from .vst_metadata import additional_classes, static_catalog
    for key, row in additional_classes({k: v['class_id'] for k, v in effects.items()},
                                       static_catalog()[0], 'fx'):
        effects[key] = dict(key=key, name=row.name, class_id=row.cid, module=row.module,
                            vendor=row.vendor, category=row.category, available=True, error=None)
    return effects
