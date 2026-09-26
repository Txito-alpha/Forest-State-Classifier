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

import json
import os

from typing import Callable, Dict, List, Optional, Sequence

import numpy as np
from osgeo import gdal, ogr

from .categories import DEFAULT_CATEGORIES

gdal.UseExceptions()
ogr.UseExceptions()

Progress = Optional[Callable[[int, str], None]]

_INDEX_FIELD = "rinkyo_idx"


def zonal_class_counts(
    class_raster: str,
    polygon_path: str,
    layer_name: Optional[str] = None,
    where: Optional[str] = None,
    feature_ids: Optional[Sequence[int]] = None,
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

    # ラベル編集後の正本は分類ラスタと同名系列の signature JSON。
    # GeoTIFF のRATは環境・ドライバによって更新後も "class 1" 等のまま
    # 読み出されることがあるため、隣接JSONがあればそのlabelsを優先する。
    signature_labels = _labels_from_signature(class_raster)
    if signature_labels:
        labels = ["未分類"] + signature_labels

    n_classes = len(labels)

    # --- ポリゴンを 1..N の連番で焼く --------------------------------------
    src = ogr.Open(polygon_path, 0)
    if src is None:
        raise ValueError("ポリゴンを開けません: %s" % polygon_path)
    lyr = src.GetLayerByName(layer_name) if layer_name else src.GetLayer(0)
    filters = []
    if where:
        filters.append("(%s)" % where)
    if feature_ids:
        # QGIS の選択地物 ID は OGR の FID に対応する。
        # FID は OGR SQL の擬似列として多くのファイル形式で利用できる。
        ids = ",".join(str(int(fid)) for fid in feature_ids)
        filters.append("FID IN (%s)" % ids)
    if filters:
        err = lyr.SetAttributeFilter(" AND ".join(filters))
        if err != 0:
            raise ValueError("選択地物の抽出条件を適用できませんでした")

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

    # 同じ林況ラベルが複数のclassへ割り当てられている場合は、
    # class番号別ではなくラベル別に画素数を合算する。
    # ラベルの出現順はRAT上の最初のclass順を維持する。
    label_indices: Dict[str, List[int]] = {}
    for c in range(start, len(labels)):
        name = (labels[c] or "").strip() or "class%d" % c
        label_indices.setdefault(name, []).append(c)

    # 標準の林況ラベルは、該当画素が0でも必ず出力列を作る。
    # RATにそのラベルが存在しない場合は空のindexリストにして0集計とする。
    # これにより「常緑針葉樹林_画素数」等がXLSX/GPKGから欠落しない。
    for name, _color in DEFAULT_CATEGORIES:
        if exclude_unclassified and name == "未分類":
            continue
        label_indices.setdefault(name, [])

    denom = counts[:, start:].sum(axis=1).astype(np.float64)
    denom[denom == 0] = np.nan

    records = []
    for i, key in enumerate(result["keys"]):  # type: ignore[arg-type]
        rec: Dict[str, object] = dict(key)
        total = int(counts[i, start:].sum())
        rec["集計画素数"] = total
        rec["集計面積_ha"] = float(total * area / 10000.0)

        for name, indices in label_indices.items():
            label_count = int(counts[i, indices].sum()) if indices else 0
            rec["%s_画素数" % name] = label_count
            rec["%s_ha" % name] = float(label_count * area / 10000.0)
            ratio = label_count / denom[i] * 100.0
            rec["%s_率" % name] = (None if np.isnan(ratio)
                                   else round(float(ratio), 2))
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


def _labels_from_signature(class_raster: str) -> Optional[List[str]]:
    """分類ラスタに対応するsignature JSONからclassラベルを得る。"""
    raster_path = class_raster.split("|")[0]
    stem, _ext = os.path.splitext(raster_path)
    if stem.endswith("_class"):
        stem = stem[:-6]
    signature_path = stem + "_signature.json"
    if not os.path.isfile(signature_path):
        return None
    try:
        with open(signature_path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        raw = data.get("labels")
        if not isinstance(raw, list) or not raw:
            return None
        labels = []
        for i, value in enumerate(raw, 1):
            text = str(value).strip() if value is not None else ""
            labels.append(text or "class %d" % i)
        return labels
    except (OSError, ValueError, TypeError):
        # JSONが無い・壊れている場合だけRATへフォールバックする。
        return None


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
