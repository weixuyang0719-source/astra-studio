"""Staged editing of tempo and meter for the native desktop frontend."""

import json
import math
from copy import deepcopy
from fractions import Fraction

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .midi import tempo_events
from .timing import TempoMap


_ANCHOR_FIELDS = (("seconds", "时间（秒）", float), ("beat", "拍位（四分拍）", float))
_METER_FIELDS = (("beat", "起始拍位（四分拍）", float), ("numerator", "分子", int),
                 ("denominator", "分母", int))
_EDIT_ERRORS = (ArithmeticError, KeyError, TypeError, ValueError, IndexError)


def _label(text, name="hint"):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setObjectName(name)
    widget.setWordWrap(True)
    return widget


def _validated(data):
    if not isinstance(data, dict):
        raise ValueError("速度图 JSON 必须是一个对象。")
    if "warnings" in data and (not isinstance(data["warnings"], list)
                               or any(not isinstance(value, str) for value in data["warnings"])):
        raise ValueError("warnings 必须是文字列表。")
    data = deepcopy(data)
    tempo = TempoMap(data)
    for row, seconds, beat in zip(tempo.anchors, tempo.seconds, tempo.beats):
        row.update(seconds=seconds, beat=beat)
    if "pickup_beats" in data:
        data["pickup_beats"] = float(data["pickup_beats"])
    tempo_events(tempo)

    signatures = data["time_signatures"]
    active = signatures[0]["numerator"], signatures[0]["denominator"]
    bar_start = Fraction(data.get("pickup_beats", 0)).limit_denominator(480)
    for row in signatures[1:]:
        meter = row["numerator"], row["denominator"]
        if meter == active:
            continue
        offset = Fraction(row["beat"]).limit_denominator(480)
        if (offset - bar_start) % Fraction(4 * active[0], active[1]):
            raise ValueError(f"第 {row['beat']:g} 拍的拍号变更须落在小节线上。")
        bar_start, active = offset, meter
    return data


class _Rows(QWidget):
    changed = Signal()
    add_requested = Signal()

    def __init__(self, title, fields, *, anchors=False):
        super().__init__()
        self.fields = fields
        self.anchors = anchors
        self.minimum_rows = 2 if anchors else 1
        headers = [label for _, label, _ in fields]
        if anchors:
            headers.append("到下一锚点的 BPM")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        self.table = QTableWidget(0, len(headers))
        self.table.setAccessibleName(title)
        self.table.setHorizontalHeaderLabels(headers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        layout.addWidget(self.table, 1)

        actions = QHBoxLayout()
        add = QPushButton("添加锚点" if anchors else "添加拍号")
        self.edit = QPushButton("编辑")
        self.remove = QPushButton("删除")
        for button in (add, self.edit, self.remove):
            button.setMinimumHeight(36)
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)
        add.clicked.connect(self.add_requested)
        self.edit.clicked.connect(self._edit_current)
        self.remove.clicked.connect(self._remove_current)
        self.table.currentCellChanged.connect(self._update_actions)
        self.table.cellChanged.connect(self._cell_changed)
        self._update_actions()

    def set_rows(self, rows):
        with QSignalBlocker(self.table):
            self.table.setRowCount(0)
            for index, row in enumerate(rows):
                self.insert(index, row)
        self._refresh_bpm()
        self._update_actions()

    def insert(self, index, row):
        with QSignalBlocker(self.table):
            self.table.insertRow(index)
            for column, (key, label, _) in enumerate(self.fields):
                cell = QTableWidgetItem(str(row[key]))
                cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if column == 0:
                    cell.setData(Qt.ItemDataRole.UserRole, deepcopy(row))
                if index == 0 and (self.anchors or key == "beat"):
                    cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    cell.setToolTip(f"首行{label}固定在 0")
                self.table.setItem(index, column, cell)
            if self.anchors:
                bpm = QTableWidgetItem()
                bpm.setFlags(bpm.flags() & ~Qt.ItemFlag.ItemIsEditable)
                bpm.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(index, len(self.fields), bpm)

    def rows(self):
        rows = []
        for index in range(self.table.rowCount()):
            row = dict(self.table.item(index, 0).data(Qt.ItemDataRole.UserRole))
            for column, (key, label, cast) in enumerate(self.fields):
                try:
                    value = cast(self.table.item(index, column).text().strip())
                    if not math.isfinite(value) or value < 0:
                        raise ValueError
                except (OverflowError, ValueError) as error:
                    raise ValueError(f"第 {index + 1} 行的{label}需要有效的非负数。") from error
                row[key] = value
            rows.append(row)
        return rows

    def _update_actions(self, *_):
        current = self.table.currentItem()
        self.edit.setEnabled(current is not None and bool(current.flags() & Qt.ItemFlag.ItemIsEditable))
        self.remove.setEnabled(self.table.currentRow() > 0 and self.table.rowCount() > self.minimum_rows)

    def _edit_current(self):
        current = self.table.currentItem()
        if current is not None and current.flags() & Qt.ItemFlag.ItemIsEditable:
            self.table.setFocus()
            self.table.editItem(current)

    def _remove_current(self):
        row = self.table.currentRow()
        if row > 0 and self.table.rowCount() > self.minimum_rows:
            self.table.removeRow(row)
            self._refresh_bpm()
            self._update_actions()
            self.changed.emit()

    def _cell_changed(self, row, column):
        if column < len(self.fields):
            self._refresh_bpm((row - 1, row))
            self.changed.emit()

    def _refresh_bpm(self, indices=None):
        if not self.anchors:
            return
        count = self.table.rowCount()
        with QSignalBlocker(self.table):
            for row in range(count) if indices is None else indices:
                if not 0 <= row < count:
                    continue
                text = "沿用上一段"
                if row + 1 < count:
                    try:
                        ds, db = (float(self.table.item(row + 1, col).text())
                                  - float(self.table.item(row, col).text()) for col in (0, 1))
                        bpm = 60 * db / ds if ds > 0 and db > 0 else 0
                        text = f"{bpm:.2f}" if math.isfinite(bpm) and bpm > 0 else "区间无效"
                    except (ValueError, ArithmeticError):
                        text = "区间无效"
                self.table.item(row, len(self.fields)).setText(text)


