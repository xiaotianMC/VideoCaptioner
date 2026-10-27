"""Silent frame preview for systems without a working Qt video decoder."""

import json
import shutil

from PyQt5.QtCore import QObject, QPointF, QProcess, QRectF, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QImage, QPainter, QPen
from PyQt5.QtMultimedia import QMediaPlayer
from PyQt5.QtWidgets import QWidget


class PreviewFrame(QWidget):
    rectangleDrawn = pyqtSignal(int, int, int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.image = QImage()
        self.video_size = QSize(16, 9)
        self.canvas_size = QSize(1280, 720)
        self.rectangles = []
        self.drawing_enabled = False
        self.anchor = self.cursor_point = None
        self.setMinimumSize(240, 140)

    def set_image(self, image):
        self.image = image
        self.update()

    def set_video_size(self, size):
        self.video_size = size
        self.update()

    def video_rect(self):
        size = self.video_size.scaled(self.size(), Qt.KeepAspectRatio)
        return QRectF(
            (self.width() - size.width()) / 2,
            (self.height() - size.height()) / 2,
            size.width(),
            size.height(),
        )

    def set_rectangles(self, rectangles, resolution):
        self.rectangles = rectangles
        self.canvas_size = QSize(*resolution)
        self.update()

    def set_drawing(self, enabled):
        self.drawing_enabled = enabled
        self.anchor = self.cursor_point = None
        self.setCursor(Qt.CrossCursor if enabled else Qt.ArrowCursor)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.black)
        if not self.image.isNull():
            rect = self.video_rect()
            # FFmpeg pads raw frames; exclude that padding when mapping script coordinates.
            size = self.video_size.scaled(self.image.size(), Qt.KeepAspectRatio)
            source = QRectF(
                (self.image.width() - size.width()) / 2,
                (self.image.height() - size.height()) / 2,
                size.width(),
                size.height(),
            )
            painter.drawImage(rect, self.image, source)
            painter.setClipRect(rect)
            sx, sy = (
                rect.width() / self.canvas_size.width(),
                rect.height() / self.canvas_size.height(),
            )
            for shape in self.rectangles:
                color = QColor(shape.color)
                color.setAlpha(round(255 * shape.opacity / 100))
                painter.fillRect(
                    QRectF(
                        rect.x() + shape.x * sx,
                        rect.y() + shape.y * sy,
                        shape.width * sx,
                        shape.height * sy,
                    ),
                    color,
                )
            if self.anchor is not None:
                painter.setBrush(QColor(0, 166, 184, 70))
                painter.setPen(QPen(Qt.white, 1, Qt.DashLine))
                painter.drawRect(QRectF(self.anchor, self.cursor_point).normalized())

    def clamp_point(self, point):
        rect = self.video_rect()
        return QPointF(
            max(rect.left(), min(point.x(), rect.right())),
            max(rect.top(), min(point.y(), rect.bottom())),
        )

    def mousePressEvent(self, event):
        if (
            self.drawing_enabled
            and not self.image.isNull()
            and event.button() == Qt.LeftButton
            and self.video_rect().contains(event.pos())
        ):
            self.anchor = self.cursor_point = QPointF(event.pos())
            self.update()

    def mouseMoveEvent(self, event):
        if self.anchor is not None:
            self.cursor_point = self.clamp_point(event.pos())
            self.update()

    def mouseReleaseEvent(self, event):
        if self.anchor is None or event.button() != Qt.LeftButton:
            return
        drawn = QRectF(self.anchor, self.clamp_point(event.pos())).normalized()
        self.anchor = self.cursor_point = None
        self.update()
        if min(drawn.width(), drawn.height()) < 3:
            return
        rect = self.video_rect()
        sx, sy = self.canvas_size.width() / rect.width(), self.canvas_size.height() / rect.height()
        x, y = round((drawn.x() - rect.x()) * sx), round((drawn.y() - rect.y()) * sy)
        right = min(self.canvas_size.width(), round((drawn.right() - rect.x()) * sx))
        bottom = min(self.canvas_size.height(), round((drawn.bottom() - rect.y()) * sy))
        self.rectangleDrawn.emit(x, y, max(1, right - x), max(1, bottom - y))


