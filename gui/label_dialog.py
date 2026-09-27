# -*- coding: utf-8 -*-
"""
label_dialog - 分類結果の意味づけ画面

マニュアル 6.3 の「シンボロジで1クラスずつ色とラベルを手入力する」工程を
置き換える。Red-NIR 散布図（図28）と一覧を左右に並べ、どちらを選んでも
連動するようにしてある。matplotlib には依存せず QPainter で描く。

License: GPL v2
"""

from __future__ import annotations

import copy
from typing import Callable, List, Optional

from qgis.PyQt.QtCore import QPointF, QRectF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QPainter, QPen
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
    QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..core.categories import (
    COLOR_BY_NAME, DEFAULT_CATEGORIES, apply_default_colors, color_for,
)
from ..core.rinkyo_core import Signature, suggest_labels


class ScatterWidget(QWidget):
    """クラス平均を Red-NIR 平面に描く。点をクリックすると選択が飛ぶ。"""

    classClicked = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(320, 320)
        self._x: List[float] = []
        self._y: List[float] = []
        self._colors: List[QColor] = []
        self._current = -1
        self._x_label = "Red"
        self._y_label = "NIR"

    def set_data(self, x, y, colors, x_label="Red", y_label="NIR"):
        self._x = [float(v) for v in x]
        self._y = [float(v) for v in y]
        self._colors = list(colors)
        self._x_label = x_label
        self._y_label = y_label
        self.update()

    def set_current(self, index: int) -> None:
        self._current = index
        self.update()

    # -- 座標変換 ----------------------------------------------------------
    def _plot_rect(self) -> QRectF:
        return QRectF(46, 10, max(self.width() - 60, 10),
                      max(self.height() - 46, 10))

    def _to_screen(self, i: int) -> QPointF:
        r = self._plot_rect()
        x0, x1 = min(self._x), max(self._x)
        y0, y1 = min(self._y), max(self._y)
        sx = (self._x[i] - x0) / (x1 - x0) if x1 > x0 else 0.5
        sy = (self._y[i] - y0) / (y1 - y0) if y1 > y0 else 0.5
        return QPointF(r.left() + sx * r.width(),
                       r.bottom() - sy * r.height())

    def paintEvent(self, event):  # noqa: N802  Qt の命名規則
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        r = self._plot_rect()
        painter.setPen(QPen(QColor(150, 150, 150)))
        painter.drawLine(r.bottomLeft(), r.bottomRight())
        painter.drawLine(r.bottomLeft(), r.topLeft())
        painter.drawText(QRectF(r.left(), r.bottom() + 4, r.width(), 20),
                         Qt.AlignCenter, self._x_label)
        painter.save()
        painter.translate(14, r.center().y())
        painter.rotate(-90)
        painter.drawText(QRectF(-40, -10, 80, 20), Qt.AlignCenter,
                         self._y_label)
        painter.restore()

        if not self._x:
            return
        for i in range(len(self._x)):
            p = self._to_screen(i)
            col = self._colors[i] if i < len(self._colors) else QColor("#999")
            painter.setBrush(col)
            if i == self._current:
                painter.setPen(QPen(QColor(20, 20, 20), 2))
                painter.drawEllipse(p, 8, 8)
            else:
                painter.setPen(QPen(QColor(90, 90, 90), 1))
                painter.drawEllipse(p, 5, 5)
            painter.setPen(QPen(QColor(60, 60, 60)))
            painter.drawText(QPointF(p.x() + 8, p.y() - 6), str(i + 1))

    def mousePressEvent(self, event):  # noqa: N802
        if not self._x:
            return
        pos = event.pos()
        best, best_d = -1, 1e18
        for i in range(len(self._x)):
            p = self._to_screen(i)
            d = (p.x() - pos.x()) ** 2 + (p.y() - pos.y()) ** 2
            if d < best_d:
                best, best_d = i, d
        if best >= 0 and best_d < 400:
            self.classClicked.emit(best)


# 適用処理。成功なら None、失敗ならエラーメッセージを返す。
ApplyCallback = Callable[[Signature], Optional[str]]


