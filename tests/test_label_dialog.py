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


def main():
    QCoreApplication.setOrganizationName("fsc-test")
    QCoreApplication.setApplicationName("fsc-test")
    app = QApplication.instance() or QApplication([])
    rng = np.random.default_rng(0)
    sig = rc.Signature(band_names=["Blue", "Green", "Red", "NIR", "NDVI"],
                       means=rng.random((6, 5)) * 3000,
                       covs=np.stack([np.eye(5)] * 6),
                       counts=np.arange(6) + 10,
                       labels=["未分類"] * 6, colors=["#cccccc"] * 6)
    highlights = []
    dlg = ld.LabelDialog(sig, None, on_apply=lambda s: None,
                         on_highlight=lambda i, s, d: highlights.append(i))
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

    dlg._dirty = False
    dlg.done(0)
    print("すべて通りました。")


if __name__ == "__main__":
    main()
