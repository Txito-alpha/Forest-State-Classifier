# -*- coding: utf-8 -*-
"""
batch - フォルダ入力（再帰検索）まわりの下請け（GDAL のみ、QGIS API 非依存）

- フォルダからラスタを探す（再帰／非再帰、ワイルドカード複数指定）
- 見つかったラスタのバンド数・座標参照系の確認
- 一時 VRT によるモザイク
- ファイルごとに処理する場合の出力先・ファイル名の割り当て
- 複数ファイルからの標本抽出（共通シグネチャ用）

License: GPL v2
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from osgeo import gdal, osr

from .rinkyo_raster import sample_raster

gdal.UseExceptions()

DEFAULT_PATTERNS = "*.tif;*.tiff;*.vrt;*.jp2;*.img"

# このプラグイン自身が書き出すファイル。入力フォルダの中に出力フォルダを
# 置いた場合に、前回の結果を入力として拾ってしまわないよう除外する。
OUTPUT_SUFFIXES = ("_class", "_loglik", "_clip", "_clip_bbox", "_mosaic")

Progress = Optional[Callable[[int, str], None]]


# ---------------------------------------------------------------------------
# 検索
# ---------------------------------------------------------------------------
def parse_patterns(text: Optional[str]) -> List[str]:
    """「*.tif;*.vrt」「*.tif, *.vrt」「*.tif *.vrt」のいずれでも受け付ける。"""
    text = text if text and text.strip() else DEFAULT_PATTERNS
    out = []
    for token in text.replace(",", ";").replace(" ", ";").split(";"):
        token = token.strip()
        if not token:
            continue
        if "*" not in token and "?" not in token:
            # 「tif」「.tif」だけ書かれた場合も拡張子として扱う
            token = "*." + token.lstrip(".")
        out.append(token.lower())
    return out


def is_plugin_output(path: str) -> bool:
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    return stem.endswith(OUTPUT_SUFFIXES)


def _is_under(path: str, parent: str) -> bool:
    try:
        path = os.path.normcase(os.path.abspath(path))
        parent = os.path.normcase(os.path.abspath(parent))
        return os.path.commonpath([path, parent]) == parent
    except ValueError:  # Windows でドライブが異なる場合
        return False


def find_rasters(folder: str, patterns: Optional[Iterable[str]] = None,
                 recursive: bool = True,
                 exclude_dirs: Sequence[str] = ()) -> List[str]:
    """folder 以下からパターンに合うファイルを探し、パス順に並べて返す。

    - 大文字小文字は区別しない（Windows の .TIF 対策）
    - exclude_dirs 以下（出力フォルダなど）は探さない
    - このプラグインの出力（*_class.tif など）は除外する
    """
    if not folder or not os.path.isdir(folder):
        raise ValueError("フォルダが見つかりません: %s" % folder)
    pats = list(patterns) if patterns else parse_patterns(None)
    pats = [p.lower() for p in pats]
    excludes = [d for d in exclude_dirs if d]

    found: List[str] = []
    for root, dirs, files in os.walk(folder):
        # 隠しフォルダと除外フォルダは潜らない。並びも安定させる。
        dirs[:] = sorted(
            d for d in dirs
            if not d.startswith(".")
            and not any(_is_under(os.path.join(root, d), ex) for ex in excludes))
        if any(_is_under(root, ex) for ex in excludes):
            files = []
        for name in sorted(files):
            low = name.lower()
            if any(fnmatch.fnmatchcase(low, p) for p in pats):
                path = os.path.join(root, name)
                if not is_plugin_output(path):
                    found.append(os.path.normpath(path))
        if not recursive:
            break
    return found


# ---------------------------------------------------------------------------
# 情報取得と整合チェック
# ---------------------------------------------------------------------------
@dataclass
class RasterInfo:
    path: str
    ok: bool = True
    error: str = ""
    band_count: int = 0
    band_names: List[str] = field(default_factory=list)
    wkt: str = ""
    bounds: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    size: Tuple[int, int] = (0, 0)

    @property
    def crs_key(self) -> str:
        """座標参照系の比較用キー（EPSG が取れればそれ、無ければ WKT）。"""
        if not self.wkt:
            return ""
        srs = osr.SpatialReference()
        try:
            srs.ImportFromWkt(self.wkt)
            srs.AutoIdentifyEPSG()
        except RuntimeError:
            return self.wkt
        name = srs.GetAuthorityName(None)
        code = srs.GetAuthorityCode(None)
        return "%s:%s" % (name, code) if name and code else srs.ExportToWkt()


def raster_info(path: str) -> RasterInfo:
    info = RasterInfo(path=path)
    try:
        ds = gdal.Open(path, gdal.GA_ReadOnly)
    except RuntimeError as exc:
        info.ok = False
        info.error = str(exc)
        return info
    if ds is None:
        info.ok = False
        info.error = "GDAL で開けません"
        return info
    info.band_count = ds.RasterCount
    info.band_names = [ds.GetRasterBand(i).GetDescription() or ""
                       for i in range(1, ds.RasterCount + 1)]
    info.wkt = ds.GetProjection() or ""
    w, h = ds.RasterXSize, ds.RasterYSize
    gt = ds.GetGeoTransform()
    xs = [gt[0] + px * gt[1] + py * gt[2] for px, py in
          ((0, 0), (w, 0), (0, h), (w, h))]
    ys = [gt[3] + px * gt[4] + py * gt[5] for px, py in
          ((0, 0), (w, 0), (0, h), (w, h))]
    info.bounds = (min(xs), min(ys), max(xs), max(ys))
    info.size = (w, h)
    ds = None
    if info.band_count == 0:
        info.ok = False
        info.error = "バンドがありません"
    return info


def check_inputs(paths: Sequence[str], bands: Optional[Sequence[int]] = None,
                 same_crs: bool = False, same_bands: bool = False,
                 infos: Optional[Dict[str, RasterInfo]] = None
                 ) -> Tuple[List[str], List[str]]:
    """使えるファイルと、除外したファイルの理由を返す。

    - bands を渡した場合は、最大のバンド番号を持たないファイルを除外する。
    - same_crs=True（モザイク用）なら、1本目と座標参照系が違うものを除外する。
    - same_bands=True（モザイク・共通シグネチャ用）なら、1本目とバンド数が
      違うものを除外する。
    """
    ok: List[str] = []
    problems: List[str] = []
    need = max(bands) if bands else 0
    ref = None
    for p in paths:
        info = (infos or {}).get(p) or raster_info(p)
        if not info.ok:
            problems.append("%s: %s" % (p, info.error))
            continue
        if need and info.band_count < need:
            problems.append("%s: バンド数が足りません（%d < %d）"
                            % (p, info.band_count, need))
            continue
        if ref is None:
            ref = info
        else:
            if same_crs and info.crs_key != ref.crs_key:
                problems.append("%s: 座標参照系が1本目（%s）と異なります"
                                % (p, os.path.basename(ref.path)))
                continue
            if same_bands and info.band_count != ref.band_count:
                problems.append("%s: バンド数が1本目と異なります（%d ≠ %d）"
                                % (p, info.band_count, ref.band_count))
                continue
        ok.append(p)
    return ok, problems


def union_bounds(infos: Iterable[RasterInfo]):
    boxes = [i.bounds for i in infos if i.ok]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def bounds_intersect(a, b) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


# ---------------------------------------------------------------------------
# モザイク
# ---------------------------------------------------------------------------
def build_mosaic_vrt(paths: Sequence[str], vrt_path: str,
                     nodata: Optional[float] = 0.0) -> str:
    """複数のラスタを 1 本の VRT にまとめる（画素はコピーしない）。

    重なり部分は後ろのファイルが優先。NoData を指定しておくと、
    タイルの余白（0）で隣のタイルの画素を塗りつぶさずに済む。
    """
    if not paths:
        raise ValueError("モザイクにするファイルがありません")
    kwargs = {"resolution": "highest"}
    if nodata is not None:
        kwargs.update(srcNodata=nodata, VRTNodata=nodata)
    opts = gdal.BuildVRTOptions(**kwargs)
    os.makedirs(os.path.dirname(os.path.abspath(vrt_path)), exist_ok=True)
    ds = gdal.BuildVRT(vrt_path, list(paths), options=opts)
    if ds is None:
        raise RuntimeError("VRT を作れませんでした: %s" % vrt_path)
    # バンド名（Blue/Green/... や NDVI）は 1 本目から引き継ぐ
    first = gdal.Open(paths[0], gdal.GA_ReadOnly)
    for i in range(1, min(ds.RasterCount, first.RasterCount) + 1):
        desc = first.GetRasterBand(i).GetDescription()
        if desc:
            ds.GetRasterBand(i).SetDescription(desc)
    first = None
    ds.FlushCache()
    ds = None
    return vrt_path


# ---------------------------------------------------------------------------
# ファイルごとに処理する場合の出力先
# ---------------------------------------------------------------------------
@dataclass
class Job:
    source: str
    out_dir: str
    basename: str


def _safe(text: str) -> str:
    bad = '\\/:*?"<>| '
    return "".join("_" if c in bad else c for c in text.strip()) or "rinkyo"


def plan_jobs(paths: Sequence[str], root: str, out_dir: str,
              prefix: str = "", keep_tree: bool = True) -> List[Job]:
    """入力ごとの出力フォルダとファイル名を決める。

    keep_tree=True なら、入力フォルダからの相対フォルダ構成を出力側に再現する。
    同じ場所に同名（拡張子違い）のファイルがあれば拡張子を名前に足して区別する。
    """
    jobs: List[Job] = []
    used = set()
    for p in paths:
        rel_dir = ""
        if keep_tree and root and _is_under(p, root):
            rel_dir = os.path.relpath(os.path.dirname(p), root)
            if rel_dir == ".":
                rel_dir = ""
        stem, ext = os.path.splitext(os.path.basename(p))
        name = _safe("%s_%s" % (prefix, stem) if prefix else stem)
        target = os.path.normpath(os.path.join(out_dir, rel_dir))
        key = os.path.normcase(os.path.join(target, name))
        if key in used:
            name = _safe("%s_%s" % (name, ext.lstrip(".")))
            key = os.path.normcase(os.path.join(target, name))
            n = 2
            base = name
            while key in used:
                name = "%s_%d" % (base, n)
                key = os.path.normcase(os.path.join(target, name))
                n += 1
        used.add(key)
        jobs.append(Job(source=p, out_dir=target, basename=name))
    return jobs


# ---------------------------------------------------------------------------
# 複数ファイルからの標本抽出
# ---------------------------------------------------------------------------
def sample_many(paths: Sequence[str], max_samples: int = 100000,
                bands: Optional[Sequence[int]] = None,
                nodata: Optional[float] = 0.0,
                progress: Progress = None,
                is_canceled: Optional[Callable[[], bool]] = None):
    """各ファイルから等間隔に標本を取り、つなげて返す。

    標本数の上限は画素数に比例して配分するので、大きな画像ほど
    多く採られる（小さな画像に引っ張られない）。
    """
    if not paths:
        raise ValueError("標本を取るファイルがありません")
    areas = []
    for p in paths:
        ds = gdal.Open(p, gdal.GA_ReadOnly)
        areas.append(float(ds.RasterXSize) * ds.RasterYSize)
        ds = None
    total = sum(areas) or 1.0

    chunks = []
    names: List[str] = []
    for i, (p, a) in enumerate(zip(paths, areas)):
        if is_canceled and is_canceled():
            raise RuntimeError("処理が中止されました")
        budget = max(1000, int(max_samples * a / total))
        x, nm = sample_raster(p, max_samples=budget, bands=bands,
                              nodata=nodata)
        if not names:
            names = nm
        if x.size:
            chunks.append(x)
        if progress:
            progress((i + 1) * 100 // len(paths),
                     "標本抽出中… %d / %d" % (i + 1, len(paths)))
    if not chunks:
        raise ValueError("有効な画素が1つもありません（NoData のみ）")
    return np.vstack(chunks), names
