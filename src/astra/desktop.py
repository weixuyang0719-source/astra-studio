"""Astra Studio, the local Qt transcription and score workbench."""

from .release_paths import data_root
import json
import math
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QScrollArea, QSplitter, QToolButton, QVBoxLayout, QWidget,
)

from .desktop_jobs import JobController
from .desktop_widgets import AUDIO_TYPES, AudioDrop, AudioTransport, ScorePreview, audio_path, button, label
from .pipeline import read_json, save_json


ROOT = data_root()
PREFERENCES = ROOT / ".cache" / "desktop.json"
OUTPUTS = ROOT / "outputs"
MODES = {"MIDI 和乐谱": "all", "仅 MIDI": "midi", "仅乐谱": "score"}
SCALES = {"原速 ×1": 1.0, "半速 ×0.5": 0.5, "倍速 ×2": 2.0}
GRIDS = {"自动判断": "auto", "直拍": "straight", "三连音": "triplet", "细网格": "fine"}
INSTRUMENTS = {"piano": "钢琴", "guitar": "吉他"}
MEDIA_FILTER = "音频文件 (" + " ".join("*" + suffix for suffix in sorted(AUDIO_TYPES)) + ")"
TEMPO_FILTER = "速度图 (*.json *.mid *.midi)"
ROLE = Qt.ItemDataRole.UserRole

STYLE = """
QWidget { color: #EDF2F4; font-family: 'Microsoft YaHei UI', 'Segoe UI'; font-size: 11pt; }
QMainWindow, QDialog, #sidebar, #resultsPanel { background: #141D26; }
#workspace, QScrollArea#workScroll { background: #1C2833; border: none; }
#brand { font-family: Bahnschrift; font-size: 26pt; font-weight: 600; color: #8EDBCD; }
#pageTitle { font-family: Bahnschrift, 'Microsoft YaHei UI'; font-size: 22pt; font-weight: 600; }
#resultTitle { font-size: 17pt; font-weight: 600; }
#section, #dropTitle, #phase { font-weight: 600; }
#hint, #statusMeta { color: #A9BBC8; font-size: 10pt; }
#error { color: #F3AD9F; }
#divider { border: none; background: #344653; max-height: 1px; }
#audioDrop { background: #263541; border: 1px dashed #64808D; border-radius: 12px; }
#transport { background: #1C2833; border: 1px solid #344653; border-radius: 8px; }
#scorePreview { background: #263541; border: 1px solid #344653; border-radius: 10px; }
#previewEmpty { color: #A9BBC8; padding: 24px; }
QPushButton, QToolButton, QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
  background: #263541; border: 1px solid #455966; border-radius: 6px; min-height: 34px;
}
QPushButton, QToolButton { padding: 0 10px; }
QPushButton:hover, QToolButton:hover { background: #334653; border-color: #728B97; }
QPushButton:pressed, QToolButton:pressed { background: #1D2A34; }
QPushButton:disabled, QToolButton:disabled, QLineEdit:disabled, QComboBox:disabled, QCheckBox:disabled {
  color: #83939F; background: #202C36; border-color: #344653;
}
QPushButton#primary { background: #8EDBCD; color: #142B2C; border-color: #8EDBCD; font-weight: 700; }
QPushButton#primary:hover { background: #AAE8DD; }
QPushButton#primary:pressed { background: #75C5B9; }
QPushButton#primary:disabled { background: #314C50; color: #B3C9C7; border-color: #314C50; }
QPushButton#rail { text-align: left; background: #1C2833; }
QPushButton#rail:hover { background: #263541; }
QPushButton:focus, QToolButton:focus, QLineEdit:focus, QComboBox:focus, QListWidget:focus,
QPlainTextEdit:focus, QTableWidget:focus { border: 1px solid #8EDBCD; }
QPushButton#primary:focus { border: 2px solid #EDF2F4; }
QLineEdit { padding: 0 8px; }
QComboBox { padding: 0 8px; }
QComboBox QAbstractItemView { color: #EDF2F4; background: #263541; selection-background-color: #34575B; }
QCheckBox { min-height: 34px; spacing: 6px; }
QCheckBox#piano { color: #E4C496; }
QCheckBox#guitar { color: #B9C6F4; }
QCheckBox:focus { outline: 1px solid #8EDBCD; }
QListWidget, QPlainTextEdit, QTableWidget { background: #1C2833; border: 1px solid #344653; border-radius: 6px; }
QListWidget::item { min-height: 26px; padding: 2px 6px; }
QListWidget::item:hover { background: #263541; }
QListWidget::item:selected { background: #35535B; color: #EDF2F4; }
QListWidget#recent { background: #141D26; border-color: #141D26; }
QListWidget#recent:focus { border-color: #8EDBCD; }
QHeaderView::section { background: #263541; color: #EDF2F4; padding: 6px; border: 1px solid #344653; }
QTableWidget { alternate-background-color: #22313D; selection-background-color: #35535B; selection-color: #EDF2F4; }
QTableCornerButton::section { background: #263541; border: 1px solid #344653; }
QTabWidget::pane { background: #1C2833; border: 1px solid #344653; }
QTabBar::tab { background: #263541; padding: 8px 12px; }
QTabBar::tab:selected { color: #8EDBCD; }
QProgressBar { border: none; border-radius: 3px; background: #344653; min-height: 6px; max-height: 6px; }
QProgressBar::chunk { background: #8EDBCD; border-radius: 3px; }
QSlider::groove:horizontal { height: 4px; background: #526975; border-radius: 2px; }
QSlider::handle:horizontal { width: 12px; margin: -4px 0; background: #8EDBCD; border-radius: 6px; }
QSlider::handle:horizontal:disabled { background: #728593; }
QSlider:focus { border: 1px solid #8EDBCD; }
QScrollBar:vertical { width: 10px; background: #1C2833; border: none; }
QScrollBar::handle:vertical { background: #607683; min-height: 24px; border-radius: 4px; }
QScrollBar::handle:vertical:hover { background: #8EA7B1; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QSplitter::handle { background: #344653; }
QSplitter::handle:hover { background: #8EDBCD; }
QToolTip { background: #263541; color: #EDF2F4; border: 1px solid #728B97; }
"""


