"""Native range preview with an isolated local OSMD WebEngine surface."""
import json
from pathlib import Path

from PySide6.QtCore import QObject, QIODevice, QSaveFile, QSettings, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (
    QWebEnginePage, QWebEngineProfile, QWebEngineSettings, QWebEngineUrlRequestInterceptor,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QFileDialog, QStackedWidget

from .release_paths import data_root
from .studio_score_window import ScoreWindow


WEB = Path(__file__).resolve().parent/'audio_output'/'studio_web'/'live_score'
ENTRY = WEB/'index.html'
ASSETS = {ENTRY, WEB/'sheet.js', WEB/'sheet.css', WEB/'vendor'/'opensheetmusicdisplay.min.js'}


class _LocalScoreResources(QWebEngineUrlRequestInterceptor):
    def interceptRequest(self, info):
        url = info.requestUrl()
        if url.isLocalFile():
            allowed = Path(url.toLocalFile()).resolve() in ASSETS
        else:
            allowed = (url.scheme() == 'qrc' and not url.host() and url.path() == '/qtwebchannel/qwebchannel.js') or url.scheme() == 'data'
        info.block(not allowed)


class _LocalScorePage(QWebEnginePage):
    def acceptNavigationRequest(self, url, _navigation_type, is_main_frame):
        return is_main_frame and url.isLocalFile() and Path(url.toLocalFile()).resolve() == ENTRY

    def createWindow(self, _kind):
        return None


class _ScoreChannel(QObject):
    prepare = Signal(str, str, str)
    prepareSelection = Signal(str, str, str)
    previewZoom = Signal(float)
    commit = Signal(str)
    invalidate = Signal()
    clear = Signal()
    loaded = Signal(bool)
    composition = Signal(str, bool, str)
    publication = Signal(str)

    @Slot(bool)
    def initialized(self, ok):
        self.loaded.emit(ok)

    @Slot(str, bool, str)
    def composed(self, token, ok, message):
        self.composition.emit(token, ok, message)

    @Slot(str)
    def published(self, token):
        self.publication.emit(token)


class _ScoreSurface(QWebEngineView):
    def __init__(self, profile, parent):
        super().__init__(parent)
        self.connection = _ScoreChannel(self)
        page = _LocalScorePage(profile, self)
        page.setBackgroundColor(QColor('#f5f6fc'))
        page.setAudioMuted(True)
        settings = page.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, False)
        self.channel = QWebChannel(page)
        self.channel.registerObject('scoreSurface', self.connection)
        page.setWebChannel(self.channel)
        self.setPage(page)
        self.show_started = False

    def start(self):
        if not self.show_started:
            self.show_started = True
            self.load(QUrl.fromLocalFile(str(ENTRY)))


