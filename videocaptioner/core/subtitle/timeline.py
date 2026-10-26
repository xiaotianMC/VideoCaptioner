"""Editable subtitle timings; retain ASS headers, styles and event fields."""

import re
from pathlib import Path

from videocaptioner.core.asr.asr_data import ASRData, ASRDataSeg


class SubtitleTimeline:
    def __init__(self):
        self.data = ASRData([])
        self.ass_lines = []
        self.ass_events = []

    @classmethod
    def load(cls, path):
        document = cls()
        path = Path(path)
        if path.suffix.lower() == ".ass":
            document.ass_lines = path.read_text(encoding="utf-8-sig").splitlines()
            in_events = False
            fields = []
            segments = []
            for index, line in enumerate(document.ass_lines):
                stripped = line.strip()
                if stripped.startswith("["):
                    in_events = stripped.lower() == "[events]"
                elif in_events and stripped.lower().startswith("format:"):
                    fields = [s.strip().lower() for s in stripped.split(":", 1)[1].split(",")]
                elif in_events and stripped.lower().startswith("dialogue:"):
                    if (
                        not fields
                        or fields[-1] != "text"
                        or not {"start", "end", "style"}.issubset(fields)
                    ):
                        raise ValueError("ASS Events 缺少有效的 Format 行")
                    values = stripped.split(":", 1)[1].lstrip().split(",", len(fields) - 1)
                    if len(values) != len(fields):
                        raise ValueError("ASS Dialogue 字段不完整")
                    event = dict(zip(fields, values))
                    start = cls._ass_ms(event["start"])
                    end = cls._ass_ms(event["end"])
                    segments.append(ASRDataSeg(event["text"].replace(r"\N", "\n"), start, end))
                    document.ass_events.append((index, fields, values))
            # Preserve event order, including simultaneous bilingual lines and empty text.
            document.data.segments = segments
        else:
            document.data = ASRData.from_subtitle_file(str(path))
        document.validate()
        return document

    @staticmethod
    def _ass_ms(value):
        match = re.fullmatch(r"\s*(\d+):(\d{2}):(\d{2})\.(\d{2})\s*", value)
        if not match:
            raise ValueError(f"无效的 ASS 时间：{value}")
        h, m, s, cs = map(int, match.groups())
        if m >= 60 or s >= 60:
            raise ValueError(f"无效的 ASS 时间：{value}")
        return ((h * 60 + m) * 60 + s) * 1000 + cs * 10

    def validate(self):
        if not self.data.segments:
            raise ValueError("字幕文件中没有可编辑的字幕")
        for seg in self.data:
            if seg.start_time < 0 or seg.end_time <= seg.start_time:
                raise ValueError("字幕起止时间无效：结束时间必须晚于开始时间")

    def state(self, index):
        seg = self.data.segments[index]
        return (seg.start_time, seg.end_time, seg.text, seg.translated_text)

    def set_state(self, index, state):
        start, end, text, translated = state
        if start < 0 or end <= start:
            raise ValueError("结束时间必须晚于开始时间，开始时间不能为负数")
        if self.ass_events:
            start = int(start) // 10 * 10
            end = max(start + 10, int(end) // 10 * 10)
        seg = self.data.segments[index]
        seg.start_time, seg.end_time = int(start), int(end)
        seg.text, seg.translated_text = text, translated

    def lane(self, index):
        if self.ass_events:
            _, fields, values = self.ass_events[index]
            return int(values[fields.index("style")].strip().lower() == "secondary")
        return 0

    def text(self, index, translation=False):
        seg = self.data.segments[index]
        text = seg.translated_text if translation else seg.text
        return re.sub(r"\{[^}]*\}", "", text).replace(r"\n", "\n").replace(r"\h", " ")

    def export_text(self, suffix):
        self.validate()
        if suffix.lower() == ".ass" and self.ass_events:
            lines = list(self.ass_lines)
            for seg, (index, fields, original) in zip(self.data, self.ass_events):
                values = list(original)
                values[fields.index("start")] = ASRDataSeg._ms_to_ass_ts(seg.start_time)
                values[fields.index("end")] = ASRDataSeg._ms_to_ass_ts(seg.end_time)
                values[fields.index("text")] = seg.text.replace("\n", r"\N")
                lines[index] = "Dialogue: " + ",".join(values)
            return "\n".join(lines) + "\n"
        data = ASRData(list(self.data.segments))
        if suffix.lower() == ".srt":
            return data.to_srt()
        if suffix.lower() == ".ass":
            return data.to_ass()
        raise ValueError("请选择 .srt 或 .ass 文件")

    def to_dict(self):
        return {
            "segments": self.data.to_json(),
            "ass_lines": self.ass_lines,
            "ass_events": self.ass_events,
        }

    @classmethod
    def from_dict(cls, value):
        document = cls()
        # Do not sort on restore: ASS event mappings use the original order.
        document.data.segments = [
            ASRDataSeg(
                s["original_subtitle"],
                s["start_time"],
                s["end_time"],
                s.get("translated_subtitle", ""),
            )
            for s in value["segments"].values()
        ]
        document.ass_lines = value.get("ass_lines", [])
        document.ass_events = value.get("ass_events", [])
        if document.ass_lines and len(document.ass_events) != len(document.data):
            raise ValueError("ASS 草稿的事件数量不匹配")
        for index, fields, values in document.ass_events:
            if (
                not isinstance(index, int)
                or not 0 <= index < len(document.ass_lines)
                or len(fields) != len(values)
                or not {"start", "end", "style", "text"}.issubset(fields)
            ):
                raise ValueError("ASS 草稿的事件映射无效")
        document.validate()
        return document


def adjusted_times(start, end, delta, edge, limit=0):
    """Clamp a drag without reversing a clip or shortening a move at time zero."""
    if edge == "start":
        return max(0, min(start + delta, end - 10)), end
    if edge == "end":
        ceiling = max(end, limit) if limit else end + max(delta, 0)
        return start, max(start + 10, min(end + delta, ceiling))
    delta = max(-start, delta)
    if limit:
        delta = min(delta, max(end, limit) - end)
    return start + delta, end + delta
