"""Run directly: python -m unittest discover -s tests/test_subtitle -p test_timeline_editor.py."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QEvent, QPoint, Qt, QUrl
from PyQt5.QtGui import QMouseEvent
from PyQt5.QtMultimedia import QMediaContent, QMediaPlayer
from PyQt5.QtTest import QSignalSpy, QTest
from PyQt5.QtWidgets import QApplication

from videocaptioner.core.subtitle.timeline import SubtitleTimeline, adjusted_times
from videocaptioner.ui.components.ffmpeg_preview import FFmpegPreview
from videocaptioner.ui.view.subtitle_edit_interface import SubtitleEditInterface

ASS = """[Script Info]
PlayResX: 1920
PlayResY: 1080
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Custom,Arial,40,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,-1,0,0,90,110,2,15,1,2,3,8,20,20,40,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Comment: 0,0:00:00.00,0:00:01.00,Custom,,0,0,0,,keep this comment
Dialogue: 2,0:00:02.00,0:00:04.00,Custom,actor,10,20,30,,{\\i1}Hello, world\\NSecond line
Dialogue: 0,0:00:00.50,0:00:01.50,Secondary,,0,0,0,,翻译
"""
SRT = (
    "1\n00:00:00,500 --> 00:00:02,000\n第一句字幕\n\n2\n00:00:03,000 --> 00:00:05,000\n第二句字幕\n"
)


class TimelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source.srt"
        self.source.write_text(SRT, encoding="utf-8")
        self.widgets = []

    def tearDown(self):
        for widget in self.widgets:
            widget.close()
            widget.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def editor(self, draft="draft.json"):
        widget = SubtitleEditInterface(draft_path=self.root / draft)
        self.widgets.append(widget)
        return widget

    def test_drag_bounds(self):
        self.assertEqual(adjusted_times(100, 1000, -500, "move"), (0, 900))
        self.assertEqual(adjusted_times(100, 1000, 3000, "move", 2000), (1100, 2000))
        self.assertEqual(adjusted_times(100, 1000, 3000, "start"), (990, 1000))
        self.assertEqual(adjusted_times(100, 1000, -3000, "end"), (100, 110))
        self.assertEqual(adjusted_times(100, 1000, 3000, "end", 2000), (100, 2000))

    def test_ass_retains_styles_events_and_order_after_restore(self):
        path = self.root / "custom.ass"
        path.write_text(ASS, encoding="utf-8-sig")
        document = SubtitleTimeline.load(path)
        self.assertEqual(document.state(0)[:2], (2000, 4000))
        self.assertEqual(document.lane(1), 1)
        self.assertEqual(document.text(0), "Hello, world\nSecond line")
        state = document.state(0)
        document.set_state(0, (2201, 4809, state[2], state[3]))
        self.assertEqual(document.state(0)[:2], (2200, 4800))
        restored = SubtitleTimeline.from_dict(json.loads(json.dumps(document.to_dict())))
        output = restored.export_text(".ass")
        expected = ASS.replace(
            "Dialogue: 2,0:00:02.00,0:00:04.00", "Dialogue: 2,0:00:02.20,0:00:04.80"
        )
        self.assertEqual(output, expected)
        self.assertEqual(path.read_text(encoding="utf-8-sig"), ASS)

    def test_trim_undo_redo_and_disk_recovery(self):
        editor = self.editor()
        self.assertTrue(editor.load_subtitles(str(self.source)))
        editor.trim(0, 750, 2500)
        self.assertEqual(editor.document.state(0)[:2], (750, 2500))
        editor.undo_stack.undo()
        self.assertEqual(editor.document.state(0)[:2], (500, 2000))
        editor.undo_stack.redo()
        editor.save_draft()
        restored = self.editor()
        self.assertEqual(restored.document.state(0)[:2], (750, 2500))
        output = self.root / "export.srt"
        self.assertTrue(restored.export_to(str(output)))
        self.assertEqual(SubtitleTimeline.load(output).state(0)[:2], (750, 2500))
        self.assertEqual(self.source.read_text(encoding="utf-8"), SRT)

    def test_mouse_trim_and_move_commit_once(self):
        editor = self.editor()
        editor.resize(1100, 850)
        editor.show()
        editor.load_subtitles(str(self.source))
        self.app.processEvents()
        track = editor.track
        viewport = track.viewport()
        rect = track.clip_rect(0, 0)
        start = QPoint(round(rect.right() - 2), round(rect.center().y()))
        finish = start + QPoint(40, 0)
        QTest.mousePress(viewport, Qt.LeftButton, pos=start)
        self.app.sendEvent(
            viewport,
            QMouseEvent(QEvent.MouseMove, finish, Qt.NoButton, Qt.LeftButton, Qt.NoModifier),
        )
        QTest.mouseRelease(viewport, Qt.LeftButton, pos=finish)
        self.assertEqual(editor.document.state(0)[:2], (500, 2500))
        self.assertEqual(editor.undo_stack.count(), 1)
        rect = track.clip_rect(0, 0)
        start = QPoint(round(rect.center().x()), round(rect.center().y()))
        finish = start + QPoint(40, 0)
        QTest.mousePress(viewport, Qt.LeftButton, pos=start)
        self.app.sendEvent(
            viewport,
            QMouseEvent(QEvent.MouseMove, finish, Qt.NoButton, Qt.LeftButton, Qt.NoModifier),
        )
        QTest.mouseRelease(viewport, Qt.LeftButton, pos=finish)
        self.assertEqual(editor.document.state(0)[:2], (1000, 3000))
        self.assertEqual(editor.undo_stack.count(), 2)

    def test_preview_exact_end_and_export_handoff(self):
        editor = self.editor()
        editor.load_subtitles(str(self.source))
        editor.position_changed(500)
        self.assertEqual(editor.caption.text(), "第一句字幕")
        editor.position_changed(2000)
        self.assertEqual(editor.caption.text(), "")
        editor.trim(0, 700, 2100)
        # Handoff must write the latest edit even after an earlier export.
        editor.export_path = str(self.root / "handoff.srt")
        video = self.root / "video.mp4"
        video.touch()
        editor.video_path = str(video)
        emitted = []
        editor.finished.connect(lambda *args: emitted.append(args))
        editor.continue_synthesis()
        self.assertEqual(emitted, [(str(video), editor.export_path)])
        self.assertEqual(SubtitleTimeline.load(editor.export_path).state(0)[:2], (700, 2100))

    def test_save_failure_does_not_continue(self):
        editor = self.editor()
        editor.load_subtitles(str(self.source))
        video = self.root / "video.mp4"
        video.touch()
        editor.video_path = str(video)
        editor.export_path = str(self.root / "nonexistent" / "out.srt")
        emitted = []
        editor.finished.connect(lambda *args: emitted.append(args))
        with patch.object(editor, "error") as error:
            editor.continue_synthesis()
            error.assert_called_once()
        self.assertEqual(emitted, [])

    def test_invalid_timing_and_corrupt_draft(self):
        editor = self.editor()
        editor.load_subtitles(str(self.source))
        editor.end_input.setValue(0.1)
        with patch.object(editor, "error") as error:
            editor.apply_inputs()
            error.assert_called_once()
        self.assertEqual(editor.document.state(0)[:2], (500, 2000))
        corrupt = self.root / "corrupt.json"
        corrupt.write_text("broken", encoding="utf-8")
        restored = self.editor("corrupt.json")
        self.assertIn("恢复失败", restored.status.text())
        self.assertEqual(corrupt.read_text(encoding="utf-8"), "broken")

    def test_replacing_unsaved_subtitles_can_be_cancelled(self):
        editor = self.editor()
        editor.load_subtitles(str(self.source))
        editor.trim(0, 800, 2600)
        with patch(
            "videocaptioner.ui.view.subtitle_edit_interface.QMessageBox.question",
            return_value=0x00400000,
        ):
            self.assertFalse(editor.load_subtitles(str(self.source)))
        self.assertEqual(editor.document.state(0)[:2], (800, 2600))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is optional")
    def test_ffmpeg_frame_playback_seek_and_pause(self):
        video = self.root / "preview.mp4"
        subprocess.run(
            [
                shutil.which("ffmpeg"),
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=160x90:rate=25",
                "-t",
                "3",
                "-c:v",
                "libx264",
                str(video),
            ],
            check=True,
            timeout=20,
        )
        player = FFmpegPreview()
        frames = QSignalSpy(player.frameReady)
        duration = QSignalSpy(player.durationChanged)
        try:
            player.setMedia(QMediaContent(QUrl.fromLocalFile(str(video))))
            self.assertTrue(frames.wait(5000))
            self.assertFalse(frames[-1][0].isNull())
            if not player.duration():
                self.assertTrue(duration.wait(5000))
            self.assertEqual(player.duration(), 3000)
            player.setPosition(1000)
            self.assertTrue(frames.wait(5000))
            self.assertEqual(player.position(), 1000)
            player.play()
            self.assertTrue(frames.wait(5000))
            self.assertTrue(frames.wait(5000))
            self.assertGreater(player.position(), 1000)
            player.pause()
            self.assertEqual(player.state(), QMediaPlayer.PausedState)
            self.assertEqual(player.decoder.state(), player.decoder.NotRunning)
        finally:
            player.stop()


if __name__ == "__main__":
    unittest.main()
