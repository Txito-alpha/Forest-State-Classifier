# -*- coding: utf-8 -*-
"""意味づけの TIF 本体への埋め込み（0.3.0）の動作確認。

QGIS の外で `python3 tests/test_tif_labels.py` として実行できる。
numpy と GDAL の Python バインディングが必要。
"""

import copy
import os
import shutil
import sys
import tempfile

import numpy as np
from osgeo import gdal, ogr, osr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import pipeline, tif_labels  # noqa: E402
from core import rinkyo_raster as rr  # noqa: E402
from core.zonal import zonal_class_counts  # noqa: E402

gdal.UseExceptions()

# 自動で付く候補名と区別できるよう、テスト専用の名前にする
LABELS = ["針葉樹テスト", "広葉樹テスト", "混交林テスト", "草地テスト"]


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def write_tile(path, seed):
    """4 種類の地物が縞状に並んだ 4 バンド画像。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rng = np.random.default_rng(seed)
    means = [[350, 600, 400, 2500], [400, 750, 450, 6000],
             [600, 500, 400, 250], [1200, 1300, 1500, 2000]]
    data = np.zeros((4, 64, 64), dtype=np.float32)
    for r in range(64):
        data[:, r, :] = rng.normal(means[(r // 16) % 4], 40, (64, 4)).T
    ds = gdal.GetDriverByName("GTiff").Create(path, 64, 64, 4,
                                              gdal.GDT_Float32)
    ds.SetGeoTransform((0, 10.0, 0, 640, 0, -10.0))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(6680)
    ds.SetProjection(srs.ExportToWkt())
    for i, name in enumerate(["Blue", "Green", "Red", "NIR"]):
        ds.GetRasterBand(i + 1).WriteArray(data[i])
        ds.GetRasterBand(i + 1).SetDescription(name)
    ds = None


def classify(src, out_dir, signature=None):
    o = pipeline.Options(mode="single", inputs=[src], out_dir=out_dir,
                         basename="r", n_classes=4, max_samples=4000,
                         min_class_size=5, max_iterations=20,
                         signature=signature, write_likelihood=False,
                         existing=pipeline.EXISTING_OVERWRITE)
    return pipeline.Runner(o).run().results[0]


def isolate(path, tmp, name):
    """TIF 単体だけを別フォルダにコピーする（.aux.xml と json は持たない）。"""
    d = os.path.join(tmp, name)
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, os.path.basename(path))
    shutil.copy(path, dst)
    return dst


def label(sig):
    sig = copy.deepcopy(sig)
    sig.labels = list(LABELS)
    sig.colors = ["#1b5e20", "#8bc34a", "#4caf50", "#ffeb3b"]
    return sig


def write_like_apply(path, sig):
    """意味づけ画面の「適用」と同じ書き込み。"""
    ds = gdal.Open(path, gdal.GA_Update)
    rr.write_rat(ds, sig)
    ds = None
    with open(tif_labels.signature_path_for(path), "w",
              encoding="utf-8") as fh:
        fh.write(sig.to_json())


def make_polygon(path):
    drv = ogr.GetDriverByName("GPKG")
    ds = drv.CreateDataSource(path)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(6680)
    lyr = ds.CreateLayer("p", srs, ogr.wkbPolygon)
    lyr.CreateField(ogr.FieldDefn("小班名", ogr.OFTString))
    f = ogr.Feature(lyr.GetLayerDefn())
    f.SetField("小班名", "1")
    f.SetGeometry(ogr.CreateGeometryFromWkt(
        "POLYGON((0 0,640 0,640 640,0 640,0 0))"))
    lyr.CreateFeature(f)
    ds = None


def main():
    tmp = tempfile.mkdtemp(prefix="fsc_tif_")
    try:
        src = os.path.join(tmp, "入力 2024", "s2.tif")
        write_tile(src, 1)
        res = classify(src, os.path.join(tmp, "out"))
        path = res.result_path

        print("分類直後から TIF にシグネチャが埋め込まれていること:")
        emb = tif_labels.read_embedded_signature(isolate(path, tmp, "a"))
        require(emb is not None, "分類直後の TIF にシグネチャがありません")
        require(np.allclose(emb.means, res.signature.means),
                "埋め込んだシグネチャが違います")

        print("適用後、TIF 単体（.aux.xml・json なし）で意味づけが読めること:")
        sig = label(res.signature)
        write_like_apply(path, sig)
        alone = isolate(path, tmp, "b")
        require(not os.path.exists(alone + ".aux.xml"), "aux が付いてきています")
        ds = gdal.Open(alone)
        band = ds.GetRasterBand(1)
        md = band.GetMetadata()
        require([md.get("CLASS_%d" % i) for i in range(1, 5)] == LABELS,
                "バンドメタデータのクラス名が違います: %s" % md)
        ct = band.GetColorTable()
        require(ct is not None and ct.GetColorEntry(1)[:3] == (0x1b, 0x5e, 0x20),
                "カラーマップが TIF に入っていません")
        ds = None
        got, origin = tif_labels.load_signature(alone)
        require(origin == "tif" and got.labels == LABELS
                and got.colors == sig.colors,
                "TIF 単体から意味づけを復元できません")
        require(np.allclose(got.covs, sig.covs), "共分散が復元できません")

        print("ピクセル値が変わっていないこと:")
        a = gdal.Open(path).ReadAsArray()
        write_like_apply(path, label(sig))
        require(np.array_equal(a, gdal.Open(path).ReadAsArray()),
                "意味づけでピクセル値が変わりました")

        print("同じ内容の適用ではファイルが大きくならないこと:")
        size = os.path.getsize(path)
        for _ in range(5):
            write_like_apply(path, sig)
        require(os.path.getsize(path) == size,
                "同じ内容の適用でファイルが大きくなりました: %d -> %d"
                % (size, os.path.getsize(path)))
        grown = size
        for i in range(5):
            s2 = copy.deepcopy(sig)
            s2.labels[0] = "針葉樹テスト%d" % i
            write_like_apply(path, s2)
        grown = os.path.getsize(path) - grown
        # ラベルを変えるとメタデータのタグ 1 つ分だけ追記される（それ以上は増えない）
        ds = gdal.Open(path)
        tag = sum(len(v.encode("utf-8")) for v in (
            ds.GetMetadataItem(k, tif_labels.DOMAIN)
            for k in (tif_labels.KEY_SIGNATURE, tif_labels.KEY_LABELS)))
        ds = None
        print("    ラベル変更 1 回あたり +%d バイト（埋め込み %d バイト）"
              % (grown // 5, tag))
        require(grown // 5 < 2 * tag + 4096,
                "ラベル変更でファイルが膨らみすぎます: +%d" % grown)
        write_like_apply(path, sig)

        print("小班集計が .aux.xml・json なしでもラベルを使うこと:")
        poly = os.path.join(tmp, "poly.gpkg")
        make_polygon(poly)
        result = zonal_class_counts(alone, poly, key_fields=("小班名",))
        require(result["labels"][1:5] == LABELS,
                "小班集計のラベルが違います: %s" % result["labels"][:5])

        print(".aux.xml が古い内容に戻っても、反映ツールで修復できること:")
        aux = path + ".aux.xml"
        os.remove(aux)
        _sig, note = tif_labels.apply_labels_to_raster(path)
        require("TIF" in note, "埋め込みから修復していません: %s" % note)
        ds = gdal.Open(path)
        rat = ds.GetRasterBand(1).GetDefaultRAT()
        require(rat is not None and rat.GetValueAsString(1, 2) == LABELS[0],
                "RAT が修復されていません")

        print("0.3.0 より前の TIF（埋め込みなし・json あり）も反映できること:")
        old = isolate(path, tmp, "old")
        ds = gdal.Open(old, gdal.GA_Update)
        ds.SetMetadata({}, tif_labels.DOMAIN)
        ds.GetRasterBand(1).SetMetadata({})
        ds = None
        require(tif_labels.read_embedded_signature(old) is None,
                "埋め込みを消せていません")
        with open(tif_labels.signature_path_for(old), "w",
                  encoding="utf-8") as fh:
            fh.write(sig.to_json())
        _sig, note = tif_labels.apply_labels_to_raster(old)
        require("json" in note, "json から反映していません: %s" % note)
        require(tif_labels.read_embedded_signature(old).labels == LABELS,
                "json の意味づけが TIF に埋め込まれていません")

        print("意味づけ情報が何もない TIF はエラーになること:")
        bare = isolate(old, tmp, "bare")
        ds = gdal.Open(bare, gdal.GA_Update)
        ds.SetMetadata({}, tif_labels.DOMAIN)
        ds = None
        try:
            tif_labels.apply_labels_to_raster(bare)
            raise AssertionError("エラーになりません")
        except ValueError as exc:
            print("   ", exc)

        print("同じシグネチャで分類した別年次へ、意味づけを一括反映できること:")
        src2 = os.path.join(tmp, "入力 2025", "s2.tif")
        write_tile(src2, 2)
        res2 = classify(src2, os.path.join(tmp, "out2025"),
                        signature=copy.deepcopy(res.signature))
        require(tif_labels.load_signature(res2.result_path)[0].labels
                != LABELS, "別年次にはまだ意味づけが無いはずです")
        target, _note = tif_labels.apply_labels_to_raster(
            res2.result_path, sig)
        require(target.labels == LABELS, "別年次に反映されていません")
        got = tif_labels.read_embedded_signature(res2.result_path)
        require(got.labels == LABELS, "別年次の TIF に埋め込まれていません")
        own_counts = np.bincount(
            gdal.Open(res2.result_path).ReadAsArray().ravel(),
            minlength=5)[1:5]
        require(np.array_equal(got.counts, own_counts)
                or np.array_equal(got.counts, res2.signature.counts),
                "画素数が別年次の値になっていません")

        print("クラス定義が違うラスタには既定では反映しないこと:")
        other = classify(src2, os.path.join(tmp, "other"))
        if tif_labels.same_classes(other.signature, sig):
            print("    （偶然同じクラス定義になったため省略）")
        else:
            try:
                tif_labels.apply_labels_to_raster(other.result_path, sig)
                raise AssertionError("クラス定義が違うのに反映されました")
            except ValueError as exc:
                print("   ", exc)
            tif_labels.apply_labels_to_raster(other.result_path, sig,
                                              allow_mismatch=True)
            require(tif_labels.read_embedded_signature(
                other.result_path).labels == LABELS,
                "allow_mismatch で反映されません")

        print("\nすべて通りました。")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
