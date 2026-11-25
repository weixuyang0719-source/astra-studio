"""Score play-control float: a separate tool window, never a separate sound engine.

The trusted Studio page owns the current score/VST command closure. This window
sees only bounded, epoch-tagged presentation state, not project paths or files.
See SCORE-TRANSPORT-PROVENANCE.md for the pinned MuseScore reference behavior.
"""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QRect, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QCursor, QGuiApplication
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (
    QWebEnginePage, QWebEngineProfile, QWebEngineSettings, QWebEngineUrlRequestInterceptor,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QVBoxLayout, QWidget

_NAMES = frozenset((
    "score_playback_float.html", "score_playback_float.js", "score_playback_chrome.js",
    "score_playback_chrome.css", "musescore465/MusescoreIcon.ttf",
))
_MAIN = "score_playback_float.html"
_MAX_EPOCH = 2_147_483_647
_ACTIONS = frozenset((
    "playPause", "rewind", "seek", "seekBeat", "loop", "loopIn", "loopOut",
    "metronome", "repeat", "chordSymbols", "audition", "pan", "countIn",
    "midiInput", "writtenPitch", "soundingPitch", "speed",
))
_DATA = frozenset((
    "epoch", "revision", "duration", "position", "state", "start", "end", "measure",
    "maxMeasure", "beat", "maxBeat", "tempoBpm", "tempoUnit", "tempoDotted",
    "speed", "speedMin", "speedMax", "requestedSpeed", "requestedMetronome", "capabilities",
)) | _ACTIONS.intersection((
    "loop", "metronome", "repeat", "chordSymbols", "audition", "pan", "countIn",
    "midiInput", "writtenPitch", "soundingPitch",
))


def _number(value, low, high):
    return (type(value) in (float, int) and low <= value <= high)


def _positive_int(value):
    return type(value) is int and 0 < value <= _MAX_EPOCH


def _dict(text, limit):
    if not isinstance(text, str) or len(text) > limit:
        return None
    try:
        value = json.loads(text, parse_constant=lambda _v: None)
    except (TypeError, ValueError, RecursionError):
        return None
    return value if type(value) is dict else None


def model_packet(epoch, text):
    value = _dict(text, 2200)
    if not value or not _positive_int(epoch) or value.keys() - _DATA or value.get("epoch") != epoch:
        return None
    if value.get("revision") is not None and not _positive_int(value["revision"]):
        return None
    caps = value.get("capabilities")
    if not isinstance(caps, list) or len(caps) > len(_ACTIONS) or any(type(c) is not str or c not in _ACTIONS for c in caps):
        return None
    # Presentation values must be scalars; no unknown HTML, path, or nested field is shipped to the tool.
    for k, x in value.items():
        if k == "capabilities":
            continue
        if k == "state" and x not in ("playing", "paused", "pending", "counting"):
            return None
        if k == "tempoUnit" and x not in ("whole", "half", "quarter", "eighth", "sixteenth"):
            return None
        if k not in ("state", "tempoUnit") and x is not None and type(x) is not bool and not _number(x, 0, _MAX_EPOCH):
            return None
    return value


def permitted(url, paths):
    if url.scheme() == "qrc":
        return not (url.host() or url.hasQuery() or url.hasFragment()) and url.path() == "/qtwebchannel/qwebchannel.js"
    if url.scheme() != "file" or url.host() or url.hasQuery() or url.hasFragment():
        return False
    try:
        return Path(url.toLocalFile()).resolve() in paths
    except (OSError, RuntimeError, ValueError):
        return False


class _Resources(QWebEngineUrlRequestInterceptor):
    def __init__(self, profile, paths):
        super().__init__(profile)
        self.paths = paths

    def interceptRequest(self, info):
        info.block(not permitted(info.requestUrl(), self.paths))


class _Page(QWebEnginePage):
    def __init__(self, profile, view, entry, paths):
        super().__init__(profile, view)
        self.entry, self.paths = entry, paths

    def acceptNavigationRequest(self, url, _kind, main):
        return main and permitted(url, self.paths) and url == self.entry

    def createWindow(self, _kind):
        return None


class _FloatLink(QObject):
    # The floating WebChannel cannot reach the main Studio/score/backend objects.
    modelChanged = Signal(str)

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner

    @Slot()
    def ready(self):
        self.owner._publish()

    @Slot(str)
    def command(self, text):
        self.owner._relay(text)

    @Slot(int)
    def returnToDock(self, epoch):
        self.owner._return(epoch)

    @Slot(int, float, float)
    def startDrag(self, epoch, x, y):
        self.owner._start_drag(epoch, x, y)

    @Slot(int)
    def moveDrag(self, epoch):
        self.owner._move_drag(epoch)

    @Slot(int, bool)
    def endDrag(self, epoch, cancelled):
        self.owner._end_drag(epoch, cancelled)

    @Slot(int, bool)
    def showMenu(self, epoch, opened):
        self.owner._show_menu(epoch, opened)


class _Tool(QWidget):
    def __init__(self, owner, parent):
        # Stock Dock module uses these window flags on floating toolbar surfaces.
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.owner = owner
        self.setWindowTitle("播放控制")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

    def closeEvent(self, event):
        if self.owner.epoch:
            self.owner._return(self.owner.epoch)
            event.ignore()
            return
        event.accept()


class ScorePlaybackFloatOwner(QObject):
    userAction = Signal(str)
    dockChanged = Signal(int, bool)
    _WIDTH = 480
    _HEIGHT = 108
    _MENU_HEIGHT = 302

    def __init__(self, window, view, web_root):
        super().__init__(window)
        self.window, self.view = window, view
        root = web_root.resolve(strict=True)
        paths = {root / name for name in _NAMES}
        if any(not p.is_file() or p.resolve() != p for p in paths):
            raise ValueError("Score playback floating assets unavailable")
        self.entry = QUrl.fromLocalFile(str(root / _MAIN))
        self.paths = frozenset(paths)
        self.epoch = 0
        self._latest_epoch = self._retired_epoch = 0
        self._model = self._data = None
        self._tool = None
        self._link = None
        self._dock_rect = None
        self._drag_offset = self._drag_origin = None
        self._closed = False

    def _setup(self):
        if self._tool is not None:
            return
        tool = _Tool(self, self.window)
        view = QWebEngineView(tool)
        view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        view.setAcceptDrops(False)
        profile = QWebEngineProfile(tool)
        profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.NoCache)
        profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
        resources = _Resources(profile, self.paths)
        profile.setUrlRequestInterceptor(resources)
        page = _Page(profile, view, self.entry, self.paths)
        view.setPage(page)
        page.setBackgroundColor(QColor('#f8f8f8'))
        settings = page.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, False)
        page.permissionRequested.connect(lambda request: request.deny())
        channel = QWebChannel(page)
        self._link = _FloatLink(self)
        channel.registerObject("scoreFloat", self._link)
        page.setWebChannel(channel)
        page.loadFinished.connect(self._loaded)
        layout = QVBoxLayout(tool)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(view)
        tool.setFixedSize(self._WIDTH, self._HEIGHT)
        self._tool, self._view, self._profile, self._resources, self._page = tool, view, profile, resources, page
        page.load(self.entry)

    def _loaded(self, okay):
        if not okay:
            self._return(self.epoch)
        elif self.epoch:
            self._publish()

    def _active(self, epoch):
        return not self._closed and epoch > 0 and self.epoch == epoch

    def _local_scale(self, n):
        return round(n * self.view.zoomFactor())

    def _point(self, x, y):
        return self.view.mapToGlobal(QPoint(self._local_scale(x), self._local_scale(y)))

    def _position(self, x, y):
        _candidate = QPoint(x, y)
        handle = self.window.windowHandle()
        screen = handle.screen() if handle else self.window.screen()
        # QWidget coords here are Qt device independent points, even on mixed-DPI monitors.
        screen = QGuiApplication.screenAt(_candidate) or screen
        if screen:
            g = screen.availableGeometry()
            x = max(g.left(), min(x, g.right() - self._tool.width() + 1))
            y = max(g.top(), min(y, g.bottom() - self._tool.height() + 1))
        return QPoint(x, y)

    @Slot(int, str, str, result=bool)
    def open(self, epoch, model, placement):
        packet, geo = model_packet(epoch, model), _dict(placement, 420)
        if (self._closed or self.epoch or epoch < self._latest_epoch or epoch <= self._retired_epoch or packet is None or not geo
                or set(geo) != {"x", "y", "w", "h", "atX", "atY"}
                or not all(_number(v, -30000, 30000) for v in geo.values())
                or not 0 < geo["w"] <= 1000 or not 0 < geo["h"] <= 200):
            return False
        self._setup()
        self.epoch = self._latest_epoch = epoch
        self._data, self._model = packet, model
        point = self._point(geo["atX"], geo["atY"])
        self._tool.setFixedSize(self._WIDTH, self._HEIGHT)
        self._tool.move(self._position(point.x() - 12, point.y()))
        self._tool.show()
        self._publish()
        # Leave the incoming main WebChannel method before delivering the
        # independent location notification. The open result also acks it.
        QTimer.singleShot(0, self, lambda: self._announce(epoch, True))
        return True

    def _announce(self, epoch, visible):
        if not self._closed and self._latest_epoch == epoch and self._active(epoch) == visible:
            self.dockChanged.emit(epoch, visible)
            self._notify_main(dict(kind="location", epoch=epoch, visible=visible))

    def _notify_main(self, packet):
        # Explicitly target the trusted Studio view. A signal emitted during
        # the second view's transport dispatch is not a reliable host receipt.
        # No string is interpolated as executable source: only an escaped JSON
        # string passed to the one immutable receiver. No project/audio data.
        selector = getattr(self.view, "page", None)
        if callable(selector):
            text = json.dumps(packet, ensure_ascii=True, separators=(",", ":"))
            selector().runJavaScript("window.AstraScoreTransport?.acceptNativeNotice(" +
                                     json.dumps(text, ensure_ascii=True) + ");")

    @Slot(int, str)
    def update(self, epoch, text):
        if not self._active(epoch):
            return
        packet = model_packet(epoch, text)
        if packet is not None and text != self._model:
            self._data, self._model = packet, text
            self._publish()

    @Slot(int, str)
    def setDockTarget(self, epoch, packet):
        geo = _dict(packet, 256)
        if (not self._active(epoch) or not geo or set(geo) != {"x", "y", "w", "h"}
                or not all(_number(n, -30000, 30000) for n in geo.values())
                or not 0 < geo["w"] <= 1000 or not 0 < geo["h"] <= 300):
            return
        self._dock_rect = geo

    def _publish(self):
        if self._link is not None and self._model:
            self._link.modelChanged.emit(self._model)

    def _relay(self, text):
        command = _dict(text, 512)
        if not command or command.keys() - {"epoch", "revision", "action", "value"}:
            return
        epoch, rev, action = (command.get(k) for k in ("epoch", "revision", "action"))
        if (not _positive_int(epoch) or not self._active(epoch) or not _positive_int(rev)
                or rev != self._data.get("revision") or type(action) is not str
                or action not in self._data["capabilities"]):
            return
        # The current main-score controller checks capabilities and inputs again immediately before delivery.
        QTimer.singleShot(0, self, lambda: self._emit_action(epoch, rev, action, text))

    def _emit_action(self, epoch, revision, action, text):
        if (self._active(epoch) and self._data.get("revision") == revision and action in self._data["capabilities"]):
            self.userAction.emit(text)
            self._notify_main(dict(kind="action", command=json.loads(text)))

    def _hide(self):
        self.epoch = 0
        self._model = self._data = None
        self._dock_rect = None
        self._drag_offset = self._drag_origin = None
        if self._tool:
            self._tool.hide()

    def _return(self, epoch):
        if self._active(epoch):
            self._hide()
            QTimer.singleShot(0, self, lambda: self._announce(epoch, False))

    @Slot(int)
    def dismiss(self, epoch):
        if _positive_int(epoch):
            self._retired_epoch = max(epoch, self._retired_epoch)
            if self._active(epoch):
                self._hide()

    def _start_drag(self, epoch, x, y):
        if self._active(epoch) and _number(x, 0, 60) and _number(y, 0, 60):
            self._drag_origin = self._tool.pos()
            self._drag_offset = QPoint(round(x), round(y))

    def _move_drag(self, epoch):
        if not self._active(epoch) or self._drag_offset is None:
            return
        p = QCursor.pos() - self._drag_offset
        self._tool.move(self._position(p.x(), p.y()))

    def _end_drag(self, epoch, cancelled):
        if not self._active(epoch) or self._drag_origin is None:
            return
        origin = self._drag_origin
        self._drag_origin = self._drag_offset = None
        if cancelled:
            self._tool.move(origin)
        elif self._dock_rect:
            rect = self._dock_rect
            near = QRect(self._point(rect["x"], rect["y"]),
                         self._point(rect["x"] + rect["w"], rect["y"] + rect["h"]))
            if near.contains(QCursor.pos()):
                self._return(epoch)

    def _show_menu(self, epoch, opened):
        if self._active(epoch):
            self._tool.setFixedSize(self._WIDTH, self._MENU_HEIGHT if opened else self._HEIGHT)
            p = self._tool.pos()
            self._tool.move(self._position(p.x(), p.y()))

    @Slot()
    def shutdown(self):
        self._closed = True
        self._hide()
        if self._tool is not None:
            self._page.setWebChannel(None)
            self._page.deleteLater()
            self._tool.deleteLater()
            self._tool = None
