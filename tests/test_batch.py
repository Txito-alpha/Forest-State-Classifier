# -*- coding: utf-8 -*-
"""フォルダ入力（再帰検索・モザイク・ファイルごと処理）の動作確認。

QGIS の外で `python3 tests/test_batch.py` として実行できる。
numpy と GDAL の Python バインディングが必要。
"""

import os
import shutil
import sys
import tempfile

import numpy as np
from osgeo import gdal, osr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core import batch, pipeline  # noqa: E402
from core import rinkyo_core as rc  # noqa: E402

gdal.UseExceptions()

BAND_NAMES = ["Blue", "Green", "Red", "NIR", "NDVI"]
CLASSES = [
    [350, 600, 400, 2500],
    [400, 750, 450, 6000],
    [600, 500, 400, 250],
    [1200, 1300, 1500, 2000],
]


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def write_tile(path, x0, y0, size=64, seed=0, bands=5, epsg=6680):
    """4 種類の地物が縞状に並んだ 5 バンド画像を作る。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rng = np.random.default_rng(seed)
    data = np.zeros((5, size, size), dtype=np.float32)
    for r in range(size):
        mu = CLASSES[(r // 16) % len(CLASSES)]
        vals = rng.normal(mu, 60, size=(size, 4))
        data[:4, r, :] = vals.T
    data[4] = (data[3] - data[2]) / (data[3] + data[2])
    data = data[:bands]
    ds = gdal.GetDriverByName("GTiff").Create(
        path, size, size, bands, gdal.GDT_Float32)
    ds.SetGeoTransform((x0, 10.0, 0, y0, 0, -10.0))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    ds.SetProjection(srs.ExportToWkt())
    for i in range(bands):
        b = ds.GetRasterBand(i + 1)
        b.WriteArray(data[i])
        b.SetDescription(BAND_NAMES[i])
    ds = None


def make_tree(root):
    write_tile(os.path.join(root, "a", "tile_1.tif"), 0, 640, seed=1)
    write_tile(os.path.join(root, "a", "tile_2.TIF"), 640, 640, seed=2)
    write_tile(os.path.join(root, "b", "c", "tile_3.tif"), 0, 0, seed=3)
    write_tile(os.path.join(root, "b", "tile_1.tif"), 640, 0, seed=4)
    # 除外されるべきもの
    write_tile(os.path.join(root, "a", "old_class.tif"), 0, 0, seed=5)
    open(os.path.join(root, "a", "note.txt"), "w").close()
    # バンド数不足
    write_tile(os.path.join(root, "bad", "three_band.tif"), 0, 0,
               seed=6, bands=3)


def test_find(root):
    found = batch.find_rasters(root, batch.parse_patterns("tif"), True)
    rel = [os.path.relpath(p, root).replace(os.sep, "/") for p in found]
    print("再帰検索:", rel)
    require(rel == ["a/tile_1.tif", "a/tile_2.TIF", "b/tile_1.tif",
                    "b/c/tile_3.tif", "bad/three_band.tif"],
            "再帰検索の結果が想定と違います: %s" % rel)
    flat = batch.find_rasters(os.path.join(root, "a"), None, False)
    require(len(flat) == 2, "非再帰検索の件数が違います")
    ex = batch.find_rasters(root, None, True,
                            exclude_dirs=[os.path.join(root, "b")])
    require(all("%sb%s" % (os.sep, os.sep) not in p for p in ex),
            "除外フォルダが効いていません")
    require(batch.parse_patterns("*.tif, .vrt jp2")
            == ["*.tif", "*.vrt", "*.jp2"], "パターン解釈が違います")
    return found


def test_plan(root, found):
    jobs = batch.plan_jobs(found, root, "/out", prefix="r")
    names = [(os.path.relpath(j.out_dir, "/out"), j.basename) for j in jobs]
    print("出力割当:", names)
    require(len({(d, n) for d, n in names}) == len(names),
            "出力先が重複しています")
    flat = batch.plan_jobs(found, root, "/out", keep_tree=False)
    require(len({j.basename for j in flat}) == len(flat),
            "フォルダ構成なしで名前が重複しています")


def run(opts):
    out = pipeline.Runner(opts, log=lambda m: print("   ", m)).run()
    for r in out.results:
        require(os.path.isfile(r.result_path), "出力がありません")
        require(os.path.isfile(r.signature_path), "シグネチャがありません")
        ds = gdal.Open(r.result_path)
        arr = ds.GetRasterBand(1).ReadAsArray()
        require(arr.max() > 0, "分類結果が空です")
        require(ds.GetRasterBand(1).GetDefaultRAT() is not None,
                "RAT がありません")
    return out


def base_opts(**kw):
    o = pipeline.Options(n_classes=8, max_samples=5000, min_class_size=5,
                         max_iterations=20, bands=[1, 2, 3, 4, 5],
                         existing=pipeline.EXISTING_OVERWRITE)
    for k, v in kw.items():
        setattr(o, k, v)
    return o


def main():
    tmp = tempfile.mkdtemp(prefix="fsc_")
    try:
        root = os.path.join(tmp, "入力 フォルダ")
        make_tree(root)
        found = test_find(root)
        test_plan(root, found)

        print("単一ファイル:")
        out = run(base_opts(mode="single", inputs=[found[0]],
                            out_dir=os.path.join(tmp, "o1"), basename="one"))
        require(len(out.results) == 1, "単一の結果数が違います")

        print("モザイク:")
        out = run(base_opts(mode="mosaic", inputs=found, input_root=root,
                            out_dir=os.path.join(tmp, "o2"), basename="m"))
        require(len(out.results) == 1, "モザイクの結果数が違います")
        require(len(out.skipped) == 1, "バンド不足が除外されていません")
        ds = gdal.Open(out.results[0].result_path)
        require((ds.RasterXSize, ds.RasterYSize) == (128, 128),
                "モザイクの大きさが違います")
        require(os.path.basename(out.results[0].stack_path) == "m_mosaic.vrt",
                "VRT 名が違います")

        print("ファイルごと（共通シグネチャ）:")
        o3 = os.path.join(tmp, "o3")
        out = run(base_opts(mode="each", inputs=found, input_root=root,
                            out_dir=o3, basename="r"))
        require(len(out.results) == 4, "個別処理の結果数が違います")
        sigs = [rc.Signature.from_json(open(r.signature_path,
                                            encoding="utf-8").read())
                for r in out.results]
        require(all(np.array_equal(s.means, sigs[0].means) for s in sigs),
                "共通シグネチャになっていません")
        require(os.path.isfile(os.path.join(o3, "b", "c",
                                            "r_tile_3_class.tif")),
                "フォルダ構成が再現されていません")

        print("ファイルごと（個別シグネチャ・範囲指定）:")
        out = run(base_opts(mode="each", inputs=found, input_root=root,
                            out_dir=os.path.join(tmp, "o4"), basename="",
                            shared_signature=False,
                            clip_extent=(0, 1, 1280, 640)))
        require(len(out.results) == 2, "範囲外のファイルが除外されていません")
        require(out.shared_signature is None, "個別シグネチャのはずです")

        print("ファイルごと（既存シグネチャ適用）:")
        out = run(base_opts(mode="each", inputs=found, input_root=root,
                            out_dir=os.path.join(tmp, "o5"), basename="y",
                            shared_signature=False, signature=sigs[0]))
        require(len(out.results) == 4, "既存シグネチャ適用の結果数が違います")

        print("同名の出力があるとき:")
        o6 = os.path.join(tmp, "o6")
        first = run(base_opts(mode="each", inputs=found, input_root=root,
                              out_dir=o6, basename="r"))
        stamps = {r.result_path: os.path.getmtime(r.result_path)
                  for r in first.results}

        out = run(base_opts(mode="each", inputs=found, input_root=root,
                            out_dir=o6, basename="r",
                            existing=pipeline.EXISTING_SKIP))
        require(not out.results, "スキップされていません: %s" % out.results)
        require(len(out.existing_skipped) == 4,
                "既存スキップの件数が違います: %s" % out.existing_skipped)
        require(all(os.path.getmtime(p) == t for p, t in stamps.items()),
                "スキップしたのに出力が書き換わっています")

        out = run(base_opts(mode="each", inputs=found, input_root=root,
                            out_dir=o6, basename="r",
                            existing=pipeline.EXISTING_RENAME))
        require(len(out.results) == 4, "別名保存の件数が違います")
        require(all(r.basename.endswith("_2") for r in out.results),
                "別名になっていません: %s" % [r.basename for r in out.results])
        require(all(os.path.getmtime(p) == t for p, t in stamps.items()),
                "別名保存なのに既存の出力が書き換わっています")

        out = run(base_opts(mode="each", inputs=found, input_root=root,
                            out_dir=o6, basename="r",
                            existing=pipeline.EXISTING_RENAME))
        require(all(r.basename.endswith("_3") for r in out.results),
                "連番が進んでいません: %s" % [r.basename for r in out.results])

        out = run(base_opts(mode="each", inputs=found, input_root=root,
                            out_dir=o6, basename="r",
                            existing=pipeline.EXISTING_OVERWRITE))
        require(len(out.results) == 4, "上書きの件数が違います")
        require(any(os.path.getmtime(p) > t for p, t in stamps.items()),
                "上書きされていません")

        # 1件だけ消しておくと、その1件だけが処理される
        os.remove(list(stamps)[0])
        out = run(base_opts(mode="each", inputs=found, input_root=root,
                            out_dir=o6, basename="r",
                            existing=pipeline.EXISTING_SKIP))
        require(len(out.results) == 1 and len(out.existing_skipped) == 3,
                "続きからの流し直しになっていません")

        print("モザイクでも効くこと:")
        o7 = os.path.join(tmp, "o7")
        run(base_opts(mode="mosaic", inputs=found, input_root=root,
                      out_dir=o7, basename="m"))
        out = run(base_opts(mode="mosaic", inputs=found, input_root=root,
                            out_dir=o7, basename="m",
                            existing=pipeline.EXISTING_SKIP))
        require(not out.results, "モザイクでスキップされていません")

        print("出力フォルダを入力の中に置いた場合:")
        inner = os.path.join(root, "out")
        run(base_opts(mode="each", inputs=found[:1], input_root=root,
                      out_dir=inner, basename="z"))
        again = batch.find_rasters(root, None, True, exclude_dirs=[inner])
        require(len(again) == len(found), "出力を入力として拾っています")
        again = batch.find_rasters(root, None, True)
        require(len(again) == len(found), "出力ファイル名の除外が効いていません")

        print("\nすべて通りました。")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
