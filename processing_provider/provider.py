# -*- coding: utf-8 -*-
"""
provider - バッチ処理用のプロセシングプロバイダ

GUI を使わずモデルデザイナや qgis_process から呼びたい場合の入口。
中身は GUI 版とまったく同じコアを叩くので、結果は一致する。

License: GPL v2
"""

from __future__ import annotations

import os

from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsProcessingProvider

from .alg_classify import UnsupervisedClassifyAlgorithm
from .alg_zonal import ZonalClassSummaryAlgorithm


class RinkyoProvider(QgsProcessingProvider):
    def loadAlgorithms(self):
        self.addAlgorithm(UnsupervisedClassifyAlgorithm())
        self.addAlgorithm(ZonalClassSummaryAlgorithm())

    def id(self):
        return "rinkyo"

    def name(self):
        return "林況分類"

    def longName(self):
        return "衛星画像による林況把握（教師なし分類）"

    def icon(self):
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            "icon.png")
        return QIcon(path) if os.path.exists(path) else QgsProcessingProvider.icon(self)
