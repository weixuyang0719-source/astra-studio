"""Local-only WebEngine presentation for the Astra desktop workbench."""
from .release_paths import data_root
from pathlib import Path
import json
import sys

from .studio_renderer import configure_renderer

configure_renderer()

from PySide6.QtCore import QEvent, QTimer, QUrl, Qt
from PySide6.QtGui import QColor, QIcon, QPalette, QFontDatabase, QFont
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings, QWebEngineUrlRequestInterceptor
from PySide6.QtWebEngineWidgets import QWebEngineView

from .studio_bridge import StudioBridge


ROOT = data_root()
WEB_ROOT = Path(__file__).with_name('studio_web').resolve()
ENTRY = WEB_ROOT / 'index.html'


def permitted_resource(url):
    if url.scheme() == 'file':
        path = Path(url.toLocalFile()).resolve()
        return path.is_relative_to(WEB_ROOT)
    return url.scheme() in ('data', 'about') or (
        url.scheme() == 'qrc' and url.path() == '/qtwebchannel/qwebchannel.js'
    )


class LocalResources(QWebEngineUrlRequestInterceptor):
    def interceptRequest(self, info):
        if not permitted_resource(info.requestUrl()):
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
            folder = ROOT / '.cache'
            folder.mkdir(exist_ok=True)
            with (folder / 'frontend-errors.log').open('a', encoding='utf-8') as handle:
                handle.write(f'{source}:{line}: {message}\n')


class StudioView(QWebEngineView):
    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self._wheel_zoom = None
        self._wheel_zoom_timer = QTimer(self)
        self._wheel_zoom_timer.setSingleShot(True)
        self._wheel_zoom_timer.timeout.connect(self._release_wheel_zoom)
        # Native scrollbars bypass DOM wheel cancellation. Restore only wheel-
        # initiated browser zoom synchronously, leaving explicit scale/DPI alone.
        self.page().zoomFactorChanged.connect(self._protect_wheel_zoom)
        for child in self.children():
            child.installEventFilter(self)
        self.setAcceptDrops(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

    def setPage(self, page):
        super().setPage(page)
        page.zoomFactorChanged.connect(self._protect_wheel_zoom)

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
            self.bridge.accept_input(urls[0].toLocalFile())
            # Native drops have no WebChannel command acknowledgement. Seed the
            # same snapshot that picker actions obtain, including pending imports.
            snapshot=json.dumps(self.bridge.snapshot(),ensure_ascii=True)
            self.page().runJavaScript(f"if(typeof acceptSnapshot==='function')acceptSnapshot({snapshot});")
        event.acceptProposedAction()
        return True

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
        self.setWindowTitle('Astra Studio')
        self.setWindowIcon(QIcon(str(Path(__file__).resolve().parent / 'assets' / 'astra.ico')))
        self.setMinimumSize(980, 640)
        available = self.screen().availableGeometry()
        self.resize(min(1440, available.width() - 32), min(940, available.height() - 48))
        self.setStyleSheet('QMainWindow { background: #fafbfe; }')
        self.close_pending = False
        self.bridge = StudioBridge(self)
        self.view = StudioView(self.bridge, self)
        self.setCentralWidget(self.view)
        self.profile = QWebEngineProfile(self)
        self.profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.NoCache)
        self.resources = LocalResources(self.profile)
        self.profile.setUrlRequestInterceptor(self.resources)
        self.page = LocalPage(self.profile, self.view)
        self.page.setBackgroundColor(QColor('#fafbfe'))
        self.view.setPage(self.page)
        settings = self.page.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, False)
        self.channel = QWebChannel(self.page)
        self.channel.registerObject('bridge', self.bridge)
        self.page.setWebChannel(self.channel)
        self.page.permissionRequested.connect(lambda request: request.deny())
        self.page.loadFinished.connect(self._loaded)
        self.bridge.jobs.completed.connect(self._after_job)
        self.bridge.jobs.failed.connect(self._after_job)
        self.bridge.jobs.cancelled.connect(self._after_job)
        self.bridge.ncm_tool.finished.connect(self._after_job)
        if not ENTRY.is_file():
            raise FileNotFoundError(f'缺少本地界面文件：{ENTRY}')
        self.view.load(QUrl.fromLocalFile(str(ENTRY)))

    def _loaded(self, okay):
        if not okay:
            QMessageBox.critical(self, '界面载入失败', '请重新运行安装器。详细信息见 .cache/frontend-errors.log。')

    def _after_job(self, *_args):
        if self.close_pending:
            QTimer.singleShot(0, self.close)

    def closeEvent(self, event):
        converting = self.bridge.ncm_tool.running
        project=getattr(self.bridge,'_project',None)
        structure=getattr(self.bridge,'_structure_session',None)
        pending=bool(project and structure and structure.pending(project['path']))
        if pending and not self.close_pending:
            question=QMessageBox(self)
            question.setWindowTitle('结构草稿尚未应用')
            question.setText('当前拍位结构草稿尚未保存。继续编辑，或丢弃草稿并退出？'
                + ('\n正在运行的任务也将取消；已完成的导出会保留。' if self.bridge.jobs.running or converting else ''))
            keep=question.addButton('继续编辑',QMessageBox.ButtonRole.RejectRole)
            discard=question.addButton('丢弃并退出',QMessageBox.ButtonRole.DestructiveRole)
            question.setDefaultButton(keep)
            question.setEscapeButton(keep)
            question.exec()
            if question.clickedButton()!=discard:
                event.ignore()
                return
            structure.clear()
            if self.bridge.jobs.running or converting:
                self.close_pending=True
                event.ignore()
                self.bridge.jobs.cancel()
                self.bridge.ncm_tool.cancel()
                return
        if self.bridge.jobs.running or converting:
            event.ignore()
            if not self.close_pending and QMessageBox.question(
                self, '任务正在运行',
                '关闭 Astra？正在处理的 NCM 文件完成后将停止转换，已输出的文件会保留。'
                if converting and not self.bridge.jobs.running else
                '取消运行中的任务并关闭 Astra？已完成的导出会保留。',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            ) == QMessageBox.StandardButton.Yes:
                self.close_pending = True
                self.bridge.jobs.cancel()
                self.bridge.ncm_tool.cancel()
            return
        if self.bridge.close() is False:
            event.ignore()
            return
        event.accept()


def main():
    if sys.platform == 'win32':
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Astra.LocalMusic.Studio')
    app = QApplication.instance() or QApplication(['Astra Studio'])
    app.setApplicationName('Astra Studio')
    app.setWindowIcon(QIcon(str(Path(__file__).resolve().parent / 'assets' / 'astra.ico')))
    font_id = QFontDatabase.addApplicationFont(str(WEB_ROOT / 'fonts' / 'NotoSansSC[wght].ttf'))
    families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
    if families:
        app.setFont(QFont(families[0], 10))
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
