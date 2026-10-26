"""Silent frame preview for systems without a working Qt video decoder."""

import shutil

from PyQt5.QtCore import QObject, QProcess, QRectF, Qt, pyqtSignal
from PyQt5.QtGui import QImage, QPainter
from PyQt5.QtMultimedia import QMediaPlayer
from PyQt5.QtWidgets import QWidget


class PreviewFrame(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.image = QImage()
        self.setMinimumSize(240, 140)

    def set_image(self, image):
        self.image = image
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.black)
        if not self.image.isNull():
            size = self.image.size().scaled(self.size(), Qt.KeepAspectRatio)
            rect = QRectF(
                (self.width() - size.width()) / 2,
                (self.height() - size.height()) / 2,
                size.width(),
                size.height(),
            )
            painter.drawImage(rect, self.image)


class FFmpegPreview(QObject):
    positionChanged = pyqtSignal(int)
    durationChanged = pyqtSignal(int)
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
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    self.path,
                ],
            )
            self.decode(single=True)

    def probed(self, code, *_):
        try:
            value = float(bytes(self.probe.readAllStandardOutput()).decode().strip())
            self._duration = max(0, round(value * 1000))
            self.durationChanged.emit(self._duration)
        except (ValueError, OverflowError):
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
