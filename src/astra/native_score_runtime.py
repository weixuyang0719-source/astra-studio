"""Local transport to the pinned MuseScore worker; no Python score model."""
from base64 import b64decode
from functools import lru_cache
from hashlib import sha256
from struct import unpack
import json
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory

from .preview_cancel import check_cancelled, run_process
from .release_paths import data_root

RENDERER = 'musescore-studio-4.6.5-native'
FRAME_PREFIX = b'ASTRA_SCORE '
MAX_FRAME = 2 * 1024 * 1024  # bounded high-DPI score raster response
MAX_SOURCE = 64 * 1024 * 1024
RANGE_RASTER_SCALES = (1, 1.5, 2)


def enabled():
    return bool(os.environ.get('ASTRA_NATIVE_SCORE_WORKER_EXE'))


def runtime():
    worker, sdk = (os.environ.get(k) for k in ('ASTRA_NATIVE_SCORE_WORKER_EXE', 'ASTRA_NATIVE_SCORE_QT_ROOT'))
    if not worker or not sdk:
        raise OSError('缺少原生制谱程序或匹配的 Qt 环境。')
    exe = Path(worker).resolve(strict=True)
    qt = Path(sdk).resolve(strict=True)
    if not exe.is_file() or not (qt/'plugins'/'platforms').is_dir():
        raise OSError('原生制谱运行环境不完整。')
    return exe, qt


def environment(qt):
    values = {key: value for key, value in os.environ.items()
              if not key.upper().startswith(('QT_', 'QTWEBENGINE_', 'QML_', 'QML2_', 'ASTRA_NATIVE_SCORE_',
                                             'MUSESCORE_'))}
    values.update(ASTRA_NATIVE_SCORE_WORKER='1', QT_QPA_PLATFORM='offscreen', QT_QUICK_BACKEND='software',
                  QT_QPA_PLATFORM_PLUGIN_PATH=str(qt/'plugins'/'platforms'),
                  PATH=str(qt/'bin')+os.pathsep+os.environ.get('PATH', ''))
    return values


def digest(path):
    h = sha256()
    with path.open('rb') as f:
        while block := f.read(128 * 1024):
            h.update(block)
    return h.hexdigest()


def range_raster_scale(value):
    if type(value) not in (int, float) or value not in RANGE_RASTER_SCALES:
        raise ValueError('局部谱面清晰度无效。')
    return float(value)


@lru_cache(maxsize=4)
def _worker_content_identity(exe, stat_key):
    return digest(exe)


def range_renderer_identity():
    exe, qt = runtime()
    st = exe.stat()
    stamp = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    return RENDERER, exe, qt, _worker_content_identity(exe, stamp)


def selected_midi(project, state):
    """Get the exact immutable import, even if a project moved or lost its pointer."""
    project = Path(project).resolve(strict=True)
    manifest = json.loads((project/'midi_import.json').read_bytes())
    proof = manifest.get('selected_sha256', '')
    if not isinstance(proof, str) or not re.fullmatch(r'[a-f0-9]{64}', proof):
        raise ValueError('找不到所选钢琴 MIDI 的校验信息。')
    for candidate in (state.get('selected_midi'), project/'selected_source.mid'):
        if not candidate:
            continue
        p = Path(candidate)
        if (not p.is_symlink() and p.is_file() and p.resolve().parent == project
                and p.suffix.lower() in ('.mid', '.midi') and 0 < p.stat().st_size <= MAX_SOURCE
                and digest(p) == proof):
            return p.resolve()
    raise ValueError('未找到可校验的所选钢琴 MIDI，原 MIDI 未修改。')


def _local_source(source):
    from .project_access import directory_grant
    candidate = Path(source)
    permit=directory_grant(candidate)
    resolved=permit.file(candidate) if permit is not None else candidate.resolve(strict=True)
    if (candidate.is_symlink() or (permit is None and not resolved.is_relative_to(data_root())) or not resolved.is_file()
            or resolved.suffix.lower() not in ('.musicxml', '.mid', '.midi', '.mscz')
            or not 0 < resolved.stat().st_size <= MAX_SOURCE):
        raise ValueError('本次谱面来源不属于隔离候选，或文件大小超出限制。')
    return resolved