class FFmpegPreview(QObject):
    positionChanged = pyqtSignal(int)
    durationChanged = pyqtSignal(int)
    videoSizeChanged = pyqtSignal(QSize)
    stateChanged = pyqtSignal(int)
    frameReady = pyqtSignal(QImage)
    failed = pyqtSignal(str)
    WIDTH, HEIGHT, FPS = 640, 360, 25

    def __init__(self, parent=None):
        super().__init__(parent)
        self.decoder = QProcess(self)
        self.probe = QProcess(self)
        self.decoder.readyReadStandardOutput.connect(self.read_frames)
        self.decoder.readyReadStandardError.connect(self.read_errors)
        self.decoder.finished.connect(self.finished)
        self.decoder.errorOccurred.connect(lambda _: self.failed.emit(self.decoder.errorString()))
        self.probe.finished.connect(self.probed)
        self.path = ""
        self._position = self._duration = self.origin = self.frames = 0
        self._state = QMediaPlayer.StoppedState
        self.buffer = bytearray()
        self.last_error = ""

    def setMedia(self, media):
        self.stop()
        self._duration = 0
        self.durationChanged.emit(0)
        self.path = media.canonicalUrl().toLocalFile()
        if self.probe.state() != QProcess.NotRunning:
            self.probe.kill()
            self.probe.waitForFinished(1000)
        if self.path:
            self.probe.start(
                shutil.which("ffprobe") or "ffprobe",
                [
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration:stream=codec_type,width,height",
                    "-of",
                    "json",
                    self.path,
                ],
            )
            self.decode(single=True)

    def probed(self, code, *_):
        try:
            info = json.loads(bytes(self.probe.readAllStandardOutput()))
            value = float(info["format"]["duration"])
            self._duration = max(0, round(value * 1000))
            for stream in info.get("streams", []):
                if (
                    stream.get("codec_type") == "video"
                    and stream.get("width", 0) > 0
                    and stream.get("height", 0) > 0
                ):
                    self.videoSizeChanged.emit(QSize(stream["width"], stream["height"]))
                    break
            self.durationChanged.emit(self._duration)
        except (ValueError, KeyError, OverflowError):
            self.failed.emit("无法读取视频时长，请检查文件和 FFprobe。")

    def position(self):
        return self._position

    def duration(self):
        return self._duration

    def state(self):
        return self._state

    def set_state(self, state):
        self._state = state
        self.stateChanged.emit(state)

    def stop_decoder(self):
        if self.decoder.state() != QProcess.NotRunning:
            self.decoder.blockSignals(True)
            self.decoder.kill()
            self.decoder.waitForFinished(1000)
            self.decoder.blockSignals(False)
        self.decoder.readAllStandardOutput()
        self.decoder.readAllStandardError()
        self.buffer.clear()

    def decode(self, single=False):
        self.stop_decoder()
        if not self.path:
            return
        self.origin, self.frames = self._position, 0
        self.last_error = ""
        args = ["-nostdin", "-hide_banner", "-loglevel", "error"]
        if not single:
            args += ["-re"]
        args += [
            "-ss",
            str(self.origin / 1000),
            "-i",
            self.path,
            "-an",
            "-sn",
            "-vf",
            f"fps={self.FPS},scale={self.WIDTH}:{self.HEIGHT}:force_original_aspect_ratio=decrease,pad={self.WIDTH}:{self.HEIGHT}:(ow-iw)/2:(oh-ih)/2",
        ]
        if single:
            args += ["-frames:v", "1"]
        args += ["-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"]
        self.decoder.start(shutil.which("ffmpeg") or "ffmpeg", args)

    def play(self):
        if self._duration and self._position >= self._duration - 40:
            self._position = 0
        self.set_state(QMediaPlayer.PlayingState)
        self.decode()

    def pause(self):
        self.stop_decoder()
        self.set_state(QMediaPlayer.PausedState)

    def stop(self):
        self.stop_decoder()
        if self.probe.state() != QProcess.NotRunning:
            self.probe.blockSignals(True)
            self.probe.kill()
            self.probe.waitForFinished(1000)
            self.probe.blockSignals(False)
        self._position = 0
        self.set_state(QMediaPlayer.StoppedState)

    def setPosition(self, ms):
        self._position = max(0, min(ms, self._duration - 1)) if self._duration else max(0, ms)
        self.positionChanged.emit(self._position)
        self.decode(single=self._state != QMediaPlayer.PlayingState)

    def read_errors(self):
        self.last_error = (
            self.last_error + bytes(self.decoder.readAllStandardError()).decode(errors="replace")
        )[-4000:]

    def read_frames(self):
        self.buffer.extend(bytes(self.decoder.readAllStandardOutput()))
        frame_bytes = self.WIDTH * self.HEIGHT * 3
        count = len(self.buffer) // frame_bytes
        if not count:
            return
        last = bytes(self.buffer[(count - 1) * frame_bytes : count * frame_bytes])
        del self.buffer[: count * frame_bytes]
        self.frames += count
        self._position = self.origin + (self.frames - 1) * 1000 // self.FPS
        self.frameReady.emit(QImage(last, self.WIDTH, self.HEIGHT, QImage.Format_RGB888).copy())
        self.positionChanged.emit(self._position)

    def finished(self, code, *_):
        if code:
            self.failed.emit(self.last_error or "FFmpeg 视频预览失败")
        elif self._state == QMediaPlayer.PlayingState:
            self.set_state(QMediaPlayer.StoppedState)

    def errorString(self):
        return self.last_error
