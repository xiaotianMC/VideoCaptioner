"""Video preview and a draggable subtitle timeline using Qt's media backend."""

import json
import math
from pathlib import Path

from PyQt5.QtCore import QIODevice, QRectF, QSaveFile, Qt, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import QColor, QKeySequence, QPainter, QPen
from PyQt5.QtMultimedia import QMediaContent, QMediaPlayer
from PyQt5.QtMultimediaWidgets import QVideoWidget
from PyQt5.QtWidgets import (
    QAbstractScrollArea,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QShortcut,
    QSplitter,
    QStackedWidget,
    QTableWidgetItem,
    QUndoCommand,
    QUndoStack,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    BodyLabel,
    CaptionLabel,
    CardWidget,
    CommandBar,
    DoubleSpinBox,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    Slider,
    SmoothScrollDelegate,
    TableWidget,
    isDarkTheme,
    qconfig,
    themeColor,
)
from qfluentwidgets import FluentIcon as FIF

from videocaptioner.config import APPDATA_PATH
from videocaptioner.core.subtitle.timeline import SubtitleTimeline, adjusted_times
from videocaptioner.ui.common.config import cfg
from videocaptioner.ui.components.ffmpeg_preview import FFmpegPreview, PreviewFrame


def time_label(ms):
    seconds, millis = divmod(max(0, ms), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}.{millis:03}"


def atomic_write(path, text):
    file = QSaveFile(str(path))
    if not file.open(QIODevice.WriteOnly):
        raise OSError(file.errorString())
    payload = text.encode("utf-8")
    if file.write(payload) != len(payload) or not file.commit():
        raise OSError(file.errorString())