def _one_shot(source, output, operation, *, page=None, raster_scale=None):
    check_cancelled()
    exe, qt = runtime()
    env = environment(qt)
    env['ASTRA_NATIVE_SCORE_OPERATION'] = operation
    if page is not None:
        env['ASTRA_NATIVE_SCORE_PAGE'] = str(page)
    if raster_scale is not None:
        if operation != 'preview':
            raise ValueError('仅屏幕预览可设置清晰度。')
        env['ASTRA_NATIVE_SCORE_RASTER_SCALE'] = format(range_raster_scale(raster_scale), 'g')
    args = [str(exe), '-o', str(output), str(source)]
    result = run_process(args, cwd=exe.parent, env=env, timeout=45, capture_output=True,
                         stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    payload = [line[len(FRAME_PREFIX):] for line in result.stdout.splitlines() if line.startswith(FRAME_PREFIX)]
    if len(payload) != 1 or len(payload[0]) > MAX_FRAME:
        raise RuntimeError(f'原生制谱未返回有效结果（退出码 {result.returncode}）。')
    response = json.loads(payload[0])
    if result.returncode or response.get('error'):
        raise RuntimeError(response.get('error') or '原生制谱未完成。')
    if (response.get('renderer') != RENDERER or type(response.get('pages')) is not int
            or not 0 < response['pages'] <= 2000 or response.get('source_sha256') != digest(source)):
        raise RuntimeError('原生谱面结果与本次来源不一致。')
    check_cancelled()
    return response


def prepare_native_score(generated, target):
    # Render and save the future right-hand document away from formal pointers.
    from zipfile import ZipFile
    from .project_access import directory_grant
    target = Path(target)
    parent = target.parent.resolve(strict=True)
    permit = directory_grant(target)
    if permit is not None:
        permit.file(target, missing=True)
    if (target.suffix != '.mscz' or target.is_symlink() or target.exists()
            or target.parent != parent
            or (permit is None and not parent.is_relative_to(data_root()))):
        raise ValueError('无效的更新谱面保存位置。')
    with TemporaryDirectory(prefix='notes-score-update-',dir=data_root()) as folder:
        source = Path(folder)/'source.musicxml'
        source.write_bytes(generated)
        exe, qt = runtime()
        env = environment(qt)
        args = [str(exe), '-o', str(target), str(_local_source(source))]
        commands = b'{"kind":"save","request":1,"baseRevision":1}\n{"kind":"quit"}\n'
        # A dedicated short-lived worker; no access to the current authored score.
        check_cancelled()
        result = subprocess.run(args, cwd=exe.parent, env=env, timeout=45, capture_output=True,
                                input=commands, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        frames = [ln[len(FRAME_PREFIX):] for ln in result.stdout.splitlines() if ln.startswith(FRAME_PREFIX)]
        if result.returncode or len(frames) != 2 or max(map(len, frames)) > MAX_FRAME:
            raise RuntimeError('原生谱面未成功渲染并保存。')
        initial, saved = map(json.loads, frames)
        if (initial.get('request') != 0 or initial.get('error') or initial.get('revision') != 1
                or not initial.get('frame') or not 0 < initial.get('pages',0) <= 2000
                or saved.get('request') != 1 or saved.get('error') or saved.get('dirty')
                or saved.get('revision') != 1 or saved.get('saved') != 1):
            raise RuntimeError(str(initial.get('error') or saved.get('error') or '原生谱面渲染或保存失败。'))
        if (target.is_symlink() or not target.is_file() or target.stat().st_nlink != 1
                or not 0 < target.stat().st_size <= MAX_SOURCE):
            raise RuntimeError('原生谱面文件无效。')
        with ZipFile(target) as archive:
            if not any(k.endswith('.mscx') for k in archive.namelist()) or archive.testzip() is not None:
                raise RuntimeError('原生谱面文件不完整。')
        with target.open('rb+') as stream:
            os.fsync(stream.fileno())
        check_cancelled()


def render_range_page(musicxml, source_sha256, index=0, *, raster_scale=1):
    if type(index) is not int or not 0 <= index < 2000:
        raise ValueError('局部谱页无效。')
    scale = range_raster_scale(raster_scale)
    source = _local_source(musicxml)
    # A dummy name in a unique, derived directory is never created by preview.
    with TemporaryDirectory(prefix='native-range-') as folder:
        destination = Path(folder)/'read-only.mscz'
        result = _one_shot(source, destination, 'preview', page=index, raster_scale=scale)
        if destination.exists():
            raise RuntimeError('只读预览意外生成了可编辑文件。')
    frame = result.get('frame', '')
    width, height = result.get('width'), result.get('height')
    # A previous installed worker is compatible with explicit 1x only.
    reported_scale = result.get('rasterScale', 1)
    logical_height = result.get('logicalHeight', height)
    frame_cap = 1_000_000 if scale == 1 else 2_000_000
    if (result.get('source_sha256') != source_sha256 or type(result.get('page')) is not int
            or result['page'] != index or index >= result['pages'] or type(reported_scale) not in (int,float)
            or reported_scale != scale or type(width) is not int or width != int(960*scale)
            or type(logical_height) is not int or not 480 <= logical_height <= 1800
            or type(height) is not int or height != int(logical_height*scale+.5)
            or not isinstance(frame, str) or not 32 <= len(frame) < frame_cap or len(frame)%4 != 0
            or not re.fullmatch(r'[A-Za-z0-9+/]+={0,2}', frame)):
        raise RuntimeError('局部预览内容与所选来源、页码或清晰度不符。')
    header = b64decode(frame[:32])
    if header[:16] != b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' or unpack('>II', header[16:24]) != (width, height):
        raise RuntimeError('局部预览的图片尺寸不符。')
    return {'dataUrl':'data:image/png;base64,'+frame, 'index':index, 'pages':result['pages'],
            'width':width, 'height':height, 'logicalHeight':logical_height, 'rasterScale':scale}


def render_pdf(musicxml, target):
    source = _local_source(musicxml)
    target = Path(target)
    from .project_access import directory_grant
    parent = target.parent.resolve(strict=True)
    grant=directory_grant(target)
    src_grant=directory_grant(source)
    if grant is not None:
        grant.file(target,missing=True)
    if ((grant is None and not parent.is_relative_to(data_root())) or src_grant!=grant
            or target.suffix != '.pdf' or target.is_symlink() or target.resolve().parent != parent):
        raise ValueError('PDF 仅能写入隔离候选指定的输出目录。')
    with TemporaryDirectory(prefix='native-pdf-', dir=parent) as folder:
        pending = Path(folder)/'out.pdf'
        row = _one_shot(source, pending, 'print')
        with pending.open('rb') as f:
            if f.read(5) != b'%PDF-':
                raise RuntimeError('原生制谱未生成有效 PDF。')
        check_cancelled()
        pending.replace(target)
    return {k:row[k] for k in ('renderer', 'source_sha256', 'pages')}