def _metadata(path):
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _combo(name, options):
    widget = QComboBox()
    widget.setAccessibleName(name)
    for text, value in options.items():
        widget.addItem(text, value)
    return widget


def _select(widget, value):
    widget.setCurrentIndex(max(0, widget.findData(value)))


def _divider():
    line = QFrame()
    line.setObjectName("divider")
    line.setFixedHeight(1)
    return line


def _row(*widgets):
    layout = QHBoxLayout()
    layout.setSpacing(8)
    for widget in widgets:
        layout.addWidget(widget)
    return layout


def _file_title(path):
    names = {"tempo.mid": "速度与拍号轨", "report.json": "导出报告"}
    if path.name in names:
        return names[path.name]
    instrument, _, subtype = path.stem.partition("_")
    if instrument not in INSTRUMENTS:
        return path.name
    kind = {".pdf": "PDF 乐谱", ".musicxml": "MusicXML 乐谱", ".wav": "分轨音频"}.get(path.suffix.lower())
    if subtype in ("raw", "quantized"):
        kind = "原始 MIDI" if subtype == "raw" else "量化 MIDI"
    return f"{INSTRUMENTS[instrument]}  {kind}" if kind else path.name


class MainWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Astra Studio · 本地音频转谱")
        self.setWindowIcon(QIcon(str(ROOT / "assets" / "astra.ico")))
        self.setMinimumSize(1080, 700)
        screen = self.screen().availableGeometry()
        self.resize(min(1360, screen.width() - 24), min(850, screen.height() - 48))
        self.setStyleSheet(STYLE)
        self.preferences = _metadata(PREFERENCES)
        self.audio = None
        self.project = None
        self.export = None
        self.request = None
        self.close_pending = False
        self.jobs = JobController(self)
        self._build()
        self.jobs.stage.connect(self._stage)
        self.jobs.log.connect(self._log)
        self.jobs.completed.connect(self._completed)
        self.jobs.failed.connect(self._failed)
        self.jobs.cancelled.connect(self._cancelled)
        self._restore_preferences()
        self._recent_projects()
        self._refresh_controls()

    def _build(self):
        shell = QWidget()
        shell.setObjectName("sidebar")
        self.setCentralWidget(shell)
        outer = QHBoxLayout(shell)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._sidebar())
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(5)
        self.splitter.addWidget(self._workspace())
        self.splitter.addWidget(self._results())
        self.splitter.setStretchFactor(0, 5)
        self.splitter.setStretchFactor(1, 5)
        self.splitter.setSizes([565, 610])
        outer.addWidget(self.splitter, 1)
        self.open_shortcut = QShortcut(QKeySequence.StandardKey.Open, self)
        self.open_shortcut.activated.connect(self._choose_audio)
        self.run_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self)
        self.run_shortcut.activated.connect(self._run)

    def _sidebar(self):
        rail = QWidget()
        rail.setObjectName("sidebar")
        rail.setFixedWidth(182)
        layout = QVBoxLayout(rail)
        layout.setContentsMargins(16, 20, 14, 16)
        layout.setSpacing(8)
        layout.addWidget(label("Astra", "brand"))
        layout.addWidget(label("本地音频转谱", "hint"))
        layout.addSpacing(12)
        self.new_button = button("新建分析", self._new, "rail")
        self.open_project_button = button("打开已有项目…", self._choose_project, "rail")
        layout.addWidget(self.new_button)
        layout.addWidget(self.open_project_button)
        layout.addSpacing(8)
        layout.addWidget(_divider())
        layout.addWidget(label("最近项目", "section"))
        self.recent = QListWidget()
        self.recent.setObjectName("recent")
        self.recent.setAccessibleName("最近项目")
        self.recent.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.recent.itemActivated.connect(self._activate_project)
        self.recent.itemClicked.connect(self._activate_project)
        layout.addWidget(self.recent, 1)
        self.recent_empty = label("完成转谱或打开已有项目后，项目会显示在这里。", "hint", True)
        layout.addWidget(self.recent_empty)
        layout.addWidget(button("使用说明", lambda: self._open(ROOT / "README.md"), "rail"))
        return rail

    def _workspace(self):
        scroll = QScrollArea()
        scroll.setObjectName("workScroll")
        scroll.setMinimumWidth(420)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        workspace = QWidget()
        workspace.setObjectName("workspace")
        scroll.setWidget(workspace)
        layout = QVBoxLayout(workspace)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(label("音频工作台", "pageTitle"))
        layout.addWidget(label("钢琴可转写 MIDI 与乐谱。吉他保留 WAV 分轨。", "hint", True))
        self.drop = AudioDrop()
        self.drop.browse.connect(self._choose_audio)
        self.drop.dropped.connect(self._set_audio)
        layout.addWidget(self.drop)

        self.settings = QWidget()
        settings = QVBoxLayout(self.settings)
        settings.setContentsMargins(0, 0, 0, 0)
        settings.setSpacing(6)
        settings.addWidget(label("乐器", "section"))
        self.instruments = {}
        row = QHBoxLayout()
        row.setSpacing(14)
        for key, title in INSTRUMENTS.items():
            check = QCheckBox(title)
            check.setObjectName(key)
            check.setChecked(True)
            self.instruments[key] = check
            row.addWidget(check)
        row.addStretch()
        settings.addLayout(row)
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(8)
        self.mode = _combo("钢琴导出格式", MODES)
        self.guitar_output = label("吉他分轨 · WAV", "hint")
        form.addRow("导出格式", _row(self.mode, self.guitar_output))
        self.output = QLineEdit(str(OUTPUTS))
        self.output.setAccessibleName("输出目录")
        form.addRow("输出目录", _row(self.output, button("浏览", self._choose_output)))
        settings.addLayout(form)
        self.keep_stems = QCheckBox("保留钢琴分轨音频")
        self.keep_stems.setChecked(True)
        settings.addWidget(self.keep_stems)
        self.advanced_toggle = self._toggle("高级设置", False)
        settings.addWidget(self.advanced_toggle)
        self.advanced = self._advanced()
        self.advanced.setVisible(False)
        self.advanced_toggle.toggled.connect(self.advanced.setVisible)
        settings.addWidget(self.advanced)
        layout.addWidget(self.settings)
        layout.addWidget(_divider())

        self.start_button = button("开始转谱", self._run, "primary")
        for check in self.instruments.values():
            check.toggled.connect(self._instrument_options)
        self._instrument_options()
        self.start_button.setToolTip("Ctrl+Enter 开始转谱")
        self.cancel_button = button("取消处理", self._cancel)
        self.cancel_button.hide()
        self.retry_button = button("重试上次", self._retry)
        self.retry_button.hide()
        actions = _row(self.start_button, self.cancel_button, self.retry_button)
        actions.addStretch()
        layout.addLayout(actions)
        self.phase = label("等待选择音频", "phase", True)
        layout.addWidget(self.phase)
        self.details = label("", "error", True)
        self.details.hide()
        layout.addWidget(self.details)
        self.progress = QProgressBar()
        self.progress.setAccessibleName("处理进度")
        self.progress.setTextVisible(False)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.log_toggle = self._toggle("运行日志", False)
        layout.addWidget(self.log_toggle)
        self.log_region = QWidget()
        log_layout = QVBoxLayout(self.log_region)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(6)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setPlaceholderText("开始处理后，实际步骤会显示在这里。")
        self.logs.setAccessibleName("运行日志内容")
        self.logs.setMaximumBlockCount(2500)
        self.logs.setFixedHeight(132)
        log_layout.addWidget(self.logs)
        self.open_log_button = button("打开本次日志", self._open_log)
        self.open_log_button.setEnabled(False)
        log_layout.addWidget(self.open_log_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.log_region.hide()
        self.log_toggle.toggled.connect(self.log_region.setVisible)
        layout.addWidget(self.log_region)
        layout.addStretch()
        return scroll

    def _toggle(self, text, checked):
        widget = QToolButton()
        widget.setText(text)
        widget.setCheckable(True)
        widget.setChecked(checked)
        widget.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        widget.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)
        widget.toggled.connect(lambda state: widget.setArrowType(Qt.ArrowType.DownArrow if state else Qt.ArrowType.RightArrow))
        return widget

    def _advanced(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.setSpacing(6)
        form = QFormLayout()
        form.setSpacing(8)
        self.tempo = QLineEdit()
        self.tempo.setAccessibleName("速度图文件")
        self.tempo.setPlaceholderText("JSON 或 MIDI")
        form.addRow("速度图", _row(self.tempo, button("浏览", self._choose_tempo)))
        self.bpm = QLineEdit()
        self.bpm.setAccessibleName("固定 BPM")
        self.bpm.setPlaceholderText("自动或沿用缓存")
        form.addRow("固定 BPM", self.bpm)
        self.meter = QLineEdit("4/4")
        self.meter.setAccessibleName("固定拍号")
        form.addRow("固定拍号", self.meter)
        self.scale = _combo("速度倍率", SCALES)
        form.addRow("速度倍率", self.scale)
        self.quantization = _combo("量化网格", GRIDS)
        form.addRow("量化网格", self.quantization)
        layout.addLayout(form)
        layout.addWidget(label("速度图和固定 BPM 选一项。均留空时优先复用已保存的速度图。倍率仅用于本次。", "hint", True))
        return widget

    def _results(self):
        panel = QWidget()
        panel.setObjectName("resultsPanel")
        panel.setMinimumWidth(410)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 20, 16, 14)
        layout.setSpacing(8)
        layout.addWidget(label("项目结果", "resultTitle"))
        self.result_caption = label("完成转谱或打开已有项目后，结果会显示在这里。", "hint", True)
        layout.addWidget(self.result_caption)
        self.summary = label("", "statusMeta", True)
        self.summary.hide()
        layout.addWidget(self.summary)
        self.files = QListWidget()
        self.files.setAccessibleName("最近成功导出的文件")
        self.files.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.files.setMinimumHeight(69)
        self.files.setMaximumHeight(136)
        self.files.currentItemChanged.connect(self._file_selected)
        self.files.itemActivated.connect(lambda item: self._open(Path(item.data(ROLE))))
        layout.addWidget(self.files)
        layout.addWidget(label("试听音频", "section"))
        self.transport = AudioTransport()
        layout.addWidget(self.transport)
        self.preview_caption = label("乐谱预览", "section")
        layout.addWidget(self.preview_caption)
        self.preview = ScorePreview()
        layout.addWidget(self.preview, 1)
        self.edit_tempo_button = button("修正节拍…", self._edit_tempo)
        self.reexport_button = button("仅重导出", self._reexport)
        self.folder_button = button("打开文件夹", lambda: self._open(self.export))
        layout.addLayout(_row(self.edit_tempo_button, self.reexport_button, self.folder_button))
        return panel

    def _restore_preferences(self):
        output = self.preferences.get("output")
        if isinstance(output, str) and output:
            self.output.setText(output)
        _select(self.mode, self.preferences.get("mode"))
        _select(self.quantization, self.preferences.get("quantization"))
        selected = self.preferences.get("instruments", list(INSTRUMENTS))
        if isinstance(selected, list) and selected:
            for key, check in self.instruments.items():
                check.setChecked(key in selected)
        self.keep_stems.setChecked(bool(self.preferences.get("keep_stems", True)))
        self.advanced_toggle.setChecked(bool(self.preferences.get("advanced", False)))
        volume = self.preferences.get("volume")
        if type(volume) is int and 0 <= volume <= 100:
            self.transport.volume.setValue(volume)
        sizes = self.preferences.get("splitter")
        if isinstance(sizes, list) and len(sizes) == 2 and all(type(size) is int and size > 0 for size in sizes):
            self.splitter.setSizes(sizes)

    def _save_preferences(self):
        self.preferences.update(
            output=self.output.text().strip(), mode=self.mode.currentData(),
            quantization=self.quantization.currentData(), keep_stems=self.keep_stems.isChecked(),
            instruments=self._selected_instruments(), advanced=self.advanced_toggle.isChecked(),
            splitter=self.splitter.sizes(), volume=self.transport.volume.value(),
        )
        try:
            save_json(PREFERENCES, self.preferences)
        except (OSError, ValueError) as exc:
            self._log(f"无法保存界面设置。{exc}")

    def _recent_projects(self):
        stored = self.preferences.get("recent", [])
        remembered = [Path(path) for path in stored if isinstance(path, str)] if isinstance(stored, list) else []
        discovered = sorted(OUTPUTS.glob("*/project.json"), key=lambda file: file.stat().st_mtime, reverse=True)
        projects = list(dict.fromkeys(remembered + [file.parent for file in discovered]))
        self.recent.clear()
        for path in projects:
            state = _metadata(path / "project.json")
            if not state.get("latest_export"):
                continue
            title = self._project_title(path, state)
            item = QListWidgetItem(title)
            item.setToolTip(f"{title}\n{path}")
            item.setData(ROLE, str(path))
            self.recent.addItem(item)
            if path == self.project:
                self.recent.setCurrentItem(item)
            if self.recent.count() >= 18:
                break
        self.recent.setVisible(self.recent.count() > 0)
        self.recent_empty.setVisible(self.recent.count() == 0)

    def _choose_audio(self):
        if not self.jobs.running:
            path, _ = QFileDialog.getOpenFileName(self, "选择要转谱的音频", str(self.audio.parent) if self.audio else "", MEDIA_FILTER)
            if path:
                self._set_audio(path)

    def _set_audio(self, path):
        if self.jobs.running:
            return
        selected = audio_path(path)
        if not selected:
            self._problem("请选择存在的本地音频文件。")
            return
        if selected.resolve() != self.audio:
            self._clear_overrides()
        self.audio = selected.resolve()
        self.drop.set_path(self.audio)
        if self.project is None:
            self.transport.set_sources([("原曲", self.audio)])
        self.phase.setText("音频已就绪")
        self.details.hide()
        self._refresh_controls()

    def _new(self):
        if self.jobs.running:
            return
        self.audio = None
        self.drop.set_path(None)
        self._clear_overrides()
        if not self.project:
            self.transport.set_sources([])
        self.details.hide()
        self.phase.setText("等待选择音频")
        self._refresh_controls()
        self.drop.browse_button.setFocus()

    def _clear_overrides(self):
        self.tempo.clear()
        self.bpm.clear()
        self.meter.setText("4/4")
        _select(self.scale, 1.0)

    def _choose_output(self):
        path = QFileDialog.getExistingDirectory(self, "选择输出目录", self.output.text())
        if path:
            self.output.setText(path)

    def _choose_tempo(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择速度图", "", TEMPO_FILTER)
        if path:
            self.tempo.setText(path)
            self.bpm.clear()

    def _choose_project(self):
        if not self.jobs.running:
            path = QFileDialog.getExistingDirectory(self, "选择含 project.json 的项目文件夹", self.output.text())
            if path:
                self._show_project(Path(path), restore=True)

    def _activate_project(self, item):
        if not self.jobs.running:
            path = Path(item.data(ROLE))
            if path != self.project:
                self._show_project(path, restore=True)

    @staticmethod
    def _project_title(path, state):
        source = state.get("source")
        return Path(source).stem if isinstance(source, str) and source else path.name

    def _show_project(self, project, *, restore=False):
        project = project.resolve()
        state = _metadata(project / "project.json")
        export_value = state.get("latest_export")
        export = project / "exports" / Path(export_value).name if isinstance(export_value, str) and export_value else None
        if not state or not export or not export.is_dir():
            self._problem("此文件夹没有可打开的成功导出。")
            return
        report = _metadata(export / "report.json")
        same_project = project == self.project
        self.project, self.export = project, export
        title = self._project_title(project, state)
        self.result_caption.setText(f"{title}\n最近成功版本  {export.name}")
        self.result_caption.setToolTip(str(export))
        self._report_summary(report)
        self._list_results(project, export, state, report, same_project)
        if restore:
            source = state.get("source")
            self.audio = audio_path(source) if isinstance(source, str) else None
            self.drop.set_path(self.audio)
            self.output.setText(str(project.parent))
            self._clear_overrides()
            for key, check in self.instruments.items():
                check.setChecked(key in state.get("instruments", []))
            self.keep_stems.setChecked(bool(state.get("keep_stems", True)))
            config = state.get("config") or {}
            _select(self.quantization, config.get("quantization") if isinstance(config, dict) else None)
            files = report.get("files") or []
            midi = any(name.endswith(("_raw.mid", "_quantized.mid")) for name in files if isinstance(name, str))
            score = any(name.endswith((".musicxml", ".pdf")) for name in files if isinstance(name, str))
            _select(self.mode, "all" if midi and score else "score" if score else "midi")
            self.phase.setText("已打开缓存项目")
            self.details.hide()
        recent = self.preferences.get("recent", [])
        recent = recent if isinstance(recent, list) else []
        self.preferences["recent"] = [str(project)] + [entry for entry in recent if entry != str(project)][:17]
        self._recent_projects()
        self._refresh_controls()
        self._save_preferences()

    def _report_summary(self, report):
        data = report.get("instruments")
        stats = {key: value for key, value in data.items() if isinstance(value, dict)} if isinstance(data, dict) else {}
        if not stats:
            self.summary.hide()
            return
        def count(key):
            values = [item.get(key) for item in stats.values()]
            return str(sum(values)) if all(type(value) is int for value in values) else "未统计"
        notes = " / ".join(f"{INSTRUMENTS.get(key, key)} {item.get('notes', '—')} 音符" for key, item in stats.items())
        warnings = report.get("warnings")
        warning_count = str(len(warnings)) if isinstance(warnings, list) else "未统计"
        self.summary.setText(f"{notes}  ·  警告 {warning_count}\n量化偏移 >80 ms  起点 {count('large_quantization_shifts')} / 终点 {count('large_quantization_end_shifts')}")
        self.summary.show()

    def _list_results(self, project, export, state, report, preserve):
        files = report.get("files")
        files = [export / name for name in files if isinstance(name, str) and Path(name).name == name] if isinstance(files, list) else []
        if not files:
            files = sorted(path for path in export.iterdir() if path.suffix.lower() in {".pdf", ".musicxml", ".mid"})
        report_path = export / "report.json"
        if report_path.is_file():
            files.append(report_path)
        source = state.get("source")
        original = audio_path(source) if isinstance(source, str) else None
        sources = [("原曲", original)] if original else []
        for instrument, title in INSTRUMENTS.items():
            if instrument not in state.get("instruments", []):
                continue
            stem = audio_path(project / f"{instrument}.wav")
            if stem:
                sources.append((f"{title}分轨", stem))
                files.append(stem)
        self.transport.set_sources(sources, preserve=preserve)
        files = [file for file in files if file.is_file()]
        files.sort(key=lambda file: (file.suffix.lower() != ".pdf", file.name == "report.json"))
        self.files.blockSignals(True)
        self.files.clear()
        for path in files:
            item = QListWidgetItem(_file_title(path))
            item.setData(ROLE, str(path))
            item.setToolTip(path.name)
            self.files.addItem(item)
        self.files.blockSignals(False)
        preview = next((path for path in files if path.suffix.lower() == ".pdf"), None)
        self.preview.load(preview)
        self.preview_caption.setText(f"乐谱预览  ·  {_file_title(preview)}" if preview else "乐谱预览")
        if preview:
            self.files.setCurrentRow(files.index(preview))

    def _file_selected(self, current, _previous):
        if not current:
            return
        path = Path(current.data(ROLE))
        if path.suffix.lower() == ".pdf":
            self.preview.load(path)
            self.preview_caption.setText(f"乐谱预览  ·  {_file_title(path)}")
        elif path.suffix.lower() in AUDIO_TYPES:
            self.transport.select(path)

    def _selected_instruments(self):
        return [key for key, check in self.instruments.items() if check.isChecked()]

    def _instrument_options(self):
        guitar_only = self._selected_instruments() == ["guitar"]
        self.mode.setVisible(not guitar_only)
        self.guitar_output.setVisible(guitar_only)
        self.keep_stems.setVisible(not guitar_only)
        self.advanced_toggle.setVisible(not guitar_only)
        self.advanced.setVisible(not guitar_only and self.advanced_toggle.isChecked())
        self.start_button.setText("开始分轨" if guitar_only else "开始转谱")

    def _options(self):
        tempo, bpm_text = self.tempo.text().strip(), self.bpm.text().strip()
        if tempo and bpm_text:
            raise ValueError("请在速度图和固定 BPM 之间选择一项。")
        if tempo:
            path = Path(tempo).expanduser().resolve()
            if not path.is_file() or path.suffix.lower() not in {".json", ".mid", ".midi"}:
                raise ValueError("请选择存在的 JSON 或 MIDI 速度图。")
            tempo = str(path)
        bpm, meter = None, "4/4"
        if bpm_text:
            try:
                bpm = float(bpm_text)
                if not math.isfinite(bpm) or not 10 <= bpm <= 600:
                    raise ValueError
            except ValueError as exc:
                raise ValueError("固定 BPM 须在 10 到 600 之间。") from exc
            try:
                numerator, denominator = map(int, self.meter.text().split("/"))
                if not 1 <= numerator <= 32 or denominator not in (1, 2, 4, 8, 16, 32, 64, 128):
                    raise ValueError
            except ValueError as exc:
                raise ValueError("请输入有效拍号，例如 4/4 或 6/8。") from exc
            meter = f"{numerator}/{denominator}"
        return {"mode": self.mode.currentData(), "tempo_path": tempo or None, "bpm": bpm,
                "meter": meter, "beat_scale": self.scale.currentData()}

    def _run(self):
        self._prepare("run")

    def _reexport(self):
        self._prepare("reexport")

    def _prepare(self, kind):
        if self.jobs.running:
            return
        try:
            instruments = self._selected_instruments()
            if kind == "run" and not (self.audio and self.audio.is_file() and instruments):
                raise ValueError("请选择有效音频和至少一种乐器。")
            if kind == "reexport" and not self.project:
                raise ValueError("请先打开含有成功导出的项目。")
            output = Path(self.output.text().strip() or OUTPUTS).expanduser().resolve()
            guitar_only = instruments == ["guitar"]
            options = {"mode": "all"} if guitar_only else self._options()
        except (OSError, ValueError) as exc:
            self._problem(str(exc))
            return
        self._submit({"kind": kind, "audio": str(self.audio or ""), "output": str(output),
                      "project": str(self.project or ""), "instruments": instruments,
                      "keep_stems": guitar_only or self.keep_stems.isChecked(), "quantization": self.quantization.currentData(),
                      "options": options})

    def _submit(self, request):
        if self.jobs.running:
            return
        self.request = request
        self.logs.clear()
        self.details.hide()
        self.retry_button.hide()
        self.phase.setText("正在启动重导出…" if request["kind"] == "reexport" else "正在启动转谱…")
        self.progress.setRange(0, 0)
        self.progress.show()
        self._save_preferences()
        try:
            self.jobs.start(request)
        except (OSError, RuntimeError, ValueError) as exc:
            self._failed(str(exc))
        self._refresh_controls()

    def _refresh_controls(self):
        busy = self.jobs.running
        for widget in (self.drop, self.settings, self.new_button, self.open_project_button, self.recent):
            widget.setEnabled(not busy)
        self.start_button.setEnabled(not busy and bool(self.audio))
        for widget in (self.edit_tempo_button, self.reexport_button):
            widget.setEnabled(not busy and bool(self.project))
        self.folder_button.setEnabled(bool(self.export))
        self.cancel_button.setVisible(busy)
        if not busy:
            self.cancel_button.setEnabled(True)
        self.open_log_button.setEnabled(bool(self.jobs.log_path))

    def _stage(self, stage):
        if self.cancel_button.isEnabled():
            self.phase.setText(stage)
        self.open_log_button.setEnabled(bool(self.jobs.log_path))

    def _log(self, line):
        self.logs.appendPlainText(line.rstrip())
        self.open_log_button.setEnabled(bool(self.jobs.log_path))

    def _completed(self, project):
        _select(self.scale, 1.0)
        self.progress.hide()
        self.phase.setText("处理完成")
        self._show_project(Path(project))
        self._refresh_controls()
        self._finish_close()

    def _failed(self, message):
        self.progress.hide()
        self.phase.setText("处理失败")
        self._problem(message)
        self._log(message)
        self.log_toggle.setChecked(True)
        self.retry_button.setVisible(self.request is not None)
        self._refresh_controls()
        self._finish_close()

    def _cancelled(self):
        self.progress.hide()
        self.phase.setText("已取消处理")
        self._refresh_controls()
        self._finish_close()

    def _problem(self, message):
        self.details.setText(message.splitlines()[0] if message else "请检查运行日志。")
        self.details.setToolTip(message)
        self.details.show()

    def _retry(self):
        if self.request:
            self._submit(self.request)

    def _cancel(self):
        if self.jobs.running:
            self.cancel_button.setEnabled(False)
            self.phase.setText("正在取消处理…")
            self.jobs.cancel()

    def _edit_tempo(self):
        if self.jobs.running or not self.project:
            return
        from .desktop_tempo import TempoDialog

        source = self.export / "tempo_map.json"
        if not source.is_file():
            source = self.project / "tempo_map.json"
        try:
            dialog = TempoDialog(read_json(source), self)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self._problem(f"无法读取项目速度图。{exc}")
            return
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        filename = f"tempo_override_{datetime.now():%Y%m%d_%H%M%S_%f}.json"
        override = self.project / filename
        try:
            save_json(override, dialog.data)
        except (OSError, ValueError) as exc:
            self._problem(f"无法保存修订后的速度图。{exc}")
            return
        self.tempo.setText(str(override))
        self.bpm.clear()
        _select(self.scale, 1.0)
        self._reexport()

    def _open_log(self):
        self._open(self.jobs.log_path)

    def _open(self, path):
        if path and (not Path(path).exists() or not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))):
            self._problem(f"无法打开 {Path(path).name}。请检查文件或 Windows 默认打开方式。")

    def _finish_close(self):
        if self.close_pending:
            QTimer.singleShot(0, self.close)

    def closeEvent(self, event):
        if self.jobs.running:
            event.ignore()
            if not self.close_pending and QMessageBox.question(
                self, "任务正在运行", "取消当前任务并关闭 Astra？已有成功导出会保留。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
            ) == QMessageBox.StandardButton.Yes:
                self.close_pending = True
                self._cancel()
            return
        self._save_preferences()
        event.accept()


def main():
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Astra.LocalMusic.Studio")
    app = QApplication.instance() or QApplication(["Astra Studio"])
    app.setApplicationName("Astra Studio")
    app.styleHints().setColorScheme(Qt.ColorScheme.Dark)
    app.setStyle("Fusion")
    palette = app.palette()
    for role, color in {
        QPalette.ColorRole.Window: "#141D26", QPalette.ColorRole.WindowText: "#EDF2F4",
        QPalette.ColorRole.Base: "#1C2833", QPalette.ColorRole.AlternateBase: "#263541",
        QPalette.ColorRole.Text: "#EDF2F4", QPalette.ColorRole.Button: "#263541",
        QPalette.ColorRole.ButtonText: "#EDF2F4", QPalette.ColorRole.Highlight: "#35535B",
        QPalette.ColorRole.HighlightedText: "#EDF2F4", QPalette.ColorRole.PlaceholderText: "#A9BBC8",
    }.items():
        palette.setColor(role, QColor(color))
    app.setPalette(palette)
    app.setWindowIcon(QIcon(str(ROOT / "assets" / "astra.ico")))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
