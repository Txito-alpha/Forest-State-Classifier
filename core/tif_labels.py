# -*- coding: utf-8 -*-
"""
tif_labels - 意味づけ（クラス名・色）を分類ラスタ（GeoTIFF）本体に埋め込む

GeoTIFF には「クラス名」を入れる標準の場所がなく、GDAL はラスタ属性テーブル
（RAT）を .tif.aux.xml という別ファイルに書く。そのため TIF だけを
コピー・共有したり、QGIS が開いている間に .aux.xml が古い内容で
上書きされたりすると、クラス名が「class 1」などに戻ってしまう。

ここでは次の 3 つを TIF 本体（GDAL_METADATA タグ）に書き込み、
TIF 単体で意味づけを持ち運べるようにする。

  * バンド 1 のメタデータ CLASS_<値> = クラス名
    （QGIS のレイヤプロパティ「情報」や gdalinfo でそのまま読める）
  * ドメイン FOREST_STATE_CLASSIFIER の LABELS
    = {"labels": [...], "colors": [...]}（意味づけのたびに更新する小さな JSON）
  * 同ドメインの SIGNATURE = シグネチャ本体（クラス平均・共分散）
    （クラス定義は分類後に変わらないので、内容が変わったときだけ書く）

色は従来どおり TIFF のカラーマップ（パレット）に入る。RAT も従来どおり
.aux.xml に書く（QGIS の凡例は RAT を使うため）。

QGIS API には依存しない（GDAL と numpy のみ）。

License: GPL v2
"""

from __future__ import annotations

import copy
import json
import os
from typing import List, Optional, Tuple

import numpy as np
from osgeo import gdal

from .rinkyo_core import Signature

gdal.UseExceptions()

DOMAIN = "FOREST_STATE_CLASSIFIER"
KEY_LABELS = "LABELS"
KEY_SIGNATURE = "SIGNATURE"
LABELS_FORMAT = "fsc-labels-1"
CLASS_KEY = "CLASS_%d"
UNCLASSIFIED = "未分類"


# ---------------------------------------------------------------------------
# パス
# ---------------------------------------------------------------------------
def raster_file(path: str) -> str:
    """QGIS のレイヤソース（'|layername=...' 付き）からファイルパスを取り出す。"""
    return (path or "").split("|")[0]


def signature_path_for(raster_path: str) -> str:
    """分類ラスタ <名前>_class.tif に対応する <名前>_signature.json のパス。"""
    stem = os.path.splitext(raster_file(raster_path))[0]
    if stem.endswith("_class"):
        stem = stem[:-len("_class")]
    return stem + "_signature.json"


