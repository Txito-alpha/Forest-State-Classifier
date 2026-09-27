# -*- coding: utf-8 -*-
"""
alg_apply_labels - 意味づけを分類ラスタ（TIF）へ反映するプロセシングアルゴリズム

  * シグネチャを指定しない場合: 各ラスタ自身の意味づけ（TIF への埋め込み、
    無ければ隣の _signature.json）で、カラーマップ・RAT・埋め込みを書き直す。
    .aux.xml が消えた・古いまま（凡例が「class 1」など）の修復や、
    0.3.0 より前に作った分類ラスタへの埋め込みに使う。
  * シグネチャを指定した場合: そのラベル・色を各ラスタに当てる。
    共通シグネチャで一括分類した結果や、経年比較で同じシグネチャを当てた
    別年次の結果へ、1 回の意味づけをまとめて反映できる。

License: GPL v2
"""

from __future__ import annotations

import os

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm, QgsProcessingException,
    QgsProcessingOutputNumber, QgsProcessingParameterBoolean,
    QgsProcessingParameterDefinition, QgsProcessingParameterFile,
    QgsProcessingParameterMultipleLayers,
)

from ..core import tif_labels
from ..core.rinkyo_core import Signature

INPUT = "INPUT"
SIGNATURE = "SIGNATURE"
ALLOW_MISMATCH = "ALLOW_MISMATCH"
WRITE_JSON = "WRITE_JSON"
UPDATED = "UPDATED"
FAILED = "FAILED"


class ApplyLabelsAlgorithm(QgsProcessingAlgorithm):
    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterMultipleLayers(
            INPUT, self.tr("分類結果ラスタ（*_class.tif）"),
            QgsProcessing.TypeRaster))
        self.addParameter(QgsProcessingParameterFile(
            SIGNATURE,
            self.tr("意味づけの元にするシグネチャ（省略時は各ラスタ自身の意味づけ）"),
            extension="json", optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            WRITE_JSON,
            self.tr("隣のシグネチャファイル（*_signature.json）も更新する"),
            True))
        mismatch = QgsProcessingParameterBoolean(
            ALLOW_MISMATCH,
            self.tr("クラス定義が異なるラスタにも反映する（非推奨）"), False)
        mismatch.setFlags(mismatch.flags()
                          | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(mismatch)
        self.addOutput(QgsProcessingOutputNumber(
            UPDATED, self.tr("反映したラスタ数")))
        self.addOutput(QgsProcessingOutputNumber(
            FAILED, self.tr("反映できなかったラスタ数")))

    def processAlgorithm(self, parameters, context, feedback):
        layers = self.parameterAsLayerList(parameters, INPUT, context)
        if not layers:
            raise QgsProcessingException(self.tr("分類結果ラスタを指定してください。"))

        source = None
        sig_path = self.parameterAsFile(parameters, SIGNATURE, context)
        if sig_path:
            try:
                with open(sig_path, encoding="utf-8-sig") as fh:
                    source = Signature.from_json(fh.read())
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise QgsProcessingException(
                    self.tr("シグネチャを読み込めません: %s\n%s")
                    % (sig_path, exc))
            if not any(source.labels):
                feedback.pushWarning(self.tr(
                    "指定したシグネチャには意味づけ（ラベル）がありません。"))
        allow = self.parameterAsBool(parameters, ALLOW_MISMATCH, context)
        write_json = self.parameterAsBool(parameters, WRITE_JSON, context)

        updated = failed = 0
        paths = []
        for layer in layers:
            path = tif_labels.raster_file(layer.source())
            if path not in paths:
                paths.append(path)
        for i, path in enumerate(paths):
            if feedback.isCanceled():
                break
            name = os.path.basename(path)
            try:
                _sig, note = tif_labels.apply_labels_to_raster(
                    path, source, allow_mismatch=allow, write_json=write_json)
                feedback.pushInfo("%s: %s" % (name, note))
                updated += 1
            except (ValueError, RuntimeError, OSError) as exc:
                feedback.reportError("%s: %s" % (name, exc))
                failed += 1
            feedback.setProgress(100.0 * (i + 1) / len(paths))

        # 開いているレイヤは読み直して、新しい色・凡例を表示する
        for layer in layers:
            try:
                layer.reload()
                layer.triggerRepaint()
            except Exception as exc:  # noqa: BLE001  表示だけなので続行
                feedback.pushWarning(self.tr("レイヤを読み直せませんでした: %s")
                                     % exc)
        if failed:
            feedback.pushWarning(self.tr("%d 件は反映できませんでした。") % failed)
        return {UPDATED: updated, FAILED: failed}

    # -- メタ情報 ----------------------------------------------------------
    def name(self):
        return "applylabels"

    def displayName(self):
        return self.tr("意味づけを分類ラスタ（TIF）へ反映")

    def shortHelpString(self):
        return self.tr(
            "意味づけ（クラス名と色）を分類結果の TIF に書き込みます。\n\n"
            "色は TIF のカラーマップに、クラス名とシグネチャは TIF 本体の"
            "メタデータに埋め込み、ラスタ属性テーブル（.aux.xml）も書き直します。"
            "TIF だけをコピー・共有しても意味づけが失われません。\n\n"
            "シグネチャを省略すると、各ラスタ自身の意味づけで書き直します"
            "（凡例が「class 1」などに戻ってしまった場合の修復）。\n"
            "シグネチャを指定すると、そのクラス名と色を各ラスタに当てます。"
            "同じシグネチャで分類した複数のラスタ（フォルダ一括の共通シグネチャ、"
            "経年比較で既存シグネチャを当てた別年次の結果）へ、"
            "1 回の意味づけをまとめて反映できます。"
            "クラス定義が異なるラスタは既定では飛ばします。")

    def group(self):
        return self.tr("林況把握")

    def groupId(self):
        return "rinkyo"

    def createInstance(self):
        return ApplyLabelsAlgorithm()

    @staticmethod
    def tr(message):
        return QCoreApplication.translate("RinkyoProcessing", message)
