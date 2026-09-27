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
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QSpinBox, QStackedWidget, QTabWidget, QVBoxLayout, QWidget,
)
from qgis.core import (
    QgsCoordinateReferenceSystem, QgsMapLayerProxyModel, QgsProject,
    QgsRasterLayer, QgsRectangle,
)
from qgis.gui import QgsExtentGroupBox, QgsFileWidget, QgsMapLayerComboBox

from ..core import batch, pipeline

RASTER_FILTER = ("ラスタ (*.tif *.tiff *.vrt *.jp2 *.img);;すべて (*.*)")

SETTINGS_GROUP = "HokkaidoITTeam/RinkyoClassifier"


class MainDialog(QDialog):
    """入力画像・クラス数・出力先を決める。設定は QSettings に残す。"""

    def __init__(self, iface=None, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.setWindowTitle(self.tr("衛星画像の教師なし分類"))
        self.resize(600, 620)
        self.signature_path: Optional[str] = None

        # --- 入力 ---------------------------------------------------------
        self._single_files: List[str] = []
        self._folder_infos: List[batch.RasterInfo] = []

        self.input_mode = QComboBox(self)
        self.input_mode.addItem(self.tr("単一ファイル"), pipeline.MODE_SINGLE)
        self.input_mode.addItem(self.tr("フォルダ（再帰検索）"), "folder")
        self.input_mode.setToolTip(self.tr(
            "単一ファイル: プロジェクトのラスタレイヤ、またはファイルを直接指定します。\n"
            "フォルダ: フォルダ内のラスタを探して、まとめて処理します。"))

        # 単一ファイル
        self.layer_combo = QgsMapLayerComboBox(self)
        self.layer_combo.setFilters(QgsMapLayerProxyModel.RasterLayer)
        self.layer_combo.setAllowEmptyLayer(False)
        self.file_button = QPushButton(self.tr("ファイル…"), self)
        self.file_button.setToolTip(self.tr(
            "プロジェクトに読み込んでいないファイルを直接指定します。"))
        single_row = QHBoxLayout()
        single_row.setContentsMargins(0, 0, 0, 0)
        single_row.addWidget(self.layer_combo, 1)
        single_row.addWidget(self.file_button)
        single_page = QWidget(self)
        single_form = QFormLayout(single_page)
        single_form.setContentsMargins(0, 0, 0, 0)
        single_form.addRow(self.tr("ラスタ"), single_row)

        # フォルダ
        self.folder_widget = QgsFileWidget(self)
        self.folder_widget.setStorageMode(QgsFileWidget.GetDirectory)
        self.folder_widget.setDialogTitle(self.tr("入力フォルダ"))
        self.recursive_check = QCheckBox(self.tr("サブフォルダも検索する"), self)
        self.recursive_check.setChecked(True)
        self.pattern_edit = QLineEdit(self)
        self.pattern_edit.setPlaceholderText(batch.DEFAULT_PATTERNS)
        self.pattern_edit.setToolTip(self.tr(
            "検索するファイル名のパターン。「;」区切りで複数指定できます。\n"
            "大文字小文字は区別しません。*_class.tif などこのプラグインの\n"
            "出力と、出力フォルダの中は自動的に除外します。"))
        self.search_button = QPushButton(self.tr("再検索"), self)
        pattern_row = QHBoxLayout()
        pattern_row.setContentsMargins(0, 0, 0, 0)
        pattern_row.addWidget(self.pattern_edit, 1)
        pattern_row.addWidget(self.search_button)

        self.file_list = QListWidget(self)
        self.file_list.setMaximumHeight(110)
        self.file_list.setToolTip(self.tr(
            "チェックを外したファイルは処理しません。\n"
            "読めないファイルやバンド数が1本目と違うファイルは、"
            "最初からチェックを外しています。"))
        self.file_count = QLabel(self)
        self.check_all = QPushButton(self.tr("全選択"), self)
        self.check_none = QPushButton(self.tr("全解除"), self)
        count_row = QHBoxLayout()
        count_row.setContentsMargins(0, 0, 0, 0)
        count_row.addWidget(self.file_count, 1)
        count_row.addWidget(self.check_all)
        count_row.addWidget(self.check_none)

        self.folder_mode = QComboBox(self)
        self.folder_mode.addItem(
            self.tr("モザイク（VRT にまとめて 1 つの分類結果）"),
            pipeline.MODE_MOSAIC)
        self.folder_mode.addItem(
            self.tr("ファイルごと（ファイル単位で分類結果を出力）"),
            pipeline.MODE_EACH)
        self.folder_mode.setToolTip(self.tr(
            "モザイク: 隣接する図郭などを一続きの画像として扱います。\n"
            "  座標参照系とバンド数が揃っている必要があります。\n"
            "ファイルごと: 年次違い・地域違いの画像を順番に処理します。"))
        self.shared_check = QCheckBox(
            self.tr("全ファイル共通のクラス定義にする"), self)
        self.shared_check.setChecked(True)
        self.shared_check.setToolTip(self.tr(
            "全ファイルから標本を集めて 1 回だけクラスタリングし、\n"
            "同じシグネチャで全ファイルを分類します。クラス番号の意味が\n"
            "ファイル間で揃い、意味づけも 1 回で済みます。\n"
            "外すと、ファイルごとに別々のクラス定義を作ります。"))
        self.keep_tree_check = QCheckBox(
            self.tr("入力のフォルダ構成を出力先に再現する"), self)
        self.keep_tree_check.setChecked(True)
        each_opts = QVBoxLayout()
        each_opts.setContentsMargins(0, 0, 0, 0)
        each_opts.addWidget(self.shared_check)
        each_opts.addWidget(self.keep_tree_check)

        folder_page = QWidget(self)
        folder_form = QFormLayout(folder_page)
        folder_form.setContentsMargins(0, 0, 0, 0)
        folder_form.addRow(self.tr("フォルダ"), self.folder_widget)
        folder_form.addRow("", self.recursive_check)
        folder_form.addRow(self.tr("パターン"), pattern_row)
        folder_form.addRow(self.tr("対象ファイル"), self.file_list)
        folder_form.addRow("", count_row)
        folder_form.addRow(self.tr("処理方法"), self.folder_mode)
        folder_form.addRow("", each_opts)

        self.input_stack = QStackedWidget(self)
        self.input_stack.addWidget(single_page)
        self.input_stack.addWidget(folder_page)

        self.band_list = QListWidget(self)
        self.band_list.setMaximumHeight(120)
        self.band_list.setToolTip(self.tr(
            "分類に使うバンドを選びます。Blue/Green/Red/NIR/NDVI の\n"
            "5バンド構成が既定です。フォルダ入力では1本目のファイルの\n"
            "バンド構成を表示し、同じバンド番号を全ファイルに使います。"))

        input_box = QGroupBox(self.tr("入力画像"), self)
        input_form = QFormLayout(input_box)
        input_form.addRow(self.tr("入力モード"), self.input_mode)
        input_form.addRow(self.input_stack)
        input_form.addRow(self.tr("使用バンド"), self.band_list)

        # --- 処理範囲（VRT など巨大な入力を軽くするため） -------------------
        self.extent_box = QgsExtentGroupBox(self)
        self.extent_box.setTitle(self.tr("処理範囲を絞る（任意）"))
        self.extent_box.setCheckable(True)
        self.extent_box.setChecked(False)
        self.extent_box.setToolTip(self.tr(
            "VRT などサイズの大きい画像を、指定範囲だけに切り出してから\n"
            "処理します。「現在のキャンバス範囲」または地図上での指定が\n"
            "使えます。"))
        if self.iface is not None:
            self.extent_box.setMapCanvas(self.iface.mapCanvas())
        else:
            self.extent_box.setOutputCrs(QgsProject.instance().crs())

        self.mask_combo = QgsMapLayerComboBox(self)
        self.mask_combo.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        self.mask_combo.setAllowEmptyLayer(True)
        self.mask_combo.setCurrentIndex(0)
        self.mask_selected_only = QCheckBox(self.tr("選択地物のみを使う"), self)
        mask_row = QHBoxLayout()
        mask_row.addWidget(self.mask_combo, 1)
        mask_row.addWidget(self.mask_selected_only)

        extent_extra = QFormLayout()
        extent_extra.addRow(self.tr("ポリゴンで切り抜く（任意）"), mask_row)
        self.extent_box.layout().addLayout(extent_extra)

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

        self.existing_combo = QComboBox(self)
        self.existing_combo.addItem(self.tr("スキップする"),
                                    pipeline.EXISTING_SKIP)
        self.existing_combo.addItem(self.tr("上書きする"),
                                    pipeline.EXISTING_OVERWRITE)
        self.existing_combo.addItem(self.tr("別名で保存する（_2 を付ける）"),
                                    pipeline.EXISTING_RENAME)
        self.existing_combo.setToolTip(self.tr(
            "フォルダ入力で、出力先に同名の分類結果（<名前>_class.tif）が\n"
            "既にある場合の扱いです。\n"
            "スキップ: そのファイルは処理しません。途中で止まった処理を\n"
            "  続きから流し直すときに使えます。\n"
            "上書き: 前の結果を置き換えます。\n"
            "別名: <名前>_2_class.tif のように連番を付けて残します。\n"
            "単一ファイルモードでは常に上書きします。"))

        self.likelihood_check = QCheckBox(self.tr("対数尤度ラスタも出力する"), self)
        self.likelihood_check.setChecked(True)
        self.likelihood_check.setToolTip(self.tr(
            "値が低い画素は、どのクラスにも似ていない場所です。\n"
            "現地確認の優先順位付けに使えます。"))

        out_box = QGroupBox(self.tr("出力"), self)
        out_form = QFormLayout(out_box)
        out_form.addRow(self.tr("出力フォルダ"), out_row)
        out_form.addRow(self.tr("ファイル名"), self.basename)
        out_form.addRow(self.tr("同名の出力があるとき"), self.existing_combo)
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

        # 縦に長くなりすぎて画面に収まらないディスプレイがあるため、
        # 入出力（入力画像・処理範囲・出力）とパラメータ（クラスタリング・
        # 経年比較）でタブを分ける。実行・キャンセルはタブの外に固定する。
        io_page = QWidget(self)
        io_layout = QVBoxLayout(io_page)
        io_layout.setContentsMargins(0, 6, 0, 0)
        io_layout.addWidget(input_box)
        io_layout.addWidget(self.extent_box)
        io_layout.addWidget(out_box)
        io_layout.addWidget(self.note)
        io_layout.addStretch(1)

        param_page = QWidget(self)
        param_layout = QVBoxLayout(param_page)
        param_layout.setContentsMargins(0, 6, 0, 0)
        param_layout.addWidget(param_box)
        param_layout.addWidget(reuse_box)
        param_layout.addStretch(1)

        self.tabs = QTabWidget(self)
        self.tabs.addTab(io_page, self.tr("入出力"))
        self.tabs.addTab(param_page, self.tr("パラメータ"))

        root = QVBoxLayout(self)
        root.addWidget(self.tabs, 1)
        root.addWidget(buttons)

        self.layer_combo.currentIndexChanged.connect(
            lambda _i: self._refresh_bands())
        self.file_button.clicked.connect(self._pick_input_file)
        self.input_mode.currentIndexChanged.connect(self._on_mode_changed)
        self.folder_mode.currentIndexChanged.connect(self._update_folder_ui)
        self.folder_widget.fileChanged.connect(lambda _p: self._search())
        self.recursive_check.toggled.connect(lambda _c: self._search())
        self.pattern_edit.editingFinished.connect(self._search)
        self.search_button.clicked.connect(self._search)
        self.check_all.clicked.connect(lambda: self._check_files(True))
        self.check_none.clicked.connect(lambda: self._check_files(False))
        self.file_list.itemChanged.connect(lambda _i: self._update_count())
        self.out_button.clicked.connect(self._pick_out_dir)
        self.reuse_button.clicked.connect(self._pick_signature)
        self.reuse_check.toggled.connect(self._toggle_reuse)

        self._restore()
        self._on_mode_changed()

    # -- 入力モード --------------------------------------------------------
    def is_folder_mode(self) -> bool:
        return self.input_mode.currentData() == "folder"

    def pipeline_mode(self) -> str:
        if not self.is_folder_mode():
            return pipeline.MODE_SINGLE
        return self.folder_mode.currentData()

    def _on_mode_changed(self, *_args) -> None:
        folder = self.is_folder_mode()
        self.input_stack.setCurrentIndex(1 if folder else 0)
        if folder and not self._folder_infos and self.folder_widget.filePath():
            self._search()
        self._update_folder_ui()
        self._refresh_bands()

    def _update_folder_ui(self, *_args) -> None:
        each = self.pipeline_mode() == pipeline.MODE_EACH
        self.existing_combo.setEnabled(self.is_folder_mode())
        self.shared_check.setEnabled(each and not self.reuse_check.isChecked())
        self.keep_tree_check.setEnabled(each)
        if each:
            self.basename.setPlaceholderText(
                self.tr("接頭辞（任意）: <接頭辞>_<元のファイル名>_class.tif"))
        else:
            self.basename.setPlaceholderText(self.tr("出力ファイル名の接頭辞"))

    def _pick_input_file(self) -> None:
        start = self._single_files[-1] if self._single_files else ""
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("入力ラスタ"), start, self.tr(RASTER_FILTER))
        if not path:
            return
        path = os.path.normpath(path)
        if path not in self._single_files:
            self._single_files.append(path)
            self.layer_combo.setAdditionalItems(self._single_files)
        idx = self.layer_combo.findText(path)
        if idx >= 0:
            self.layer_combo.setCurrentIndex(idx)
        self.basename.setText(_safe_name(
            os.path.splitext(os.path.basename(path))[0]))
        self._refresh_bands()

    def single_source(self) -> Optional[str]:
        """単一ファイルモードの入力パス（レイヤ or 直接指定ファイル）。"""
        layer = self.layer_combo.currentLayer()
        if isinstance(layer, QgsRasterLayer):
            return layer.source().split("|")[0]
        text = self.layer_combo.currentText()
        return text if text in self._single_files else None

    # -- フォルダ検索 ------------------------------------------------------
    def _search(self) -> None:
        folder = self.folder_widget.filePath().strip()
        self.file_list.blockSignals(True)
        self.file_list.clear()
        self.file_list.blockSignals(False)
        self._folder_infos = []
        if not folder:
            self._update_count()
            self._refresh_bands()
            return
        if not os.path.isdir(folder):
            self.file_count.setText(self.tr("フォルダが見つかりません"))
            self._refresh_bands()
            return
        out_dir = self.out_dir.text().strip()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            paths = batch.find_rasters(
                folder, batch.parse_patterns(self.pattern_edit.text()),
                recursive=self.recursive_check.isChecked(),
                exclude_dirs=[out_dir] if out_dir else [])
            infos = []
            for p in paths:
                infos.append(batch.raster_info(p))
                QApplication.processEvents()
        finally:
            QApplication.restoreOverrideCursor()

        ref = next((i for i in infos if i.ok), None)
        self.file_list.blockSignals(True)
        for info in infos:
            rel = os.path.relpath(info.path, folder)
            if info.ok:
                text = "%s  (%d バンド)" % (rel, info.band_count)
            else:
                text = "%s  (読めません)" % rel
            item = QListWidgetItem(text, self.file_list)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setData(Qt.UserRole, info.path)
            usable = info.ok and ref is not None and (
                info.band_count == ref.band_count)
            item.setCheckState(Qt.Checked if usable else Qt.Unchecked)
            if not info.ok:
                item.setToolTip(info.error)
            elif not usable:
                item.setToolTip(self.tr("バンド数が1本目（%s）と異なります")
                                % os.path.basename(ref.path))
            else:
                item.setToolTip(info.path)
        self.file_list.blockSignals(False)
        self._folder_infos = infos
        if not self.basename.text().strip() and \
                self.pipeline_mode() != pipeline.MODE_EACH:
            self.basename.setText(_safe_name(os.path.basename(
                os.path.normpath(folder))))
        self._update_count()
        self._refresh_bands()

    def _check_files(self, on: bool) -> None:
        self.file_list.blockSignals(True)
        for row in range(self.file_list.count()):
            self.file_list.item(row).setCheckState(
                Qt.Checked if on else Qt.Unchecked)
        self.file_list.blockSignals(False)
        self._update_count()

    def _update_count(self) -> None:
        total = self.file_list.count()
        checked = len(self.selected_files())
        if total == 0:
            self.file_count.setText(self.tr("ラスタが見つかりません"))
        else:
            self.file_count.setText(
                self.tr("%d 件見つかりました（%d 件を処理）") % (total, checked))

    def selected_files(self) -> List[str]:
        out = []
        for row in range(self.file_list.count()):
            item = self.file_list.item(row)
            if item.checkState() == Qt.Checked:
                out.append(item.data(Qt.UserRole))
        return out

    # -- バンド一覧 --------------------------------------------------------
    def _refresh_bands(self) -> None:
        """現在の入力からバンド一覧と処理範囲の既定値を作り直す。"""
        if self.is_folder_mode():
            chosen = set(self.selected_files())
            infos = [i for i in self._folder_infos
                     if i.ok and (i.path in chosen or not chosen)]
            if not infos:
                self._fill_bands_list([])
                return
            ref = infos[0]
            crs = QgsCoordinateReferenceSystem.fromWkt(ref.wkt)
            same = [i for i in infos if i.crs_key == ref.crs_key]
            box = batch.union_bounds(same)
            self._set_extent(QgsRectangle(*box), crs)
            self._fill_bands_list(ref.band_names or [""] * ref.band_count)
            return

        layer = self.layer_combo.currentLayer()
        if isinstance(layer, QgsRasterLayer) and layer.isValid():
            self._set_extent(layer.extent(), layer.crs())
            provider = layer.dataProvider()
            names = []
            for i in range(1, provider.bandCount() + 1):
                desc = layer.bandDescription(i) if hasattr(
                    layer, "bandDescription") else ""
                names.append(desc or provider.generateBandName(i))
            self._fill_bands_list(names)
            if not self.basename.text():
                self.basename.setText(_safe_name(layer.name()))
            return

        path = self.single_source()
        info = batch.raster_info(path) if path else None
        if info is None or not info.ok:
            self._fill_bands_list([])
            return
        self._set_extent(QgsRectangle(*info.bounds),
                         QgsCoordinateReferenceSystem.fromWkt(info.wkt))
        self._fill_bands_list(info.band_names or [""] * info.band_count)

    def _set_extent(self, rect, crs) -> None:
        self.extent_box.setOriginalExtent(rect, crs)
        self.extent_box.setOutputCrs(crs)

    def _fill_bands_list(self, names: List[str]) -> None:
        previous = {}
        for row in range(self.band_list.count()):
            item = self.band_list.item(row)
            previous[int(item.data(Qt.UserRole))] = item.checkState()
        same_layout = len(previous) == len(names)
        self.band_list.clear()
        for i, name in enumerate(names, start=1):
            text = "%d: %s" % (i, name or "Band %d" % i)
            item = QListWidgetItem(text, self.band_list)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(previous.get(i, Qt.Checked)
                               if same_layout else Qt.Checked)
            item.setData(Qt.UserRole, i)

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
        self._update_folder_ui()

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
        if self.is_folder_mode():
            if not os.path.isdir(self.folder_widget.filePath().strip()):
                QMessageBox.warning(self, self.tr("確認"),
                                    self.tr("入力フォルダを指定してください。"))
                return
            if not self.selected_files():
                QMessageBox.warning(
                    self, self.tr("確認"),
                    self.tr("処理するファイルがありません。フォルダと"
                            "パターンを確認してください。"))
                return
        else:
            layer = self.layer_combo.currentLayer()
            if layer is not None and layer.providerType() != "gdal":
                QMessageBox.warning(
                    self, self.tr("確認"),
                    self.tr("GDAL で読めるファイルベースのラスタを指定してください。"))
                return
            if layer is not None and not layer.isValid():
                layer = None
            if layer is None and not self.single_source():
                QMessageBox.warning(self, self.tr("確認"),
                                    self.tr("入力ラスタを選んでください。"))
                return
        bands = self.selected_bands()
        if len(bands) < 2:
            QMessageBox.warning(self, self.tr("確認"),
                                self.tr("バンドを2つ以上選んでください。"))
            return
        if not self.out_dir.text().strip():
            QMessageBox.warning(self, self.tr("確認"), self.tr("出力フォルダを指定してください。"))
            return
        if (not self.basename.text().strip()
                and self.pipeline_mode() != pipeline.MODE_EACH):
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
        mask_layer = self.mask_combo.currentLayer()
        clip_extent = None
        if self.extent_box.isChecked():
            r = self.extent_box.outputExtent()
            if not r.isEmpty():
                clip_extent = (r.xMinimum(), r.yMinimum(),
                               r.xMaximum(), r.yMaximum())
        mode = self.pipeline_mode()
        folder = self.is_folder_mode()
        basename = self.basename.text().strip()
        if mode == pipeline.MODE_EACH:
            basename = _safe_name(basename) if basename else ""
        else:
            basename = _safe_name(basename)
        if folder:
            layer_name = os.path.basename(
                os.path.normpath(self.folder_widget.filePath()))
        elif isinstance(layer, QgsRasterLayer):
            layer_name = layer.name()
        else:
            layer_name = os.path.basename(self.single_source() or "")
        return {
            "input_mode": mode,
            "stack_path": None if folder else self.single_source(),
            "input_paths": self.selected_files() if folder else [],
            "input_root": (os.path.normpath(self.folder_widget.filePath())
                           if folder else ""),
            "shared_signature": self.shared_check.isChecked(),
            "existing": (self.existing_combo.currentData() if folder
                         else pipeline.EXISTING_OVERWRITE),
            "keep_tree": self.keep_tree_check.isChecked(),
            "layer_name": layer_name,
            "bands": self.selected_bands(),
            "out_dir": self.out_dir.text().strip(),
            "basename": basename,
            "n_classes": self.n_classes.value(),
            "max_samples": self.max_samples.value(),
            "min_class_size": self.min_class_size.value(),
            "min_separation": self.min_separation.value(),
            "max_iterations": self.max_iterations.value(),
            "convergence": self.convergence.value(),
            "write_likelihood": self.likelihood_check.isChecked(),
            "signature_path": (self.reuse_edit.text()
                               if self.reuse_check.isChecked() else None),
            "clip_extent": clip_extent,
            "clip_mask_layer": mask_layer,
            "clip_selected_only": self.mask_selected_only.isChecked(),
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
        s.setValue("input_mode", self.input_mode.currentData())
        s.setValue("input_folder", self.folder_widget.filePath())
        s.setValue("recursive", self.recursive_check.isChecked())
        s.setValue("patterns", self.pattern_edit.text())
        s.setValue("folder_mode", self.folder_mode.currentData())
        s.setValue("shared_signature", self.shared_check.isChecked())
        s.setValue("keep_tree", self.keep_tree_check.isChecked())
        s.setValue("existing", self.existing_combo.currentData())
        s.setValue("current_tab", self.tabs.currentIndex())
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
        # 復元中に検索が何度も走らないよう、シグナルを止めておく
        for w in (self.folder_widget, self.recursive_check, self.pattern_edit,
                  self.input_mode, self.folder_mode):
            w.blockSignals(True)
        self.recursive_check.setChecked(s.value("recursive", True, type=bool))
        self.pattern_edit.setText(s.value("patterns", "", type=str))
        self.folder_widget.setFilePath(s.value("input_folder", "", type=str))
        _select_data(self.input_mode,
                     s.value("input_mode", pipeline.MODE_SINGLE, type=str))
        _select_data(self.folder_mode,
                     s.value("folder_mode", pipeline.MODE_MOSAIC, type=str))
        for w in (self.folder_widget, self.recursive_check, self.pattern_edit,
                  self.input_mode, self.folder_mode):
            w.blockSignals(False)
        self.shared_check.setChecked(
            s.value("shared_signature", True, type=bool))
        self.keep_tree_check.setChecked(s.value("keep_tree", True, type=bool))
        _select_data(self.existing_combo,
                     s.value("existing", pipeline.EXISTING_SKIP, type=str))
        self.tabs.setCurrentIndex(s.value("current_tab", 0, type=int))
        s.endGroup()
        if not self.out_dir.text():
            project_path = QgsProject.instance().homePath()
            if project_path:
                self.out_dir.setText(project_path)


def _select_data(combo: QComboBox, value) -> None:
    idx = combo.findData(value)
    if idx >= 0:
        combo.setCurrentIndex(idx)


def _safe_name(text: str) -> str:
    """ファイル名に使いにくい文字を落とす。"""
    bad = '\\/:*?"<>| '
    out = "".join("_" if c in bad else c for c in (text or "").strip())
    return out or "rinkyo"
