"""Local-only WebEngine presentation for the Astra desktop workbench."""
from pathlib import Path
import json
import os
import sys

from astra.studio_renderer import configure_renderer

configure_renderer()

from PySide6.QtCore import QEvent, QTimer, QUrl, Qt
from PySide6.QtGui import QColor, QIcon, QPalette, QFontDatabase, QFont
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox, QVBoxLayout, QWidget
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings, QWebEngineUrlRequestInterceptor
from PySide6.QtWebEngineWidgets import QWebEngineView

from .unified_studio_bridge import UnifiedStudioBridge as StudioBridge
from .unified_window_close import OutputWindowClose
from .score_preview_resources import SCHEME, SCHEME_NAME, ScorePreviewResources, permits_image_request, register_scheme
from astra.save_dialogs import CloseJobDialog, NativeScoreUnsavedDialog

register_scheme()


ROOT = Path(__file__).resolve().parent
TRIAL_SESSION_ROOT = ROOT.parent / 'sessions' / 'diagnostics'
WEB_ROOT = Path(__file__).resolve().parent / 'studio_web'
ENTRY = WEB_ROOT / 'index.html'


def studio_profile(parent):
    """The page's own settings (score view toggles, toolbar, shortcuts) live in
    the release's data folder so they survive a restart. Development runs
    without a release data root keep the old in-memory profile."""
    root = os.environ.get('ASTRA_RELEASE_DATA_ROOT')
    if root:
        try:
            storage = Path(root).resolve(strict=True) / 'web-storage'
            storage.mkdir(exist_ok=True)
        except OSError:
            storage = None
        if storage is not None:
            profile = QWebEngineProfile('astra-studio', parent)
            profile.setPersistentStoragePath(str(storage))
            profile.setCachePath(str(storage / 'cache'))
            profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
            return profile
    return QWebEngineProfile(parent)


def permitted_resource(url):
    if url.scheme() == 'file':
        path = Path(url.toLocalFile()).resolve()
        return path.is_relative_to(WEB_ROOT)
    return url.scheme() in ('data', 'about') or (
        url.scheme() == 'qrc' and url.path() == '/qtwebchannel/qwebchannel.js'
    )


class LocalResources(QWebEngineUrlRequestInterceptor):
    def interceptRequest(self, info):
        if info.requestUrl().scheme() == SCHEME_NAME:
            info.block(not permits_image_request(info, QUrl.fromLocalFile(str(ENTRY))))
        elif not permitted_resource(info.requestUrl()):
            info.block(True)


class LocalPage(QWebEnginePage):
    def acceptNavigationRequest(self, url, navigation_type, is_main_frame):
        if is_main_frame:
            return url.isLocalFile() and Path(url.toLocalFile()).resolve() == ENTRY
        return permitted_resource(url)

    def createWindow(self, _kind):
        return None

    def javaScriptConsoleMessage(self, level, message, line, source):
        if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
            folder = TRIAL_SESSION_ROOT / 'logs'
            folder.mkdir(parents=True, exist_ok=True)
            with (folder / 'frontend-errors.log').open('a', encoding='utf-8') as handle:
                handle.write(f'{source}:{line}: {message}\n')


