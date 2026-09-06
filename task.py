# -*- coding: utf-8 -*-
"""
task - 教師なし分類をバックグラウンドで走らせる QgsTask

重い処理を UI スレッドで回すと QGIS が固まり、利用者からは
「落ちた」ように見える。分類は必ずこのタスク経由で実行する。

License: GPL v2
"""

from __future__ import annotations

import os
import traceback
from typing import List, Optional, Sequence

from qgis.core import Qgis, QgsMessageLog, QgsTask

from .core import rinkyo_core as rc
from .core.categories import apply_default_colors
from .core import rinkyo_raster as rr

LOG_TAG = "RinkyoClassifier"


class ClassifyTask(QgsTask):
    """バンド合成 → 標本抽出 → クラスタリング → 全画素分類 を通しで実行する。

    signature を渡した場合はクラスタリングを飛ばし、既存の分類基準を
    そのまま当てる（別年次の画像へ同じクラス定義を適用する用途）。
    """

    def __init__(
        self,
        stack_path: str,
        out_dir: str,
        basename: str,
        n_classes: int = 50,
        max_samples: int = 100000,
        min_class_size: int = 17,
        min_separation: float = 0.0,
        max_iterations: int = 30,
        convergence: float = 98.0,
        bands: Optional[Sequence[int]] = None,
        nodata: float = 0.0,
        write_likelihood: bool = True,
        signature: Optional[rc.Signature] = None,
    ):
        super().__init__("教師なし分類", QgsTask.CanCancel)
        self.stack_path = stack_path
        self.out_dir = out_dir
        self.basename = basename
        self.n_classes = n_classes
        self.max_samples = max_samples
        self.min_class_size = min_class_size
        self.min_separation = min_separation
        self.max_iterations = max_iterations
        self.convergence = convergence
        self.bands = list(bands) if bands else None
        self.nodata = nodata
        self.write_likelihood = write_likelihood

        self.signature: Optional[rc.Signature] = signature
        self.result_path: Optional[str] = None
        self.likelihood_path: Optional[str] = None
        self.signature_path: Optional[str] = None
        self.messages: List[str] = []
        self.exception: Optional[str] = None

    # -- 進捗ヘルパ --------------------------------------------------------
    def _stage(self, base: int, span: int):
        def cb(pct: int, msg: str) -> None:
            self.setProgress(min(99.0, base + pct * span / 100.0))
            self.setDescription(msg)
        return cb

    # -- 本体 --------------------------------------------------------------
    def run(self) -> bool:
        try:
            os.makedirs(self.out_dir, exist_ok=True)
            reuse = self.signature is not None

            if not reuse:
                samples, names = rr.sample_raster(
                    self.stack_path,
                    max_samples=self.max_samples,
                    bands=self.bands,
                    nodata=self.nodata,
                    progress=self._stage(0, 15),
                )
                self.messages.append("標本 %d 画素 / %d バンド"
                                     % (samples.shape[0], samples.shape[1]))
                if self.isCanceled():
                    return False

                res = rc.cluster(
                    samples,
                    n_classes=self.n_classes,
                    min_class_size=self.min_class_size,
                    min_separation=self.min_separation,
                    max_iterations=self.max_iterations,
                    convergence=self.convergence,
                    band_names=names,
                    progress=self._stage(15, 35),
                )
                self.signature = res.signature
                self.signature.title = self.basename
                self.messages.append(
                    "反復 %d 回 / 収束 %.1f%% / クラス数 %d（削除 %d・統合 %d）"
                    % (res.n_iterations, res.convergence,
                       res.signature.n_classes, res.dropped, res.merged))
                if not res.converged:
                    self.messages.append(
                        "収束しきっていません。反復回数を増やすか、"
                        "初期クラス数を下げてください。")
            else:
                self.messages.append("既存のシグネチャを適用します（クラス数 %d）"
                                     % self.signature.n_classes)

            if self.isCanceled():
                return False

            # ラベル未設定なら自動案を入れておく
            if self.signature and not any(self.signature.labels):
                try:
                    self.signature.labels = rc.suggest_labels(
                        self.signature,
                        idx_ndvi=self._ndvi_index(self.signature))
                except (ValueError, IndexError) as exc:
                    QgsMessageLog.logMessage(
                        "自動ラベル推定をスキップしました: %s" % exc,
                        LOG_TAG, Qgis.Info)
            # ラスタ属性テーブルを書く前に色を確定させる。
            # ここを飛ばすと出力が全部グレーになる。
            apply_default_colors(self.signature)

            self.signature_path = os.path.join(
                self.out_dir, self.basename + "_signature.json")
            with open(self.signature_path, "w", encoding="utf-8") as fh:
                fh.write(self.signature.to_json())

            self.result_path = os.path.join(
                self.out_dir, self.basename + "_class.tif")
            if self.write_likelihood:
                self.likelihood_path = os.path.join(
                    self.out_dir, self.basename + "_loglik.tif")

            rr.classify_raster(
                self.stack_path,
                self.signature,
                self.result_path,
                bands=self.bands,
                nodata=self.nodata,
                likelihood_path=self.likelihood_path,
                progress=self._stage(50, 49),
                is_canceled=self.isCanceled,
            )
            self.setProgress(100.0)
            return True

        except RuntimeError as exc:
            # classify_raster 内で中止された場合もここに来る
            if self.isCanceled():
                return False
            self.exception = str(exc)
        except (OSError, ValueError, MemoryError) as exc:
            self.exception = "%s: %s" % (type(exc).__name__, exc)
        except Exception as exc:  # noqa: BLE001  最後の砦。ログに残して落とす
            self.exception = "%s: %s" % (type(exc).__name__, exc)
            QgsMessageLog.logMessage(traceback.format_exc(), LOG_TAG,
                                     Qgis.Critical)
        return False

    @staticmethod
    def _ndvi_index(sig: rc.Signature) -> Optional[int]:
        for i, name in enumerate(sig.band_names):
            if "ndvi" in (name or "").lower():
                return i
        return None
