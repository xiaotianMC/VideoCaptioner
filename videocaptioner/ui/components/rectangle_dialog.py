from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QFormLayout
from qfluentwidgets import BodyLabel, MessageBoxBase, SpinBox, SubtitleLabel

from videocaptioner.core.subtitle.drawing import AssRectangle
from videocaptioner.ui.components.MySettingCard import ColorPickerButton


class RectangleDialog(MessageBoxBase):
    def __init__(self, rectangle, resolution, parent=None):
        super().__init__(parent)
        self.viewLayout.addWidget(SubtitleLabel("矩形属性", self))
        form = QFormLayout()
        self.inputs = []
        width, height = resolution
        for label, value, minimum, maximum in [
            ("X 位置", rectangle.x, 0, width - 1),
            ("Y 位置", rectangle.y, 0, height - 1),
            ("宽度", rectangle.width, 1, width),
            ("高度", rectangle.height, 1, height),
        ]:
            spin = SpinBox(self)
            spin.setRange(minimum, maximum)
            spin.setValue(value)
            self.inputs.append(spin)
            form.addRow(BodyLabel(label, self), spin)
        x, y, w, h = self.inputs
        x.valueChanged.connect(lambda value: w.setMaximum(width - value))
        y.valueChanged.connect(lambda value: h.setMaximum(height - value))
        w.setMaximum(width - x.value())
        h.setMaximum(height - y.value())
        self.color = ColorPickerButton(QColor(rectangle.color), "矩形填充颜色", self)
        self.opacity = SpinBox(self)
        self.opacity.setRange(0, 100)
        self.opacity.setSuffix(" %")
        self.opacity.setValue(rectangle.opacity)
        form.addRow(BodyLabel("填充颜色", self), self.color)
        form.addRow(BodyLabel("不透明度", self), self.opacity)
        self.viewLayout.addLayout(form)
        hint = BodyLabel(f"坐标按 ASS 脚本分辨率 {width} × {height} 计算。", self)
        hint.setWordWrap(True)
        self.viewLayout.addWidget(hint)
        self.yesButton.setText("应用")
        self.cancelButton.setText("取消")
        self.widget.setMinimumWidth(360)

    def rectangle(self):
        return AssRectangle(
            *(spin.value() for spin in self.inputs), self.color.color.name(), self.opacity.value()
        )
