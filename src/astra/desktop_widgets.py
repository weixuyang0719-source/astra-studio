"""Local file input, audio transport and PDF preview for Astra Studio."""

from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QSlider, QStackedWidget,
    QSizePolicy, QVBoxLayout, QWidget,
)


AUDIO_TYPES = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".aif", ".aiff", ".wma", ".opus"}


def audio_path(path):
    candidate = Path(path)
    return candidate if candidate.suffix.lower() in AUDIO_TYPES and candidate.is_file() else None


def label(text, name=None, wrap=False):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget


def button(text, callback, name=None):
    widget = QPushButton(text)
    if name:
        widget.setObjectName(name)
    widget.clicked.connect(callback)
    return widget


class StaffMark(QWidget):
    """A small decorative clefless staff, never used as an audio visualization."""

    def __init__(self):
        super().__init__()
        self.setFixedSize(64, 56)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#3D5B66"), 1))
        for y in (16, 22, 28, 34, 40):
            painter.drawLine(5, y, 59, y)
        painter.setPen(QPen(QColor("#8EDBCD"), 1.6))
        painter.setBrush(QColor("#8EDBCD"))
        for x, y in ((19, 34), (32, 28), (45, 22)):
            painter.drawEllipse(x - 4, y - 2, 8, 4)
            painter.drawLine(x + 4, y, x + 4, y - 14)


