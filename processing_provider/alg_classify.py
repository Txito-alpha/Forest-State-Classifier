# -*- coding: utf-8 -*-
"""
alg_classify - 教師なし分類のプロセシングアルゴリズム

License: GPL v2
"""

from __future__ import annotations

import os

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransformContext,
    QgsProcessing, QgsProcessingAlgorithm, QgsProcessingException,
    QgsProcessingOutputNumber, QgsProcessingParameterBoolean,
    QgsProcessingParameterEnum, QgsProcessingParameterExtent,
    QgsProcessingParameterFeatureSource, QgsProcessingParameterFile,
    QgsProcessingParameterFolderDestination, QgsProcessingParameterNumber,
    QgsProcessingParameterRasterLayer, QgsProcessingParameterString,
    QgsVectorFileWriter,
)

from ..core import batch, pipeline
from ..core import rinkyo_core as rc

INPUT = "INPUT"
INPUT_MODE = "INPUT_MODE"
INPUT_FOLDER = "INPUT_FOLDER"
RECURSIVE = "RECURSIVE"
PATTERN = "PATTERN"
SHARED_SIGNATURE = "SHARED_SIGNATURE"
KEEP_TREE = "KEEP_TREE"
CLASSES = "CLASSES"
SAMPLES = "SAMPLES"
MIN_SIZE = "MIN_SIZE"
SEPARATION = "SEPARATION"
ITERATIONS = "ITERATIONS"
SIGNATURE_IN = "SIGNATURE_IN"
BASENAME = "BASENAME"
LIKELIHOOD = "LIKELIHOOD"
EXTENT = "EXTENT"
MASK = "MASK"
OUTPUT = "OUTPUT"
CLASSIFIED_COUNT = "CLASSIFIED_COUNT"

# INPUT_MODE の選択肢（並び順 = 値）
MODE_VALUES = [pipeline.MODE_SINGLE, pipeline.MODE_MOSAIC, pipeline.MODE_EACH]


