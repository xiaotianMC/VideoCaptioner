"""Run with unittest discover; FFmpeg checks use the existing local binaries."""

import copy
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QEvent, QPoint, QSize, Qt
from PyQt5.QtGui import QImage, QMouseEvent
from PyQt5.QtTest import QSignalSpy, QTest
from PyQt5.QtWidgets import QApplication

from tests.test_subtitle.test_timeline_editor import ASS, SRT
from videocaptioner.core.entities import SynthesisConfig, SynthesisTask
from videocaptioner.core.subtitle.ass_utils import auto_wrap_ass_file
from videocaptioner.core.subtitle.drawing import AssRectangle, contains_ass_drawing
from videocaptioner.core.subtitle.timeline import SubtitleTimeline
from videocaptioner.ui.components.ffmpeg_preview import PreviewFrame
from videocaptioner.ui.task_factory import TaskFactory
from videocaptioner.ui.thread.video_synthesis_thread import VideoSynthesisThread
from videocaptioner.ui.view.subtitle_edit_interface import SubtitleEditInterface


class RectangleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.widgets = []

    def tearDown(self):
        for widget in self.widgets:
            widget.close()
            widget.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def editor(self):
        editor = SubtitleEditInterface(draft_path=self.root / "draft.json")
        self.widgets.append(editor)
        return editor

    def test_rectangle_tags_round_trip_and_invalid_geometry(self):
        for opacity in range(101):
            shape = AssRectangle(10, 20, 300, 100, "#19a4f2", opacity)
            self.assertTrue(contains_ass_drawing(shape.to_ass()))
            self.assertEqual(AssRectangle.from_ass(shape.to_ass()), shape)
        for args in [
            (-1, 0, 20, 10),
            (0, 0, 0, 10),
            (0, 0, 20, 10, "red"),
            (0, 0, 20, 10, "#ffffff", 101),
        ]:
            with self.assertRaises(ValueError):
                AssRectangle(*args)
        with self.assertRaises(ValueError):
            SubtitleTimeline().add_rectangle(AssRectangle(1270, 10, 100, 20), 0, 1000)

    def test_srt_to_ass_rectangle_retains_text_and_rejects_srt(self):
        source = self.root / "source.srt"
        source.write_text(SRT, encoding="utf-8")
        document = SubtitleTimeline.load(source)
        before = copy.deepcopy(document.data.to_json())
        shape = AssRectangle(128, 72, 256, 144, "#ff0000", 100)
        document.add_rectangle(shape, 0, 1000)
        self.assertEqual(document.data.to_json()["1"], before["1"])
        self.assertEqual(document.resolution(), (1280, 720))
        target = self.root / "rectangle.ass"
        target.write_text(document.export_text(".ass"), encoding="utf-8")
        restored = SubtitleTimeline.load(target)
        self.assertIn(shape, [restored.rectangle(i) for i in range(len(restored.data))])
        self.assertEqual(source.read_text(encoding="utf-8"), SRT)
        with self.assertRaisesRegex(ValueError, "SRT 不支持图形"):
            document.export_text(".srt")

    def test_ass_insert_before_attachments_and_undo_restores_original(self):
        source = self.root / "source.ass"
        original = ASS + "\n[Fonts]\nfontname: preserved-font\npreserved-data\n"
        source.write_text(original, encoding="utf-8")
        editor = self.editor()
        editor.load_subtitles(str(source))
        before = editor.document.export_text(".ass")
        shape = AssRectangle(100, 100, 200, 150)
        editor.add_rectangle(shape, 500, 1500)
        output = editor.document.export_text(".ass")
        self.assertLess(output.index(shape.to_ass()), output.index("[Fonts]"))
        self.assertIn("preserved-data", output)
        editor.undo_stack.undo()
        self.assertEqual(editor.document.export_text(".ass"), before)
        editor.undo_stack.redo()
        self.assertEqual(editor.document.rectangle(len(editor.document.data) - 1), shape)

    def test_new_rectangle_persists_and_undo_to_empty_does_not_resurrect(self):
        editor = self.editor()
        shape = AssRectangle(100, 100, 200, 150)
        editor.add_rectangle(shape, 500, 1500)
        self.assertEqual(editor.track.lanes(0), [2])
        self.assertTrue(editor.text_input.isReadOnly())
        editor.start_input.setValue(0.7)
        editor.apply_inputs()
        self.assertEqual(editor.document.state(0)[:2], (700, 1500))
        self.assertEqual(editor.document.rectangle(0), shape)
        self.assertTrue(editor.save_draft(), editor.status.text())
        self.assertTrue(editor.draft_path.is_file())
        restored = self.editor()
        self.assertEqual(len(restored.document.data), 1, restored.status.text())
        self.assertEqual(restored.document.rectangle(0), shape)
        editor.position_changed(700)
        self.assertEqual(editor.fallback_frame.rectangles, [shape])
        self.assertEqual(editor.caption.text(), "")
        editor.position_changed(1500)
        self.assertEqual(editor.fallback_frame.rectangles, [])
        editor.undo_stack.undo()
        editor.undo_stack.undo()
        editor.save_draft()
        empty = self.editor()
        self.assertEqual(len(empty.document.data), 0)
        # Prevent the other live test editor from writing its older snapshot during cleanup.
        restored.document = SubtitleTimeline()

    def test_mouse_draw_reverse_direction_ignores_letterboxing(self):
        frame = PreviewFrame()
        self.widgets.append(frame)
        frame.resize(640, 480)
        image = QImage(640, 360, QImage.Format_RGB888)
        image.fill(Qt.black)
        frame.set_image(image)
        frame.set_rectangles([], (1280, 720))
        frame.set_drawing(True)
        frame.show()
        self.app.processEvents()
        events = QSignalSpy(frame.rectangleDrawn)
        QTest.mouseClick(frame, Qt.LeftButton, pos=QPoint(20, 20))
        self.assertEqual(len(events), 0)
        QTest.mousePress(frame, Qt.LeftButton, pos=QPoint(320, 240))
        self.app.sendEvent(
            frame,
            QMouseEvent(
                QEvent.MouseMove, QPoint(64, 96), Qt.NoButton, Qt.LeftButton, Qt.NoModifier
            ),
        )
        QTest.mouseRelease(frame, Qt.LeftButton, pos=QPoint(64, 96))
        self.assertEqual(list(events[0]), [128, 72, 512, 288])
        frame.set_video_size(QSize(720, 1280))
        self.assertEqual(round(frame.video_rect().x()), 185)
        self.assertEqual(round(frame.video_rect().width()), 270)
        frame.set_video_size(QSize(16, 9))
        frame.set_rectangles([AssRectangle(128, 72, 256, 144, "#ff0000", 100)], (1280, 720))
        preview = frame.grab().toImage()
        self.assertGreater(preview.pixelColor(100, 110).red(), 230)
        self.assertLess(preview.pixelColor(10, 10).red(), 20)

    def test_rectangle_timeline_drag_keeps_geometry(self):
        editor = self.editor()
        editor.resize(1100, 850)
        editor.show()
        shape = AssRectangle(100, 100, 300, 200)
        editor.add_rectangle(shape, 500, 1500)
        self.app.processEvents()
        rect = editor.track.clip_rect(0, 2)
        start = QPoint(round(rect.right() - 2), round(rect.center().y()))
        end = start + QPoint(40, 0)
        viewport = editor.track.viewport()
        QTest.mousePress(viewport, Qt.LeftButton, pos=start)
        self.app.sendEvent(
            viewport, QMouseEvent(QEvent.MouseMove, end, Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
        )
        QTest.mouseRelease(viewport, Qt.LeftButton, pos=end)
        self.assertEqual(editor.document.state(0)[:2], (500, 2000))
        self.assertEqual(editor.document.rectangle(0), shape)
        editor.undo_stack.undo()
        self.assertEqual(editor.document.state(0)[:2], (500, 1500))

    def test_vector_paths_are_not_word_wrapped_and_synthesis_keeps_ass(self):
        document = SubtitleTimeline()
        shape = AssRectangle(10, 20, 300, 100)
        document.add_rectangle(shape, 0, 1000)
        source = self.root / "rect.ass"
        source.write_text(document.export_text(".ass"), encoding="utf-8")
        wrapped = self.root / "wrapped.ass"
        with patch(
            "videocaptioner.core.subtitle.ass_utils.wrap_ass_text",
            side_effect=AssertionError("wrapped drawing"),
        ):
            auto_wrap_ass_file(str(source), str(wrapped))
        self.assertIn(shape.to_ass(), wrapped.read_text(encoding="utf-8"))
        factory_task = TaskFactory.create_synthesis_task("input.mp4", str(source))
        self.assertFalse(factory_task.synthesis_config.soft_subtitle)
        task = SynthesisTask(
            video_path="input.mp4",
            subtitle_path=str(source),
            output_path=str(self.root / "result.mp4"),
            synthesis_config=SynthesisConfig(need_video=True, soft_subtitle=True),
        )
        thread = VideoSynthesisThread(task)
        errors = []
        thread.error.connect(errors.append)
        with (
            patch("videocaptioner.ui.thread.video_synthesis_thread.add_subtitles") as burn,
            patch(
                "videocaptioner.ui.thread.video_synthesis_thread.ASRData.from_subtitle_file"
            ) as parse,
        ):
            thread.run()
            self.assertEqual(errors, [])
            burn.assert_called_once()
            self.assertEqual(burn.call_args.args[1], str(source))
            self.assertFalse(burn.call_args.kwargs["soft_subtitle"])
            parse.assert_not_called()

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is optional")
    def test_exported_rectangle_renders_at_expected_pixels_with_libass(self):
        source = self.root / "source.ass"
        source.write_text(ASS, encoding="utf-8")
        document = SubtitleTimeline.load(source)
        document.add_rectangle(AssRectangle(192, 108, 384, 216, "#ff0000", 100), 0, 1000)
        target = self.root / "render.ass"
        target.write_text(document.export_text(".ass"), encoding="utf-8")
        escaped = target.as_posix().replace(":", "\\:")
        result = subprocess.run(
            [
                shutil.which("ffmpeg"),
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x180:r=1:d=1",
                "-vf",
                f"ass='{escaped}'",
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "pipe:1",
            ],
            capture_output=True,
            check=True,
            timeout=20,
        )
        image = QImage(result.stdout, 320, 180, QImage.Format_RGB888).copy()
        color = image.pixelColor(50, 30)
        self.assertGreater(color.red(), 230)
        self.assertLess(color.green(), 20)
        self.assertLess(image.pixelColor(25, 15).red(), 20)
        self.assertLess(image.pixelColor(100, 60).red(), 20)


if __name__ == "__main__":
    unittest.main()
