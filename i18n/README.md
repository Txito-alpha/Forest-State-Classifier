# Translations / 翻訳

Source strings in this plugin are Japanese, wrapped in `self.tr()` /
`QCoreApplication.translate()` so that Qt Linguist can extract them.

## Adding a new language

1. Install `pyqt5-dev-tools` (provides `pylupdate5`) and Qt Linguist (`linguist`).
2. Add your target language's `.ts` file to `TRANSLATIONS` in
   `rinkyo_classifier.pro` (e.g. `TRANSLATIONS = RinkyoClassifier_en.qm`).
3. From this `i18n/` directory, run:
   ```
   pylupdate5 rinkyo_classifier.pro
   linguist RinkyoClassifier_en.ts
   ```
4. Translate the strings in Linguist, save, then compile:
   ```
   lrelease RinkyoClassifier_en.ts
   ```
5. The resulting `RinkyoClassifier_en.qm` is picked up automatically at
   startup based on the QGIS UI locale (`locale/userLocale`).

## 新しい言語を追加する

1. `pyqt5-dev-tools`（`pylupdate5` を含む）と Qt Linguist をインストールします。
2. `rinkyo_classifier.pro` の `TRANSLATIONS` に対象言語の `.ts` を追加します。
3. `i18n/` ディレクトリで `pylupdate5 rinkyo_classifier.pro` を実行し、
   `linguist` で翻訳します。
4. `lrelease` で `.qm` にコンパイルすると、QGIS の UI ロケールに応じて
   自動的に読み込まれます。