# ---------------------------------------------------------------------------
# 書き込み
# ---------------------------------------------------------------------------
def _compact(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _padded(values, n: int) -> List[str]:
    out = [str(v) if v is not None else "" for v in list(values or [])[:n]]
    return out + [""] * (n - len(out))


def _rounded(values) -> list:
    """有効数字 8 桁に丸めたリスト（埋め込みを小さくするため）。

    GeoTIFF はメタデータを書き直すたびにタグ全体をファイル末尾へ追記する
    ので、埋め込みが小さいほど意味づけの繰り返しでファイルが膨らみにくい。
    8 桁あれば最尤分類の結果には影響しない。
    """
    arr = np.asarray(values, dtype=np.float64)
    return json.loads(np.array2string(
        arr, separator=",", threshold=arr.size + 1,
        formatter={"float_kind": lambda v: "%.8g" % v}).replace("nan", "0"))


def _signature_text(sig: Signature) -> str:
    """ラベルと色を除いたシグネチャ本体の JSON（クラス定義が同じなら不変）。"""
    d = json.loads(sig.to_json())
    d["labels"] = [""] * sig.n_classes
    d["colors"] = [""] * sig.n_classes
    d["means"] = _rounded(sig.means)
    d["covs"] = _rounded(sig.covs)
    return _compact(d)


def embed_labels(dataset, sig: Signature,
                 labels: Optional[List[str]] = None,
                 colors: Optional[List[str]] = None) -> bool:
    """意味づけを TIF 本体のメタデータに書く。何か書いたら True。

    内容が変わっていない項目は書かない。GeoTIFF は更新のたびにタグの
    領域をファイル末尾へ追記するので、無駄な書き込みでファイルが
    膨らまないようにする。
    """
    n = sig.n_classes
    labels = _padded(labels if labels is not None else sig.labels, n)
    colors = _padded(colors if colors is not None else sig.colors, n)
    changed = False

    band = dataset.GetRasterBand(1)
    current = band.GetMetadata() or {}
    wanted = {k: v for k, v in current.items() if not k.startswith("CLASS_")}
    wanted[CLASS_KEY % 0] = UNCLASSIFIED
    for i in range(n):
        wanted[CLASS_KEY % (i + 1)] = labels[i] or "class %d" % (i + 1)
    if wanted != current:
        band.SetMetadata(wanted)
        changed = True

    text = _compact({"_format": LABELS_FORMAT, "labels": labels,
                     "colors": colors})
    if dataset.GetMetadataItem(KEY_LABELS, DOMAIN) != text:
        dataset.SetMetadataItem(KEY_LABELS, text, DOMAIN)
        changed = True

    text = _signature_text(sig)
    if dataset.GetMetadataItem(KEY_SIGNATURE, DOMAIN) != text:
        dataset.SetMetadataItem(KEY_SIGNATURE, text, DOMAIN)
        changed = True
    return changed


# ---------------------------------------------------------------------------
# 読み出し
# ---------------------------------------------------------------------------
def _open(path_or_ds):
    if isinstance(path_or_ds, str):
        return gdal.Open(raster_file(path_or_ds), gdal.GA_ReadOnly)
    return path_or_ds


def read_embedded_labels(path_or_ds) -> Optional[Tuple[List[str], List[str]]]:
    """TIF に埋め込んだ (labels, colors)。無ければ None。"""
    try:
        ds = _open(path_or_ds)
        text = ds.GetMetadataItem(KEY_LABELS, DOMAIN) if ds else None
        if not text:
            return None
        data = json.loads(text)
        labels = [str(v or "") for v in data.get("labels") or []]
        colors = [str(v or "") for v in data.get("colors") or []]
        if not labels:
            return None
        return labels, _padded(colors, len(labels))
    except (RuntimeError, ValueError, TypeError, AttributeError):
        return None


def read_embedded_signature(path_or_ds) -> Optional[Signature]:
    """TIF に埋め込んだシグネチャ（意味づけ込み）。無ければ None。"""
    try:
        ds = _open(path_or_ds)
        text = ds.GetMetadataItem(KEY_SIGNATURE, DOMAIN) if ds else None
        if not text:
            return None
        sig = Signature.from_json(text)
    except (RuntimeError, ValueError, TypeError, AttributeError, KeyError):
        return None
    found = read_embedded_labels(ds)
    if found:
        labels, colors = found
        sig.labels = _padded(labels, sig.n_classes)
        sig.colors = _padded(colors, sig.n_classes)
    return sig


def read_sidecar_signature(raster_path: str) -> Optional[Signature]:
    path = signature_path_for(raster_path)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return Signature.from_json(fh.read())
    except (OSError, ValueError, TypeError, KeyError):
        return None


def load_signature(raster_path: str) -> Tuple[Optional[Signature], str]:
    """分類ラスタの意味づけ込みシグネチャを探す。

    TIF 本体の埋め込みを優先し（ファイルと食い違うことがない）、
    無ければ隣の _signature.json を使う。戻り値は (シグネチャ, 出どころ)。
    出どころは "tif" / "json" / ""。
    """
    sig = read_embedded_signature(raster_path)
    if sig is not None:
        return sig, "tif"
    sig = read_sidecar_signature(raster_path)
    if sig is not None:
        return sig, "json"
    return None, ""


# ---------------------------------------------------------------------------
# 分類ラスタへの反映（プロセシング・一括処理用）
# ---------------------------------------------------------------------------
def class_counts(raster_path: str, n_classes: int) -> np.ndarray:
    """分類ラスタの値 1..n_classes の画素数。"""
    ds = gdal.Open(raster_file(raster_path), gdal.GA_ReadOnly)
    hist = ds.GetRasterBand(1).GetHistogram(-0.5, 255.5, 256, False, False)
    return np.asarray(hist[1:n_classes + 1], dtype=np.int64)


def max_class_value(raster_path: str) -> int:
    ds = gdal.Open(raster_file(raster_path), gdal.GA_ReadOnly)
    hist = ds.GetRasterBand(1).GetHistogram(-0.5, 255.5, 256, False, False)
    used = [v for v, c in enumerate(hist) if c]
    return max(used) if used else 0


def same_classes(a: Signature, b: Signature) -> bool:
    """クラス定義（クラス数・バンド数・平均）が同じか。"""
    if a.n_classes != b.n_classes or a.n_bands != b.n_bands:
        return False
    return bool(np.allclose(a.means, b.means, rtol=1e-6, atol=1e-6))


def apply_labels_to_raster(raster_path: str,
                           source: Optional[Signature] = None,
                           allow_mismatch: bool = False,
                           write_json: bool = True) -> Tuple[Signature, str]:
    """意味づけを分類ラスタ（TIF 本体・カラーマップ・RAT）に書き込む。

    source を省略すると、ラスタ自身の意味づけ（TIF 埋め込み、無ければ
    隣の _signature.json）で書き直す（.aux.xml が壊れた・消えた場合の修復）。

    source を渡すと、その意味づけ（ラベル・色）をこのラスタへ当てる。
    同じシグネチャで分類したラスタ（フォルダ一括の共通シグネチャや、
    経年比較で既存シグネチャを当てた別年次の結果）への一括反映に使う。
    クラス定義が違うラスタには、allow_mismatch=True のときだけ反映する。

    戻り値は (書き込んだシグネチャ, 説明)。問題があれば ValueError。
    """
    from .rinkyo_raster import write_rat

    path = raster_file(raster_path)
    if not os.path.isfile(path):
        raise ValueError("ファイルがありません: %s" % path)
    own, origin = load_signature(path)

    if source is None:
        if own is None:
            raise ValueError("意味づけの情報（TIF への埋め込み・%s）が"
                             "見つかりません" % os.path.basename(
                                 signature_path_for(path)))
        target = own
        note = "自身の意味づけで書き直し（%s）" % (
            "TIF" if origin == "tif" else "_signature.json")
    else:
        top = max_class_value(path)
        if top > source.n_classes:
            raise ValueError("ラスタの値（最大 %d）がシグネチャのクラス数"
                             "（%d）を超えています" % (top, source.n_classes))
        if own is not None and not same_classes(own, source):
            if not allow_mismatch:
                raise ValueError("クラス定義がシグネチャと違います"
                                 "（別のシグネチャで分類したラスタです）")
        if own is not None and own.n_classes == source.n_classes:
            target = copy.deepcopy(own)
        else:
            target = copy.deepcopy(source)
            target.counts = class_counts(path, source.n_classes)
        target.labels = _padded(source.labels, target.n_classes)
        target.colors = _padded(source.colors, target.n_classes)
        note = "指定したシグネチャの意味づけを反映"

    ds = gdal.Open(path, gdal.GA_Update)
    try:
        write_rat(ds, target)
        ds.FlushCache()
    finally:
        ds = None

    if write_json:
        with open(signature_path_for(path), "w", encoding="utf-8") as fh:
            fh.write(target.to_json())
    return target, note