class StudioView(QWebEngineView):
    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.native_panel_overlay = None
        self._wheel_zoom = None
        self._wheel_zoom_timer = QTimer(self)
        self._wheel_zoom_timer.setSingleShot(True)
        self._wheel_zoom_timer.timeout.connect(self._release_wheel_zoom)
        # Native scrollbars bypass DOM wheel cancellation. Restore only wheel-
        # initiated browser zoom synchronously, leaving explicit scale/DPI alone.
        self.setAcceptDrops(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.native_panel_overlay is not None:
            self.native_panel_overlay.place_over(self.geometry())

    def setPage(self, page):
        super().setPage(page)
        page.zoomFactorChanged.connect(self._protect_wheel_zoom)
        # Register on the installed release page, not an implicit temporary one.
        for child in self.children():
            child.installEventFilter(self)

    def _protect_wheel_zoom(self, factor):
        if self._wheel_zoom is not None and abs(factor - self._wheel_zoom) > 1e-6:
            self.setZoomFactor(self._wheel_zoom)

    def _release_wheel_zoom(self):
        self._wheel_zoom = None

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Wheel:
            if event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier):
                if self._wheel_zoom is None:
                    self._wheel_zoom = self.zoomFactor()
                self._wheel_zoom_timer.start(400)
        return super().eventFilter(watched, event)

    def childEvent(self, event):
        super().childEvent(event)
        if event.added():
            event.child().installEventFilter(self)

    def _accept_drop(self, event):
        if not event.mimeData().hasUrls():
            return False
        urls = event.mimeData().urls()
        if len(urls) != 1 or not urls[0].isLocalFile():
            return False
        if event.type() == QEvent.Type.Drop:
            path = urls[0].toLocalFile()
            guard = getattr(self.bridge, '_leave_project_guard', None)
            if getattr(self.bridge, '_closed', False):
                return False
            if guard is None or guard(lambda: self._drop_input(path)):
                self._drop_input(path)
        event.acceptProposedAction()
        return True

    def _drop_input(self, path):
        self.bridge.accept_input(path)
        # Native drops have no WebChannel command acknowledgement. Seed the
        # same snapshot that picker actions obtain, including pending imports.
        snapshot=json.dumps(self.bridge.snapshot(),ensure_ascii=True)
        self.page().runJavaScript(f"if(typeof acceptSnapshot==='function')acceptSnapshot({snapshot});")

    def dragEnterEvent(self, event):
        if not self._accept_drop(event):
            event.ignore()

    def dragMoveEvent(self, event):
        if not self._accept_drop(event):
            event.ignore()

    def dropEvent(self, event):
        if not self._accept_drop(event):
            event.ignore()


class StudioWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        if os.environ.get('ASTRA_RELEASE_ISOLATED_PROJECTS') == '1':
            self._base_window_title = f"Astra Studio - 正式版 {os.environ.get('ASTRA_RELEASE_NUMBER') or '2'}"
        else:
            self._base_window_title = ('Astra Studio 生谱候选' if os.environ.get('ASTRA_LIVE_SCORE') == '1'
                                       else 'Astra Studio 整合候选')
        self.setWindowTitle(self._base_window_title)
        self.setWindowIcon(QIcon(str(ROOT / 'assets' / 'astra.ico')))
        self.setMinimumSize(980, 640)
        available = self.screen().availableGeometry()
        self.resize(min(1440, available.width() - 32), min(940, available.height() - 48))
        self.setStyleSheet('QMainWindow { background: #fafbfe; }')
        self.close_pending = False
        self._close_retry_timer=QTimer(self)
        self._close_retry_timer.setSingleShot(True)
        self._close_retry_timer.setInterval(0)
        self._close_retry_timer.timeout.connect(self.close)
        self.bridge = StudioBridge(self)
        self.view = StudioView(self.bridge, self)
        if os.environ.get('ASTRA_EXPERIMENTAL_NATIVE_TRACKS_PANEL') == '1':
            from .native_tracks_panel import NativeTracksPanel
            workbench=QWidget(self)
            layout=QVBoxLayout(workbench)
            layout.setContentsMargins(0,0,0,0)
            layout.setSpacing(0)
            layout.addWidget(self.view,1)
            self.native_tracks_panel=NativeTracksPanel(workbench,self.bridge,self.view)
            self.bridge._experimental_native_panel=self.native_tracks_panel
            self.view.native_panel_overlay=self.native_tracks_panel
            self.setCentralWidget(workbench)
            self.native_tracks_panel.place_over(self.view.geometry())
        else:
            self.setCentralWidget(self.view)
        self.profile = studio_profile(self)
        self.profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.NoCache)
        self.resources = LocalResources(self.profile)
        self.profile.setUrlRequestInterceptor(self.resources)
        self.bridge._score_preview_resources = True
        self.score_preview_resources = ScorePreviewResources(self.profile, lambda: self.bridge._current_score_preview)
        self.profile.installUrlSchemeHandler(SCHEME, self.score_preview_resources)
        self.page = LocalPage(self.profile, self.view)
        self.page.setBackgroundColor(QColor('#fafbfe'))
        self.view.setPage(self.page)
        settings = self.page.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, False)
        self.channel = QWebChannel(self.page)
        self.channel.registerObject('bridge', self.bridge)
        if os.environ.get('ASTRA_NATIVE_SCORE_WORKER_EXE'):
            from .inline_native_score import InlineNativeScoreBridge
            self.native_score = InlineNativeScoreBridge(self.bridge, self.channel)
            self.channel.registerObject('inlineNativeScore', self.native_score)
            self.native_score.saveStateChanged.connect(self._sync_window_title)
            QApplication.instance().aboutToQuit.connect(self.native_score.close)
            self.bridge._leave_project_guard = self._leave_project_guard
            self.bridge._leave_project_picker_state = self._leave_project_picker_state
            from .score_playback_float import ScorePlaybackFloatOwner
            self.score_playback_float = ScorePlaybackFloatOwner(self, self.view, WEB_ROOT)
            self.channel.registerObject('scorePlaybackFloat', self.score_playback_float)
            QApplication.instance().aboutToQuit.connect(self.score_playback_float.shutdown)
        if os.environ.get('ASTRA_NATIVE_WEB_TRANSPORT') == '1':
            from .native_web_transport import NativeWebTransport
            self.native_transport = NativeWebTransport(self.channel)
            self.channel.registerObject('nativeWebTransport', self.native_transport)
        if hasattr(self,'native_tracks_panel'):
            self.channel.registerObject('nativeTracksPanel',self.native_tracks_panel.web_page)
        self.page.setWebChannel(self.channel)
        self.page.permissionRequested.connect(lambda request: request.deny())
        self.page.loadFinished.connect(self._loaded)
        self.bridge.jobs.completed.connect(self._after_job)
        self.bridge.jobs.failed.connect(self._after_job)
        self.bridge.jobs.cancelled.connect(self._after_job)
        self.bridge.ncm_tool.finished.connect(self._after_job)
        self.bridge.editorSaveChanged.connect(self._sync_window_title)
        self.bridge.stateChanged.connect(self._sync_window_title)
        if not ENTRY.is_file():
            raise FileNotFoundError(f'缺少本地界面文件：{ENTRY}')
        self.view.load(QUrl.fromLocalFile(str(ENTRY)))

    def _sync_window_title(self, *_args):
        if getattr(self, '_output_closer', None) is not None:
            return
        project = getattr(self.bridge, '_project', None) or {}
        name = project.get('name')
        try:
            save = self.bridge._edit_save_status()
        except Exception:
            save = {}
        score = getattr(self, 'native_score', None)
        dirty = bool(save.get('save_dirty') or (
            project.get('path') and score is not None and score.unsaved_project() == project['path']))
        other_dirty = len(save.get('other_dirty_projects') or ())
        if name:
            prefix = '● ' if dirty else ''
            suffix = ' (未保存)' if dirty else (f' (+{other_dirty} 个未保存)' if other_dirty else '')
            title = f'{prefix}{name}{suffix} — {self._base_window_title}'
        elif other_dirty:
            title = f'● {other_dirty} 个未保存工程 — {self._base_window_title}'
        else:
            title = self._base_window_title
        if self.windowTitle() != title:
            self.setWindowTitle(title)

    def _native_score_gate(self, *, closing, proceed):
        """Return True to continue now; False when cancelled or waiting for a save."""
        score = getattr(self, 'native_score', None)
        if score is None:
            return True
        if score.close_guard_pending():
            QMessageBox.information(self, '右侧乐谱处理中', '右侧乐谱还有操作未确认，请稍后再试。')
            return False
        if not score.unsaved_edits():
            return True
        identity = score.close_guard_identity()
        project = getattr(self.bridge, '_project', None) or {}
        name = project.get('name') or Path(score.project or '').name or '当前工程'
        dialog = NativeScoreUnsavedDialog(self, project_name=name, closing=closing)
        dialog.exec()
        if dialog.choice in ('discard', 'save') and (
                score.close_guard_pending() or score.close_guard_identity() != identity):
            QMessageBox.information(self, '右侧乐谱已变化', '请查看右侧乐谱后再试。')
            return False
        if dialog.choice == 'discard':
            # This permits only the caller's current attempt. If the picker or
            # another confirmation is cancelled, the next attempt asks again.
            return True
        if dialog.choice == 'save':
            def failed(message):
                QMessageBox.warning(self, '右侧乐谱未保存',
                                    f'{message}\n\n窗口和当前工程保持不变，可在右侧乐谱中保存后重试。')
            score.save_then(lambda: QTimer.singleShot(0, proceed), failed)
        return False

    def _leave_project_guard(self, proceed):
        if (getattr(self, '_output_closer', None) is not None or getattr(self.bridge, '_closed', False)
                or getattr(self.bridge, '_editor_closing', False)):
            return False
        return self._native_score_gate(closing=False, proceed=proceed)

    def _leave_project_picker_state(self):
        score = getattr(self, 'native_score', None)
        return (getattr(self, '_output_closer', None) is None,
                id(getattr(self.bridge, '_project', None)),
                score.close_guard_identity() if score is not None else None,
                score.close_guard_pending() if score is not None else False)

    def _loaded(self, okay):
        if not okay:
            QMessageBox.critical(self, '界面载入失败', '请重新运行安装器。详细信息见 session/logs/frontend-errors.log。')

    def _after_job(self, *_args):
        if self.close_pending:
            self._close_retry_timer.start()

    def _output_close_failed(self, message):
        # The WebChannel is already detached; errors must remain native-visible.
        dialog=getattr(self,'_output_close_dialog',None)
        if dialog is None:
            dialog=QMessageBox(self);self._output_close_dialog=dialog
            dialog.setWindowTitle('暂时无法安全关闭')
            dialog.setIcon(QMessageBox.Icon.Warning)
            dialog.setStandardButtons(QMessageBox.StandardButton.Ok)
        dialog.setText('音频输出尚未确认释放，窗口会保留，不会强制退出。')
        dialog.setDetailedText(str(message));dialog.open()

    def closeEvent(self, event):
        closer=getattr(self,'_output_closer',None)
        panel=getattr(self,'native_tracks_panel',None)
        score_tool=getattr(self,'score_playback_float',None)
        if closer is not None:
            if closer.done:
                if panel:panel.shutdown()
                event.accept()
            else:
                event.ignore()
                if closer.error:self._output_close_failed(closer.error)
            return
        if not self._native_score_gate(closing=True, proceed=self.close):
            event.ignore()
            return
        converting = self.bridge.ncm_tool.running
        project = getattr(self.bridge, '_project', None)
        structure = getattr(self.bridge, '_structure_session', None)
        pending = bool(project and structure and structure.pending(project['path']))
        if self.bridge.jobs.running or converting:
            event.ignore()
            if not self.close_pending:
                dlg = CloseJobDialog(
                    self,
                    converting_only=bool(converting and not self.bridge.jobs.running),
                    has_structure_draft=pending,
                )
                dlg.exec()
                if dlg.confirmed:
                    if pending:
                        structure.clear()
                    self.close_pending = True
                    self.bridge.jobs.cancel()
                    self.bridge.ncm_tool.cancel()
            return
        event.ignore()
        self._output_closer = OutputWindowClose(self.bridge, self.channel, self.page, self)
        self._output_closer.failed.connect(self._output_close_failed)
        self._output_closer.finished.connect(self._close_retry_timer.start)
        if self._output_closer.begin() is False:
            self._output_closer.deleteLater()
            self._output_closer = None
            self._sync_window_title()
        else:
            if panel:
                panel.setTracks(False)
            if score_tool:
                score_tool.shutdown()
            if not self._output_closer.done:
                self.setWindowTitle(f'正在关闭音频输出… — {self._base_window_title}')


