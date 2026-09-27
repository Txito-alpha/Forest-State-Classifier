# -*- coding: utf-8 -*-
"""
task - 教師なし分類をバックグラウンドで走らせる QgsTask

重い処理を UI スレッドで回すと QGIS が固まり、利用者からは
「落ちた」ように見える。分類は必ずこのタスク経由で実行する。

処理の中身は core.pipeline にあり、プロセシングアルゴリズムと共通。

License: GPL v2
"""

from __future__ import annotations

import traceback
from typing import List, Optional, Sequence

from qgis.core import Qgis, QgsMessageLog, QgsTask

from .core import pipeline
from .core import rinkyo_core as rc

LOG_TAG = "RinkyoClassifier"


class ClassifyTask(QgsTask):
    """入力モードに応じて 標本抽出 → クラスタリング → 全画素分類 を実行する。

    - mode="single": stack_path の 1 ファイル
    - mode="mosaic": input_paths を VRT にまとめて 1 回
    - mode="each"  : input_paths をファイルごとに分類
      （shared_signature=True なら全ファイル共通のクラス定義）

    existing で、同名の分類結果が既にある場合の扱い（上書き／スキップ／別名）
    を指定する。

    signature を渡した場合はクラスタリングを飛ばし、既存の分類基準を
    そのまま当てる（別年次の画像へ同じクラス定義を適用する用途）。
    """

    def __init__(
        self,
        stack_path: Optional[str],
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
        clip_extent: Optional[Sequence[float]] = None,
        clip_mask_path: Optional[str] = None,
        mode: str = pipeline.MODE_SINGLE,
        input_paths: Optional[Sequence[str]] = None,
        input_root: str = "",
        shared_signature: bool = True,
        keep_tree: bool = True,
        existing: str = pipeline.EXISTING_SKIP,
    ):
        title = "教師なし分類"
        if mode != pipeline.MODE_SINGLE:
            title += "（フォルダ %d 件）" % len(input_paths or [])
        super().__init__(title, QgsTask.CanCancel)
        self.mode = mode
        self.basename = basename
        self.out_dir = out_dir
        inputs = [stack_path] if mode == pipeline.MODE_SINGLE else list(
            input_paths or [])
        self.options = pipeline.Options(
            mode=mode,
            inputs=inputs,
            input_root=input_root,
            out_dir=out_dir,
            basename=basename,
            shared_signature=shared_signature,
            keep_tree=keep_tree,
            existing=existing,
            n_classes=n_classes,
            max_samples=max_samples,
            min_class_size=min_class_size,
            min_separation=min_separation,
            max_iterations=max_iterations,
            convergence=convergence,
            bands=list(bands) if bands else None,
            nodata=nodata,
            write_likelihood=write_likelihood,
            signature=signature,
            # (xmin, ymin, xmax, ymax)。入力ラスタと同じ CRS で渡すこと。
            clip_extent=tuple(clip_extent) if clip_extent else None,
            clip_mask_path=clip_mask_path,
        )

        self.outcome: Optional[pipeline.Outcome] = None
        self.messages: List[str] = []
        self.exception: Optional[str] = None

    # -- 単一結果向けの互換プロパティ --------------------------------------
    @property
    def results(self) -> List[pipeline.Result]:
        return self.outcome.results if self.outcome else []

    @property
    def signature(self) -> Optional[rc.Signature]:
        if self.outcome and self.outcome.shared_signature is not None:
            return self.outcome.shared_signature
        if self.results:
            return self.results[0].signature
        return self.options.signature

    @property
    def result_path(self) -> Optional[str]:
        return self.results[0].result_path if self.results else None

    @property
    def likelihood_path(self) -> Optional[str]:
        return self.results[0].likelihood_path if self.results else None

    @property
    def signature_path(self) -> Optional[str]:
        return self.results[0].signature_path if self.results else None

    # -- 本体 --------------------------------------------------------------
    def _progress(self, pct: float, msg: str) -> None:
        self.setProgress(min(99.0, float(pct)))
        self.setDescription(msg)

    def run(self) -> bool:
        runner = pipeline.Runner(self.options, progress=self._progress,
                                 is_canceled=self.isCanceled)
        try:
            self.outcome = runner.run()
            self.setProgress(100.0)
            return True
        except pipeline.Canceled:
            return False
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
        finally:
            self.messages = list(runner.outcome.messages)
        return False
