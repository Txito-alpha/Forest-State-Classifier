# -*- coding: utf-8 -*-
"""RinkyoClassifier - 衛星画像による林況把握（教師なし分類）

License: GPL v2
"""


def _ensure_std_streams():
    """sys.stdout / sys.stderr が None なら差し替える。

    QGIS(Windows) はコンソールを持たないため、この2つが None になる。
    numpy 2.x はバージョン不整合を検出すると sys.stderr.write() で
    説明を書いてから ImportError を投げる作りなので、None のままだと
    「AttributeError: 'NoneType' object has no attribute 'write'」だけが
    表示され、本当の原因が隠れてしまう。
    """
    import io
    import sys

    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, io.StringIO())


def _check_numpy():
    """numpy と GDAL バインディングの組み合わせを確認する。

    問題があれば利用者向けの説明文を返し、無ければ None を返す。
    """
    import os
    import sys

    try:
        import numpy
    except ImportError as exc:
        return "numpy を読み込めません: %s" % exc

    # osgeo._gdal_array は QGIS 同梱の numpy に対してビルドされた
    # C拡張なので、numpy が差し替わっているとここで落ちる。
    try:
        from osgeo import gdal_array  # noqa: F401
    except ImportError as exc:
        qgis_site = os.path.join(sys.prefix, "Lib", "site-packages")
        loaded = os.path.dirname(os.path.dirname(numpy.__file__))
        hint = ""
        if os.path.normcase(loaded) != os.path.normcase(qgis_site):
            hint = ("\n\n読み込まれている numpy は QGIS 同梱のものではありません。\n"
                    "  使用中: %s\n  QGIS側: %s\n"
                    "先に読まれている方を無効化（フォルダ名を変更）してから "
                    "QGIS を再起動してください。" % (loaded, qgis_site))
        return ("numpy %s と GDAL の Python バインディングが噛み合っていません。\n%s%s"
                % (numpy.__version__, exc, hint))
    return None


def _install_translator():
    """QGIS のロケール設定に合わせた .qm があれば読み込む。

    i18n/RinkyoClassifier_<locale>.qm が見つかった場合のみ有効化する。
    見つからなければソース文字列（日本語）のまま動作する。
    """
    import os

    from qgis.core import QgsApplication
    from qgis.PyQt.QtCore import QCoreApplication, QLocale, QSettings, QTranslator

    locale = QSettings().value("locale/userLocale", QLocale().name(), type=str)
    plugin_dir = os.path.dirname(__file__)
    qm_path = os.path.join(plugin_dir, "i18n",
                           "RinkyoClassifier_%s.qm" % locale[:2])
    if os.path.exists(qm_path):
        translator = QTranslator()
        translator.load(qm_path)
        QCoreApplication.installTranslator(translator)
        # ガベージコレクトされないよう QgsApplication に持たせておく
        QgsApplication.instance()._rinkyo_translator = translator


def classFactory(iface):  # noqa: N802  QGIS の規約
    _ensure_std_streams()
    _install_translator()

    problem = _check_numpy()
    if problem:
        from qgis.core import Qgis, QgsMessageLog

        QgsMessageLog.logMessage(problem, "RinkyoClassifier", Qgis.Critical)
        iface.messageBar().pushMessage(
            "林況分類",
            "numpy の環境に問題があります。ログメッセージパネルを確認してください。",
            level=Qgis.Critical, duration=0)

    from .plugin import RinkyoClassifierPlugin
    return RinkyoClassifierPlugin(iface)
