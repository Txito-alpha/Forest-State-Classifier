# Forest State Classifier

QGIS plugin for unsupervised classification of Sentinel-2 imagery for
forest condition mapping, without requiring GRASS GIS.

Sentinel-2 衛星画像の教師なし分類による林況把握を行う QGIS プラグインです。
GRASS GIS のインストールを必要としません。

Implements the workflow described in *"Forest condition mapping using
satellite imagery"* (Hokkaido Research Organization, Forestry Research
Institute, 2025) as a single plugin. Clustering and maximum likelihood
classification are reimplemented in numpy.

## Features / 主な機能

- Unsupervised clustering + maximum likelihood classification (numpy-based, no GRASS)
- Interactive class labelling with a Red-NIR scatter plot
- Raster attribute table output
- Signature reuse across years for multi-year comparison
- Per-subcompartment (小班) aggregation
- Folder input with recursive search: mosaic into one VRT, or classify file by file
  with a shared signature / フォルダ入力（再帰検索）: VRT モザイク、またはファイルごとの分類
- Choose what to do when an output of the same name already exists: skip, overwrite
  or keep both / 同名の出力があるとき: スキップ・上書き・別名保存を選択
- Labels are written into the classified GeoTIFF itself (palette + embedded class names
  and signature), so the TIF alone carries its labelling. A Processing tool re-applies
  labels to many rasters at once or repairs a stale `.aux.xml` /
  意味づけ（クラス名・色）を分類 TIF 本体に埋め込み、TIF 単体で持ち運べる。
  プロセシング「意味づけを分類ラスタ（TIF）へ反映」で一括反映・修復が可能

### Where the labelling is stored / 意味づけの保存先

| What | Where |
|---|---|
| Colors / 色 | TIFF color map (palette) inside the TIF |
| Class names / クラス名 | Band 1 metadata `CLASS_<value>` inside the TIF (GDAL_METADATA tag) |
| Labels + colors (JSON) | Domain `FOREST_STATE_CLASSIFIER`, item `LABELS` inside the TIF |
| Signature / シグネチャ | Domain `FOREST_STATE_CLASSIFIER`, item `SIGNATURE` inside the TIF, and `<name>_signature.json` |
| Raster attribute table (QGIS legend) | `<name>_class.tif.aux.xml` (GeoTIFF cannot hold a RAT internally) |

## Requirements

- QGIS >= 3.28

## Tests

```
python3 tests/test_core.py    # numpy only
python3 tests/test_batch.py   # numpy + GDAL (folder input)
python3 tests/test_tif_labels.py   # numpy + GDAL (labels embedded in the TIF)
QT_QPA_PLATFORM=offscreen python3 tests/test_label_dialog.py   # PyQt5 (labelling dialog)
```

## Internationalization / 多言語対応

UI strings are wrapped in `self.tr()` for translation. See
[`i18n/README.md`](i18n/README.md) for how to add a new language.

## License

GPL v2 — see [LICENSE](LICENSE).
