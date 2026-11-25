"""Refined Astra-styled native Qt dialogs matching Astra's airy pastel UI."""
import sys
from PySide6.QtCore import QEventLoop, QTimer, Qt, Slot
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


def _ui_font(pt: float, *, bold: bool = False) -> QFont:
    """Use static native CJK font + PreferNoHinting to avoid Qt variable-TTF uneven strokes."""
    family = 'Microsoft YaHei UI' if sys.platform == 'win32' and 'Microsoft YaHei UI' in QFontDatabase.families() else ''
    font = QFont(family) if family else QFont()
    font.setPointSizeF(pt)
    font.setWeight(QFont.Weight.Bold if bold else QFont.Weight.Normal)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias | QFont.StyleStrategy.PreferQuality)
    return font


# Exact Astra Studio OKLCH palette (tokens.css):
#   n = lavender-gray neutral (#ffffff, #fafbfe, #f5f6fc, #eceff8, #d9dce9, #41434e)
#   a = azure-indigo action   (#f3f7ff, #e8effe, #acc7fe, #6188da -> #436bc4 -> #5a5fc8)
#   t = time violet           (#f7f5ff, #efedff, #7264a7)
#   w = sakura pending/draft  (#fef3f8, #ffe8f1, #f6d1df, #cf99af, #7d445e)
#   r = soft crimson error    (#fef4f4, #ffe9e9, #fecfcf, #c66c70, #8c3c42)
# Note: Never declare font-size/font-weight in QSS so Qt preserves QFont PreferNoHinting.
_DIALOG_QSS = """
QDialog#AstraCloseDialog {
    background: #ffffff;
    color: #41434e;
}
QWidget#AstraDialogHead {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #f3f7ff, stop:1 #ffffff);
}
QWidget#AstraDialogBody {
    background: #ffffff;
}
QWidget#AstraDialogFoot {
    background: #f5f6fc;
}
QLabel#AstraDialogTitle {
    color: #41434e;
}
QLabel#AstraDialogLead {
    color: #6d707b;
}
QFrame#AstraItemCard {
    background: #f8f9fe;
    border: 1px solid #e6eaf5;
    border-radius: 13px;
}
QFrame#AstraItemCard[tone="sakura"] {
    background: #fef6fa;
    border: 1px solid #f4d6e3;
}
QFrame#AstraItemCard[tone="danger"] {
    background: #fef6f6;
    border: 1px solid #f7d4d4;
}
QLabel#AstraCardTitle {
    color: #41434e;
}
QLabel#AstraCardDetail {
    color: #6d707b;
}
QLabel#AstraCardDetail[tone="danger"] {
    color: #ad4d54;
}
QLabel#AstraCardBadge {
    padding: 2px 10px;
    border-radius: 10px;
    background: #e8effe;
    border: 1px solid #d5e3fd;
    color: #3455a0;
}
QLabel#AstraCardBadge[tone="sakura"] {
    background: #ffe8f1;
    border: 1px solid #f7d1e0;
    color: #7d445e;
}
QLabel#AstraCardBadge[tone="danger"] {
    background: #ffe9e9;
    border: 1px solid #f9cdcd;
    color: #8c3c42;
}
QLabel#AstraCardBadge[tone="violet"] {
    background: #efedff;
    border: 1px solid #dfdafa;
    color: #5b4f87;
}
QPushButton {
    min-height: 34px;
    padding: 0 18px;
    border-radius: 11px;
    background: #ffffff;
    color: #565964;
    border: 1px solid #dce0ee;
}
QPushButton:hover {
    background: #f3f7ff;
    border-color: #acc7fe;
    color: #3455a0;
}
QPushButton:pressed {
    background: #e8effe;
}
QPushButton[role="primary"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #6188da, stop:0.55 #436bc4, stop:1 #5a5fc8);
    color: #ffffff;
    border: 0;
}
QPushButton[role="primary"]:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #557cd1, stop:0.55 #385eb3, stop:1 #4f54bc);
    color: #ffffff;
}
QPushButton[role="primary"]:pressed {
    background: #3455a0;
}
QPushButton[role="danger"] {
    background: transparent;
    color: #ad4d54;
    border: 1px solid transparent;
    padding: 0 14px;
}
QPushButton[role="danger"]:hover {
    background: #ffe9e9;
    border-color: #f3c4c4;
    color: #8c3c42;
}
QPushButton[role="danger"]:pressed {
    background: #fedfde;
}
QProgressBar#AstraBusyBar {
    border: 0;
    border-radius: 4px;
    background: #eceff8;
    max-height: 7px;
    min-height: 7px;
}
QProgressBar#AstraBusyBar::chunk {
    border-radius: 4px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #6188da, stop:0.6 #7264a7, stop:1 #cf99af);
}
QScrollArea#AstraCardScroll {
    background: transparent;
    border: 0;
}
"""