class LiveScoreWindow(ScoreWindow):
    """Keep the last successful same-project page while a new revision is built."""

    def __init__(self, bridge):
        settings = QSettings(str(data_root()/'.cache'/'live-score-window.ini'), QSettings.Format.IniFormat)
        super().__init__(bridge, settings=settings)
        self.setWindowTitle('看谱 · Astra Studio 生谱候选')
        self.canvas_stack = QStackedWidget(self)
        self.layout().replaceWidget(self.scroll, self.canvas_stack)
        self.canvas_stack.addWidget(self.scroll)
        self.profile = QWebEngineProfile(self)
        self.profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.NoCache)
        self.resources = _LocalScoreResources(self.profile)
        self.profile.setUrlRequestInterceptor(self.resources)
        self.surface = _ScoreSurface(self.profile, self.canvas_stack)
        self.canvas_stack.addWidget(self.surface)
        self.surface.connection.loaded.connect(self._loaded)
        self.surface.connection.composition.connect(self._composed)
        self.surface.connection.publication.connect(self._published)
        self.surface.loadFinished.connect(self._load_done)
        self.surface.page().renderProcessTerminated.connect(self._renderer_stopped)
        self._engine_ready = False
        self._engine_failed = False
        self._waiting_save = False
        self._result_connected = False
        self._layout = None
        self._visible = None
        self._visible_token = None
        self._pdf_mode = False
        self._unprinted_source = None
        self.render_deadline = QTimer(self)
        self.render_deadline.setSingleShot(True)
        self.render_deadline.setInterval(12_000)
        self.render_deadline.timeout.connect(
            lambda: self.invalidate('SVG 排版未完成，请刷新重试。'))
        self.mode_button = self.button(self.preview_actions, 'PDF 预览', self._switch_mode)
        self.save_pdf_button = self.button(self.preview_actions, '另存选区 PDF', self._save_pdf)
        self.save_pdf_button.hide()
        self.save_source_button = self.button(self.preview_actions, '另存选区 MusicXML', self._save_failed_source)
        self.save_source_button.hide()
        self.open_button.hide()
        self.prev.hide()
        self.next.hide()

    def _switch_mode(self):
        self._pdf_mode = not self._pdf_mode
        self.mode_button.setText('SVG 预览' if self._pdf_mode else 'PDF 预览')
        self.refresh()

    def _label_visible(self, *, previous=False):
        visible = self._visible
        if visible is None:
            self.scope.clear()
            return
        where = visible['requested_range']
        label = '上次可见谱' if previous else '当前谱面'
        revision = visible.get('validated_revision', visible['revision'])
        self.scope.setText(f"{label} · 修订 {revision[:8]} · L/R {where['start']:.2f}–{where['end']:.2f} s")

    def update_context(self, context):
        identity = {key:context[key] for key in ('project','base_revision','start','end')}
        if identity == self.context:
            return
        if self.context and identity['project'] != self.context['project']:
            self.close()
            self.surface.connection.clear.emit()
            self.pixmap = type(self.pixmap)()
            self.image.clear()
            self._visible = self.result = None
            self._visible_token = None
            self.canvas_stack.setCurrentWidget(self.scroll)
            self.scope.clear()
        self.context = identity
        if self.isVisible():
            self.invalidate('选区或修订已改变。')
            self.update_timer.start()

    def _cancel_layout(self):
        self.render_deadline.stop()
        self._layout = None
        self.surface.connection.invalidate.emit()

    def invalidate(self, text):
        self._unprinted_source = None
        self.save_source_button.hide()
        self._cancel_layout()
        self._waiting_save = False
        # Keep both native PDF and SVG surfaces untouched; just mark their scope.
        super().invalidate(text)
        self._label_visible(previous=True)

    def _has_saved_revision(self):
        return (self.bridge._score_range_saved_revision(self.context['project']) == self.context['base_revision'])

    def refresh(self):
        self.update_timer.stop()
        if not self.context or not self.isVisible():
            return
        self.cancel_request()
        self._cancel_layout()
        self.token = None
        self._waiting_save = False
        self._unprinted_source = None
        self.save_source_button.hide()
        self.request = dict(self.context)
        self.result = None
        self.disable_actions()
        self._label_visible(previous=True)
        try:
            if self.context['start'] is None or self.context['end'] is None or self.context['end'] <= self.context['start']:
                self.invalidate('请选择左端早于右端的 L/R 范围。')
                return
            if not self._has_saved_revision():
                self._waiting_save = True
                self.set_status('请先保存编辑。保存后将刷新当前 L/R 预览。')
                self.timer.start()
                return
            if not self._pdf_mode and self._engine_failed:
                self._engine_failed = False
                self._engine_ready = False
                self.surface.show_started = False
            self.set_status('正在更新选区 PDF…' if self._pdf_mode else '正在准备选区 MusicXML…', True)
            response = self.bridge._score_range_preview_start(self.context, render_pdf=self._pdf_mode)
            self.token = response['token']
            if not self._result_connected:
                self.bridge._score_range_signal.done.connect(self._range_ready)
                self._result_connected = True
            self.timer.start()
        except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
            self.invalidate('预览更新失败 · '+str(exc))

    @Slot(str, object, str)
    def _range_ready(self, token, _result, _error):
        if token == self.token:
            self.poll()

    def _is_current(self, result):
        return (self.request == self.context
                and result.get('validated_revision', result.get('revision')) == self.context['base_revision']
                and result.get('requested_range') == {key:self.context[key] for key in ('start','end')})

    def poll(self):
        if not self.isVisible():
            return
        project = getattr(self.bridge, '_project', None)
        if not project or not self.context or Path(project['path']).resolve() != Path(self.context['project']).resolve():
            self.close()
            return
        try:
            if self._waiting_save:
                if self._has_saved_revision():
                    self.refresh()
                return
            if not self.token or self._visible_token == self.token:
                return
            response = self.bridge._score_range_status(self.token)
            if response['status'] in ('stale','cancelled','failed'):
                if response['status'] in ('stale','cancelled'):
                    self.invalidate('预览范围或谱面已改变。')
                    self.update_timer.start()
                else:
                    self._preview_failed('预览更新失败 · '+(response.get('error') or '请重试。'),
                                         response.get('result'))
                return
            if response['status'] != 'ready':
                return
            result = response['result']
            if not self._is_current(result):
                self.invalidate('预览版本或范围不匹配，请重新刷新。')
                return
            if result.get('render_error'):
                self._preview_failed(result['render_error'],result)
                return
            if result.get('renderer') == 'osmd':
                if self._layout is None:
                    self._layout = (self.token, result)
                    self.set_status('OSMD 正在排版选区…', True)
                    self.render_deadline.start()
                    self.surface.resize(self.canvas_stack.size())
                    self.surface.start()
                    if self._engine_ready:
                        self._send_layout()
            else:
                if not self.present_pdf(result, require_image=True):
                    self.invalidate('PDF 页面读取失败。可刷新或切回 SVG 预览。')
                    return
                self._visible = result
                self._visible_token = self.token
                self.canvas_stack.setCurrentWidget(self.scroll)
                # Its hidden viewport can still be small until the stack shows it.
                self.render_image()
                self.prev.show()
                self.next.show()
                self.open_button.show()
                self.save_pdf_button.setVisible(True)
                self.save_pdf_button.setEnabled(True)
                self._label_visible()
        except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
            self.invalidate('预览更新失败 · '+str(exc))

    def _loaded(self, ok):
        self._engine_ready = ok
        self._engine_failed = not ok
        if not ok and self._layout is not None:
            self.invalidate('OSMD 资源加载失败。可刷新重试。')
        elif ok and self._layout is not None:
            self._send_layout()

    def _load_done(self, ok):
        if not ok and self._layout is not None:
            self._loaded(False)

    def _renderer_stopped(self, *_):
        self._engine_failed = True
        self._engine_ready = False
        if self.canvas_stack.currentWidget() is self.surface:
            self._visible = self.result = None
            self._visible_token = None
            self.canvas_stack.setCurrentWidget(self.scroll)
        self.invalidate('谱面视图已中断，请刷新重试。')

    def _send_layout(self):
        token, result = self._layout
        self.surface.connection.prepareSelection.emit(token, result['musicxml_text'], result.get('free_timing', ''))

    def _valid_layout(self, token):
        if self._layout is None or self._layout[0] != token or self.token != token:
            return False
        try:
            current = self.bridge._score_range_status(token)
            return current['status'] == 'ready' and self._is_current(current['result']) and self.isVisible()
        except (ValueError, KeyError, TypeError, OSError, RuntimeError):
            return False

    def _composed(self, token, ok, message):
        if not self._valid_layout(token):
            return
        if not ok:
            self._preview_failed('SVG 排版失败 · '+message,self._layout[1])
            return
        try:
            pages = json.loads(message)['pages']
            if type(pages) is not int or pages <= 0:
                raise ValueError('Invalid page count')
        except (KeyError, TypeError, ValueError):
            self._preview_failed('未能确认 A4 页数。',self._layout[1])
            return
        self._layout = (token, {**self._layout[1], 'page_count': pages})
        self.surface.connection.commit.emit(token)

    def _published(self, token):
        if not self._valid_layout(token):
            return
        _, result = self._layout
        self._cancel_layout()
        self.result = self._visible = {k:v for k,v in result.items() if k != 'musicxml_text'}
        self._visible_token = token
        self.timer.stop()
        self.canvas_stack.setCurrentWidget(self.surface)
        self.prev.hide()
        self.next.hide()
        self.open_button.hide()
        self.save_pdf_button.hide()
        self.save_source_button.hide()
        self.pages.setText(f"{result['page_count']} 页")
        self.surface.connection.previewZoom.emit(self.zoom)
        self._label_visible()
        self.set_status('SVG 局部草稿 · 可继续编辑' if result['preview_kind']=='edited_range_draft' else 'SVG 选区预览')
        warnings = [str(warning) for warning in result.get('notation_warnings', []) if warning]
        self.notation_notice.setVisible(bool(warnings))
        self.notation_notice.setText(f'部分谱面标记需核对 · {len(warnings)} 项')
        self.notation_notice.setToolTip('\n'.join(warnings))

    def disable_actions(self):
        super().disable_actions()
        if hasattr(self, 'save_pdf_button'):
            self.save_pdf_button.setEnabled(False)

    def _preview_failed(self, message, result):
        source = None
        if result and self._is_current(result) and result.get('musicxml'):
            candidate = Path(result['musicxml']).resolve()
            if candidate.is_relative_to(self.bridge._score_range_cache(self.context['project'])) and candidate.is_file():
                source = (dict(self.context), candidate)
        self.invalidate(message)
        if source:
            self._unprinted_source = source
            self.save_source_button.show()

    def _save_pdf(self):
        result, token = self.result, self.token
        if not result or not result.get('pdf') or self._visible_token != token:
            return
        self._save_range_file(Path(result['pdf']), self.context['base_revision'],
                              lambda: self.token == token and self._is_current(result))

    def _save_failed_source(self):
        if self._unprinted_source:
            context, path = self._unprinted_source
            self._save_range_file(path, context['base_revision'], lambda: self.context == context)

    def _save_range_file(self, origin, revision, is_current):
        suffix = origin.suffix
        kind = 'PDF' if suffix == '.pdf' else 'MusicXML'
        name, _ = QFileDialog.getSaveFileName(self, f'另存选区 {kind}',
                                             f'Astra-LR-{revision[:8]}{suffix}', f'{kind} (*{suffix})')
        if not name:
            return
        if not is_current() or not self._has_saved_revision():
            self.set_status('保存时选区或修订已变化。请从当前预览重试。')
            return
        target = QSaveFile(name)
        try:
            content = origin.read_bytes()
            if not target.open(QIODevice.OpenModeFlag.WriteOnly) or target.write(content) != len(content) or not target.commit():
                raise OSError(target.errorString())
            self.set_status(f'已保存选区 {kind}')
        except OSError as exc:
            target.cancelWriting()
            self.set_status(f'保存选区 {kind} 失败 · '+str(exc))

    def scale(self, multiplier):
        if self.canvas_stack.currentWidget() is self.surface:
            self.zoom = 1. if multiplier == 0 else max(.5, min(3., self.zoom * multiplier))
            self.percent.setText(f'{self.zoom*100:.0f}%')
        else:
            super().scale(multiplier)
        self.surface.connection.previewZoom.emit(self.zoom)

    def closeEvent(self, event):
        self._cancel_layout()
        self._waiting_save = False
        super().closeEvent(event)