class TempoDialog(QDialog):
    """Accept a validated copy. The controller owns all persistence and export."""

    def __init__(self, data: dict, parent=None):
        super().__init__(parent)
        self.data = None
        self._base = deepcopy(data)
        self.setWindowTitle("修正节拍与拍号")
        self.resize(760, 580)
        self.setMinimumSize(540, 420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(_label("修正节拍与拍号", "resultTitle"))
        layout.addWidget(_label("用音频秒时间对齐拍位。应用后将使用这份速度图重新导出当前项目。"))

        self._stack = QStackedWidget()
        self._tabs = QTabWidget()
        self.anchors = _Rows("速度锚点", _ANCHOR_FIELDS, anchors=True)
        self.meters = _Rows("拍号", _METER_FIELDS)
        self.anchors.table.setObjectName("tempoAnchors")
        self.meters.table.setObjectName("tempoMeters")
        self.anchors.layout().addWidget(_label("双击数值可编辑。第一个锚点固定为 0 秒、0 拍。"))
        self.pickup = QLineEdit()
        self.pickup.setObjectName("tempoPickup")
        self.pickup.setMaximumWidth(128)
        self.pickup.setMinimumHeight(36)
        pickup_label = _label("弱起长度（四分拍）")
        pickup_label.setBuddy(self.pickup)
        self.pickup.setAccessibleName(pickup_label.text())
        pickup_bar = QHBoxLayout()
        pickup_bar.addWidget(pickup_label)
        pickup_bar.addWidget(self.pickup)
        pickup_bar.addStretch(1)
        self.meters.layout().addLayout(pickup_bar)
        self.meters.layout().addWidget(_label("拍号变化须落在小节线上。没有弱起时填 0。"))
        self._tabs.addTab(self.anchors, "速度锚点")
        self._tabs.addTab(self.meters, "拍号与弱起")
        self._stack.addWidget(self._tabs)

        self.json = QPlainTextEdit()
        self.json.setObjectName("tempoJson")
        self.json.setAccessibleName("速度图专家 JSON")
        self.json.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.json.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._stack.addWidget(self.json)
        layout.addWidget(self._stack, 1)

        self.feedback = _label("", "error")
        self.feedback.hide()
        layout.addWidget(self.feedback)

        actions = QHBoxLayout()
        self.expert = QPushButton("专家 JSON")
        self.expert.setMinimumHeight(36)
        self.expert.clicked.connect(self._toggle_expert)
        actions.addWidget(self.expert)
        actions.addStretch(1)
        buttons = QDialogButtonBox()
        cancel = buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        self.apply = buttons.addButton("应用并重导出", QDialogButtonBox.ButtonRole.AcceptRole)
        self.apply.setObjectName("primary")
        for button in (cancel, self.apply):
            button.setMinimumHeight(36)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        actions.addWidget(buttons)
        layout.addLayout(actions)

        self.anchors.add_requested.connect(self._add_anchor)
        self.meters.add_requested.connect(self._add_meter)
        for changed in (self.anchors.changed, self.meters.changed, self.pickup.textChanged, self.json.textChanged):
            changed.connect(self.feedback.hide)
        try:
            self._show_tables(_validated(data))
        except _EDIT_ERRORS as error:
            self._show_json(data)
            self._show_error(error)

    def _pickup(self):
        try:
            value = float(self.pickup.text().strip())
            if not math.isfinite(value) or value < 0:
                raise ValueError
        except ValueError as error:
            raise ValueError("请输入有效的弱起长度，没有弱起时填 0。") from error
        return value

    def _read(self):
        if self._stack.currentWidget() is self.json:
            try:
                return json.loads(self.json.toPlainText())
            except json.JSONDecodeError as error:
                raise ValueError(f"JSON 第 {error.lineno} 行第 {error.colno} 列有语法错误。") from error
        return dict(self._base, anchors=self.anchors.rows(), time_signatures=self.meters.rows(),
                    pickup_beats=self._pickup())

    def _show_tables(self, data):
        self._base = data
        self.anchors.set_rows(data["anchors"])
        self.meters.set_rows(data["time_signatures"])
        self.pickup.setText(str(data.get("pickup_beats", 0)))
        self._stack.setCurrentWidget(self._tabs)
        self.expert.setText("专家 JSON")
        self.feedback.hide()

    def _show_json(self, data):
        self.json.setPlainText(json.dumps(data, ensure_ascii=False, indent=2))
        self._stack.setCurrentWidget(self.json)
        self.expert.setText("返回表格")
        self.feedback.hide()

    def _toggle_expert(self):
        try:
            data = self._read()
            if self._stack.currentWidget() is self.json:
                self._show_tables(_validated(data))
            else:
                self._show_json(data)
        except _EDIT_ERRORS as error:
            self._show_error(error)

    def _add_anchor(self):
        try:
            anchors = self.anchors.rows()
            index = self.anchors.table.currentRow()
            if not 0 <= index < len(anchors) - 1:
                index = len(anchors) - 1
                previous, current = anchors[-2:]
                span = {key: current[key] - previous[key] for key in ("seconds", "beat")}
            else:
                current, following = anchors[index:index + 2]
                span = {key: (following[key] - current[key]) / 2 for key in ("seconds", "beat")}
            if min(span.values()) <= 0:
                raise ValueError("先让锚点的秒时间和拍位按顺序递增。")
            self.anchors.insert(index + 1, {key: current[key] + delta for key, delta in span.items()})
            self.anchors._refresh_bpm((index, index + 1))
            self.anchors.table.setCurrentCell(index + 1, 0)
            self.anchors.table.setFocus()
            self.feedback.hide()
        except _EDIT_ERRORS as error:
            self._show_error(error)

    def _add_meter(self):
        try:
            meters = self.meters.rows()
            current = meters[-1]
            bar_length = 4 * current["numerator"] / current["denominator"]
            offset = (self._pickup() or bar_length) if len(meters) == 1 else current["beat"] + bar_length
            self.meters.insert(len(meters), {"beat": offset, "numerator": current["numerator"],
                                            "denominator": current["denominator"]})
            self.meters.table.setCurrentCell(len(meters), 1)
            self.meters.table.setFocus()
            self.feedback.hide()
        except _EDIT_ERRORS as error:
            self._show_error(error)

    def _show_error(self, error):
        self.feedback.setText(str(error) if not isinstance(error, KeyError)
                              else f"速度图缺少字段 {error.args[0]}。")
        self.feedback.show()

    def accept(self):
        try:
            self.data = _validated(self._read())
        except _EDIT_ERRORS as error:
            self._show_error(error)
            return
        super().accept()