def main():
    if sys.platform == 'win32':
        import ctypes
        number = os.environ.get('ASTRA_RELEASE_NUMBER') or '2'
        app_id = (f'Astra.Studio.Formal{number}' if os.environ.get('ASTRA_RELEASE_ISOLATED_PROJECTS') == '1'
                  else 'Astra.Studio.IntegratedPreview')
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    app = QApplication.instance() or QApplication(['Astra Studio'])
    app.setApplicationName('Astra Studio')
    app.setWindowIcon(QIcon(str(ROOT / 'assets' / 'astra.ico')))
    if sys.platform == 'win32' and 'Microsoft YaHei UI' in QFontDatabase.families():
        ui_font = QFont('Microsoft YaHei UI', 10)
        ui_font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        ui_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias | QFont.StyleStrategy.PreferQuality)
        app.setFont(ui_font)
    else:
        font_id = QFontDatabase.addApplicationFont(str(WEB_ROOT / 'fonts' / 'NotoSansSC[wght].ttf'))
        families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
        if families:
            ui_font = QFont(families[0], 10)
            ui_font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
            app.setFont(ui_font)
    app.styleHints().setColorScheme(Qt.ColorScheme.Light)
    app.setStyle('Fusion')
    palette = app.palette()
    for role, color in {
        QPalette.ColorRole.Window: '#f5f6fc', QPalette.ColorRole.WindowText: '#41434e',
        QPalette.ColorRole.Base: '#ffffff', QPalette.ColorRole.Text: '#41434e',
        QPalette.ColorRole.Button: '#ffffff', QPalette.ColorRole.ButtonText: '#41434e',
        QPalette.ColorRole.Highlight: '#436bc4', QPalette.ColorRole.HighlightedText: '#ffffff',
    }.items():
        palette.setColor(role, QColor(color))
    app.setPalette(palette)
    window = StudioWindow()
    window.show()
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