class LabelDialog(QDialog):
    """クラスごとにカテゴリ名と色を決める。

    on_apply を渡すと「適用」ボタンが出て、画面を閉じずに変更を
    地図（レイヤのスタイル・ラスタ属性テーブル・シグネチャ）へ反映できる。
    OK は「適用して閉じる」、キャンセルは「最後に適用した状態のまま閉じる」。

    編集は受け取ったシグネチャのコピーに対して行うので、適用するまで
    呼び出し側のシグネチャは変わらない。
    """

    def __init__(self, signature: Signature, parent=None,
                 on_apply: Optional[ApplyCallback] = None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("分類結果の意味づけ"))
        self.resize(880, 580)
        self._on_apply = on_apply
        # 作業用のコピー。self.signature は最後に適用（または OK）した内容。
        self.signature = signature
        self._work = copy.deepcopy(signature)
        work = self._work

        self._idx = self._band_indices(work)
        if not any(work.labels):
            work.labels = suggest_labels(
                work,
                idx_blue=self._idx["blue"], idx_green=self._idx["green"],
                idx_red=self._idx["red"], idx_nir=self._idx["nir"],
                idx_ndvi=self._idx["ndvi"])
        apply_default_colors(work)
        # 自動ラベルや既定色で埋めた分も「未適用の変更」として扱う
        self._dirty = (list(work.labels) != list(signature.labels)
                       or list(work.colors) != list(signature.colors))

        self.table = QTableWidget(work.n_classes, 5, self)
        self.table.setHorizontalHeaderLabels(
            [self.tr("値"), self.tr("画素数"), "Red", "NIR", self.tr("カテゴリ")])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.Stretch)

        self.scatter = ScatterWidget(self)
        self.color_button = QPushButton(self.tr("選択中のクラスの色を変更…"), self)
        self.hint = QLabel(self.tr(
            "散布図の点をクリックすると一覧が連動します。"
            "左下ほど常緑針葉樹林、右上ほど落葉広葉樹林・草地になります。"), self)
        self.hint.setWordWrap(True)

        self._build_rows()
        self._refresh_scatter()

        left = QWidget(self)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(self.scatter, 1)
        lv.addWidget(self.hint)
        lv.addWidget(self.color_button)

        splitter = QSplitter(Qt.Horizontal, self)
        splitter.addWidget(left)
        splitter.addWidget(self.table)
        splitter.setStretchFactor(1, 1)

        flags = QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        if on_apply is not None:
            flags |= QDialogButtonBox.Apply
        self.buttons = QDialogButtonBox(flags, Qt.Horizontal, self)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.apply_button = self.buttons.button(QDialogButtonBox.Apply)
        if self.apply_button is not None:
            self.apply_button.setText(self.tr("適用"))
            self.apply_button.setToolTip(self.tr(
                "画面を閉じずに、地図のレイヤへ色とラベルを反映します。"))
            self.apply_button.clicked.connect(self.apply)
        self.status = QLabel(self)

        root = QVBoxLayout(self)
        root.addWidget(splitter, 1)
        bottom = QHBoxLayout()
        bottom.addWidget(self.status, 1)
        bottom.addWidget(self.buttons)
        root.addLayout(bottom)

        self.scatter.classClicked.connect(self._select_row)
        self.table.currentCellChanged.connect(
            lambda r, c, pr, pc: self.scatter.set_current(r))
        self.color_button.clicked.connect(self._pick_color)
        if work.n_classes:
            self._select_row(0)
        self._update_status()

    # -- 適用・確定・取消 --------------------------------------------------
    def is_dirty(self) -> bool:
        return self._dirty

    def apply(self) -> bool:
        """作業中の内容を反映する。失敗したら False（画面は開いたまま）。"""
        snapshot = copy.deepcopy(self._work)
        if self._on_apply is not None:
            try:
                error = self._on_apply(snapshot)
            except Exception as exc:  # noqa: BLE001  画面を落とさない
                error = "%s: %s" % (type(exc).__name__, exc)
            if error:
                QMessageBox.warning(self, self.tr("適用できませんでした"),
                                    error)
                return False
        self.signature = snapshot
        self._dirty = False
        self._update_status(applied=self._on_apply is not None)
        return True

    def accept(self) -> None:  # noqa: D401  OK ＝ 適用して閉じる
        if self._dirty and not self.apply():
            return
        super().accept()

    def reject(self) -> None:
        if self._dirty:
            answer = QMessageBox.question(
                self, self.tr("確認"),
                self.tr("適用していない変更があります。破棄して閉じますか？"),
                QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Cancel)
            if answer != QMessageBox.Discard:
                return
        super().reject()

    def _mark_dirty(self) -> None:
        self._dirty = True
        self._update_status()

    def _update_status(self, applied: bool = False) -> None:
        if self.apply_button is not None:
            self.apply_button.setEnabled(self._dirty)
        if self._dirty:
            self.status.setText(self.tr("未適用の変更があります"))
            self.status.setStyleSheet("color: #b36b00;")
        elif applied:
            self.status.setText(self.tr("地図に反映しました"))
            self.status.setStyleSheet("color: #2e7d32;")
        else:
            self.status.setText("")
            self.status.setStyleSheet("")

    # -- 構築 --------------------------------------------------------------
    def _build_rows(self) -> None:
        sig = self._work
        for i in range(sig.n_classes):
            self._set_readonly(i, 0, str(i + 1))
            self._set_readonly(i, 1, "{:,}".format(int(sig.counts[i])))
            self._set_readonly(i, 2, "%.0f" % sig.means[i, self._idx["red"]])
            self._set_readonly(i, 3, "%.0f" % sig.means[i, self._idx["nir"]])

            combo = QComboBox(self.table)
            combo.setEditable(True)
            combo.addItems([c for c, _ in DEFAULT_CATEGORIES])
            combo.setCurrentText(sig.labels[i] or self.tr("未分類"))
            combo.currentTextChanged.connect(
                lambda text, row=i: self._on_label_changed(row, text))
            self.table.setCellWidget(i, 4, combo)
        self.table.resizeColumnsToContents()

    def _set_readonly(self, row: int, col: int, text: str) -> None:
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        if col in (1, 2, 3):
            item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.table.setItem(row, col, item)

    # -- 操作 --------------------------------------------------------------
    def _on_label_changed(self, row: int, text: str) -> None:
        work = self._work
        work.labels[row] = text
        # カテゴリを変えたら色も追従させる（手で選んだ色は上書きしない）
        if text in COLOR_BY_NAME:
            work.colors[row] = COLOR_BY_NAME[text]
        elif not work.colors[row]:
            work.colors[row] = color_for(text, row)
        self._refresh_scatter()
        self._mark_dirty()

    def _select_row(self, index: int) -> None:
        self.table.selectRow(index)
        self.scatter.set_current(index)

    def _pick_color(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        initial = QColor(self._work.colors[row] or "#cccccc")
        color = QColorDialog.getColor(initial, self, self.tr("クラスの色"))
        if color.isValid() and color.name() != self._work.colors[row]:
            self._work.colors[row] = color.name()
            self._refresh_scatter()
            self._mark_dirty()

    def _refresh_scatter(self) -> None:
        sig = self._work
        self.scatter.set_data(
            sig.means[:, self._idx["red"]],
            sig.means[:, self._idx["nir"]],
            [QColor(c or "#cccccc") for c in sig.colors],
        )

    # -- バンド位置の推定 --------------------------------------------------
    @staticmethod
    def _band_indices(sig: Signature) -> dict:
        """バンド名から Blue/Green/Red/NIR/NDVI の位置を推定する。

        名前が付いていない画像もあるので、見つからなければ
        B2,B3,B4,B8 の並び（マニュアルの手順どおりの順序）を仮定する。
        """
        names = [(n or "").lower() for n in sig.band_names]
        patterns = {
            "blue": ("blue", "b2"),
            "green": ("green", "b3"),
            "red": ("red", "b4"),
            "nir": ("nir", "b8"),
            "ndvi": ("ndvi",),
        }
        out = {}
        for key, needles in patterns.items():
            found = None
            for i, name in enumerate(names):
                if key != "ndvi" and "ndvi" in name:
                    continue  # NDVI バンドを Red/NIR と取り違えない
                if any(x in name for x in needles):
                    found = i
                    break
            out[key] = found

        for key, default in (("blue", 0), ("green", 1),
                             ("red", 2), ("nir", 3)):
            if out[key] is None or out[key] >= sig.n_bands:
                out[key] = min(default, sig.n_bands - 1)
        if out["ndvi"] is not None and out["ndvi"] >= sig.n_bands:
            out["ndvi"] = None
        return out


def apply_signature_style(layer, signature: Signature) -> None:
    """分類ラスタのレイヤに、シグネチャの色とラベルを反映する。

    QGIS 3.30 以降ならラスタ属性テーブルがそのまま効くが、
    古い環境でも見た目が揃うようパレット凡例を明示的に組む。
    """
    from qgis.core import QgsPalettedRasterRenderer

    apply_default_colors(signature)
    classes: List[QgsPalettedRasterRenderer.Class] = []
    for i in range(signature.n_classes):
        label = signature.labels[i] or "class %d" % (i + 1)
        classes.append(QgsPalettedRasterRenderer.Class(
            i + 1, QColor(signature.colors[i] or color_for(label, i)), label))
    renderer = QgsPalettedRasterRenderer(layer.dataProvider(), 1, classes)
    layer.setRenderer(renderer)
    layer.triggerRepaint()
