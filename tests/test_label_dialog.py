# -*- coding: utf-8 -*-
"""意味づけ画面（複数選択・まとめて変更）の動作確認。

QGIS が無くても PyQt5 だけで動く（qgis.PyQt を PyQt5 に差し替える）。

    QT_QPA_PLATFORM=offscreen python3 tests/test_label_dialog.py
"""

import os
import sys
import types

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

try:
    import qgis.PyQt  # noqa: F401
except ImportError:
    import PyQt5
    from PyQt5 import QtCore, QtGui, QtTest, QtWidgets
    qgis = types.ModuleType("qgis")
    qgis.PyQt = types.ModuleType("qgis.PyQt")
    sys.modules["qgis"] = qgis
    sys.modules["qgis.PyQt"] = qgis.PyQt
    for name, mod in (("QtCore", QtCore), ("QtGui", QtGui),
                      ("QtWidgets", QtWidgets), ("QtTest", QtTest)):
        sys.modules["qgis.PyQt." + name] = mod
    del PyQt5

import importlib  # noqa: E402

from qgis.PyQt.QtCore import QCoreApplication, Qt  # noqa: E402
from qgis.PyQt.QtTest import QTest  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication  # noqa: E402

PKG = os.path.basename(os.path.dirname(HERE))
ld = importlib.import_module(PKG + ".gui.label_dialog")
rc = importlib.import_module(PKG + ".core.rinkyo_core")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def test_category_filter(app):
    """値一覧のカテゴリ絞り込みと、散布図の減色（dim）を確認する。"""
    rng = np.random.default_rng(1)
    sig = rc.Signature(band_names=["Blue", "Green", "Red", "NIR", "NDVI"],
                       means=rng.random((6, 5)) * 3000,
                       covs=np.stack([np.eye(5)] * 6),
                       counts=np.arange(6) + 10,
                       labels=["A", "B", "A", "C", "B", "A"],
                       colors=["#111111", "#222222", "#333333",
                               "#444444", "#555555", "#666666"])
    dlg = ld.LabelDialog(sig, None)
    dlg.show()
    app.processEvents()
    t = dlg.table

    require(not dlg.category_filter_check.isChecked(),
            "絞り込みは既定で無効になっていません")
    dlg._select_row(0)  # ラベル "A"
    dlg.category_filter_check.setChecked(True)
    app.processEvents()

    hidden = [t.isRowHidden(i) for i in range(6)]
    require(hidden == [False, True, False, True, True, False],
            "カテゴリ絞り込みの結果が違います: %s" % hidden)
    require(dlg._filter_label == "A", "絞り込み対象カテゴリが違います")

    alphas = [c.alpha() for c in dlg.scatter._colors]
    require(alphas == [255, ld.DIM_ALPHA, 255, ld.DIM_ALPHA, ld.DIM_ALPHA, 255],
            "散布図の減色が違います: %s" % alphas)

    # 現在行のカテゴリを変えると、絞り込み対象も追従する
    combo = t.cellWidget(0, 4)
    combo.setCurrentText("C")
    app.processEvents()
    hidden = [t.isRowHidden(i) for i in range(6)]
    require(hidden == [False, True, True, False, True, True],
            "カテゴリ変更後の絞り込みが更新されていません: %s" % hidden)

    dlg.category_filter_check.setChecked(False)
    app.processEvents()
    require(all(not t.isRowHidden(i) for i in range(6)),
            "絞り込み解除で行が再表示されません")
    require(all(c.alpha() == 255 for c in dlg.scatter._colors),
            "絞り込み解除で散布図の減色が戻っていません")

    dlg._dirty = False
    dlg.done(0)