class _AstraTwinDots(QWidget):
    """Signature Astra cute azure + sakura twin dots matching .dialog-head h2::before."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(20, 14)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor('#436bc4'))
        p.drawEllipse(1, 3, 8, 8)
        p.setBrush(QColor('#cf99af'))
        p.drawEllipse(11, 4, 6, 6)
        p.end()


def _styled_button(text, role='secondary'):
    btn = QPushButton(text)
    btn.setProperty('role', role)
    btn.setFont(_ui_font(9.5, bold=False))
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setAutoDefault(role == 'primary')
    btn.setDefault(role == 'primary')
    return btn


def _item_card(title, detail, badge=None, *, card_tone='normal', badge_tone='normal', detail_tone='normal'):
    card = QFrame()
    card.setObjectName('AstraItemCard')
    if card_tone != 'normal':
        card.setProperty('tone', card_tone)
    outer = QVBoxLayout(card)
    outer.setContentsMargins(15, 11, 15, 11)
    outer.setSpacing(5)
    head = QHBoxLayout()
    head.setContentsMargins(0, 0, 0, 0)
    head.setSpacing(8)
    name = QLabel(title)
    name.setObjectName('AstraCardTitle')
    name.setFont(_ui_font(10.0, bold=True))
    name.setWordWrap(True)
    head.addWidget(name, 1)
    if badge:
        pill = QLabel(badge)
        pill.setObjectName('AstraCardBadge')
        pill.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        pill.setFont(_ui_font(8.5, bold=False))
        if badge_tone != 'normal':
            pill.setProperty('tone', badge_tone)
        pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        head.addWidget(pill, 0, Qt.AlignmentFlag.AlignVCenter)
    outer.addLayout(head)
    if detail:
        sub = QLabel(detail)
        sub.setObjectName('AstraCardDetail')
        sub.setFont(_ui_font(9.0, bold=False))
        if detail_tone != 'normal':
            sub.setProperty('tone', detail_tone)
        sub.setWordWrap(True)
        outer.addWidget(sub)
    return card


class _AstraBaseDialog(QDialog):
    def __init__(self, parent, title, *, min_width=456, max_width=532):
        super().__init__(parent)
        self.setObjectName('AstraCloseDialog')
        self.setWindowTitle(title)
        self.setFont(_ui_font(9.5, bold=False))
        self.setWindowModality(Qt.WindowModality.WindowModal if parent is not None else Qt.WindowModality.ApplicationModal)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setStyleSheet(_DIALOG_QSS)
        self.setMinimumWidth(min_width)
        self.setMaximumWidth(max_width)

        self._shell = QVBoxLayout(self)
        self._shell.setContentsMargins(0, 0, 0, 0)
        self._shell.setSpacing(0)

        self._head_widget = QWidget()
        self._head_widget.setObjectName('AstraDialogHead')
        self._head_layout = QVBoxLayout(self._head_widget)
        self._head_layout.setContentsMargins(24, 20, 24, 12)
        self._head_layout.setSpacing(7)
        self._shell.addWidget(self._head_widget)

        self._body_widget = QWidget()
        self._body_widget.setObjectName('AstraDialogBody')
        self._body_layout = QVBoxLayout(self._body_widget)
        self._body_layout.setContentsMargins(24, 4, 24, 20)
        self._body_layout.setSpacing(10)
        self._shell.addWidget(self._body_widget, 1)

        self._foot_widget = QWidget()
        self._foot_widget.setObjectName('AstraDialogFoot')
        self._foot_layout = QHBoxLayout(self._foot_widget)
        self._foot_layout.setContentsMargins(20, 14, 24, 14)
        self._foot_layout.setSpacing(10)
        self._shell.addWidget(self._foot_widget)

    def _add_header(self, heading, lead):
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)
        title_row.addWidget(_AstraTwinDots(), 0, Qt.AlignmentFlag.AlignVCenter)
        title = QLabel(heading)
        title.setObjectName('AstraDialogTitle')
        title.setFont(_ui_font(11.25, bold=True))
        title.setWordWrap(True)
        title_row.addWidget(title, 1)
        self._head_layout.addLayout(title_row)
        if lead:
            body = QLabel(lead)
            body.setObjectName('AstraDialogLead')
            body.setFont(_ui_font(9.25, bold=False))
            body.setWordWrap(True)
            self._head_layout.addWidget(body)

    def _add_cards(self, cards):
        if not cards:
            self._body_widget.hide()
            return
        container = QWidget()
        container.setStyleSheet('background: transparent;')
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(9)
        for card in cards:
            layout.addWidget(card)
        if len(cards) > 4:
            scroll = QScrollArea()
            scroll.setObjectName('AstraCardScroll')
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidget(container)
            scroll.setMaximumHeight(252)
            self._body_layout.addWidget(scroll)
        else:
            self._body_layout.addWidget(container)

    def _add_footer(self, left_buttons, right_buttons):
        if not left_buttons and not right_buttons:
            self._foot_widget.hide()
            return
        for btn in left_buttons:
            self._foot_layout.addWidget(btn)
        self._foot_layout.addStretch(1)
        for btn in right_buttons:
            self._foot_layout.addWidget(btn)


class CloseConfirmDialog(_AstraBaseDialog):
    """Unified Astra close prompt for unsaved projects, structure draft, and beat-recording draft."""

    def __init__(self, parent, *, dirty_items=(), has_structure_draft=False, has_beat_recording=False):
        self.choice = 'cancel'
        dirty_items = list(dirty_items or ())
        multi = len(dirty_items) > 1
        if has_beat_recording and not dirty_items and not has_structure_draft:
            win_title = '录拍草稿尚未应用'
            heading = '录拍草稿还未应用'
            lead = '直接关闭会放弃本次录拍内容。如果想保留拍点调整，请先返回界面点击「应用」。'
        elif has_beat_recording:
            win_title = '录拍草稿与工程修改尚未保存'
            heading = '还有未应用的录拍与工程修改'
            lead = '未应用的录拍不会自动写入工程。你可以先返回继续编辑，或放弃录拍后再处理以下工程：'
        elif has_structure_draft and not dirty_items:
            win_title = '拍位结构草稿尚未应用'
            heading = '拍位结构草稿还未应用'
            lead = '当前的拍位结构调整还没有写入工程。继续编辑，还是放弃草稿并关闭？'
        elif multi:
            win_title = '保存工程修改'
            heading = f'关闭前要保存这 {len(dirty_items)} 个工程吗？'
            lead = '以下工程在本次会话中有尚未保存的更改：'
        else:
            win_title = '保存工程修改'
            name = dirty_items[0]['name'] if dirty_items else '当前工程'
            heading = f'关闭前要保存「{name}」吗？'
            lead = '你最近的编辑还没有写入工程文件，不保存的话这部分修改会丢失。'
            if has_structure_draft:
                lead += ' 未应用的拍位结构草稿会在关闭时放弃。'
        super().__init__(parent, win_title)
        self._add_header(heading, lead)

        cards = []
        if has_beat_recording:
            cards.append(_item_card(
                '拍点录制草稿',
                '尚未点击「应用」；继续关闭将放弃本次录拍与采纳范围。',
                badge='待应用',
                card_tone='sakura',
                badge_tone='sakura',
            ))
        if has_structure_draft:
            cards.append(_item_card(
                '拍位结构草稿',
                '尚未应用到工程；关闭窗口将会放弃此草稿。',
                badge='待应用',
                card_tone='sakura',
                badge_tone='sakura',
            ))
        for item in dirty_items:
            has_err = bool(item.get('error'))
            if has_err:
                badge, btone, ctone, dtone = '上次未保存', 'danger', 'danger', 'danger'
                detail = item['error']
            else:
                badge = '当前工程' if item.get('is_current') else '后台工程'
                btone = 'normal' if item.get('is_current') else 'violet'
                ctone, dtone = 'normal', 'normal'
                detail = item.get('detail') or '编辑尚未正式保存'
            cards.append(_item_card(
                item['name'],
                detail,
                badge=badge,
                card_tone=ctone,
                badge_tone=btone,
                detail_tone=dtone,
            ))
        self._add_cards(cards)

        left, right = [], []
        if has_beat_recording and not dirty_items and not has_structure_draft:
            btn_discard = _styled_button('放弃录拍并关闭', 'danger')
            btn_cancel = _styled_button('返回继续编辑', 'primary')
            btn_discard.clicked.connect(lambda: self._pick('discard'))
            btn_cancel.clicked.connect(lambda: self._pick('cancel'))
            left.append(btn_discard)
            right.append(btn_cancel)
        elif has_beat_recording and dirty_items:
            btn_discard = _styled_button('全部不保存', 'danger')
            btn_save = _styled_button('放弃录拍并保存工程', 'secondary')
            btn_cancel = _styled_button('返回继续编辑', 'primary')
            btn_discard.clicked.connect(lambda: self._pick('discard'))
            btn_save.clicked.connect(lambda: self._pick('discard_recording_and_save'))
            btn_cancel.clicked.connect(lambda: self._pick('cancel'))
            left.append(btn_discard)
            right.extend([btn_save, btn_cancel])
        elif has_beat_recording and has_structure_draft:
            btn_discard = _styled_button('全部放弃并关闭', 'danger')
            btn_cancel = _styled_button('返回继续编辑', 'primary')
            btn_discard.clicked.connect(lambda: self._pick('discard'))
            btn_cancel.clicked.connect(lambda: self._pick('cancel'))
            left.append(btn_discard)
            right.append(btn_cancel)
        elif has_structure_draft and not dirty_items:
            btn_discard = _styled_button('放弃草稿并关闭', 'danger')
            btn_cancel = _styled_button('继续编辑', 'primary')
            btn_discard.clicked.connect(lambda: self._pick('discard'))
            btn_cancel.clicked.connect(lambda: self._pick('cancel'))
            left.append(btn_discard)
            right.append(btn_cancel)
        else:
            btn_discard = _styled_button('不保存' if not multi else '全部不保存', 'danger')
            btn_cancel = _styled_button('取消', 'secondary')
            btn_save = _styled_button('全部保存并关闭' if multi else '保存并关闭', 'primary')
            btn_discard.clicked.connect(lambda: self._pick('discard'))
            btn_cancel.clicked.connect(lambda: self._pick('cancel'))
            btn_save.clicked.connect(lambda: self._pick('save'))
            left.append(btn_discard)
            right.extend([btn_cancel, btn_save])

        self._add_footer(left, right)
        self.adjustSize()

    def _pick(self, choice):
        self.choice = choice
        self.accept()


class CloseSaveErrorDialog(_AstraBaseDialog):
    """Shown when Save-and-Close fails so the user can retry, return, or discard explicitly."""

    def __init__(self, parent, failed_items):
        self.choice = 'cancel'
        failed_items = list(failed_items or ())
        super().__init__(parent, '保存未完成')
        self._add_header(
            '保存还没有完成',
            '以下工程暂时未能确认写入磁盘，窗口会先保留以防丢失修改：',
        )
        cards = [
            _item_card(
                item['name'],
                item.get('error') or '仍有未保存的编辑',
                badge='写入失败',
                card_tone='danger',
                badge_tone='danger',
                detail_tone='danger',
            )
            for item in failed_items
        ]
        self._add_cards(cards)
        btn_discard = _styled_button('不保存并关闭', 'danger')
        btn_cancel = _styled_button('返回编辑器', 'secondary')
        btn_retry = _styled_button('重试保存', 'primary')
        btn_discard.clicked.connect(lambda: self._pick('discard'))
        btn_cancel.clicked.connect(lambda: self._pick('cancel'))
        btn_retry.clicked.connect(lambda: self._pick('retry'))
        self._add_footer([btn_discard], [btn_cancel, btn_retry])
        self.adjustSize()

    def _pick(self, choice):
        self.choice = choice
        self.accept()


class CloseJobDialog(_AstraBaseDialog):
    """Confirmation dialog when closing while background analysis or NCM conversion is running."""

    def __init__(self, parent, *, converting_only=False, has_structure_draft=False):
        self.confirmed = False
        super().__init__(parent, '任务正在运行', min_width=420, max_width=500)
        if converting_only:
            lead = '正在转换本地 NCM 音频。关闭后会在当前文件完成后停止，已输出的文件会保留。'
        else:
            lead = '当前有正在进行的分析或导出任务。关闭后会取消未完成的任务，已完成的导出会保留。'
        if has_structure_draft:
            lead += '\n未应用的拍位结构草稿也会在关闭时放弃。'
        self._add_header('后台任务还在运行，确定关闭吗？', lead)
        self._body_widget.hide()
        btn_close = _styled_button('取消任务并关闭', 'danger')
        btn_keep = _styled_button('继续运行', 'primary')
        btn_close.clicked.connect(self._confirm)
        btn_keep.clicked.connect(self.reject)
        self._add_footer([btn_close], [btn_keep])
        self.adjustSize()

    def _confirm(self):
        self.confirmed = True
        self.accept()


class NativeScoreUnsavedDialog(_AstraBaseDialog):
    """Right-hand (MuseScore) score edits are not part of the MIDI project save."""

    def __init__(self, parent, *, project_name='当前工程', closing=True):
        self.choice = 'cancel'
        action = '关闭窗口' if closing else '切换工程'
        super().__init__(parent, '右侧乐谱尚未保存')
        self._add_header(f'{action}前要保存右侧乐谱吗？',
                         f'「{project_name}」的右侧乐谱有未保存的编辑。'
                         f'{action}会结束这份乐谱文档，不保存的话这部分修改会丢失。')
        self._add_cards([_item_card('右侧乐谱', '乐谱编辑尚未写入工程的乐谱文件', badge='未保存')])
        btn_discard = _styled_button('不保存', 'danger')
        btn_cancel = _styled_button('取消', 'secondary')
        btn_save = _styled_button('保存后继续', 'primary')
        btn_discard.clicked.connect(lambda: self._pick('discard'))
        btn_cancel.clicked.connect(lambda: self._pick('cancel'))
        btn_save.clicked.connect(lambda: self._pick('save'))
        self._add_footer([btn_discard], [btn_cancel, btn_save])
        self.adjustSize()

    def _pick(self, choice):
        self.choice = choice
        self.accept()


class CloseBusyDialog(_AstraBaseDialog):
    """Non-flashing wait dialog for active close inspection or disk saves."""

    def __init__(self, parent, check_done, *, title='正在保存', heading='正在保存工程…',
                 lead='正在将修改安全写入磁盘，马上就好。', cancellable=True, delay_ms=160):
        super().__init__(parent, title, min_width=380, max_width=450)
        self._check_done = check_done
        self._delay_ms = max(0, int(delay_ms))
        self.error = None
        self.cancelled = False
        self._finished = False

        self._add_header(heading, lead)
        bar = QProgressBar()
        bar.setObjectName('AstraBusyBar')
        bar.setRange(0, 0)
        bar.setTextVisible(False)
        self._body_layout.addWidget(bar)
        if cancellable:
            btn_cancel = _styled_button('取消关闭', 'secondary')
            btn_cancel.clicked.connect(self._cancel)
            self._add_footer([], [btn_cancel])
        else:
            self._foot_widget.hide()
            self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        self.adjustSize()

        self._timer = QTimer(self)
        self._timer.setInterval(25)
        self._timer.timeout.connect(self._poll)

    def _cancel(self):
        self.cancelled = True
        self.reject()

    def reject(self):
        if not self._finished:
            self.cancelled = True
        super().reject()

    @Slot()
    def _poll(self):
        try:
            if self._check_done():
                self._finished = True
                self.accept()
        except Exception as exc:
            self.error = exc
            self.reject()

    def run(self):
        try:
            if self._check_done():
                return True
        except Exception:
            raise
        if self._delay_ms > 0:
            loop = QEventLoop(self)
            poll = QTimer(self)
            poll.setInterval(20)
            reveal = QTimer(self)
            reveal.setSingleShot(True)
            reveal.setInterval(self._delay_ms)

            def tick():
                try:
                    if self._check_done():
                        self._finished = True
                        loop.quit()
                except Exception as exc:
                    self.error = exc
                    loop.quit()

            poll.timeout.connect(tick)
            reveal.timeout.connect(loop.quit)
            poll.start()
            reveal.start()
            loop.exec()
            poll.stop()
            reveal.stop()
            if self.error is not None:
                raise self.error
            if self._finished:
                return True
        self._timer.start()
        try:
            result = self.exec()
            if self.error is not None:
                raise self.error
            return result == QDialog.DialogCode.Accepted and not self.cancelled
        finally:
            self._timer.stop()
