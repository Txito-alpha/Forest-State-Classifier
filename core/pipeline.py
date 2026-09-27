# -*- coding: utf-8 -*-
"""
pipeline - 入力モード（単一ファイル／フォルダのモザイク／フォルダの個別処理）を
まとめて扱う実行部（GDAL + numpy のみ、QGIS API 非依存）

GUI のバックグラウンドタスクとプロセシングアルゴリズムの両方から呼ぶ。
同じ関数を通るので、どちらから実行しても結果は一致する。

License: GPL v2
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

from . import batch
from . import rinkyo_core as rc
from . import rinkyo_raster as rr
from .categories import apply_default_colors

MODE_SINGLE = "single"   # 1ファイル
MODE_MOSAIC = "mosaic"   # フォルダ内のファイルを VRT にまとめて 1 回分類
MODE_EACH = "each"       # フォルダ内のファイルをそれぞれ分類
MODES = (MODE_SINGLE, MODE_MOSAIC, MODE_EACH)

# 同名の出力があったときの扱い
EXISTING_OVERWRITE = "overwrite"   # 上書きする（0.2.0 までの動作）
EXISTING_SKIP = "skip"             # そのファイルを処理しない
EXISTING_RENAME = "rename"         # basename_2, _3 … と別名で保存する
EXISTING_POLICIES = (EXISTING_OVERWRITE, EXISTING_SKIP, EXISTING_RENAME)

Progress = Optional[Callable[[float, str], None]]
Log = Callable[[str], None]


class Canceled(Exception):
    """利用者による中止。"""


@dataclass
class Options:
    mode: str = MODE_SINGLE
    # MODE_SINGLE: 入力ファイル 1 本 / フォルダ系: 検索済みファイルの一覧
    inputs: List[str] = field(default_factory=list)
    input_root: str = ""
    out_dir: str = ""
    basename: str = "rinkyo"
    shared_signature: bool = True   # MODE_EACH で全ファイル共通のクラス定義にする
    keep_tree: bool = True          # MODE_EACH で相対フォルダ構成を再現する
    existing: str = EXISTING_SKIP   # 同名の出力があったときの扱い

    n_classes: int = 50
    max_samples: int = 100000
    min_class_size: int = 17
    min_separation: float = 0.0
    max_iterations: int = 30
    convergence: float = 98.0
    bands: Optional[List[int]] = None
    nodata: Optional[float] = 0.0
    write_likelihood: bool = True

    signature: Optional[rc.Signature] = None   # 既存シグネチャの再利用
    clip_extent: Optional[Sequence[float]] = None   # 入力と同じ CRS
    clip_mask_path: Optional[str] = None


@dataclass
class Result:
    source: str
    out_dir: str
    basename: str
    stack_path: str = ""
    result_path: str = ""
    likelihood_path: Optional[str] = None
    signature_path: str = ""
    signature: Optional[rc.Signature] = None


@dataclass
class Outcome:
    results: List[Result] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    # 既存の出力があるためスキップした入力ファイル（skipped の部分集合）
    existing_skipped: List[str] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)
    messages: List[str] = field(default_factory=list)
    shared_signature: Optional[rc.Signature] = None

    @property
    def is_batch(self) -> bool:
        return len(self.results) > 1 or bool(self.skipped or self.failures)


def ndvi_index(sig: rc.Signature) -> Optional[int]:
    for i, name in enumerate(sig.band_names):
        if "ndvi" in (name or "").lower():
            return i
    return None


def finalize_signature(sig: rc.Signature, log: Log) -> None:
    """ラベル未設定なら自動案を入れ、色を確定させる。

    色の確定を飛ばすとラスタ属性テーブルが全部グレーになる。
    """
    if not any(sig.labels):
        try:
            sig.labels = rc.suggest_labels(sig, idx_ndvi=ndvi_index(sig))
        except (ValueError, IndexError) as exc:
            log("自動ラベル推定をスキップしました: %s" % exc)
    apply_default_colors(sig)


def _scaled(progress: Progress, base: float, span: float):
    def cb(pct, msg):
        if progress:
            progress(base + float(pct) * span / 100.0, msg)
    return cb


class Runner:
    """Options に従って処理を実行し、Outcome を返す。"""

    def __init__(self, opts: Options, progress: Progress = None,
                 is_canceled: Optional[Callable[[], bool]] = None,
                 log: Optional[Log] = None):
        if opts.mode not in MODES:
            raise ValueError("不明な入力モード: %s" % opts.mode)
        if opts.existing not in EXISTING_POLICIES:
            raise ValueError("不明な既存出力の扱い: %s" % opts.existing)
        self.o = opts
        self.progress = progress
        self.is_canceled = is_canceled or (lambda: False)
        self.outcome = Outcome()
        self._log_cb = log

    # -- 小道具 ------------------------------------------------------------
    def log(self, msg: str) -> None:
        self.outcome.messages.append(msg)
        if self._log_cb:
            self._log_cb(msg)

    def _check_cancel(self) -> None:
        if self.is_canceled():
            raise Canceled()

    def _report(self, pct: float, msg: str) -> None:
        if self.progress:
            self.progress(min(99.0, pct), msg)

    # -- 本体 --------------------------------------------------------------
    def run(self) -> Outcome:
        o = self.o
        if not o.inputs:
            raise ValueError("入力ファイルがありません")
        os.makedirs(o.out_dir, exist_ok=True)

        if o.mode == MODE_SINGLE:
            jobs = [batch.Job(o.inputs[0], o.out_dir, o.basename)]
        else:
            jobs = self._folder_jobs()
        self._check_cancel()

        # 0) 同名の出力がある場合の扱い（スキップ／別名／上書き）
        jobs = [j for j in (self._resolve_existing(j) for j in jobs) if j]
        if not jobs:
            self.log("既存の出力があるため、すべてスキップしました。")
            return self.outcome
        self._check_cancel()

        # 1) 処理範囲の切り出し（0〜10%）
        stacks = []
        for i, job in enumerate(jobs):
            self._check_cancel()
            self._report(10.0 * i / len(jobs), "処理範囲を確認中…")
            path = self._prepare_input(job, len(jobs) > 1)
            if path:
                stacks.append((job, path))
        if not stacks:
            raise ValueError("処理できるファイルがありません。\n"
                             + "\n".join(self.outcome.skipped
                                         + self.outcome.failures))

        # 2) シグネチャ（共通にする場合はここで 1 回だけ作る）
        shared = None
        if o.signature is not None:
            shared = o.signature
            self.log("既存のシグネチャを適用します（クラス数 %d）"
                     % shared.n_classes)
        elif len(stacks) == 1 or o.shared_signature:
            shared = self._build_signature(
                [p for _, p in stacks], o.basename, 10.0, 35.0)
        if shared is not None:
            finalize_signature(shared, self.log)
            self.outcome.shared_signature = shared

        # 3) 分類（共通シグネチャなら 45〜99%、個別なら 10〜99% を等分）
        base = 45.0 if shared is not None else 10.0
        span = (99.0 - base) / len(stacks)
        for i, (job, path) in enumerate(stacks):
            self._check_cancel()
            b = base + span * i
            try:
                sig = shared
                if sig is None:
                    sig = self._build_signature([path], job.basename,
                                                b, span * 0.4)
                    finalize_signature(sig, self.log)
                    cls_base, cls_span = b + span * 0.4, span * 0.6
                else:
                    cls_base, cls_span = b, span
                res = self._classify(job, path, sig, cls_base, cls_span,
                                     len(stacks) > 1, i, len(stacks))
                self.outcome.results.append(res)
            except Canceled:
                raise
            except RuntimeError as exc:
                if self.is_canceled():
                    raise Canceled() from exc
                self._fail(job, exc, len(stacks))
            except (OSError, ValueError, MemoryError) as exc:
                self._fail(job, exc, len(stacks))

        if not self.outcome.results:
            raise RuntimeError("すべてのファイルで失敗しました。\n"
                               + "\n".join(self.outcome.failures))
        if self.outcome.skipped or self.outcome.failures:
            self.log("完了 %d 件 / スキップ %d 件 / 失敗 %d 件"
                     % (len(self.outcome.results), len(self.outcome.skipped),
                        len(self.outcome.failures)))
        return self.outcome

    # -- 各段階 ------------------------------------------------------------
    def _resolve_existing(self, job: batch.Job) -> Optional[batch.Job]:
        """同名の分類結果がある場合に、スキップするか別名にするかを決める。"""
        o = self.o
        if o.existing == EXISTING_OVERWRITE:
            return job

        def taken(name: str) -> bool:
            return os.path.exists(os.path.join(job.out_dir,
                                               name + "_class.tif"))

        if not taken(job.basename):
            return job
        if o.existing == EXISTING_SKIP:
            self._skip(job, "既存の出力があります（%s）"
                       % os.path.join(job.out_dir,
                                      job.basename + "_class.tif"))
            self.outcome.existing_skipped.append(job.source)
            return None
        name, n = job.basename, 2
        while taken("%s_%d" % (name, n)):
            n += 1
        renamed = "%s_%d" % (name, n)
        self.log("既存の出力があるため別名にします: %s → %s"
                 % (job.basename, renamed))
        return batch.Job(job.source, job.out_dir, renamed)

    def _folder_jobs(self) -> List[batch.Job]:
        o = self.o
        mosaic = o.mode == MODE_MOSAIC
        common = o.signature is not None or o.shared_signature
        ok, problems = batch.check_inputs(
            o.inputs, bands=o.bands, same_crs=mosaic,
            same_bands=mosaic or common)
        for p in problems:
            self.outcome.skipped.append(p)
            self.log("除外: %s" % p)
        if not ok:
            raise ValueError("使えるファイルがありません。\n" + "\n".join(problems))
        if o.mode == MODE_MOSAIC:
            vrt = os.path.join(o.out_dir, o.basename + "_mosaic.vrt")
            batch.build_mosaic_vrt(ok, vrt, nodata=o.nodata)
            self.log("%d 件のファイルをモザイクしました: %s" % (len(ok), vrt))
            return [batch.Job(vrt, o.out_dir, o.basename)]
        self.log("%d 件のファイルを個別に分類します。" % len(ok))
        return batch.plan_jobs(ok, o.input_root, o.out_dir,
                               prefix=o.basename, keep_tree=o.keep_tree)

    def _prepare_input(self, job: batch.Job, many: bool) -> Optional[str]:
        """処理範囲の指定があれば切り出す。範囲外のファイルは飛ばす。"""
        o = self.o
        path = job.source
        if not (o.clip_extent or o.clip_mask_path):
            return path
        os.makedirs(job.out_dir, exist_ok=True)
        try:
            if o.clip_extent:
                if many:
                    info = batch.raster_info(path)
                    if info.ok and not batch.bounds_intersect(
                            info.bounds, tuple(o.clip_extent)):
                        self._skip(job, "処理範囲と重なりません")
                        return None
                out = os.path.join(job.out_dir, job.basename + "_clip_bbox.tif")
                path = rr.clip_by_extent(path, out, tuple(o.clip_extent),
                                         nodata=0.0 if o.nodata is None else o.nodata)
            if o.clip_mask_path:
                out = os.path.join(job.out_dir, job.basename + "_clip.tif")
                path = rr.clip_by_mask(path, o.clip_mask_path, out,
                                       nodata=0.0 if o.nodata is None else o.nodata)
        except RuntimeError as exc:
            if not many:
                raise
            # ポリゴンと重ならないファイルは GDAL がエラーにする
            self._skip(job, "切り出しに失敗しました（範囲外の可能性）: %s" % exc)
            return None
        if many:
            probe, _ = rr.sample_raster(path, max_samples=2000,
                                        bands=o.bands, nodata=o.nodata)
            if not probe.size:
                self._skip(job, "処理範囲内に有効な画素がありません")
                return None
        if not many:
            self.log("処理範囲で切り出しました。")
        return path

    def _build_signature(self, paths: Sequence[str], title: str,
                         base: float, span: float) -> rc.Signature:
        o = self.o
        if len(paths) == 1:
            samples, names = rr.sample_raster(
                paths[0], max_samples=o.max_samples, bands=o.bands,
                nodata=o.nodata,
                progress=_scaled(self._report, base, span * 0.3))
        else:
            samples, names = batch.sample_many(
                paths, max_samples=o.max_samples, bands=o.bands,
                nodata=o.nodata,
                progress=_scaled(self._report, base, span * 0.3),
                is_canceled=self.is_canceled)
        if not samples.size:
            raise ValueError("有効な画素が1つもありません（NoData のみ）")
        self.log("標本 %d 画素 / %d バンド（%d ファイル）"
                 % (samples.shape[0], samples.shape[1], len(paths)))
        self._check_cancel()
        res = rc.cluster(
            samples,
            n_classes=o.n_classes,
            min_class_size=o.min_class_size,
            min_separation=o.min_separation,
            max_iterations=o.max_iterations,
            convergence=o.convergence,
            band_names=names,
            progress=_scaled(self._report, base + span * 0.3, span * 0.7),
        )
        sig = res.signature
        sig.title = title
        self.log("反復 %d 回 / 収束 %.1f%% / クラス数 %d（削除 %d・統合 %d）"
                 % (res.n_iterations, res.convergence, sig.n_classes,
                    res.dropped, res.merged))
        if not res.converged:
            self.log("収束しきっていません。反復回数を増やすか、"
                     "初期クラス数を下げてください。")
        return sig

    def _classify(self, job: batch.Job, path: str, sig: rc.Signature,
                  base: float, span: float, many: bool, index: int,
                  total: int) -> Result:
        o = self.o
        os.makedirs(job.out_dir, exist_ok=True)
        res = Result(source=job.source, out_dir=job.out_dir,
                     basename=job.basename, stack_path=path, signature=sig)
        # 共通シグネチャでも、各結果の隣に置いておく
        # （「分類結果の意味づけ…」が _class.tif の隣の json を探すため）
        res.signature_path = os.path.join(
            job.out_dir, job.basename + "_signature.json")
        with open(res.signature_path, "w", encoding="utf-8") as fh:
            fh.write(sig.to_json())
        res.result_path = os.path.join(job.out_dir, job.basename + "_class.tif")
        if o.write_likelihood:
            res.likelihood_path = os.path.join(
                job.out_dir, job.basename + "_loglik.tif")

        prefix = ("[%d/%d] %s: " % (index + 1, total,
                                    os.path.basename(job.source))
                  if many else "")

        def cb(pct, msg):
            self._report(base + pct * span / 100.0, prefix + msg)

        rr.classify_raster(
            path, sig, res.result_path,
            bands=o.bands, nodata=o.nodata,
            likelihood_path=res.likelihood_path,
            progress=cb, is_canceled=self.is_canceled)
        if many:
            self.log("完了: %s → %s" % (job.source, res.result_path))
        return res

    def _skip(self, job: batch.Job, reason: str) -> None:
        text = "%s: %s" % (job.source, reason)
        self.outcome.skipped.append(text)
        self.log("スキップ: " + text)

    def _fail(self, job: batch.Job, exc: Exception, total: int) -> None:
        if total <= 1:
            raise exc
        text = "%s: %s: %s" % (job.source, type(exc).__name__, exc)
        self.outcome.failures.append(text)
        self.log("失敗: " + text)