class UnsupervisedClassifyAlgorithm(QgsProcessingAlgorithm):
    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterEnum(
            INPUT_MODE, self.tr("入力モード"),
            options=[self.tr("単一ラスタ"),
                     self.tr("フォルダ（モザイクして 1 回分類）"),
                     self.tr("フォルダ（ファイルごとに分類）")],
            defaultValue=0))
        self.addParameter(QgsProcessingParameterRasterLayer(
            INPUT, self.tr("入力画像（単一ラスタ・Blue/Green/Red/NIR/NDVI）"),
            optional=True))
        self.addParameter(QgsProcessingParameterFile(
            INPUT_FOLDER, self.tr("入力フォルダ（フォルダモード）"),
            behavior=QgsProcessingParameterFile.Folder, optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            RECURSIVE, self.tr("サブフォルダも検索する"), True))
        self.addParameter(QgsProcessingParameterString(
            PATTERN, self.tr("ファイル名パターン（; 区切り）"),
            batch.DEFAULT_PATTERNS))
        self.addParameter(QgsProcessingParameterBoolean(
            SHARED_SIGNATURE,
            self.tr("ファイルごと: 全ファイル共通のクラス定義にする"), True))
        self.addParameter(QgsProcessingParameterBoolean(
            KEEP_TREE,
            self.tr("ファイルごと: 入力のフォルダ構成を出力先に再現する"), True))
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
        self.addParameter(QgsProcessingParameterExtent(
            EXTENT, self.tr("処理範囲（任意・VRT など巨大な入力向け）"),
            optional=True))
        self.addParameter(QgsProcessingParameterFeatureSource(
            MASK, self.tr("切り抜き用ポリゴン（任意）"),
            [QgsProcessing.TypeVectorPolygon], optional=True))
        self.addParameter(QgsProcessingParameterFolderDestination(
            OUTPUT, self.tr("出力フォルダ")))
        self.addOutput(QgsProcessingOutputNumber(
            CLASSIFIED_COUNT, self.tr("分類したファイル数")))

    def processAlgorithm(self, parameters, context, feedback):
        mode = MODE_VALUES[self.parameterAsEnum(parameters, INPUT_MODE, context)]
        out_dir = self.parameterAsString(parameters, OUTPUT, context)
        base = self.parameterAsString(parameters, BASENAME, context)
        if mode != pipeline.MODE_EACH:
            base = base or "rinkyo"
        os.makedirs(out_dir, exist_ok=True)

        # --- 入力 ---------------------------------------------------------
        root = ""
        if mode == pipeline.MODE_SINGLE:
            layer = self.parameterAsRasterLayer(parameters, INPUT, context)
            if layer is None or layer.providerType() != "gdal":
                raise QgsProcessingException(
                    self.tr("GDAL で読めるラスタを指定してください。"))
            inputs = [layer.source().split("|")[0]]
            input_crs = layer.crs()
        else:
            root = self.parameterAsFile(parameters, INPUT_FOLDER, context)
            if not root or not os.path.isdir(root):
                raise QgsProcessingException(
                    self.tr("入力フォルダを指定してください。"))
            try:
                inputs = batch.find_rasters(
                    root,
                    batch.parse_patterns(self.parameterAsString(
                        parameters, PATTERN, context)),
                    recursive=self.parameterAsBool(
                        parameters, RECURSIVE, context),
                    exclude_dirs=[out_dir])
            except ValueError as exc:
                raise QgsProcessingException(str(exc)) from exc
            if not inputs:
                raise QgsProcessingException(
                    self.tr("フォルダ内にラスタが見つかりません: %s") % root)
            feedback.pushInfo(self.tr("%d 件のラスタが見つかりました。")
                              % len(inputs))
            first = next((i for i in map(batch.raster_info, inputs)
                          if i.ok), None)
            if first is None:
                raise QgsProcessingException(
                    self.tr("GDAL で読めるラスタがありません。"))
            input_crs = QgsCoordinateReferenceSystem.fromWkt(first.wkt)

        # --- 処理範囲を絞る（VRT など巨大な入力向け） ----------------------
        clip_extent = None
        if not self.parameterAsExtent(parameters, EXTENT, context).isEmpty():
            extent = self.parameterAsExtent(
                parameters, EXTENT, context, input_crs)
            clip_extent = (extent.xMinimum(), extent.yMinimum(),
                           extent.xMaximum(), extent.yMaximum())

        mask_path = None
        mask_source = self.parameterAsSource(parameters, MASK, context)
        if mask_source is not None and mask_source.featureCount():
            mask_path = os.path.join(out_dir, (base or "rinkyo")
                                     + "_clip_mask.gpkg")
            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = "GPKG"
            err = QgsVectorFileWriter.writeAsVectorFormatV3(
                mask_source, mask_path, QgsCoordinateTransformContext(),
                options)
            code = err[0] if isinstance(err, tuple) else err
            if code != QgsVectorFileWriter.NoError:
                raise QgsProcessingException(
                    self.tr("切り抜き用ポリゴンを書き出せません: %s") % (err,))

        signature = None
        sig_in = self.parameterAsFile(parameters, SIGNATURE_IN, context)
        if sig_in:
            with open(sig_in, encoding="utf-8") as fh:
                signature = rc.Signature.from_json(fh.read())

        opts = pipeline.Options(
            mode=mode,
            inputs=inputs,
            input_root=root,
            out_dir=out_dir,
            basename=base,
            shared_signature=self.parameterAsBool(
                parameters, SHARED_SIGNATURE, context),
            keep_tree=self.parameterAsBool(parameters, KEEP_TREE, context),
            n_classes=self.parameterAsInt(parameters, CLASSES, context),
            max_samples=self.parameterAsInt(parameters, SAMPLES, context),
            min_class_size=self.parameterAsInt(parameters, MIN_SIZE, context),
            min_separation=self.parameterAsDouble(
                parameters, SEPARATION, context),
            max_iterations=self.parameterAsInt(
                parameters, ITERATIONS, context),
            write_likelihood=self.parameterAsBool(
                parameters, LIKELIHOOD, context),
            signature=signature,
            clip_extent=clip_extent,
            clip_mask_path=mask_path,
        )

        def progress(pct, msg):
            feedback.setProgress(pct)
            feedback.setProgressText(msg)

        def log(msg):
            if msg.startswith(("除外", "スキップ", "失敗", "収束しきって")):
                feedback.pushWarning(msg)
            else:
                feedback.pushInfo(msg)

        runner = pipeline.Runner(opts, progress=progress,
                                 is_canceled=feedback.isCanceled, log=log)
        try:
            outcome = runner.run()
        except pipeline.Canceled:
            return {OUTPUT: out_dir, CLASSIFIED_COUNT: 0}
        except (RuntimeError, OSError, ValueError, MemoryError) as exc:
            if feedback.isCanceled():
                return {OUTPUT: out_dir, CLASSIFIED_COUNT: 0}
            raise QgsProcessingException(str(exc)) from exc
        return {OUTPUT: out_dir, CLASSIFIED_COUNT: len(outcome.results)}

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
            "当てられるので、クラス番号の意味が揃った経年比較ができます。\n\n"
            "入力が VRT など巨大な場合は、「処理範囲」（キャンバス範囲など）"
            "や「切り抜き用ポリゴン」を指定すると、その範囲だけを"
            "先に切り出してから処理するので大幅に高速化できます。\n\n"
            "入力モードを「フォルダ」にすると、入力フォルダ内（サブフォルダを"
            "含む）のラスタをパターンで探して処理します。\n"
            "・モザイク: 見つかったファイルを VRT にまとめて 1 回分類します"
            "（座標参照系とバンド数が揃っている必要があります）。\n"
            "・ファイルごと: ファイル単位で <接頭辞>_<元の名前>_class.tif を"
            "出力します。「共通のクラス定義」を有効にすると、全ファイルから"
            "標本を集めて 1 つのシグネチャで分類するので、クラス番号の意味が"
            "揃います。\n"
            "出力フォルダの中と、*_class.tif などこのプラグインの出力は"
            "検索対象から除外します。")

    def createInstance(self):
        return UnsupervisedClassifyAlgorithm()

    @staticmethod
    def tr(message):
        return QCoreApplication.translate("RinkyoProcessing", message)
