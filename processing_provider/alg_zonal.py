# -*- coding: utf-8 -*-
"""
alg_zonal - 小班別集計のプロセシングアルゴリズム

License: GPL v2
"""

from __future__ import annotations

import csv

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessingAlgorithm, QgsProcessingException,
    QgsProcessingParameterBoolean, QgsProcessingParameterFileDestination,
    QgsProcessingParameterRasterLayer, QgsProcessingParameterVectorLayer,
)

from ..core.zonal import to_records, zonal_class_counts

RASTER = "RASTER"
POLYGON = "POLYGON"
EXCLUDE = "EXCLUDE"
OUTPUT = "OUTPUT"


class ZonalClassSummaryAlgorithm(QgsProcessingAlgorithm):
    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(
            RASTER, self.tr("分類結果ラスタ")))
        self.addParameter(QgsProcessingParameterVectorLayer(
            POLYGON, self.tr("小班ポリゴン")))
        self.addParameter(QgsProcessingParameterBoolean(
            EXCLUDE, self.tr("未分類画素を分母から除く"), True))
        self.addParameter(QgsProcessingParameterFileDestination(
            OUTPUT, self.tr("集計結果 CSV"), "CSV (*.csv)"))

    def processAlgorithm(self, parameters, context, feedback):
        raster = self.parameterAsRasterLayer(parameters, RASTER, context)
        polygon = self.parameterAsVectorLayer(parameters, POLYGON, context)
        if raster is None or polygon is None:
            raise QgsProcessingException(self.tr("レイヤを指定してください。"))
        if raster.crs() != polygon.crs():
            raise QgsProcessingException(
                self.tr("ラスタ(%s)とポリゴン(%s)の座標参照系が違います。")
                % (raster.crs().authid(), polygon.crs().authid()))
        out_path = self.parameterAsFileOutput(parameters, OUTPUT, context)

        def progress(pct, msg):
            feedback.setProgress(pct)
            feedback.setProgressText(msg)

        result = zonal_class_counts(
            raster.source().split("|")[0],
            polygon.source().split("|")[0],
            progress=progress)
        records = to_records(
            result,
            exclude_unclassified=self.parameterAsBool(
                parameters, EXCLUDE, context))
        if not records:
            raise QgsProcessingException(self.tr("集計結果が空です。"))

        with open(out_path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(records[0].keys()))
            writer.writeheader()
            writer.writerows(records)
        feedback.pushInfo("%d 小班を集計しました。" % len(records))
        return {OUTPUT: out_path}

    def name(self):
        return "zonal_class_summary"

    def displayName(self):
        return self.tr("小班別の分類結果集計")

    def group(self):
        return self.tr("林況把握")

    def groupId(self):
        return "rinkyo"

    def shortHelpString(self):
        return self.tr(
            "分類ラスタと同じ格子で小班ポリゴンをラスタ化し、"
            "クラス別の画素数・面積(ha)・割合(%)を小班ごとに集計します。\n\n"
            "列名にはラスタ属性テーブルのラベルを使うので、"
            "クラス番号を覚えておく必要がありません。\n"
            "文字コードは Excel でそのまま開ける UTF-8 BOM 付きです。")

    def createInstance(self):
        return ZonalClassSummaryAlgorithm()

    @staticmethod
    def tr(message):
        return QCoreApplication.translate("RinkyoProcessing", message)
