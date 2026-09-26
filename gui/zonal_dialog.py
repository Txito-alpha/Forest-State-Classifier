# -*- coding: utf-8 -*-
"""
zonal_dialog - 分類結果を小班ごとに集計する画面

出力は「小班属性＋ラベル別の面積・割合フィールド」を持つ
GeoPackage レイヤ、または Excel ブック。属性の空間結合と
ゾーンヒストグラムを別々に回す必要がなくなる。

License: GPL v2
"""

from __future__ import annotations

import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton,
    QVBoxLayout,
)
from qgis.core import (
    QgsFeature, QgsFeatureRequest, QgsField, QgsFields, QgsMapLayerProxyModel,
    QgsProject, QgsVectorFileWriter, QgsVectorLayer, QgsWkbTypes,
)
from qgis.gui import QgsMapLayerComboBox
from qgis.PyQt.QtCore import QVariant

from ..core.zonal import to_records, zonal_class_counts


class ZonalDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("小班別に集計"))
        self.resize(520, 300)

        self.raster_combo = QgsMapLayerComboBox(self)
        self.raster_combo.setFilters(QgsMapLayerProxyModel.RasterLayer)

        self.polygon_combo = QgsMapLayerComboBox(self)
        self.polygon_combo.setFilters(QgsMapLayerProxyModel.PolygonLayer)

        self.exclude_check = QCheckBox(self.tr("未分類画素を分母から除く"), self)
        self.exclude_check.setChecked(True)

        self.out_edit = QLineEdit(self)
        out_button = QPushButton(self.tr("参照…"), self)
        out_row = QHBoxLayout()
        out_row.addWidget(self.out_edit, 1)
        out_row.addWidget(out_button)

        self.progress = QProgressBar(self)
        self.progress.setVisible(False)

        note = QLabel(self.tr(
            "分類ラスタと同じ格子でポリゴンを焼くので、"
            "リサンプリングによる面積のずれが入りません。"), self)
        note.setWordWrap(True)

        form = QFormLayout()
        form.addRow(self.tr("分類結果ラスタ"), self.raster_combo)
        form.addRow(self.tr("小班ポリゴン"), self.polygon_combo)
        form.addRow("", self.exclude_check)
        form.addRow(self.tr("出力ファイル（GPKG / XLSX）"), out_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, Qt.Horizontal, self)
        buttons.button(QDialogButtonBox.Ok).setText(self.tr("集計"))
        buttons.accepted.connect(self._run)
        buttons.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addLayout(form)
        root.addWidget(note)
        root.addWidget(self.progress)
        root.addStretch(1)
        root.addWidget(buttons)

        out_button.clicked.connect(self._pick_out)

    def _pick_out(self) -> None:
        path, selected_filter = QFileDialog.getSaveFileName(
            self,
            self.tr("出力先"),
            self.out_edit.text(),
            self.tr("GeoPackage (*.gpkg);;Excel ブック (*.xlsx)"),
        )
        if path:
            lower = path.lower()
            if not lower.endswith((".gpkg", ".xlsx")):
                path += ".xlsx" if "*.xlsx" in selected_filter else ".gpkg"
            self.out_edit.setText(path)

    def _progress(self, pct: int, msg: str) -> None:
        self.progress.setValue(int(pct))
        self.progress.setFormat("%s (%%p%%)" % msg)

    def _run(self) -> None:
        raster = self.raster_combo.currentLayer()
        polygon = self.polygon_combo.currentLayer()
        out_path = self.out_edit.text().strip()
        if raster is None or polygon is None:
            QMessageBox.warning(self, self.tr("確認"), self.tr("レイヤを選んでください。"))
            return
        if not out_path:
            QMessageBox.warning(self, self.tr("確認"), self.tr("出力先を指定してください。"))
            return
        suffix = os.path.splitext(out_path)[1].lower()
        if suffix not in (".gpkg", ".xlsx"):
            QMessageBox.warning(
                self, self.tr("確認"),
                self.tr("出力形式は .gpkg または .xlsx を指定してください。"))
            return
        if raster.crs() != polygon.crs():
            QMessageBox.warning(
                self, self.tr("確認"),
                self.tr("ラスタとポリゴンの座標参照系が違います。\n"
                "先に揃えてから実行してください。\nラスタ: %s\nポリゴン: %s")
                % (raster.crs().authid(), polygon.crs().authid()))
            return

        selected_ids = [int(fid) for fid in polygon.selectedFeatureIds()]
        if selected_ids:
            target_message = self.tr("選択中の小班 %d 件を集計します。") % len(selected_ids)
        else:
            target_message = self.tr("選択がないため、全小班を集計します。")
        self.progress.setToolTip(target_message)

        self.progress.setVisible(True)
        try:
            result = zonal_class_counts(
                raster.source().split("|")[0],
                polygon.source().split("|")[0],
                feature_ids=selected_ids or None,
                progress=self._progress,
            )
            records = to_records(
                result, exclude_unclassified=self.exclude_check.isChecked())
            self._write(polygon, records, out_path, selected_ids or None)
        except (OSError, ValueError, RuntimeError, MemoryError) as exc:
            self.progress.setVisible(False)
            QMessageBox.critical(self, self.tr("エラー"), str(exc))
            return

        if out_path.lower().endswith(".gpkg"):
            layer = QgsVectorLayer(out_path, os.path.splitext(
                os.path.basename(out_path))[0], "ogr")
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
        self.progress.setVisible(False)
        self.accept()

    def _write(self, polygon: QgsVectorLayer, records, out_path: str,
               selected_ids=None) -> None:
        """対象小班の属性 + 集計結果を GPKG または XLSX に書き出す。"""
        by_fid = {int(r.pop("_fid")): r for r in records}
        if not by_fid:
            raise ValueError(self.tr("集計対象のポリゴンがありませんでした。"))

        fields = QgsFields()
        for field in polygon.fields():
            fields.append(field)
        first = next(iter(by_fid.values()))
        extra = [k for k in first.keys()
                 if polygon.fields().indexOf(k) < 0]
        for name in extra:
            sample = first[name]
            if isinstance(sample, int):
                fields.append(QgsField(name, QVariant.Int))
            elif isinstance(sample, float) or sample is None:
                fields.append(QgsField(name, QVariant.Double))
            else:
                fields.append(QgsField(name, QVariant.String))

        is_xlsx = out_path.lower().endswith(".xlsx")
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "XLSX" if is_xlsx else "GPKG"
        options.fileEncoding = "UTF-8"
        options.layerName = self.tr("小班別集計")
        geometry_type = QgsWkbTypes.NoGeometry if is_xlsx else QgsWkbTypes.MultiPolygon
        output_crs = polygon.crs()
        writer = QgsVectorFileWriter.create(
            out_path, fields, geometry_type, output_crs,
            QgsProject.instance().transformContext(), options)
        if writer.hasError() != QgsVectorFileWriter.NoError:
            raise OSError(self.tr("書き出しに失敗しました: %s") % writer.errorMessage())

        missing = 0
        request = (QgsFeatureRequest().setFilterFids(selected_ids)
                   if selected_ids else QgsFeatureRequest())
        for src in polygon.getFeatures(request):
            rec = by_fid.get(int(src.id()))
            if rec is None:
                missing += 1
                continue
            feat = QgsFeature(fields)
            if not is_xlsx:
                feat.setGeometry(src.geometry())
            for field in polygon.fields():
                feat[field.name()] = src[field.name()]
            for name in extra:
                feat[name] = rec[name]
            writer.addFeature(feat)
        del writer

        if missing:
            raise ValueError(
                self.tr("%d 件の小班に集計結果を対応づけられませんでした。") % missing)
