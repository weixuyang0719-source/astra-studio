"""Background conversion queue for the standalone Studio NCM tool."""

import json
from pathlib import Path
from threading import Event

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, Signal
from PySide6.QtWidgets import QFileDialog


class _ConversionSignals(QObject):
    progress = Signal(int, int, object)
    done = Signal(object, str)


class _Conversion(QRunnable):
    def __init__(self, sources, output, cache, stop, signals):
        super().__init__()
        self.sources = sources
        self.output = output
        self.cache = cache
        self.stop = stop
        self.signals = signals

    def run(self):
        try:
            from .audio_import import convert_ncm_files
            report = convert_ncm_files(
                self.sources, self.output, self.cache, log=lambda message: None,
                progress=self.signals.progress.emit, cancelled=self.stop.is_set,
            )
        except Exception as error:
            self.signals.done.emit(None, str(error) or type(error).__name__)
        else:
            self.signals.done.emit(report, '')


class NcmTool(QObject):
    changed = Signal(str)
    finished = Signal()

    def __init__(self, output, cache, open_folder, parent=None, dialog_parent=None):
        super().__init__(parent)
        self._output = Path(output)
        self._cache = Path(cache)
        self._open_folder = open_folder
        self._dialog_parent = dialog_parent
        self._sources = []
        self._results = []
        self._stop = None
        self._cancelled = False
        self._error = ''
        self._signals = _ConversionSignals(self)
        self._signals.progress.connect(self._progress, Qt.ConnectionType.QueuedConnection)
        self._signals.done.connect(self._done, Qt.ConnectionType.QueuedConnection)
        self._change_timer = QTimer(self, singleShot=True)
        self._change_timer.timeout.connect(self._emit)

    @property
    def running(self):
        return self._stop is not None

    def snapshot(self):
        return json.dumps({
            'sources': self._sources, 'output': str(self._output),
            'results': self._results, 'running': self.running,
            'cancelling': self.running and self._stop.is_set(),
            'cancelled': self._cancelled, 'error': self._error,
            'can_open_output': any(row.get('path') for row in self._results),
        }, ensure_ascii=False)

    def _publish(self):
        self._change_timer.start(0)

    def _emit(self):
        self.changed.emit(self.snapshot())

    def _reset(self):
        self._results = []
        self._cancelled = False
        self._error = ''

    def _choose_files(self):
        initial = str(Path(self._sources[-1]).parent) if self._sources else ''
        choices, _ = QFileDialog.getOpenFileNames(
            self._dialog_parent, '选择要转换的 NCM 文件', initial, 'NCM 音频 (*.ncm)'
        )
        if choices:
            paths = [str(Path(path).resolve(strict=True)) for path in choices]
            if any(Path(path).suffix.lower() != '.ncm' or not Path(path).is_file() for path in paths):
                raise ValueError('请选择存在的 .ncm 文件。')
            self._sources = list(dict.fromkeys((*self._sources, *paths)))
            self._reset()

    def _choose_output(self):
        initial = next((path for path in (self._output, *self._output.parents) if path.is_dir()), '')
        choice = QFileDialog.getExistingDirectory(
            self._dialog_parent, '选择转换输出目录', str(initial)
        )
        if choice:
            self._output = Path(choice).resolve(strict=True)
            self._reset()

    def _start(self):
        if not self._sources:
            raise ValueError('请先选择至少一个 NCM 文件。')
        self._reset()
        stop = Event()
        task = _Conversion(tuple(self._sources), self._output, self._cache, stop, self._signals)
        QThreadPool.globalInstance().start(task)
        self._stop = stop

    def cancel(self):
        if self.running:
            self._stop.set()
            self._publish()

    def _progress(self, _done, _total, result):
        self._results.append(result)
        self._publish()

    def _done(self, report, error):
        self._cancelled = self._stop.is_set() and len(self._results) < len(self._sources)
        self._stop = None
        self._error = error
        if report:
            self._results = report['results']
            self._cancelled = bool(report.get('cancelled', self._cancelled))
        self._publish()
        self.finished.emit()

    def handle(self, action, data):
        try:
            if action == 'ncmOpenOutput':
                if not any(row.get('path') for row in self._results) or not self._output.is_dir():
                    raise ValueError('暂无可打开的转换输出。')
                self._open_folder(self._output)
            elif action == 'ncmCancel':
                self.cancel()
            elif not self.running:
                if action == 'ncmChooseFiles':
                    self._choose_files()
                elif action == 'ncmChooseOutput':
                    self._choose_output()
                elif action == 'ncmRemove':
                    source = data.get('path')
                    if source in self._sources:
                        self._sources.remove(source)
                        self._reset()
                elif action == 'ncmClear':
                    self._sources.clear()
                    self._reset()
                elif action == 'ncmStart':
                    self._start()
        except (OSError, RuntimeError, ValueError) as error:
            self._error = str(error)
        self._publish()
