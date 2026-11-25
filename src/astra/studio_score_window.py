"""Nonmodal native notation window, sharing the editor's authoritative preview service."""
import base64
import os
from .release_paths import data_root
from pathlib import Path
from PySide6.QtCore import Qt, QTimer, QSettings, QEvent
from PySide6.QtGui import QPixmap, QDesktopServices, QFont
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                              QScrollArea, QGraphicsOpacityEffect, QApplication, QProgressBar)


# Same roles as studio_web/tokens.css (blue-gray neutrals, one action accent, semantic tones).
TOKENS = {'bg':'#fafbfe','surface':'#ffffff','inset':'#f5f6fc','sunken':'#eceef8','line':'#dcdfee','line_soft':'#e5e7f2',
          'text':'#41434e','text2':'#6c6e7b','disabled':'#a8aab6','accent':'#436bc4','accent_hover':'#3455a0','accent_soft':'#e8effe',
          'accent_ink':'#3455a0','focus':'#6a8ad8','ok':'#3455a0','ok_soft':'#f3f7ff','warn':'#7d445e','warn_soft':'#ffe8f1',
          'danger':'#8c3c42','danger_soft':'#ffe9e9','time':'#7264a7','time_soft':'#efedff'}
SCORE_WINDOW_QSS = """
QDialog{{background:{bg};color:{text}}} QLabel{{color:{text}}}
QLabel#kind{{background:{accent_soft};color:{accent_ink};border-radius:10px;padding:2px 10px;font-weight:600}}
QLabel#status{{color:{text2};padding:2px 0}} QLabel#status[tone="working"]{{color:{accent_ink}}}
QLabel#status[tone="ok"]{{color:{ok}}} QLabel#status[tone="stale"]{{color:{warn}}} QLabel#status[tone="error"]{{color:{danger};background:{danger_soft};border-radius:6px;padding:3px 8px}}
QLabel#notice{{color:{warn};background:{warn_soft};border-radius:6px;padding:3px 8px}} QLabel#scope{{color:{text2}}}
QPushButton{{min-height:24px;padding:3px 13px;background:{surface};color:{text};border:1px solid {line};border-radius:12px}}
QPushButton:hover{{background:{inset};border-color:#c1c8d0}} QPushButton:pressed{{background:{sunken}}}
QPushButton:focus{{border:2px solid {focus};padding:2px 12px}} QPushButton:disabled{{color:{disabled};background:{inset};border-color:{line_soft}}}
QPushButton#primary{{background:{accent};color:white;border-color:{accent}}} QPushButton#primary:hover{{background:{accent_hover};border-color:{accent_hover}}}
QPushButton#primary:disabled{{background:{sunken};color:{disabled};border-color:{line_soft}}}
QScrollArea{{background:{sunken};border:1px solid {line_soft};border-radius:14px}}
QScrollBar:vertical{{background:transparent;width:11px;margin:2px}} QScrollBar:horizontal{{background:transparent;height:11px;margin:2px}}
QScrollBar::handle{{background:#c1c8d0;border-radius:4px;min-width:24px;min-height:24px}} QScrollBar::handle:hover{{background:#a5acb3}}
QScrollBar::add-line,QScrollBar::sub-line{{width:0;height:0}} QScrollBar::add-page,QScrollBar::sub-page{{background:none}}
QProgressBar{{border:0;background:{sunken};border-radius:1px}} QProgressBar::chunk{{background:{accent}}}
""".format(**TOKENS)


