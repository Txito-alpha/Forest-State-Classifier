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

## Requirements

- QGIS >= 3.28

## Internationalization / 多言語対応

UI strings are wrapped in `self.tr()` for translation. See
[`i18n/README.md`](i18n/README.md) for how to add a new language.

## License

GPL v2 — see [LICENSE](LICENSE).