class SubtitleTrack(QAbstractScrollArea):
    selected = pyqtSignal(int)
    seek = pyqtSignal(int)
    edited = pyqtSignal(int, int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.document = SubtitleTimeline()
        self.duration = 0
        self.position = 0
        self.scale = 80
        self.current = -1
        self.drag = None
        self.setMinimumHeight(196)
        self.setMaximumHeight(240)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setFrameShape(self.NoFrame)
        self.scroll_delegate = SmoothScrollDelegate(self)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.horizontalScrollBar().valueChanged.connect(lambda _: self.viewport().update())
        qconfig.themeChangedFinished.connect(self.refresh)
        cfg.themeColorChanged.connect(self.refresh)
        self.setAccessibleName("字幕时间轨道")

    def total(self):
        return max(
            self.duration, max((s.end_time for s in self.document.data), default=0) + 3000, 10000
        )

    def refresh(self):
        width = max(1, self.viewport().width() - 80)
        self.horizontalScrollBar().setRange(
            0, max(0, int(self.total() * self.scale / 1000) - width)
        )
        self.horizontalScrollBar().setPageStep(width)
        self.viewport().update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.refresh()

    def x(self, ms):
        return 80 + ms * self.scale / 1000 - self.horizontalScrollBar().value()

    def ms(self, x):
        return max(
            0,
            min(
                self.total(),
                round((x - 80 + self.horizontalScrollBar().value()) * 1000 / self.scale),
            ),
        )

    def clip_rect(self, index, lane):
        seg = self.document.data.segments[index]
        start, end = seg.start_time, seg.end_time
        if self.drag and self.drag[0] == index:
            start, end = self.drag[4:6]
        return QRectF(self.x(start), 40 + lane * 58, max(3, (end - start) * self.scale / 1000), 44)

    def lanes(self, index):
        seg = self.document.data.segments[index]
        if self.document.ass_events:
            return [self.document.lane(index)]
        return [0, 1] if seg.translated_text else [0]

    def paintEvent(self, event):
        dark = isDarkTheme()
        accent = themeColor()
        background = QColor("#292929" if dark else "#ffffff")
        surface = QColor("#323232" if dark else "#f7f7f7")
        border = QColor("#454545" if dark else "#e5e5e5")
        text_color = QColor("#ffffff" if dark else "#1a1a1a")
        muted = QColor("#c5c5c5" if dark else "#606060")
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.viewport().rect(), background)
        p.setClipRect(80, 0, max(0, self.viewport().width() - 80), self.viewport().height())
        p.fillRect(80, 0, self.viewport().width() - 80, 32, surface)
        p.fillRect(80, 94, self.viewport().width() - 80, 54, surface)
        step = 1000 * max(1, math.ceil(65 / self.scale))
        first = self.ms(80) // step * step
        for ms in range(first, self.ms(self.viewport().width()) + step, step):
            x = self.x(ms)
            p.setPen(border)
            p.drawLine(int(x), 30, int(x), 153)
            p.setPen(muted)
            p.drawText(QRectF(x + 4, 4, 90, 22), time_label(ms)[:-4])
        for index, seg in enumerate(self.document.data):
            for lane in self.lanes(index):
                rect = self.clip_rect(index, lane)
                if rect.right() < 80 or rect.left() > self.viewport().width():
                    continue
                fill = QColor(accent)
                fill.setAlpha(50 if index == self.current else 26 if lane == 0 else 15)
                p.setBrush(fill)
                p.setPen(
                    QPen(
                        accent if index == self.current else border,
                        2 if index == self.current else 1,
                    )
                )
                p.drawRoundedRect(rect, 5, 5)
                p.setPen(text_color)
                text = self.document.text(index, lane == 1 and not self.document.ass_events)
                p.save()
                p.setClipRect(rect.adjusted(7, 0, -7, 0), Qt.IntersectClip)
                p.drawText(rect.adjusted(9, 4, -6, -4), Qt.AlignVCenter, text.replace("\n", " / "))
                p.restore()
                p.setPen(accent if index == self.current else muted)
                for x in (rect.left() + 4, rect.right() - 4):
                    p.drawLine(int(x), int(rect.top() + 17), int(x), int(rect.bottom() - 17))
        p.setPen(QPen(accent, 2))
        p.drawLine(int(self.x(self.position)), 28, int(self.x(self.position)), 154)
        p.setClipping(False)
        p.fillRect(0, 0, 79, self.viewport().height(), surface)
        p.setPen(border)
        p.drawLine(79, 0, 79, self.viewport().height())
        p.setPen(text_color)
        p.drawText(QRectF(8, 40, 65, 44), Qt.AlignVCenter, "字幕")
        p.drawText(QRectF(8, 98, 65, 44), Qt.AlignVCenter, "副字幕")
        p.setPen(muted)
        p.drawText(
            QRectF(8, 163, self.viewport().width() - 8, 22),
            "拖动边缘调时长 · 拖动中间移动 · 点击刻度定位",
        )
        p.setBrush(Qt.NoBrush)
        p.setPen(border)
        p.drawRoundedRect(QRectF(self.viewport().rect()).adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)

    def hit(self, point):
        if point.x() < 80:
            return None
        # The selected clip wins in overlaps so its trim handles remain usable.
        order = list(range(len(self.document.data)))
        if self.current in order:
            order.remove(self.current)
            order.append(self.current)
        for index in reversed(order):
            for lane in self.lanes(index):
                rect = self.clip_rect(index, lane)
                if rect.contains(point):
                    edge = (
                        "start"
                        if point.x() - rect.left() < 8
                        else "end"
                        if rect.right() - point.x() < 8
                        else "move"
                    )
                    return index, edge
        return None

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton or event.x() < 80:
            return
        hit = self.hit(event.pos())
        if hit:
            index, edge = hit
            self.current = index
            self.selected.emit(index)
            seg = self.document.data.segments[index]
            self.drag = (
                index,
                edge,
                self.ms(event.x()),
                seg.start_time,
                seg.start_time,
                seg.end_time,
                seg.end_time,
            )
        else:
            self.seek.emit(self.ms(event.x()))
        self.viewport().update()

    def mouseMoveEvent(self, event):
        if self.drag:
            index, edge, anchor, start, _, _, end = self.drag
            new_start, new_end = adjusted_times(
                start, end, self.ms(event.x()) - anchor, edge, self.duration
            )
            self.drag = (index, edge, anchor, start, new_start, new_end, end)
            self.viewport().update()
            self.setToolTip(f"{time_label(new_start)} → {time_label(new_end)}")
        else:
            hit = self.hit(event.pos())
            self.viewport().setCursor(
                Qt.SizeHorCursor
                if hit and hit[1] != "move"
                else Qt.OpenHandCursor
                if hit
                else Qt.ArrowCursor
            )

    def mouseReleaseEvent(self, event):
        if self.drag:
            index, _, _, _, start, end, _ = self.drag
            self.drag = None
            self.edited.emit(index, start, end)
            self.viewport().update()

    def set_position(self, ms, follow=False):
        self.position = ms
        if follow and (self.x(ms) < 80 or self.x(ms) > self.viewport().width() - 20):
            self.horizontalScrollBar().setValue(max(0, int(ms * self.scale / 1000) - 100))
        self.viewport().update()