def test_scatter_rect(app):
    """散布図のドラッグによる矩形選択と、空クリックでの選択解除を確認する。"""
    from qgis.PyQt.QtCore import QEvent, QPoint, QRectF
    from qgis.PyQt.QtGui import QMouseEvent

    # Red(2列目)・NIR(3列目)が格子状に並ぶように平均値を置く
    means = np.zeros((6, 5))
    pts = [(0, 0), (1, 0), (2, 0), (0, 2), (1, 2), (2, 2)]
    for i, (x, y) in enumerate(pts):
        means[i, 2] = 1000 + x * 1000
        means[i, 3] = 1000 + y * 1000
    sig = rc.Signature(band_names=["Blue", "Green", "Red", "NIR", "NDVI"],
                       means=means, covs=np.stack([np.eye(5)] * 6),
                       counts=np.arange(6) + 10,
                       labels=["A", "A", "B", "A", "B", "B"],
                       colors=["#cccccc"] * 6)
    highlights = []
    dlg = ld.LabelDialog(sig, None,
                         on_highlight=lambda i, s, d: highlights.append(i))
    dlg.resize(900, 600)
    dlg.show()
    app.processEvents()
    sc = dlg.scatter

    def send(kind, pos, button=Qt.LeftButton, mod=Qt.NoModifier):
        buttons = Qt.NoButton if kind == QEvent.MouseButtonRelease \
            else Qt.LeftButton
        ev = QMouseEvent(kind, pos, button, buttons, mod)
        QApplication.sendEvent(sc, ev)
        app.processEvents()

    def drag(a, b, mod=Qt.NoModifier):
        send(QEvent.MouseButtonPress, a, mod=mod)
        mid = QPoint((a.x() + b.x()) // 2, (a.y() + b.y()) // 2)
        send(QEvent.MouseMove, mid, mod=mod)
        send(QEvent.MouseMove, b, mod=mod)
        send(QEvent.MouseButtonRelease, b, mod=mod)

    def screen(i):
        p = sc._to_screen(i)
        return QPoint(int(p.x()), int(p.y()))

    def around(indices, pad=12):
        xs = [screen(i).x() for i in indices]
        ys = [screen(i).y() for i in indices]
        return (QPoint(min(xs) - pad, min(ys) - pad),
                QPoint(max(xs) + pad, max(ys) + pad))

    # 下段の左2点（0, 1）を囲む
    a, b = around([0, 1])
    drag(a, b)
    require(dlg.selected_classes() == [0, 1],
            "矩形選択の結果が違います: %s" % dlg.selected_classes())
    require(dlg.table.currentRow() in (0, 1), "現在行が選択範囲外です")

    # 右下から左上へ逆向きに囲んでも同じように選べる（上段 3, 4）
    a, b = around([3, 4])
    drag(b, a)
    require(dlg.selected_classes() == [3, 4],
            "逆向きドラッグの結果が違います: %s" % dlg.selected_classes())

    # Ctrl ドラッグは選択に追加
    a, b = around([2])
    drag(a, b, Qt.ControlModifier)
    require(dlg.selected_classes() == [2, 3, 4],
            "Ctrl ドラッグで追加されません: %s" % dlg.selected_classes())

    # ドラッグ中は矩形を描き、離すと消える
    send(QEvent.MouseButtonPress, a)
    send(QEvent.MouseMove, b)
    require(isinstance(sc._drag_rect, QRectF), "ドラッグ中の矩形がありません")
    sc.grab()  # 描画で落ちないこと
    send(QEvent.MouseButtonRelease, b)
    require(sc._drag_rect is None, "ドラッグ後に矩形が残っています")

    # 何もない所をクリックすると無選択
    c = sc._plot_rect().center()
    empty = QPoint(int(c.x()), int(c.y()))
    require(sc.hit_test(empty) < 0, "テスト用の空き位置が点に近すぎます")
    dlg._select_row(0)
    send(QEvent.MouseButtonPress, empty)
    send(QEvent.MouseButtonRelease, empty)
    require(dlg.selected_classes() == [], "空クリックで選択が解除されません")
    require(dlg.table.currentRow() == -1 and sc._current == -1,
            "空クリック後も現在行が残っています")
    require(not dlg.color_button.isEnabled(), "無選択でも色変更が押せます")
    dlg._highlight_timer.stop()
    dlg._update_highlight()
    require(dlg._highlighted == [], "無選択なのに地図のハイライトが残っています")

    # Ctrl を押した空クリックでは選択を保つ
    dlg._select_row(1)
    send(QEvent.MouseButtonPress, empty, mod=Qt.ControlModifier)
    send(QEvent.MouseButtonRelease, empty, mod=Qt.ControlModifier)
    require(dlg.selected_classes() == [1], "Ctrl 空クリックで選択が消えました")

    # 何もない範囲を囲むと無選択
    drag(empty - QPoint(20, 20), empty + QPoint(20, 20))
    require(dlg.selected_classes() == [], "空の矩形で選択が解除されません")

    # 絞り込み中は、一覧に出ていない（薄い）点は矩形で選ばれない
    dlg._select_row(0)  # カテゴリ A
    dlg.category_filter_check.setChecked(True)
    app.processEvents()
    a, b = around(range(6))
    drag(a, b)
    require(dlg.selected_classes() == [0, 1, 3],
            "絞り込み中の矩形選択が違います: %s" % dlg.selected_classes())
    # 無選択にしても絞り込みカテゴリは保つ
    send(QEvent.MouseButtonPress, empty)
    send(QEvent.MouseButtonRelease, empty)
    require(dlg._filter_label == "A"
            and [dlg.table.isRowHidden(i) for i in range(6)]
            == [False, False, True, False, True, True],
            "無選択で絞り込みが崩れました")

    dlg._dirty = False
    dlg.done(0)


def main():
    QCoreApplication.setOrganizationName("fsc-test")
    QCoreApplication.setApplicationName("fsc-test")
    app = QApplication.instance() or QApplication([])
    test_category_filter(app)
    test_scatter_rect(app)
    rng = np.random.default_rng(0)
    sig = rc.Signature(band_names=["Blue", "Green", "Red", "NIR", "NDVI"],
                       means=rng.random((6, 5)) * 3000,
                       covs=np.stack([np.eye(5)] * 6),
                       counts=np.arange(6) + 10,
                       labels=["未分類"] * 6, colors=["#cccccc"] * 6)
    highlights = []
    dims = []

    def on_highlight(i, s, d):
        highlights.append(i)
        dims.append(d)

    dlg = ld.LabelDialog(sig, None, on_apply=lambda s: None,
                         on_highlight=on_highlight)
    dlg.show()
    app.processEvents()
    t = dlg.table

    def click(row, mod=Qt.NoModifier):
        rect = t.visualRect(t.model().index(row, 0))
        QTest.mouseClick(t.viewport(), Qt.LeftButton, mod, rect.center())
        app.processEvents()

    def pick(row, steps):
        combo = t.cellWidget(row, 4)
        QTest.mouseClick(combo, Qt.LeftButton)
        app.processEvents()
        for _ in range(steps):
            QTest.keyClick(combo.view(), Qt.Key_Down)
        QTest.keyClick(combo.view(), Qt.Key_Return)
        app.processEvents()
        return combo.currentText()

    def labels():
        return [t.cellWidget(i, 4).currentText() for i in range(6)]

    click(1)
    click(3, Qt.ControlModifier)
    click(4, Qt.ControlModifier)
    require(dlg.selected_classes() == [1, 3, 4], "Ctrl クリックで複数選択できません")
    require("3 クラスを選択中" in dlg.selection_info.text(), "選択数が表示されません")

    text = pick(4, 2)
    require(labels() == ["未分類", text, "未分類", text, text, "未分類"],
            "選択中のクラスがまとめて変わりません: %s" % labels())
    require(dlg.selected_classes() == [1, 3, 4],
            "プルダウン操作で複数選択が解除されました")
    require(len({dlg._work.colors[i] for i in (1, 3, 4)}) == 1,
            "色がまとめて変わっていません")
    require(sig.labels == ["未分類"] * 6, "適用前に元のシグネチャが変わりました")

    other = pick(5, 3)
    require(labels()[5] == other and labels()[1] == text,
            "選択外の行の変更が他に波及しました: %s" % labels())

    click(0, Qt.ShiftModifier)
    require(len(dlg.selected_classes()) > 1, "Shift クリックで範囲選択できません")
    click(2)
    require(dlg.selected_classes() == [2], "通常クリックで単独選択に戻りません")

    dlg._update_highlight()
    require(highlights and highlights[-1] == [2],
            "地図のハイライトが呼ばれていません: %s" % highlights)
    require(dims and not any(dims),
            "地図上のほかのクラスが薄く表示されています: %s" % dims)

    dlg._dirty = False
    dlg.done(0)
    print("すべて通りました。")


if __name__ == "__main__":
    main()
