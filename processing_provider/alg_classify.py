# -*- coding: utf-8 -*-
"""
alg_classify - 教師なし分類のプロセシングアルゴリズム

License: GPL v2
"""

from __future__ import annotations

import os

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessingAlgorithm, QgsProcessingException,
    QgsProcessingParameterBoolean, QgsProcessingParameterFile,
    QgsProcessingParameterFolderDestination, QgsProcessingParameterNumber,
    QgsProcessingParameterRasterLayer, QgsProcessingParameterString,
)

from ..core import rinkyo_core as rc
from ..core.categories import apply_default_colors
from ..core import rinkyo_raster as rr

INPUT = "INPUT"
CLASSES = "CLASSES"
SAMPLES = "SAMPLES"
MIN_SIZE = "MIN_SIZE"
SEPARATION = "SEPARATION"
ITERATIONS = "ITERATIONS"
SIGNATURE_IN = "SIGNATURE_IN"
BASENAME = "BASENAME"
LIKELIHOOD = "LIKELIHOOD"
OUTPUT = "OUTPUT"


class UnsupervisedClassifyAlgorithm(QgsProcessingAlgorithm):
    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(
            INPUT, self.tr("入力画像（Blue/Green/Red/NIR/NDVI）")))
        self.addParameter(QgsProcessingParameterNumber(
            CLASSES, self.tr("初期クラス数"),
            QgsProcessingParameterNumber.Integer, 50, False, 2, 255))
        self.addParameter(QgsProcessingParameterNumber(
            SAMPLES, self.tr("標本画素数"),
            QgsProcessingParameterNumber.Integer, 100000, False, 1000))
        self.addParameter(QgsProcessingParameterNumber(
            MIN_SIZE, self.tr("最小クラス標本数"),
            QgsProcessingParameterNumber.Integer, 17, False, 2))
        self.addParameter(QgsProcessingParameterNumber(
            SEPARATION, self.tr("クラス統合距離（0で統合しない）"),
            QgsProcessingParameterNumber.Double, 0.0, True, 0.0, 5.0))
        self.addParameter(QgsProcessingParameterNumber(
            ITERATIONS, self.tr("最大反復回数"),
            QgsProcessingParameterNumber.Integer, 30, False, 1, 200))
        self.addParameter(QgsProcessingParameterFile(
            SIGNATURE_IN, self.tr("既存シグネチャ（経年比較用・任意）"),
            optional=True, fileFilter="JSON (*.json)"))
        self.addParameter(QgsProcessingParameterString(
            BASENAME, self.tr("出力ファイル名の接頭辞"), "rinkyo"))
        self.addParameter(QgsProcessingParameterBoolean(
            LIKELIHOOD, self.tr("対数尤度ラスタも出力する"), True))
        self.addParameter(QgsProcessingParameterFolderDestination(
            OUTPUT, self.tr("出力フォルダ")))

    def processAlgorithm(self, parameters, context, feedback):
        layer = self.parameterAsRasterLayer(parameters, INPUT, context)
        if layer is None or layer.providerType() != "gdal":
            raise QgsProcessingException(
                self.tr("GDAL で読めるラスタを指定してください。"))
        src = layer.source().split("|")[0]
        out_dir = self.parameterAsString(parameters, OUTPUT, context)
        base = self.parameterAsString(parameters, BASENAME, context) or "rinkyo"
        os.makedirs(out_dir, exist_ok=True)

        def progress(pct, msg):
            feedback.setProgress(pct)
            feedback.setProgressText(msg)

        sig_in = self.parameterAsFile(parameters, SIGNATURE_IN, context)
        if sig_in:
            with open(sig_in, encoding="utf-8") as fh:
                signature = rc.Signature.from_json(fh.read())
            feedback.pushInfo("既存シグネチャを適用します（クラス数 %d）"
                              % signature.n_classes)
        else:
            samples, names = rr.sample_raster(
                src,
                max_samples=self.parameterAsInt(parameters, SAMPLES, context),
                progress=progress)
            feedback.pushInfo("標本 %d 画素 / %d バンド"
                              % (samples.shape[0], samples.shape[1]))
            result = rc.cluster(
                samples,
                n_classes=self.parameterAsInt(parameters, CLASSES, context),
                min_class_size=self.parameterAsInt(
                    parameters, MIN_SIZE, context),
                min_separation=self.parameterAsDouble(
                    parameters, SEPARATION, context),
                max_iterations=self.parameterAsInt(
                    parameters, ITERATIONS, context),
                band_names=names,
                progress=progress)
            signature = result.signature
            signature.title = base
            signature.labels = rc.suggest_labels(signature)
            feedback.pushInfo(
                "反復 %d 回 / 収束 %.1f%% / クラス数 %d"
                % (result.n_iterations, result.convergence,
                   signature.n_classes))
            if not result.converged:
                feedback.pushWarning(
                    "収束しきっていません。反復回数を増やしてください。")

        # GUI を通らない実行経路なので、ここで色を確定させておく
        apply_default_colors(signature)

        sig_path = os.path.join(out_dir, base + "_signature.json")
        with open(sig_path, "w", encoding="utf-8") as fh:
            fh.write(signature.to_json())

        out_path = os.path.join(out_dir, base + "_class.tif")
        lik_path = (os.path.join(out_dir, base + "_loglik.tif")
                    if self.parameterAsBool(parameters, LIKELIHOOD, context)
                    else None)
        rr.classify_raster(src, signature, out_path,
                           likelihood_path=lik_path,
                           progress=progress,
                           is_canceled=feedback.isCanceled)
        return {OUTPUT: out_dir}

    # -- 定型 --------------------------------------------------------------
    def name(self):
        return "unsupervised_classify"

    def displayName(self):
        return self.tr("教師なし分類")

    def group(self):
        return self.tr("林況把握")

    def groupId(self):
        return "rinkyo"

    def shortHelpString(self):
        return self.tr(
            "Sentinel-2 の Blue/Green/Red/NIR/NDVI からクラスタリングを行い、"
            "最尤法で全画素を分類します。GRASS GIS には依存しません。\n\n"
            "既存シグネチャを指定すると、別年次の画像に同じ分類基準を"
            "当てられるので、クラス番号の意味が揃った経年比較ができます。")

    def createInstance(self):
        return UnsupervisedClassifyAlgorithm()

    @staticmethod
    def tr(message):
        return QCoreApplication.translate("RinkyoProcessing", message)