class AudioDrop(QFrame):
    dropped = Signal(str)
    browse = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("audioDrop")
        self.setAcceptDrops(True)
        self.setAccessibleName("音频导入区")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(StaffMark())
        text = QVBoxLayout()
        text.setSpacing(4)
        self.title = label("拖放音频到这里", "dropTitle")
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.hint = label("WAV、FLAC、MP3 等本地音频", "hint", True)
        text.addWidget(self.title)
        text.addWidget(self.hint)
        layout.addLayout(text, 1)
        self.browse_button = button("选择文件", self.browse.emit)
        layout.addWidget(self.browse_button)

    def set_path(self, path):
        self.filename = path.name if path else "拖放音频到这里"
        self.title.setAccessibleName(self.filename)
        self._elide_name()
        self.hint.setText("音频已就绪" if path else "WAV、FLAC、MP3 等本地音频")
        self.setToolTip(str(path) if path else "可使用 Ctrl+O 选择音频")

    def _elide_name(self):
        name = getattr(self, "filename", "拖放音频到这里")
        self.title.setText(self.title.fontMetrics().elidedText(name, Qt.TextElideMode.ElideMiddle, self.title.width()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide_name()

    def _local_audio(self, event):
        urls = event.mimeData().urls()
        if self.isEnabled() and len(urls) == 1 and urls[0].isLocalFile():
            return audio_path(urls[0].toLocalFile())
        return None

    def dragEnterEvent(self, event):
        if self._local_audio(event):
            self.setStyleSheet("#audioDrop {border: 1px solid #8EDBCD;}")
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self.setStyleSheet("")
        event.accept()

    def dropEvent(self, event):
        self.setStyleSheet("")
        path = self._local_audio(event)
        if path:
            self.dropped.emit(str(path))
            event.acceptProposedAction()
        else:
            event.ignore()


class AudioTransport(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("transport")
        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.65)
        self.player.setAudioOutput(self.audio_output)
        self.pending = None
        self.loading = False
        self.current_sources = []
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(4)
        controls = QHBoxLayout()
        controls.setSpacing(6)
        self.sources = QComboBox()
        self.sources.setAccessibleName("试听音频来源")
        self.play = button("播放", self._toggle)
        self.stop = button("停止", self._stop)
        controls.addWidget(self.sources, 1)
        controls.addWidget(self.play)
        controls.addWidget(self.stop)
        outer.addLayout(controls)
        timeline = QHBoxLayout()
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setAccessibleName("试听进度")
        self.slider.setRange(0, 0)
        self.time = label("0:00 / 0:00", "hint")
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setAccessibleName("试听音量")
        self.volume.setToolTip("试听音量")
        self.volume.setRange(0, 100)
        self.volume.setValue(65)
        self.volume.setFixedWidth(62)
        self.volume.valueChanged.connect(lambda value: self.audio_output.setVolume(value / 100))
        timeline.addWidget(self.slider, 1)
        timeline.addWidget(self.time)
        timeline.addWidget(label("音量", "hint"))
        timeline.addWidget(self.volume)
        outer.addLayout(timeline)
        self.problem = label("", "error", True)
        self.problem.hide()
        outer.addWidget(self.problem)
        self.sources.currentIndexChanged.connect(self._switch)
        self.player.positionChanged.connect(self._position)
        self.player.durationChanged.connect(self._duration)
        self.player.seekableChanged.connect(self._resume)
        self.player.mediaStatusChanged.connect(self._status)
        self.player.playbackStateChanged.connect(self._play_state)
        self.player.errorOccurred.connect(self._error)
        self.slider.sliderReleased.connect(lambda: self.player.setPosition(self.slider.value()))
        self.slider.actionTriggered.connect(self._seek_action)
        self.set_sources([])

    def set_sources(self, sources, preserve=False):
        previous = self.sources.currentData()
        if sources == self.current_sources and self.sources.count():
            return
        self.current_sources = sources
        self.sources.blockSignals(True)
        self.sources.clear()
        for title, path in sources:
            self.sources.addItem(title, str(path))
        index = self.sources.findData(previous) if preserve else -1
        if not sources:
            self.sources.addItem("暂无可试听音频")
        self.sources.setCurrentIndex(max(0, index))
        self.sources.blockSignals(False)
        if not preserve:
            self._stop()
        self._switch()

    def select(self, path):
        index = self.sources.findData(str(path))
        if index >= 0:
            self.sources.setCurrentIndex(index)

    def _switch(self, _index=0):
        selected = self.sources.currentData()
        url = QUrl.fromLocalFile(selected) if selected else QUrl()
        self.sources.setEnabled(bool(selected))
        self.play.setEnabled(bool(selected))
        self.stop.setEnabled(bool(selected))
        self.volume.setEnabled(bool(selected))
        if url == self.player.source():
            return
        position, playing = self.pending or (self.player.position(), self._playing())
        self.problem.hide()
        self.pending = (position, playing) if selected else None
        self.loading = bool(selected)
        self.slider.setRange(0, 0)
        self.slider.setEnabled(False)
        self.player.setSource(url)
        self._position(0)

    def _status(self, status):
        if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
            self.loading = False
            self._resume()

    def _resume(self, *_args):
        if not self.pending or self.loading:
            return
        position, playing = self.pending
        if position and self.player.duration() <= 0:
            return
        self.pending = None
        if position and self.player.isSeekable():
            self.player.setPosition(min(position, self.player.duration()))
        if playing:
            self.player.play()

    def _playing(self):
        return self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def _toggle(self):
        if self.loading and self.pending:
            self.pending = (self.pending[0], not self.pending[1])
            self.play.setText("暂停" if self.pending[1] else "播放")
        elif self._playing():
            self.player.pause()
        else:
            self.player.play()

    def _stop(self):
        self.pending = None
        self.player.stop()
        self.play.setText("播放")

    def _play_state(self, _state):
        self.play.setText("暂停" if self._playing() else "播放")

    def _seek_action(self, _action):
        if not self.slider.isSliderDown():
            self.player.setPosition(self.slider.sliderPosition())

    def _duration(self, duration):
        self.slider.setRange(0, duration)
        self.slider.setEnabled(duration > 0)
        self._position(self.player.position())
        self._resume()

    def _position(self, position):
        if not self.slider.isSliderDown():
            self.slider.setValue(position)
        def clock(milliseconds):
            minute, second = divmod(milliseconds // 1000, 60)
            return f"{minute}:{second:02d}"
        self.time.setText(f"{clock(position)} / {clock(self.player.duration())}")

    def _error(self, _error, text):
        self.loading = False
        self.pending = None
        self.problem.setText(f"无法试听。{text}")
        self.problem.show()


class ScorePreview(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("scorePreview")
        self.document = QPdfDocument(self)
        self.path = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.stack = QStackedWidget()
        self.empty = label("完成含乐谱的导出后，可在这里预览 PDF。", "previewEmpty", True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.view = QPdfView(self)
        self.view.setAccessibleName("乐谱 PDF 预览")
        self.view.setDocument(self.document)
        self.view.setPageMode(QPdfView.PageMode.MultiPage)
        self.view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        self.stack.addWidget(self.empty)
        self.stack.addWidget(self.view)
        layout.addWidget(self.stack)
        self.setMinimumHeight(136)

    def load(self, path):
        if path == self.path:
            return
        self.path = path
        self.document.close()
        if path and self.document.load(str(path)) == QPdfDocument.Error.None_:
            self.stack.setCurrentWidget(self.view)
        else:
            self.empty.setText("无法读取此 PDF。" if path else "此导出暂无 PDF。可在文件列表中打开已有乐谱。")
            self.stack.setCurrentWidget(self.empty)
