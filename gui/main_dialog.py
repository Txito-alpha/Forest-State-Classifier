# -*- coding: utf-8 -*-
"""
main_dialog - 教師なし分類の設定画面

License: GPL v2
"""

from __future__ import annotations

import os
from typing import List, Optional

from qgis.PyQt.QtCore import QSettings, Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSpinBox,
    QVBoxLayout,
)
from qgis.core import QgsMapLayerProxyModel, QgsProject, QgsRasterLayer
from qgis.gui import QgsMapLayerComboBox

SETTINGS_GROUP = "HokkaidoITTeam/RinkyoClassifier"


class MainDialog(QDialog):
    """入力画像・クラス数・出力先を決める。設定は QSettings に残す。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("衛星画像の教師なし分類"))
        self.resize(560, 620)
        self.signature_path: Optional[str] = None

        # --- 入力 ---------------------------------------------------------
        self.layer_combo = QgsMapLayerComboBox(self)
        self.layer_combo.setFilters(QgsMapLayerProxyModel.RasterLayer)
        self.band_list = QListWidget(self)
        self.band_list.setMaximumHeight(140)
        self.band_list.setToolTip(self.tr(
            "分類に使うバンドを選びます。Blue/Green/Red/NIR/NDVI の\n"
            "5バンド構成が既定です。"))

        input_box = QGroupBox(self.tr("入力画像"), self)
        input_form = QFormLayout(input_box)
        input_form.addRow(self.tr("ラスタレイヤ"), self.layer_combo)
        input_form.addRow(self.tr("使用バンド"), self.band_list)

        # --- 分類設定 -----------------------------------------------------
        self.n_classes = QSpinBox(self)
        self.n_classes.setRange(2, 255)
        self.n_classes.setValue(50)
        self.n_classes.setToolTip(self.tr("初期クラス数。マニュアルの手順では 50。"))

        self.max_samples = QSpinBox(self)
        self.max_samples.setRange(1000, 5000000)
        self.max_samples.setSingleStep(10000)
        self.max_samples.setValue(100000)

        self.min_class_size = QSpinBox(self)
        self.min_class_size.setRange(2, 10000)
        self.min_class_size.setValue(17)

        self.min_separation = QDoubleSpinBox(self)
        self.min_separation.setRange(0.0, 5.0)
        self.min_separation.setSingleStep(0.05)
        self.min_separation.setValue(0.0)
        self.min_separation.setToolTip(self.tr(
            "0 より大きくすると、近すぎるクラス同士を統合します。\n"
            "標準化した空間での距離なので 0.2〜0.5 程度が目安です。"))

        self.max_iterations = QSpinBox(self)
        self.max_iterations.setRange(1, 200)
        self.max_iterations.setValue(30)

        self.convergence = QDoubleSpinBox(self)
        self.convergence.setRange(50.0, 100.0)
        self.convergence.setValue(98.0)
        self.convergence.setSuffix(" %")

        param_box = QGroupBox(self.tr("クラスタリング"), self)
        param_form = QFormLayout(param_box)
        param_form.addRow(self.tr("初期クラス数"), self.n_classes)
        param_form.addRow(self.tr("標本画素数"), self.max_samples)
        param_form.addRow(self.tr("最小クラス標本数"), self.min_class_size)
        param_form.addRow(self.tr("クラス統合距離"), self.min_separation)
        param_form.addRow(self.tr("最大反復回数"), self.max_iterations)
        param_form.addRow(self.tr("収束判定"), self.convergence)

        # --- シグネチャの再利用 -------------------------------------------
        self.reuse_check = QCheckBox(self.tr("既存のシグネチャを適用する"), self)
        self.reuse_check.setToolTip(self.tr(
            "別の年次の画像に同じ分類基準を当てます。\n"
            "クラス番号の意味が揃うので、経年比較がそのままできます。"))
        self.reuse_edit = QLineEdit(self)
        self.reuse_edit.setEnabled(False)
        self.reuse_button = QPushButton(self.tr("参照…"), self)
        self.reuse_button.setEnabled(False)
        reuse_row = QHBoxLayout()
        reuse_row.addWidget(self.reuse_edit, 1)
        reuse_row.addWidget(self.reuse_button)

        reuse_box = QGroupBox(self.tr("経年比較"), self)
        reuse_layout = QVBoxLayout(reuse_box)
        reuse_layout.addWidget(self.reuse_check)
        reuse_layout.addLayout(reuse_row)

        # --- 出力 ---------------------------------------------------------
        self.out_dir = QLineEdit(self)
        self.out_button = QPushButton(self.tr("参照…"), self)
        out_row = QHBoxLayout()
        out_row.addWidget(self.out_dir, 1)
        out_row.addWidget(self.out_button)

        self.basename = QLineEdit(self)
        self.basename.setPlaceholderText(self.tr("出力ファイル名の接頭辞"))

        self.likelihood_check = QCheckBox(self.tr("対数尤度ラスタも出力する"), self)
        self.likelihood_check.setChecked(True)
        self.likelihood_check.setToolTip(self.tr(
            "値が低い画素は、どのクラスにも似ていない場所です。\n"
            "現地確認の優先順位付けに使えます。"))

        out_box = QGroupBox(self.tr("出力"), self)
        out_form = QFormLayout(out_box)
        out_form.addRow(self.tr("出力フォルダ"), out_row)
        out_form.addRow(self.tr("ファイル名"), self.basename)
        out_form.addRow("", self.likelihood_check)

        self.note = QLabel(self.tr(
            "出力先に日本語や空白を含むパスも使えます。"
            "処理はバックグラウンドで動くので、実行中も QGIS を操作できます。"),
            self)
        self.note.setWordWrap(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, Qt.Horizontal, self)
        buttons.button(QDialogButtonBox.Ok).setText(self.tr("実行"))
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addWidget(input_box)
        root.addWidget(param_box)
        root.addWidget(reuse_box)
        root.addWidget(out_box)
        root.addWidget(self.note)
        root.addStretch(1)
        root.addWidget(buttons)

        self.layer_combo.layerChanged.connect(self._fill_bands)
        self.out_button.clicked.connect(self._pick_out_dir)
        self.reuse_button.clicked.connect(self._pick_signature)
        self.reuse_check.toggled.connect(self._toggle_reuse)

        self._restore()
        self._fill_bands(self.layer_combo.currentLayer())

    # -- バンド一覧 --------------------------------------------------------
    def _fill_bands(self, layer) -> None:
        self.band_list.clear()
        if not isinstance(layer, QgsRasterLayer) or not layer.isValid():
            return
        provider = layer.dataProvider()
        for i in range(1, provider.bandCount() + 1):
            name = provider.generateBandName(i)
            desc = layer.bandDescription(i) if hasattr(
                layer, "bandDescription") else ""
            text = "%d: %s" % (i, desc or name)
            item = QListWidgetItem(text, self.band_list)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            item.setData(Qt.UserRole, i)
        if not self.basename.text():
            self.basename.setText(_safe_name(layer.name()))

    def selected_bands(self) -> List[int]:
        out = []
        for row in range(self.band_list.count()):
            item = self.band_list.item(row)
            if item.checkState() == Qt.Checked:
                out.append(int(item.data(Qt.UserRole)))
        return out

    # -- 操作 --------------------------------------------------------------
    def _toggle_reuse(self, checked: bool) -> None:
        self.reuse_edit.setEnabled(checked)
        self.reuse_button.setEnabled(checked)
        for w in (self.n_classes, self.max_samples, self.min_class_size,
                  self.min_separation, self.max_iterations, self.convergence):
            w.setEnabled(not checked)

    def _pick_out_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, self.tr("出力フォルダ"), self.out_dir.text())
        if path:
            self.out_dir.setText(path)

    def _pick_signature(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("シグネチャファイル"), self.out_dir.text(),
            self.tr("シグネチャ (*.json);;すべて (*.*)"))
        if path:
            self.reuse_edit.setText(path)

    def _on_accept(self) -> None:
        layer = self.layer_combo.currentLayer()
        if layer is None or not layer.isValid():
            QMessageBox.warning(self, self.tr("確認"), self.tr("入力ラスタを選んでください。"))
            return
        if layer.providerType() != "gdal":
            QMessageBox.warning(
                self, self.tr("確認"),
                self.tr("GDAL で読めるファイルベースのラスタを指定してください。"))
            return
        bands = self.selected_bands()
        if len(bands) < 2:
            QMessageBox.warning(self, self.tr("確認"),
                                self.tr("バンドを2つ以上選んでください。"))
            return
        if not self.out_dir.text().strip():
            QMessageBox.warning(self, self.tr("確認"), self.tr("出力フォルダを指定してください。"))
            return
        if not self.basename.text().strip():
            QMessageBox.warning(self, self.tr("確認"), self.tr("ファイル名を入力してください。"))
            return
        if self.reuse_check.isChecked() and not os.path.isfile(
                self.reuse_edit.text()):
            QMessageBox.warning(self, self.tr("確認"),
                                self.tr("シグネチャファイルが見つかりません。"))
            return
        self._save()
        self.accept()

    # -- 値の取り出し ------------------------------------------------------
    def parameters(self) -> dict:
        layer = self.layer_combo.currentLayer()
        return {
            "stack_path": layer.source().split("|")[0],
            "layer_name": layer.name(),
            "bands": self.selected_bands(),
            "out_dir": self.out_dir.text().strip(),
            "basename": _safe_name(self.basename.text().strip()),
            "n_classes": self.n_classes.value(),
            "max_samples": self.max_samples.value(),
            "min_class_size": self.min_class_size.value(),
            "min_separation": self.min_separation.value(),
            "max_iterations": self.max_iterations.value(),
            "convergence": self.convergence.value(),
            "write_likelihood": self.likelihood_check.isChecked(),
            "signature_path": (self.reuse_edit.text()
                               if self.reuse_check.isChecked() else None),
        }

    # -- 設定の保存と復元 --------------------------------------------------
    def _save(self) -> None:
        s = QSettings()
        s.beginGroup(SETTINGS_GROUP)
        s.setValue("out_dir", self.out_dir.text())
        s.setValue("n_classes", self.n_classes.value())
        s.setValue("max_samples", self.max_samples.value())
        s.setValue("min_class_size", self.min_class_size.value())
        s.setValue("min_separation", self.min_separation.value())
        s.setValue("max_iterations", self.max_iterations.value())
        s.setValue("convergence", self.convergence.value())
        s.setValue("write_likelihood", self.likelihood_check.isChecked())
        s.endGroup()

    def _restore(self) -> None:
        s = QSettings()
        s.beginGroup(SETTINGS_GROUP)
        self.out_dir.setText(s.value("out_dir", "", type=str))
        self.n_classes.setValue(s.value("n_classes", 50, type=int))
        self.max_samples.setValue(s.value("max_samples", 100000, type=int))
        self.min_class_size.setValue(s.value("min_class_size", 17, type=int))
        self.min_separation.setValue(
            s.value("min_separation", 0.0, type=float))
        self.max_iterations.setValue(s.value("max_iterations", 30, type=int))
        self.convergence.setValue(s.value("convergence", 98.0, type=float))
        self.likelihood_check.setChecked(
            s.value("write_likelihood", True, type=bool))
        s.endGroup()
        if not self.out_dir.text():
            project_path = QgsProject.instance().homePath()
            if project_path:
                self.out_dir.setText(project_path)


def _safe_name(text: str) -> str:
    """ファイル名に使いにくい文字を落とす。"""
    bad = '\\/:*?"<>| '
    out = "".join("_" if c in bad else c for c in (text or "").strip())
    return out or "rinkyo"