class ScoreWindow(QDialog):
    def __init__(self, bridge, *, settings=None):
        super().__init__(bridge.parent(), Qt.WindowType.Tool)
        self.bridge=bridge; self.context=None; self.request=None; self.result=None; self.token=None
        self.zoom=1.; self.pixmap=QPixmap(); self.setModal(False);self.checking=False;self.pending_revision=None;self.placeholder='正在准备谱面…'
        self.setFont(QApplication.font())
        self.update_timer=QTimer(self);self.update_timer.setSingleShot(True);self.update_timer.setInterval(350);self.update_timer.timeout.connect(self.refresh)
        self.setSizeGripEnabled(True)
        self.setWindowTitle('看谱 · Astra Studio'); self.resize(820,480); self.setMinimumSize(430,260)
        self.setStyleSheet(SCORE_WINDOW_QSS)
        layout=QVBoxLayout(self);layout.setContentsMargins(14,12,14,12);layout.setSpacing(9)
        head=QHBoxLayout();head.setSpacing(8);layout.addLayout(head)
        self.kind=QLabel('局部预览');self.kind.setObjectName('kind');self.kind.setToolTip('L/R 范围的快速排版草稿，不是正式完整谱面');head.addWidget(self.kind,0,Qt.AlignmentFlag.AlignTop)
        self.status=QLabel('准备中…');self.status.setObjectName('status');self.status.setWordWrap(True);self.status.setTextFormat(Qt.TextFormat.PlainText);head.addWidget(self.status,1)
        self.activity=QProgressBar();self.activity.setRange(0,0);self.activity.setTextVisible(False);self.activity.setFixedHeight(3)
        self.activity.hide();layout.addWidget(self.activity)
        row=QHBoxLayout();row.setSpacing(6);layout.addLayout(row)
        self.prev=self.button(row,'‹',lambda:self.page(-1));self.pages=QLabel('—');row.addWidget(self.pages)
        self.next=self.button(row,'›',lambda:self.page(1));row.addStretch(1);row.addStretch(1)
        self.prev.setToolTip('上一页');self.next.setToolTip('下一页')
        self.prev.setToolTip('上一页');self.next.setToolTip('下一页')
        self.button(row,'−',lambda:self.scale(.8));self.percent=QLabel('100%');row.addWidget(self.percent)
        self.button(row,'+',lambda:self.scale(1.25));self.button(row,'适宽',lambda:self.scale(0))
        self.scroll=QScrollArea();self.scroll.setWidgetResizable(False);self.scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.image=QLabel();self.image.setAlignment(Qt.AlignmentFlag.AlignTop);self.scroll.setWidget(self.image);layout.addWidget(self.scroll,1)
        self.scroll.viewport().installEventFilter(self)
        self.opacity=QGraphicsOpacityEffect(self.image);self.image.setGraphicsEffect(self.opacity)
        self.notation_notice=QLabel();self.notation_notice.setWordWrap(True);self.notation_notice.setObjectName('notice');self.notation_notice.hide();layout.addWidget(self.notation_notice)
        self.scope=QLabel();self.scope.setWordWrap(True);self.scope.setObjectName('scope');layout.addWidget(self.scope)
        bottom=QHBoxLayout();layout.addLayout(bottom);bottom.addStretch(1)
        self.preview_actions=bottom
        self.refresh_button=self.button(bottom,'刷新当前 L/R',self.refresh);self.refresh_button.setObjectName('primary')
        self.open_button=self.button(bottom,'打开 PDF',self.open_pdf)
        self.timer=QTimer(self);self.timer.setInterval(300);self.timer.timeout.connect(self.poll)
        if settings is not None:
            self.settings=settings
        elif os.environ.get('ASTRA_RELEASE_ISOLATED_PROJECTS') == '1':
            self.settings=QSettings(str(data_root()/'.cache'/'score-window.ini'),QSettings.Format.IniFormat)
        else:
            self.settings=QSettings('Astra','ScoreWindow')
        geometry=self.settings.value('geometry')
        if geometry:self.restoreGeometry(geometry)
        self.disable_actions()

    def set_status(self,text,working=False):
        self.status.setText(text);self.activity.setVisible(working)
        tone='working' if working else 'error' if '失败' in text else 'stale' if ('改变' in text or '刷新' in text or '暂不可用' in text) else 'ok'
        if self.status.property('tone')!=tone:
            self.status.setProperty('tone',tone);self.status.style().unpolish(self.status);self.status.style().polish(self.status)
        self.refresh_button.setText('重新生成' if working else '刷新当前 L/R')
        self.refresh_button.setToolTip('后台生成不会阻止编辑；重复点击会替换待处理请求' if working else '按当前左右定位点重新生成局部预览')

    @staticmethod
    def button(row,text,callback):
        button=QPushButton(text);button.clicked.connect(callback);row.addWidget(button);return button

    def visible_geometry(self):
        frame=self.frameGeometry()
        screens=QApplication.screens()
        if not any(screen.availableGeometry().intersects(frame) and screen.availableGeometry().contains(frame.topLeft()) for screen in screens):
            area=self.parentWidget().screen().availableGeometry()
            self.resize(min(self.width(),area.width()-30),min(self.height(),area.height()-30))
            self.move(area.center()-self.rect().center())

    def show_context(self, context):
        self.update_context(context);self.visible_geometry();self.show();self.raise_();self.activateWindow()
        self.timer.start();self.refresh()

    def update_context(self,context):
        if context==self.context:return
        if self.context and context.get('project')!=self.context.get('project'):
            self.close();self.pixmap=QPixmap();self.image.clear();self.result=None
        self.context=dict(context)
        if self.pending_revision:
            self.update_timer.stop()
        if self.request and any(self.context.get(k)!=self.request.get(k) for k in ('project','start','end')):
            self.invalidate('预览范围已改变 · 请刷新')
        elif self.request:
            # A whole-project revision is not proof that this excerpt changed.
            self.poll()

    def disable_actions(self):
        self.prev.setEnabled(False);self.next.setEnabled(False);self.open_button.setEnabled(False)

    def cancel_request(self):
        cancel=getattr(self.bridge,'_score_range_cancel',None)
        if self.token and callable(cancel):cancel(self.token)

    def invalidate(self,text):
        self.cancel_request()
        self.update_timer.stop();self.pending_revision=None;self.checking=False
        self.token=None;self.request=None;self.set_status(text);self.opacity.setOpacity(.45);self.disable_actions()
        self.placeholder='暂无预览 · 请查看上方提示'
        if self.pixmap.isNull():self.render_image()

    def refresh(self):
        self.update_timer.stop();self.pending_revision=None;self.checking=False
        if not self.context or not self.isVisible():return
        self.placeholder='正在准备谱面…'
        self.request=dict(self.context);self.result=None;self.disable_actions();self.opacity.setOpacity(.85 if not self.pixmap.isNull() else 1);self.set_status('局部预览更新中 · 可继续编辑' if not self.pixmap.isNull() else '正在生成局部预览 · 可继续编辑',True)
        if self.pixmap.isNull():self.render_image()
        try:
            response=self.bridge._score_range_preview_start({k:self.context[k] for k in ('base_revision','start','end')})
            self.token=response['token']
            dependency=getattr(self.bridge,'_score_range_request',{})
            if dependency.get('dependency_signature'):self.request['dependency_signature']=dependency['dependency_signature']
            self.timer.start()
        except (ValueError,KeyError,TypeError,OSError,RuntimeError) as exc:self.invalidate(str(exc))

    def poll(self):
        if not self.isVisible():return
        project=getattr(self.bridge,'_project',None)
        if not project or str(Path(project['path']).resolve())!=str(Path(self.context['project']).resolve()):self.close();return
        if not self.token:return
        try:response=self.bridge._score_range_status(self.token)
        except (ValueError,KeyError,TypeError,OSError,RuntimeError) as exc:self.invalidate(str(exc));return
        if response['status']=='failed':self.invalidate('预览更新失败 · '+response.get('error','请手动刷新重试'));return
        if response['status']=='stale':
            self.checking=True;self.disable_actions();self.set_status('局部预览更新中 · 可继续编辑',True);self.opacity.setOpacity(.85)
            target=response.get('current_revision')
            self.pending_revision=target or self.context['base_revision']
            if (not target or target==self.context['base_revision']) and not self.update_timer.isActive():self.update_timer.start()
            return
        if response['status']=='checking':
            self.checking=True;self.disable_actions();self.set_status('正在检查这段谱面 · 可继续编辑',True);self.opacity.setOpacity(.85);return
        if response['status']!='ready':return
        result=response['result']
        if result.get('validated_revision') and result['validated_revision']!=self.context['base_revision']:
            self.checking=True;self.disable_actions();self.set_status('正在核对这段谱面…',True);return
        if not self.request or result.get('validated_revision',result.get('revision'))!=self.context['base_revision'] or result.get('requested_range')!={'start':self.request['start'],'end':self.request['end']}:
            self.invalidate('预览版本不匹配，请刷新');return
        was_checking=self.checking;self.checking=False;self.pending_revision=None;self.update_timer.stop()
        if not was_checking and self.result is result:return
        if not was_checking and self.result and self.result.get('page')==result.get('page') and self.result.get('validated_revision')==result.get('validated_revision'):return
        self.present_pdf(result)

    def present_pdf(self,result,*,require_image=False):
        page=result.get('page',{});data=page.get('dataUrl','')
        pixmap=QPixmap()
        if data.startswith('data:image/'):
            try:pixmap.loadFromData(base64.b64decode(data.split(',',1)[1],validate=True))
            except (ValueError,IndexError):pass
        if require_image and pixmap.isNull():
            return False
        self.result=result
        warnings=[str(item) for item in result.get('notation_warnings',[]) if item]
        self.notation_notice.setVisible(bool(warnings));self.notation_notice.setText('部分谱面标记需核对 · '+str(len(warnings))+' 项')
        self.notation_notice.setToolTip('\n'.join(warnings))
        self.notation_notice.setAccessibleDescription('；'.join(warnings))
        self.placeholder='图像暂不可用 · 可打开 PDF 查看' if result.get('can_open_pdf') and result.get('pdf') else '未生成可显示的谱面 · 请刷新重试'
        self.pixmap=pixmap;self.render_image();self.opacity.setOpacity(1)
        self.set_status('局部草稿 · 未重新识别节奏' if result['preview_kind']=='edited_range_draft' else '局部预览 · 与当前片段一致')
        if pixmap.isNull():self.set_status('局部预览 · 图像暂不可用' if result.get('pdf') else '局部预览 · 暂无结果')
        self.pages.setText(f"{page.get('index',0)+1} / {page.get('pages',0)}" if page.get('pages',0)>0 else '—')
        context=result.get('context_range',{})
        self.scope.setText(f"L/R {self.request['start']:.2f}–{self.request['end']:.2f} s  |  显示 {context.get('first_measure','?')}–{context.get('last_measure','?')} 小节（含上下文）")
        self.scope.setToolTip('生成范围由 L/R 决定；前后小节仅供参考。滚动和缩放不会重新制谱。草稿不替代正式制谱，新音符的时值需复核。')
        self.prev.setEnabled(page.get('index',0)>0);self.next.setEnabled(page.get('index',0)+1<page.get('pages',0))
        self.open_button.setEnabled(result.get('can_open_pdf') is True and bool(result.get('pdf')))
        return True

    def page(self,delta):
        if not self.result or not self.request:return
        request=getattr(self.bridge,'_score_range_request',{})
        try:
            result=self.bridge._score_range_page_start({'token':request.get('token'),'index':self.result['page']['index']+delta})
            self.token=result['token'];self.result=None;self.disable_actions();self.set_status('正在读取页面…',True)
        except (ValueError,KeyError,TypeError,OSError,RuntimeError) as exc:self.invalidate(str(exc))

    def scale(self,multiplier):
        self.zoom=1 if multiplier==0 else max(.5,min(3,self.zoom*multiplier));self.percent.setText(f'{self.zoom*100:.0f}%');self.render_image()

    def render_image(self):
        if self.pixmap.isNull():
            self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff);self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.image.setText(self.placeholder);self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.image.resize(max(100,self.scroll.viewport().width()-4),max(100,self.scroll.viewport().height()-4));return
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded);self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.image.setAlignment(Qt.AlignmentFlag.AlignTop)
        # Match the proportional A4 fit of the live SVG panel at 96 CSS dpi.
        fit = min(round(210 * 96 / 25.4), self.scroll.viewport().width()-4)
        width = max(1, round(fit*self.zoom))
        scaled=self.pixmap.scaledToWidth(width,Qt.TransformationMode.SmoothTransformation)
        self.image.setPixmap(scaled);self.image.resize(scaled.size())

    def eventFilter(self, watched, event):
        if watched is self.scroll.viewport() and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self.render_image)
        return super().eventFilter(watched, event)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'pixmap'):
            self.render_image()
            # Scrollbar visibility settles after layout; fit to the final viewport.
            QTimer.singleShot(0, self.render_image)

    def open_pdf(self):
        self.poll()
        if self.request and self.result and self.open_button.isEnabled():QDesktopServices.openUrl(QUrl.fromLocalFile(self.result['pdf']))

    def closeEvent(self,event):
        self.cancel_request()
        self.settings.setValue('geometry',self.saveGeometry());self.activity.hide();self.update_timer.stop();self.pending_revision=None;self.timer.stop();self.token=None;self.request=None
        super().closeEvent(event)
