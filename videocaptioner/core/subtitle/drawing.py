"""Standard ASS vector rectangles, in script resolution coordinates."""

import re
from dataclasses import dataclass


def contains_ass_drawing(text):
    return any(re.search(r"\\p[1-9]\d*", block) for block in re.findall(r"\{([^}]*)\}", text))


@dataclass(frozen=True)
class AssRectangle:
    x: int
    y: int
    width: int
    height: int
    color: str = "#00a6b8"
    opacity: int = 70

    def __post_init__(self):
        if any(
            not isinstance(value, int)
            for value in (self.x, self.y, self.width, self.height, self.opacity)
        ):
            raise ValueError("矩形坐标、宽高和不透明度必须为整数")
        if min(self.x, self.y) < 0 or min(self.width, self.height) <= 0:
            raise ValueError("矩形位置不能为负数，宽高必须大于零")
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", self.color) or not 0 <= self.opacity <= 100:
            raise ValueError("矩形颜色或不透明度无效")

    def to_ass(self):
        r, g, b = self.color[1:3], self.color[3:5], self.color[5:7]
        alpha = round(255 * (100 - self.opacity) / 100)
        return (
            rf"{{\an7\pos({self.x},{self.y})\fscx100\fscy100\frz0\bord0\shad0"
            rf"\1c&H{b}{g}{r}&\alpha&H{alpha:02X}&\3a&HFF&\4a&HFF&\p1}}"
            rf"m 0 0 l {self.width} 0 {self.width} {self.height} 0 {self.height}{{\p0}}"
        )

    @classmethod
    def from_ass(cls, text):
        # Only recognize our static rectangles, not arbitrary ASS paths/transforms.
        match = re.fullmatch(
            r"\{(?P<tags>[^{}]+)\}m 0 0 l (?P<w>\d+) 0 (?P=w) (?P<h>\d+) 0 (?P=h)\{\\p0\}", text
        )
        if not match:
            return None
        tags = match["tags"]
        pos = re.search(r"\\pos\((\d+),(\d+)\)", tags)
        color = re.search(r"\\1c&H([0-9a-fA-F]{6})&", tags)
        alpha = re.search(r"\\alpha&H([0-9a-fA-F]{2})&", tags)
        if not all((pos, color, alpha)):
            return None
        bgr = color[1]
        try:
            rectangle = cls(
                int(pos[1]),
                int(pos[2]),
                int(match["w"]),
                int(match["h"]),
                "#" + bgr[4:6] + bgr[2:4] + bgr[:2],
                round((255 - int(alpha[1], 16)) * 100 / 255),
            )
        except ValueError:
            return None
        return rectangle if rectangle.to_ass().lower() == text.lower() else None