class EditSubtitle(QUndoCommand):
    def __init__(self, editor, index, state):
        super().__init__("调整字幕")
        self.editor, self.index = editor, index
        self.before, self.after = editor.document.state(index), state

    def redo(self):
        self.editor.apply_state(self.index, self.after)

    def undo(self):
        self.editor.apply_state(self.index, self.before)


class SubtitleEditInterface(QWidget):
    finished = pyqtSignal(str, str)

    def __init__(self, parent=None, draft_path=None):
        super().__init__(parent)
        self.setObjectName("SubtitleEditInterface")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.document = SubtitleTimeline()
        self.video_path = ""
        self.subtitle_path = ""
        self.export_path = ""
        self.current = -1
        self.media_duration = 0
        self.playback_requested = False
        self.unsaved_changes = False
        self.draft_path = (
            Path(draft_path) if draft_path else APPDATA_PATH / "subtitle-edit-draft.json"
        )
        self.undo_stack = QUndoStack(self)
        self.player = QMediaPlayer(self, QMediaPlayer.VideoSurface)
        self.native_player = self.player
        self.player.setNotifyInterval(40)
        self.media_watchdog = QTimer(self)
        self.media_watchdog.setSingleShot(True)
        self.media_watchdog.setInterval(5000)
        self.media_watchdog.timeout.connect(self.use_ffmpeg_preview)
        self.autosave = QTimer(self)
        self.autosave.setSingleShot(True)
        self.autosave.setInterval(300)
        self.autosave.timeout.connect(self.save_draft)
        self.setup_ui()
        qconfig.themeChangedFinished.connect(self.update_theme)
        self.update_theme()
        self.connect_player()
        self.player.error.connect(self.media_error)
        self.undo_stack.canUndoChanged.connect(self.undo_button.setEnabled)
        self.undo_stack.canRedoChanged.connect(self.redo_button.setEnabled)
        QShortcut(QKeySequence.Undo, self, activated=self.undo_stack.undo)
        QShortcut(QKeySequence.Redo, self, activated=self.undo_stack.redo)
        # aboutToQuit also runs when the parent window quits without closing its pages.
        from PyQt5.QtWidgets import QApplication

        QApplication.instance().aboutToQuit.connect(self.shutdown)
        self.restore_draft()

    def update_theme(self):
        background = "#202020" if isDarkTheme() else "#f9f9f9"
        self.setStyleSheet(f"#SubtitleEditInterface {{ background: {background}; }}")

    def connect_player(self):
        self.player.positionChanged.connect(self.position_changed)
        self.player.durationChanged.connect(self.duration_changed)
        self.player.stateChanged.connect(self.play_state_changed)

    def play_state_changed(self, state):
        self.play_button.setText("暂停" if state == QMediaPlayer.PlayingState else "播放")
        self.play_button.setIcon(FIF.PAUSE if state == QMediaPlayer.PlayingState else FIF.PLAY)

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(6)
        toolbar = QHBoxLayout()
        self.command_bar = CommandBar(self)
        self.command_bar.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        for icon, text, slot in [
            (FIF.FOLDER, "打开视频", self.open_video),
            (FIF.ADD, "导入字幕", self.open_subtitles),
            (FIF.SAVE, "另存字幕", self.save_as),
        ]:
            self.command_bar.addAction(Action(icon, text, triggered=slot))
        self.command_bar.addSeparator()
        self.undo_button = Action(FIF.RETURN, "撤销", triggered=self.undo_stack.undo)
        self.redo_button = Action(FIF.ROTATE, "重做", triggered=self.undo_stack.redo)
        self.undo_button.setEnabled(False)
        self.redo_button.setEnabled(False)
        self.command_bar.addAction(self.undo_button)
        self.command_bar.addAction(self.redo_button)
        toolbar.addWidget(self.command_bar, 1)
        self.continue_button = PrimaryPushButton("保存并继续合成", self, icon=FIF.CHEVRON_RIGHT)
        self.continue_button.setFixedHeight(34)
        self.continue_button.clicked.connect(self.continue_synthesis)
        toolbar.addWidget(self.continue_button)
        layout.addLayout(toolbar)
        self.files_label = CaptionLabel("导入视频和字幕，或等待上一步完成", self)
        self.files_label.setWordWrap(True)
        layout.addWidget(self.files_label)

        split = QSplitter(Qt.Horizontal, self)
        split.setHandleWidth(12)
        split.setChildrenCollapsible(False)
        preview = CardWidget(split)
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(6, 6, 6, 6)
        preview_layout.setSpacing(0)
        self.video_widget = QVideoWidget(preview)
        self.video_widget.setMinimumSize(240, 140)
        self.video_widget.setStyleSheet("background: black;")
        self.player.setVideoOutput(self.video_widget)
        self.preview_stack = QStackedWidget(preview)
        self.preview_stack.addWidget(self.video_widget)
        self.fallback_frame = PreviewFrame(preview)
        self.preview_stack.addWidget(self.fallback_frame)
        preview_layout.addWidget(self.preview_stack, 1)
        self.caption = QLabel("字幕同步预览", preview)
        self.caption.setAlignment(Qt.AlignCenter)
        self.caption.setWordWrap(True)
        self.caption.setMinimumHeight(58)
        self.caption.setStyleSheet(
            "background: black; color: white; font-size: 17px; padding: 8px;"
        )
        self.caption.setTextFormat(Qt.PlainText)
        preview_layout.addWidget(self.caption)
        split.addWidget(preview)

        inspector = CardWidget(split)
        form = QFormLayout(inspector)
        form.setContentsMargins(12, 12, 12, 12)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(6)
        self.start_input, self.end_input = DoubleSpinBox(self), DoubleSpinBox(self)
        for spin, name in [(self.start_input, "字幕开始时间"), (self.end_input, "字幕结束时间")]:
            spin.setRange(0, 359999.999)
            spin.setDecimals(3)
            spin.setSingleStep(0.1)
            spin.setSuffix(" 秒")
            spin.setAccessibleName(name)
        form.addRow(BodyLabel("开始", self), self.start_input)
        form.addRow(BodyLabel("结束", self), self.end_input)
        self.text_input, self.translation_input = PlainTextEdit(self), PlainTextEdit(self)
        self.text_input.setMaximumHeight(64)
        self.translation_input.setMaximumHeight(60)
        form.addRow(BodyLabel("字幕", self), self.text_input)
        form.addRow(BodyLabel("译文", self), self.translation_input)
        self.translation_label = form.labelForField(self.translation_input)
        self.translation_label.hide()
        self.translation_input.hide()
        self.apply_button = PushButton("应用修改", self, icon=FIF.ACCEPT)
        self.apply_button.clicked.connect(self.apply_inputs)
        form.addRow(self.apply_button)
        split.addWidget(inspector)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        layout.addWidget(split, 3)

        transport = QHBoxLayout()
        self.play_button = PushButton("播放", self, icon=FIF.PLAY)
        self.play_button.clicked.connect(self.toggle_play)
        transport.addWidget(self.play_button)
        self.backend_label = CaptionLabel("", self)
        transport.addWidget(self.backend_label)
        self.time_display = BodyLabel("00:00:00.000 / 00:00:00.000", self)
        transport.addWidget(self.time_display)
        transport.addStretch()
        transport.addWidget(BodyLabel("轨道缩放", self))
        self.zoom = Slider(Qt.Horizontal, self)
        self.zoom.setRange(20, 300)
        self.zoom.setValue(80)
        self.zoom.setMaximumWidth(160)
        self.zoom.valueChanged.connect(self.zoom_changed)
        transport.addWidget(self.zoom)
        layout.addLayout(transport)
        self.track = SubtitleTrack(self)
        self.track.selected.connect(self.select_row)
        self.track.seek.connect(self.seek)
        self.track.edited.connect(self.trim)
        layout.addWidget(self.track)

        self.table = TableWidget(self)
        self.table.setColumnCount(4)
        self.table.setMinimumHeight(100)
        self.table.setHorizontalHeaderLabels(["开始", "结束", "时长", "字幕内容"])
        self.table.setSelectionBehavior(self.table.SelectRows)
        self.table.setSelectionMode(self.table.SingleSelection)
        self.table.setEditTriggers(self.table.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 110)
        self.table.setColumnWidth(1, 110)
        self.table.setColumnWidth(2, 75)
        self.table.itemSelectionChanged.connect(self.table_selected)
        layout.addWidget(self.table, 2)
        self.status = CaptionLabel("拖动字幕左右边缘调整时长；右侧可精确编辑到毫秒。", self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.set_controls()

    def error(self, message):
        self.status.setText(str(message))
        QMessageBox.warning(self, "字幕剪辑", str(message))

    def set_controls(self):
        loaded = bool(len(self.document.data))
        self.apply_button.setEnabled(loaded and self.current >= 0)
        self.continue_button.setEnabled(loaded and bool(self.video_path))
        self.play_button.setEnabled(bool(self.video_path))

    def update_files(self):
        self.files_label.setText(
            f"视频：{Path(self.video_path).name if self.video_path else '未选择'}    字幕：{Path(self.subtitle_path).name if self.subtitle_path else '未选择'}"
        )
        self.files_label.setToolTip(f"{self.video_path}\n{self.subtitle_path}")
        self.set_controls()

    def open_video(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "打开视频", "", "视频 (*.mp4 *.mkv *.mov *.avi *.webm *.m4v);;所有文件 (*)"
        )
        if path:
            self.set_video(path)
            self.save_draft()

    def set_video(self, path):
        self.playback_requested = False
        self.player.stop()
        self.video_path = str(path)
        self.media_duration = 0
        self.track.duration = 0
        self.player.setMedia(QMediaContent(QUrl.fromLocalFile(str(Path(path).resolve()))))
        if self.player is self.native_player:
            self.media_watchdog.start()
        self.update_files()
        self.track.refresh()

    def open_subtitles(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入字幕", "", "字幕 (*.srt *.ass *.json)")
        if path:
            self.load_subtitles(path)

    def load_subtitles(self, path):
        if self.unsaved_changes:
            choice = QMessageBox.question(
                self,
                "保留当前剪辑",
                "当前剪辑尚未导出，是否先保存字幕？",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if choice == QMessageBox.Cancel or (choice == QMessageBox.Save and not self.save_as()):
                return False
        # Flush the current draft before replacing it; source files stay untouched.
        if len(self.document.data) and not self.save_draft():
            return False
        try:
            document = SubtitleTimeline.load(path)
        except Exception as exc:
            self.error(f"字幕导入失败：{exc}")
            return False
        self.document = document
        self.subtitle_path = str(path)
        self.export_path = ""
        self.unsaved_changes = False
        self.current = -1
        self.undo_stack.clear()
        self.refresh_table()
        self.update_files()
        self.select_row(0)
        self.save_draft()
        return True

    def load_task(self, video_path, subtitle_path):
        if self.load_subtitles(subtitle_path):
            if video_path:
                self.set_video(video_path)
            else:
                self.player.stop()
                self.player.setMedia(QMediaContent())
                self.video_path = ""
                self.duration_changed(0)
                self.update_files()
            self.save_draft()
            return True
        return False

    def refresh_table(self):
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.document.data))
        for row in range(len(self.document.data)):
            self.update_row(row)
        self.table.blockSignals(False)
        self.track.document = self.document
        self.track.refresh()
        self.set_controls()

    def update_row(self, row):
        seg = self.document.data.segments[row]
        texts = [
            time_label(seg.start_time),
            time_label(seg.end_time),
            f"{(seg.end_time - seg.start_time) / 1000:.3f}s",
            self.document.text(row).replace("\n", " / "),
        ]
        for col, text in enumerate(texts):
            self.table.setItem(row, col, QTableWidgetItem(text))

    def table_selected(self):
        row = self.table.currentRow()
        if row >= 0:
            self.select_row(row)

    def select_row(self, row):
        if not 0 <= row < len(self.document.data):
            return
        self.current = self.track.current = row
        if self.table.currentRow() != row:
            self.table.blockSignals(True)
            self.table.selectRow(row)
            self.table.blockSignals(False)
        seg = self.document.data.segments[row]
        self.start_input.setValue(seg.start_time / 1000)
        self.end_input.setValue(seg.end_time / 1000)
        self.text_input.setPlainText(seg.text)
        self.translation_input.setPlainText(seg.translated_text)
        self.translation_input.setEnabled(not bool(self.document.ass_events))
        show_translation = bool(seg.translated_text) and not self.document.ass_events
        self.translation_label.setVisible(show_translation)
        self.translation_input.setVisible(show_translation)
        self.seek(seg.start_time)
        self.set_controls()

    def apply_inputs(self):
        if self.current < 0:
            return
        state = (
            round(self.start_input.value() * 1000),
            round(self.end_input.value() * 1000),
            self.text_input.toPlainText(),
            self.translation_input.toPlainText(),
        )
        if state[1] <= state[0] or not state[2].strip():
            self.error("字幕内容不能为空，结束时间必须晚于开始时间。")
            return
        if self.media_duration and state[1] > self.media_duration:
            self.error("字幕结束时间不能超过视频时长。")
            return
        self.commit(self.current, state)

    def trim(self, row, start, end):
        state = self.document.state(row)
        self.commit(row, (start, end, state[2], state[3]))

    def commit(self, row, state):
        if self.document.state(row) != state:
            self.undo_stack.push(EditSubtitle(self, row, state))

    def apply_state(self, row, state):
        self.document.set_state(row, state)
        self.unsaved_changes = True
        self.table.blockSignals(True)
        self.update_row(row)
        self.table.blockSignals(False)
        self.track.refresh()
        self.select_row(row)
        self.autosave.start()
        self.status.setText("修改已应用，正在保存草稿…")

    def toggle_play(self):
        if self.player.state() == QMediaPlayer.PlayingState:
            self.playback_requested = False
            self.player.pause()
        else:
            self.playback_requested = True
            self.player.play()

    def seek(self, ms):
        self.player.setPosition(ms)
        self.track.set_position(ms, True)
        self.position_changed(ms)

    def position_changed(self, ms):
        self.track.set_position(ms, self.player.state() == QMediaPlayer.PlayingState)
        self.time_display.setText(f"{time_label(ms)} / {time_label(self.media_duration)}")
        texts = []
        for row, seg in enumerate(self.document.data):
            if seg.start_time <= ms < seg.end_time:
                texts.append(self.document.text(row))
                if seg.translated_text:
                    texts.append(self.document.text(row, True))
        self.caption.setText("\n".join(texts))

    def duration_changed(self, ms):
        self.media_duration = max(0, ms)
        self.track.duration = self.media_duration
        self.track.refresh()
        self.position_changed(self.player.position())
        if ms > 0:
            self.media_watchdog.stop()

    def media_error(self, *_):
        if self.native_player.error() != QMediaPlayer.NoError:
            self.use_ffmpeg_preview()

    def use_ffmpeg_preview(self):
        if self.player is not self.native_player or not self.video_path:
            return
        self.media_watchdog.stop()
        was_playing = self.playback_requested or self.player.state() == QMediaPlayer.PlayingState
        position = max(self.player.position(), self.track.position)
        self.player.positionChanged.disconnect(self.position_changed)
        self.player.durationChanged.disconnect(self.duration_changed)
        self.player.stateChanged.disconnect(self.play_state_changed)
        self.player.error.disconnect(self.media_error)
        self.player.stop()
        self.player.setMedia(QMediaContent())
        self.player = FFmpegPreview(self)
        self.connect_player()
        self.player.frameReady.connect(self.fallback_frame.set_image)
        self.player.failed.connect(lambda message: self.status.setText("视频预览失败：" + message))
        self.preview_stack.setCurrentWidget(self.fallback_frame)
        self.backend_label.setText("FFmpeg 画面预览（无声）")
        self.backend_label.setToolTip("本机 Qt 播放器无法解码此视频，使用 FFmpeg 预览画面。")
        self.player.setMedia(
            QMediaContent(QUrl.fromLocalFile(str(Path(self.video_path).resolve())))
        )
        if position:
            self.player.setPosition(position)
        if was_playing:
            self.player.play()

    def zoom_changed(self, value):
        self.track.scale = value
        self.track.refresh()
        self.track.set_position(self.track.position, True)

    def save_as(self):
        if not len(self.document.data):
            self.error("请先导入字幕。")
            return False
        source = Path(self.subtitle_path)
        default = self.export_path or str(
            source.with_name(
                source.stem + ".edited" + (".ass" if self.document.ass_events else ".srt")
            )
        )
        path, selected = QFileDialog.getSaveFileName(
            self,
            "另存字幕",
            default,
            "ASS 字幕 (*.ass);;SRT 字幕 (*.srt)"
            if self.document.ass_events
            else "SRT 字幕 (*.srt);;ASS 字幕 (*.ass)",
        )
        if not path:
            return False
        if not Path(path).suffix:
            path += ".ass" if selected.startswith("ASS") else ".srt"
        return self.export_to(path)

    def export_to(self, path):
        try:
            atomic_write(path, self.document.export_text(Path(path).suffix))
        except Exception as exc:
            self.error(f"字幕保存失败：{exc}")
            return False
        self.export_path = str(path)
        self.unsaved_changes = False
        self.save_draft()
        self.status.setText(f"已保存：{path}")
        return True

    def continue_synthesis(self):
        if not self.video_path or not Path(self.video_path).is_file():
            self.error("请先选择存在的视频文件。")
            return
        if not len(self.document.data):
            self.error("请先导入字幕。")
            return
        if self.export_path:
            saved = self.export_to(self.export_path)
        else:
            saved = self.save_as()
        if saved:
            self.player.pause()
            self.finished.emit(self.video_path, self.export_path)

    def save_draft(self):
        self.autosave.stop()
        if not len(self.document.data):
            return True
        try:
            self.draft_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(
                self.draft_path,
                json.dumps(
                    {
                        "version": 1,
                        "document": self.document.to_dict(),
                        "video_path": self.video_path,
                        "subtitle_path": self.subtitle_path,
                        "export_path": self.export_path,
                        "unsaved_changes": self.unsaved_changes,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        except Exception as exc:
            self.status.setText(f"草稿保存失败，请另存字幕：{exc}")
            return False
        self.status.setText("草稿已自动保存；重启可恢复。导出字幕请点击“另存字幕”。")
        return True

    def restore_draft(self):
        if not self.draft_path.is_file():
            return
        try:
            value = json.loads(self.draft_path.read_text(encoding="utf-8"))
            if value.get("version") != 1:
                raise ValueError("不支持的草稿版本")
            self.document = SubtitleTimeline.from_dict(value["document"])
            self.subtitle_path = value.get("subtitle_path", "")
            self.export_path = value.get("export_path", "")
            self.unsaved_changes = value.get("unsaved_changes", True)
            self.refresh_table()
            video = value.get("video_path", "")
            if video and Path(video).is_file():
                self.set_video(video)
            self.update_files()
            self.select_row(0)
            self.status.setText(
                "已恢复上次剪辑草稿。"
                + ("请重新选择视频。" if video and not self.video_path else "")
            )
        except Exception as exc:
            self.status.setText(f"草稿恢复失败（原文件保留）：{exc}")

    def hideEvent(self, event):
        self.playback_requested = False
        self.player.pause()
        self.save_draft()
        super().hideEvent(event)

    def shutdown(self):
        self.media_watchdog.stop()
        self.save_draft()
        self.player.stop()

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)
