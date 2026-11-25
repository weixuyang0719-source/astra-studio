"""Render immutable MusicXML to an A4 vector PDF using the bundled OSMD."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

RENDERER = 'osmd-2.3.1-a4-v2'
SELECTION_RENDERER = 'osmd-2.3.1-a4-selection-v1'
_SCORE_RENDERERS = {'score': RENDERER, 'selection': SELECTION_RENDERER}
MAX_XML = 16 * 1024 * 1024


def free_score_timing(source):
    """Build a read-only, exact check of source timing for free-meter excerpts."""
    if b'<senza-misura' not in source:
        return ''
    from xml.etree import ElementTree as ET
    from astra.score_tempo import _measure_layout

    try:
        root = ET.fromstring(source.decode('utf-8-sig'))
    except ET.ParseError as exc:
        raise ValueError('无法读取谱面 MusicXML。本次未出图。') from exc
    parts = root.findall('part')
    free = sorted({index for part in parts for index, measure in enumerate(part.findall('measure'))
                   if measure.find('attributes/time/senza-misura') is not None})
    labels = [part_measure.get('number', str(i+1)) for i,part_measure in enumerate(parts[0].findall('measure'))] if parts else []
    names = '、'.join(labels[i] if i < len(labels) else str(i+1) for i in free)
    try:
        measures = {part: [] for part in parts}
        for part, measure, start, span, _ in _measure_layout(root):
            # A mid-measure division change needs its own cursor map.
            directions = [node for node in measure if node.find('divisions') is not None]
            if directions and (len(directions) != 1 or any(
                    node.find('duration') is not None for node in list(measure)[:list(measure).index(directions[0])])):
                raise ValueError('mid-measure division change')
            measures[part].append((start, span))
        rows = measures[parts[0]]
        if any(value != rows for value in measures.values()):
            raise ValueError('different staff clocks')
    except (IndexError, KeyError, ArithmeticError, ValueError) as exc:
        raise ValueError(f'自由小节 {names} 的时值无法核对。MusicXML 已保留，本次未出图。') from exc
    return json.dumps({'names': names, 'rows': [[str(start.numerator), str(start.denominator),
                       str(span.numerator), str(span.denominator)] for start, span in rows]})


def renderer_python():
    configured = os.environ.get('ASTRA_OSMD_PYTHON')
    if configured:
        return Path(configured).resolve(strict=True)
    environment = os.environ.get('ASTRA_RUNTIME_ENV_ROOT')
    if environment:
        executable = Path(environment)/'.venv'/('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        if executable.is_file():
            return executable
    current = Path(sys.executable)
    return current.with_name('python.exe') if current.name.lower() == 'pythonw.exe' else current


def render_score_pdf(musicxml, destination, *, layout_preset="score"):
    """Publish only a complete, revision-bound PDF."""
    from .preview_cancel import check_cancelled, run_process

    renderer = _SCORE_RENDERERS[layout_preset]
    check_cancelled()
    musicxml = Path(musicxml).resolve(strict=True)
    pdf = Path(destination).resolve()
    with TemporaryDirectory(prefix='local-score-', dir=pdf.parent) as folder:
        pending = Path(folder)/'score.pdf'
        command = [str(renderer_python()), '-B', '-I', str(Path(__file__).resolve()),
                   str(musicxml), str(pending)]
        if layout_preset != 'score':
            command.append(layout_preset)
        kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        result = run_process(command, timeout=75, capture_output=True, text=True,
                             encoding='utf8', errors='replace', **kwargs)
        try:
            report = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise RuntimeError(f'本地谱面进程未返回结果（退出码 {result.returncode}）。MusicXML 已保留。') from exc
        if result.returncode or not report.get('ok'):
            raise RuntimeError(report.get('error') or '本地谱面排版未完成。MusicXML 已保留。')
        if report.get('renderer') != renderer:
            raise RuntimeError('本次排版的配置未能核对，请重试。')
        with musicxml.open('rb') as stream:
            data = stream.read(MAX_XML + 1)
        if len(data) > MAX_XML or hashlib.sha256(data).hexdigest() != report.get('source_sha256'):
            raise RuntimeError('排版期间 MusicXML 已变化，请重新导出。')
        with pending.open('rb') as stream:
            if stream.read(5) != b'%PDF-':
                raise RuntimeError('本地排版未生成可读 PDF。MusicXML 已保留。')
        check_cancelled()
        pending.replace(pdf)
        return {key: value for key, value in report.items() if key != 'ok'}


def export_pdf_result(musicxml, target, warnings, *, layout_preset="score"):
    from . import native_score_runtime as native
    render = native.render_pdf if native.enabled() else None
    try:
        proof = render(musicxml, target) if render else render_score_pdf(musicxml, target, layout_preset=layout_preset)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        message = str(exc)
        warnings.append('本地 PDF 排版失败。' + message)
        return {'pdf': None, 'pdf_render': {'status': 'failed',
                'renderer': native.RENDERER if render else _SCORE_RENDERERS[layout_preset], 'error': message}}
    return {'pdf': str(target), 'pdf_render': {'status': 'ready', **proof}}


def _use_offscreen_rendering():
    for name in tuple(os.environ):
        if name.upper().startswith(('QT_', 'QTWEBENGINE_', 'QML_','QML2_')):
            os.environ.pop(name)
    os.environ.update(QT_QPA_PLATFORM='offscreen', QTWEBENGINE_DISABLE_SANDBOX='1',
        QTWEBENGINE_CHROMIUM_FLAGS='--disable-gpu --disable-gpu-compositing --disable-background-networking '
                                  '--disable-component-update --no-first-run')


def _print_in_worker(musicxml, target, layout_preset="score"):
    _use_offscreen_rendering()
    from PySide6.QtCore import QMarginsF, QTimer, QUrl
    from PySide6.QtGui import QPageLayout, QPageSize
    from PySide6.QtPdf import QPdfDocument
    from PySide6.QtWebChannel import QWebChannel
    from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEngineSettings
    from PySide6.QtWidgets import QApplication
    from astra.studio_live_score import ENTRY, _LocalScorePage, _LocalScoreResources, _ScoreChannel

    with musicxml.open('rb') as stream:
        source = stream.read(MAX_XML + 1)
    if not source or len(source) > MAX_XML:
        raise ValueError('MusicXML 为空或超过本地排版上限。')
    xml = source.decode('utf-8-sig')
    timing = free_score_timing(source)
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    profile = QWebEngineProfile(app)
    profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.NoCache)
    resources = _LocalScoreResources(profile)
    profile.setUrlRequestInterceptor(resources)
    page = _LocalScorePage(profile, app)
    page.setAudioMuted(True)
    page.setVisible(True)
    settings = page.settings()
    settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
    settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)
    channel = QWebChannel(page)
    score = _ScoreChannel(channel)
    channel.registerObject('scoreSurface', score)
    page.setWebChannel(channel)
    reply = {}
    proof = {}
    token = 'print'

    def finish(error=None):
        if reply:
            return
        if error:
            reply.update(ok=False, error=str(error))
        else:
            reply.update(ok=True, renderer=_SCORE_RENDERERS[layout_preset], source_sha256=hashlib.sha256(source).hexdigest(), **proof)
        QTimer.singleShot(0, app.quit)

    def initialized(ok):
        if ok:
            request = score.prepareSelection if layout_preset == 'selection' else score.prepare
            request.emit(token, xml, timing)
        else:
            finish('本地谱面资源加载失败。')

    def composed(revision, ok, detail):
        if revision != token:
            return
        if not ok:
            finish(detail)
            return
        try:
            proof.update(json.loads(detail))
        except (ValueError, TypeError):
            finish('本地排版未能确认全部小节。')
            return
        if (layout_preset == 'selection') != ('selection_engraving' in proof):
            finish('本次排版的配置未能核对，请重试。')
            return
        score.commit.emit(token)

    def publish(revision):
        if revision == token:
            page_size = QPageSize(QPageSize.PageSizeId.A4)
            layout = QPageLayout(page_size, QPageLayout.Orientation.Portrait, QMarginsF())
            page.printToPdf(str(target), layout)

    def printed(path, ok):
        if not ok or Path(path).resolve() != target:
            finish('写入本地 PDF 失败。')
            return
        pdf = QPdfDocument()
        try:
            pdf.load(str(target))
            count = pdf.pageCount()
            points = [pdf.pagePointSize(i) for i in range(count)]
            if (count != proof.get('pages') or count <= 0 or
                    any(abs(size.width()-595.28) > 2 or abs(size.height()-841.89) > 2 for size in points)):
                finish('PDF 的页面数量或纸张尺寸异常，已停止发布。')
                return
            finish()
        finally:
            pdf.close()

    score.loaded.connect(initialized)
    score.composition.connect(composed)
    score.publication.connect(publish)
    page.loadFinished.connect(lambda ok: finish('本地谱面页面加载失败。') if not ok else None)
    page.renderProcessTerminated.connect(lambda *_: finish('本地谱面渲染进程中断。'))
    page.pdfPrintingFinished.connect(printed)
    deadline = QTimer(page)
    deadline.setSingleShot(True)
    deadline.timeout.connect(lambda: finish('本地谱面排版超时。MusicXML 已保留。'))
    deadline.start(60_000)
    page.load(QUrl.fromLocalFile(str(ENTRY)))
    app.exec()
    deadline.stop()
    page.setWebChannel(None)
    return reply or {'ok': False, 'error': '本地谱面进程提前结束。'}


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        arguments = sys.argv[1:]
        if len(arguments) not in (2, 3):
            raise ValueError('Expected MusicXML, PDF, and optional layout preset')
        musicxml, target = (Path(s).resolve() for s in arguments[:2])
        layout_preset = arguments[2] if len(arguments) == 3 else 'score'
        if layout_preset not in _SCORE_RENDERERS:
            raise ValueError('Unknown score layout preset')
        result = _print_in_worker(musicxml, target, layout_preset)
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        result = {'ok': False, 'error': str(exc)}
    print(json.dumps(result), flush=True)
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

