# -*- coding: utf-8 -*-
"""
rinkyo_raster - ラスタ入出力（GDAL のみ、QGIS API 非依存）

バンド合成 → 標本抽出 → クラスタリング → 全画素分類 → ラスタ属性テーブル
までを、外部プロセスを一切起動せずにインプロセスで完結させる。

License: GPL v2
"""

from __future__ import annotations

from typing import Callable, List, Optional, Sequence

import numpy as np
from osgeo import gdal, osr

from .categories import color_for
from .rinkyo_core import Signature, classify_array, prepare_signature

gdal.UseExceptions()

# 分類結果は 0-255 の整数なので、DEFLATE + PREDICTOR=2 が素直に効く。
# LERC や JPEG は非可逆・浮動小数向けなので分類ラスタには使わない。
_CREATE_OPTS = [
    "TILED=YES",
    "BLOCKXSIZE=512",
    "BLOCKYSIZE=512",
    "COMPRESS=DEFLATE",
    "PREDICTOR=2",
    "BIGTIFF=IF_SAFER",
]

Progress = Optional[Callable[[int, str], None]]


def _report(progress: Progress, pct: int, msg: str) -> None:
    if progress:
        progress(int(pct), msg)


# ---------------------------------------------------------------------------
# 1. バンド合成（[ラスタ]-[その他]-[結合(gdal_merge)] の置き換え）
# ---------------------------------------------------------------------------
def stack_bands(band_paths: Sequence[str], out_path: str,
                add_ndvi: bool = True, red_index: int = 2, nir_index: int = 3,
                nodata: float = 0.0, progress: Progress = None) -> str:
    """B2/B3/B4/B8 の jp2 を 1 本の GTiff に束ね、必要なら NDVI を足す。

    gdal_merge.py をサブプロセスで呼ぶ QGIS の GDAL アルゴリズムと違い、
    パスに日本語や空白が入っても壊れない。
    """
    if len(band_paths) < 2:
        raise ValueError("バンドは2つ以上指定してください")

    first = gdal.Open(band_paths[0], gdal.GA_ReadOnly)
    w, h = first.RasterXSize, first.RasterYSize
    gt, proj = first.GetGeoTransform(), first.GetProjection()

    n_out = len(band_paths) + (1 if add_ndvi else 0)
    drv = gdal.GetDriverByName("GTiff")
    dst = drv.Create(out_path, w, h, n_out, gdal.GDT_Float32, _CREATE_OPTS)
    dst.SetGeoTransform(gt)
    dst.SetProjection(proj)

    srcs = []
    for i, p in enumerate(band_paths):
        ds = gdal.Open(p, gdal.GA_ReadOnly)
        if ds.RasterXSize != w or ds.RasterYSize != h:
            raise ValueError("バンド %s のサイズが1本目と一致しません（解像度を"
                             "揃えてから合成してください）" % p)
        srcs.append(ds)

    block = 512
    n_blocks = ((h + block - 1) // block) or 1
    for bi, y in enumerate(range(0, h, block)):
        rows = min(block, h - y)
        arrs = []
        for i, ds in enumerate(srcs):
            a = ds.GetRasterBand(1).ReadAsArray(0, y, w, rows).astype(np.float32)
            arrs.append(a)
            dst.GetRasterBand(i + 1).WriteArray(a, 0, y)
        if add_ndvi:
            red = arrs[red_index]
            nir = arrs[nir_index]
            den = nir + red
            ndvi = np.where(den != 0, (nir - red) / np.where(den == 0, 1, den), 0)
            dst.GetRasterBand(n_out).WriteArray(ndvi.astype(np.float32), 0, y)
        _report(progress, (bi + 1) * 100 // n_blocks, "バンド合成中…")

    names = ["Blue", "Green", "Red", "NIR"][:len(band_paths)]
    while len(names) < len(band_paths):
        names.append("band%d" % (len(names) + 1))
    if add_ndvi:
        names.append("NDVI")
    for i, nm in enumerate(names):
        b = dst.GetRasterBand(i + 1)
        b.SetDescription(nm)
        if i < len(band_paths):
            b.SetNoDataValue(nodata)

    dst.FlushCache()
    dst = None
    return out_path


# ---------------------------------------------------------------------------
# 2. 標本抽出
# ---------------------------------------------------------------------------
def sample_raster(path: str, max_samples: int = 100000,
                  bands: Optional[Sequence[int]] = None,
                  nodata: Optional[float] = 0.0,
                  progress: Progress = None):
    """規則的な間引きで標本画素を取り出す。

    乱数を使わず等間隔で抜くので、同じ画像からは必ず同じ標本になる。
    「実行するたび結果が変わる」問題をここで断つ。
    """
    ds = gdal.Open(path, gdal.GA_ReadOnly)
    w, h = ds.RasterXSize, ds.RasterYSize
    if bands is None:
        bands = list(range(1, ds.RasterCount + 1))

    step = max(1, int(np.sqrt(float(w) * h / max(max_samples, 1))))
    xs = np.arange(0, w, step)
    ys = np.arange(0, h, step)

    cols = []
    for k, b in enumerate(bands):
        band = ds.GetRasterBand(b)
        buf = band.ReadAsArray(
            0, 0, w, h, buf_xsize=len(xs), buf_ysize=len(ys),
            resample_alg=gdal.GRIORA_NearestNeighbour,
        ).astype(np.float64).ravel()
        cols.append(buf)
        _report(progress, (k + 1) * 100 // len(bands), "標本抽出中…")

    x = np.column_stack(cols)
    mask = np.isfinite(x).all(axis=1)
    if nodata is not None:
        mask &= ~(x == nodata).all(axis=1)
    names = [ds.GetRasterBand(b).GetDescription() or "band%d" % b
             for b in bands]
    ds = None
    return x[mask], names


# ---------------------------------------------------------------------------
# 3. 全画素分類（i.maxlik + r.out.gdal の置き換え）
# ---------------------------------------------------------------------------
def classify_raster(src_path: str, sig: Signature, out_path: str,
                    bands: Optional[Sequence[int]] = None,
                    nodata: Optional[float] = 0.0,
                    likelihood_path: Optional[str] = None,
                    progress: Progress = None,
                    is_canceled: Optional[Callable[[], bool]] = None) -> str:
    """シグネチャを使って画像全体を最尤法で分類する。

    likelihood_path を指定すると、採用クラスの対数尤度も出力する。
    値が低い画素は「どのクラスにも似ていない」＝要目視確認の場所になる。
    """
    src = gdal.Open(src_path, gdal.GA_ReadOnly)
    w, h = src.RasterXSize, src.RasterYSize
    if bands is None:
        bands = list(range(1, src.RasterCount + 1))
    if len(bands) != sig.n_bands:
        raise ValueError("画像のバンド数(%d)とシグネチャのバンド数(%d)が"
                         "一致しません" % (len(bands), sig.n_bands))

    drv = gdal.GetDriverByName("GTiff")
    dst = drv.Create(out_path, w, h, 1, gdal.GDT_Byte, _CREATE_OPTS)
    dst.SetGeoTransform(src.GetGeoTransform())
    dst.SetProjection(src.GetProjection())
    dst.GetRasterBand(1).SetNoDataValue(0)

    lik = None
    if likelihood_path:
        lik = drv.Create(likelihood_path, w, h, 1, gdal.GDT_Float32,
                         _CREATE_OPTS)
        lik.SetGeoTransform(src.GetGeoTransform())
        lik.SetProjection(src.GetProjection())

    prepared = prepare_signature(sig)
    block = 512
    n_blocks = ((h + block - 1) // block) or 1
    for bi, y in enumerate(range(0, h, block)):
        if is_canceled and is_canceled():
            dst = lik = None
            gdal.Unlink(out_path)
            raise RuntimeError("処理が中止されました")
        rows = min(block, h - y)
        stack = np.empty((rows * w, len(bands)), dtype=np.float64)
        for k, b in enumerate(bands):
            stack[:, k] = src.GetRasterBand(b).ReadAsArray(
                0, y, w, rows).astype(np.float64).ravel()

        valid = np.isfinite(stack).all(axis=1)
        if nodata is not None:
            valid &= ~(stack == nodata).all(axis=1)

        out = np.zeros(rows * w, dtype=np.uint8)
        best = np.full(rows * w, np.nan, dtype=np.float32)
        if valid.any():
            c, b_ = classify_array(stack[valid], sig, prepared)
            out[valid] = c
            best[valid] = b_
        dst.GetRasterBand(1).WriteArray(out.reshape(rows, w), 0, y)
        if lik is not None:
            lik.GetRasterBand(1).WriteArray(best.reshape(rows, w), 0, y)
        _report(progress, (bi + 1) * 100 // n_blocks,
                "分類中… %d / %d" % (bi + 1, n_blocks))

    write_rat(dst, sig)
    dst.FlushCache()
    dst = None
    if lik is not None:
        lik.FlushCache()
        lik = None
    src = None
    return out_path


# ---------------------------------------------------------------------------
# 4. ラスタ属性テーブル
# ---------------------------------------------------------------------------
def write_rat(dataset, sig: Signature, labels: Optional[List[str]] = None,
              colors: Optional[List[str]] = None) -> None:
    """GDAL のラスタ属性テーブルとカラーテーブルを書き込む。

    QGIS 3.30 以降はこの RAT をそのまま読んで凡例に反映してくれるので、
    「シンボロジで50クラスに手作業で色とラベルを入れる」工程が消える。
    """
    labels = labels or sig.labels
    colors = colors or sig.colors

    rat = gdal.RasterAttributeTable()
    rat.CreateColumn("Value", gdal.GFT_Integer, gdal.GFU_MinMax)
    rat.CreateColumn("Count", gdal.GFT_Integer, gdal.GFU_PixelCount)
    rat.CreateColumn("Class", gdal.GFT_String, gdal.GFU_Name)
    for u, nm in ((gdal.GFU_Red, "R"), (gdal.GFU_Green, "G"),
                  (gdal.GFU_Blue, "B"), (gdal.GFU_Alpha, "A")):
        rat.CreateColumn(nm, gdal.GFT_Integer, u)

    ct = gdal.ColorTable()
    ct.SetColorEntry(0, (0, 0, 0, 0))
    rat.SetRowCount(sig.n_classes + 1)
    rat.SetValueAsInt(0, 0, 0)
    rat.SetValueAsInt(0, 1, 0)
    rat.SetValueAsString(0, 2, "未分類")
    for c in range(3, 7):
        rat.SetValueAsInt(0, c, 0)

    for i in range(sig.n_classes):
        label = labels[i] if i < len(labels) else ""
        col = colors[i] if i < len(colors) else ""
        # 色が未設定でもグレー一色にはせず、ラベルまたはクラス番号から決める
        r, g, b = _hex_to_rgb(col or color_for(label, i))
        row = i + 1
        rat.SetValueAsInt(row, 0, i + 1)
        rat.SetValueAsInt(row, 1, int(sig.counts[i]))
        rat.SetValueAsString(row, 2, label or "class %d" % (i + 1))
        rat.SetValueAsInt(row, 3, r)
        rat.SetValueAsInt(row, 4, g)
        rat.SetValueAsInt(row, 5, b)
        rat.SetValueAsInt(row, 6, 255)
        ct.SetColorEntry(i + 1, (r, g, b, 255))

    band = dataset.GetRasterBand(1)
    band.SetDefaultRAT(rat)
    band.SetRasterColorTable(ct)
    band.SetRasterColorInterpretation(gdal.GCI_PaletteIndex)


def _hex_to_rgb(text: str):
    text = (text or "").lstrip("#")
    if len(text) == 6:
        try:
            return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
        except ValueError:
            pass
    return (200, 200, 200)


# ---------------------------------------------------------------------------
# 5. クリップ（[マスクレイヤで切り抜く] の置き換え）
# ---------------------------------------------------------------------------
def clip_by_mask(src_path: str, mask_path: str, out_path: str,
                 nodata: float = 0.0, where: Optional[str] = None) -> str:
    """ポリゴンで切り抜く。gdal.Warp をライブラリとして直接呼ぶ。"""
    opts = gdal.WarpOptions(
        format="GTiff",
        cutlineDSName=mask_path,
        cutlineWhere=where,
        cropToCutline=True,
        dstNodata=nodata,
        creationOptions=_CREATE_OPTS,
        targetAlignedPixels=False,
    )
    gdal.Warp(out_path, src_path, options=opts)
    return out_path


def epsg_of(path: str) -> Optional[int]:
    ds = gdal.Open(path, gdal.GA_ReadOnly)
    wkt = ds.GetProjection()
    ds = None
    if not wkt:
        return None
    srs = osr.SpatialReference()
    srs.ImportFromWkt(wkt)
    code = srs.GetAuthorityCode(None)
    return int(code) if code else None
