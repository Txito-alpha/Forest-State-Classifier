# -*- coding: utf-8 -*-
"""
zonal - 小班ポリゴンごとの分類結果集計

QGIS の [ラスタ解析]-[ゾーンヒストグラム] に相当するが、

  * クラス番号ではなくラベル名で列を作る（同じ番号でも年次で意味が違う事故を防ぐ）
  * 面積と割合まで一気に出す
  * ラスタと同じグリッドで焼くので、リサンプリング誤差が入らない

License: GPL v2
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence

import numpy as np
from osgeo import gdal, ogr

gdal.UseExceptions()
ogr.UseExceptions()

Progress = Optional[Callable[[int, str], None]]

_INDEX_FIELD = "rinkyo_idx"


def zonal_class_counts(
    class_raster: str,
    polygon_path: str,
    layer_name: Optional[str] = None,
    where: Optional[str] = None,
    key_fields: Sequence[str] = ("署名称", "林班主番", "小班名", "小班枝番"),
    progress: Progress = None,
) -> Dict[str, object]:
    """小班ごとにクラス別画素数を数える。

    戻り値:
        {
          "keys":    [{"_fid": ..., "署名称": ..., ...}, ...],
          "counts":  (n_polygons, n_classes+1) の整数配列,  # 列0は未分類
          "labels":  ["未分類", "常緑針葉樹林", ...],
          "pixel_area": 1画素の面積(m2),
        }
    """
    ras = gdal.Open(class_raster, gdal.GA_ReadOnly)
    w, h = ras.RasterXSize, ras.RasterYSize
    gt = ras.GetGeoTransform()
    proj = ras.GetProjection()
    pixel_area = abs(gt[1] * gt[5])

    band = ras.GetRasterBand(1)
    rat = band.GetDefaultRAT()
    labels = _labels_from_rat(rat)
    n_classes = len(labels)

    # --- ポリゴンを 1..N の連番で焼く --------------------------------------
    src = ogr.Open(polygon_path, 0)
    if src is None:
        raise ValueError("ポリゴンを開けません: %s" % polygon_path)
    lyr = src.GetLayerByName(layer_name) if layer_name else src.GetLayer(0)
    if where:
        lyr.SetAttributeFilter(where)

    mem_drv = ogr.GetDriverByName("Memory")
    mem_ds = mem_drv.CreateDataSource("zonal")
    mem_lyr = mem_ds.CopyLayer(lyr, "zones")
    mem_lyr.CreateField(ogr.FieldDefn(_INDEX_FIELD, ogr.OFTInteger))

    keys: List[Dict[str, object]] = []
    available = [f for f in key_fields
                 if mem_lyr.GetLayerDefn().GetFieldIndex(f) >= 0]
    mem_lyr.ResetReading()
    for i, feat in enumerate(mem_lyr):
        feat.SetField(_INDEX_FIELD, i + 1)
        mem_lyr.SetFeature(feat)
        key = {f: feat.GetField(f) for f in available}
        key["_fid"] = int(feat.GetFID())
        keys.append(key)
    n_zones = len(keys)
    if n_zones == 0:
        raise ValueError("対象となるポリゴンがありません")

    _report(progress, 10, "小班をラスタ化中… %d 件" % n_zones)
    zone_ras = _rasterize_index(mem_lyr, w, h, gt, proj, n_zones)

    # --- ブロックごとに数える ---------------------------------------------
    counts = np.zeros((n_zones + 1, n_classes), dtype=np.int64)
    block = 512
    n_blocks = ((h + block - 1) // block) or 1
    for bi, y in enumerate(range(0, h, block)):
        rows = min(block, h - y)
        z = zone_ras.GetRasterBand(1).ReadAsArray(0, y, w, rows).ravel()
        c = band.ReadAsArray(0, y, w, rows).ravel().astype(np.int64)
        sel = z > 0
        if sel.any():
            c = np.clip(c[sel], 0, n_classes - 1)
            flat = z[sel].astype(np.int64) * n_classes + c
            counts += np.bincount(
                flat, minlength=(n_zones + 1) * n_classes
            ).reshape(n_zones + 1, n_classes)
        _report(progress, 10 + (bi + 1) * 85 // n_blocks,
                "集計中… %d / %d" % (bi + 1, n_blocks))

    zone_ras = None
    ras = None
    return {
        "keys": keys,
        "counts": counts[1:],  # 0行目は背景
        "labels": labels,
        "pixel_area": pixel_area,
    }


def to_records(result: Dict[str, object],
               exclude_unclassified: bool = True) -> List[Dict[str, object]]:
    """集計結果を「1小班1レコード」の辞書リストに変換する。

    各クラスについて、画素数・面積(ha)・割合(%) を持たせる。
    属性テーブルへの結合や xlsx 出力はこの形から行う。
    """
    labels: List[str] = result["labels"]  # type: ignore[assignment]
    counts: np.ndarray = result["counts"]  # type: ignore[assignment]
    area = float(result["pixel_area"])  # type: ignore[arg-type]

    start = 1 if exclude_unclassified else 0
    denom = counts[:, start:].sum(axis=1).astype(np.float64)
    denom[denom == 0] = np.nan

    records = []
    for i, key in enumerate(result["keys"]):  # type: ignore[arg-type]
        rec: Dict[str, object] = dict(key)
        rec["集計画素数"] = int(counts[i, start:].sum())
        rec["集計面積_ha"] = float(counts[i, start:].sum() * area / 10000.0)
        for c in range(start, len(labels)):
            name = labels[c] or "class%d" % c
            rec["%s_画素数" % name] = int(counts[i, c])
            rec["%s_ha" % name] = float(counts[i, c] * area / 10000.0)
            ratio = counts[i, c] / denom[i] * 100.0
            rec["%s_率" % name] = None if np.isnan(ratio) else round(float(ratio), 2)
        records.append(rec)
    return records


# ---------------------------------------------------------------------------
def _rasterize_index(layer, w, h, gt, proj, n_zones):
    """ポリゴンを連番でラスタ化する。1600 件を超えたら 32bit に切り替える。"""
    dtype = gdal.GDT_UInt16 if n_zones < 65535 else gdal.GDT_UInt32
    drv = gdal.GetDriverByName("MEM")
    ds = drv.Create("", w, h, 1, dtype)
    ds.SetGeoTransform(gt)
    ds.SetProjection(proj)
    ds.GetRasterBand(1).Fill(0)
    gdal.RasterizeLayer(ds, [1], layer,
                        options=["ATTRIBUTE=%s" % _INDEX_FIELD,
                                 "ALL_TOUCHED=FALSE"])
    return ds


def _labels_from_rat(rat) -> List[str]:
    """RAT からクラス名の一覧を取り出す。無ければ番号を名前にする。"""
    if rat is None:
        return ["未分類"] + ["class %d" % i for i in range(1, 256)]
    name_col = -1
    value_col = -1
    for c in range(rat.GetColumnCount()):
        usage = rat.GetUsageOfCol(c)
        if usage == gdal.GFU_Name:
            name_col = c
        elif usage == gdal.GFU_MinMax:
            value_col = c
    if name_col < 0:
        return ["未分類"] + ["class %d" % i for i in range(1, 256)]

    top = 0
    pairs = {}
    for r in range(rat.GetRowCount()):
        v = rat.GetValueAsInt(r, value_col) if value_col >= 0 else r
        pairs[v] = rat.GetValueAsString(r, name_col)
        top = max(top, v)
    return [pairs.get(i, "class %d" % i) for i in range(top + 1)]


def _report(progress: Progress, pct: int, msg: str) -> None:
    if progress:
        progress(int(pct), msg)
