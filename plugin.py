# -*- coding: utf-8 -*-
"""
plugin - RinkyoClassifier のエントリポイント

ツールバーは小班JUMP3 などと共有する NationalForestToolbar に相乗りする。

License: GPL v2
"""

from __future__ import annotations

import os
from typing import Optional

from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QMessageBox, QToolBar
from qgis.core import (
    Qgis, QgsApplication, QgsMessageLog, QgsProject, QgsRasterLayer,
)

from .core.rinkyo_core import Signature
from .core.rinkyo_raster import write_rat
from .gui.label_dialog import LabelDialog, apply_signature_style
from .gui.main_dialog import MainDialog
from .task import ClassifyTask

LOG_TAG = "RinkyoClassifier"
TOOLBAR_NAME = "NationalForestToolbar"
TOOLBAR_TITLE = "国有林ツール"
MENU_TITLE = "&林況分類"


class RinkyoClassifierPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.actions = []
        self.toolbar = None
        self.provider = None
        self._task: Optional[ClassifyTask] = None

    # -- QGIS インタフェース ----------------------------------------------
    def initGui(self):  # noqa: N802  QGIS の規約
        icon_path = os.path.join(os.path.dirname(__file__), "icon.png")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()

        self.toolbar = self._shared_toolbar()

        run_action = QAction(icon, self.tr("教師なし分類を実行"),
                             self.iface.mainWindow())
        run_action.triggered.connect(self.run)
        relabel_action = QAction(self.tr("分類結果の意味づけ…"),
                                 self.iface.mainWindow())
        relabel_action.triggered.connect(self.relabel)
        zonal_action = QAction(self.tr("小班別に集計…"),
                               self.iface.mainWindow())
        zonal_action.triggered.connect(self.zonal)

        for action in (run_action, relabel_action, zonal_action):
            self.iface.addPluginToRasterMenu(self.tr(MENU_TITLE), action)
            self.actions.append(action)
        self.toolbar.addAction(run_action)

        self._register_provider()

    def unload(self):
        for action in self.actions:
            self.iface.removePluginRasterMenu(self.tr(MENU_TITLE), action)
            if self.toolbar:
                self.toolbar.removeAction(action)
        self.actions = []
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None
        # ツールバーは他プラグインと共有しているので、空のときだけ片付ける
        if self.toolbar is not None and not self.toolbar.actions():
            self.iface.mainWindow().removeToolBar(self.toolbar)
            self.toolbar.deleteLater()
        self.toolbar = None

    def _shared_toolbar(self):
        """既存の国有林ツールバーがあれば使い回す。

        addToolBar を先に呼ぶと空のツールバーが増えてしまうので、
        必ず findChildren で探してから作る。
        """
        for toolbar in self.iface.mainWindow().findChildren(QToolBar):
            if toolbar.objectName() == TOOLBAR_NAME:
                return toolbar
        toolbar = self.iface.addToolBar(self.tr(TOOLBAR_TITLE))
        toolbar.setObjectName(TOOLBAR_NAME)
        return toolbar

    def _register_provider(self):
        try:
            from .processing_provider.provider import RinkyoProvider
        except ImportError as exc:
            QgsMessageLog.logMessage(
                self.tr("プロセシングプロバイダを読み込めません: %s") % exc,
                LOG_TAG, Qgis.Warning)
            return
        self.provider = RinkyoProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    @staticmethod
    def tr(message: str) -> str:
        return QCoreApplication.translate("RinkyoClassifier", message)

    # -- 実行 --------------------------------------------------------------
    def run(self):
        dialog = MainDialog(self.iface.mainWindow())
        if not dialog.exec_():
            return
        params = dialog.parameters()

        signature = None
        if params["signature_path"]:
            try:
                with open(params["signature_path"], encoding="utf-8") as fh:
                    signature = Signature.from_json(fh.read())
            except (OSError, ValueError, KeyError) as exc:
                QMessageBox.critical(
                    self.iface.mainWindow(), self.tr("エラー"),
                    self.tr("シグネチャを読めません:\n%s") % exc)
                return

        task = ClassifyTask(
            stack_path=params["stack_path"],
            out_dir=params["out_dir"],
            basename=params["basename"],
            n_classes=params["n_classes"],
            max_samples=params["max_samples"],
            min_class_size=params["min_class_size"],
            min_separation=params["min_separation"],
            max_iterations=params["max_iterations"],
            convergence=params["convergence"],
            bands=params["bands"],
            write_likelihood=params["write_likelihood"],
            signature=signature,
        )
        task.taskCompleted.connect(lambda t=task: self._on_done(t))
        task.taskTerminated.connect(lambda t=task: self._on_failed(t))
        self._task = task
        QgsApplication.taskManager().addTask(task)
        self.iface.messageBar().pushMessage(
            self.tr("林況分類"), self.tr("処理を開始しました。進捗はタスクマネージャに表示されます。"),
            level=Qgis.Info, duration=5)

    def _on_done(self, task: ClassifyTask):
        for msg in task.messages:
            QgsMessageLog.logMessage(msg, LOG_TAG, Qgis.Info)

        layer = QgsRasterLayer(task.result_path,
                               task.basename + self.tr(" 分類結果"))
        if not layer.isValid():
            QMessageBox.critical(self.iface.mainWindow(), self.tr("エラー"),
                                 self.tr("分類結果を読み込めませんでした。"))
            return

        dialog = LabelDialog(task.signature, self.iface.mainWindow())
        if dialog.exec_():
            self._rewrite_labels(task, dialog.signature)
        apply_signature_style(layer, task.signature)
        QgsProject.instance().addMapLayer(layer)

        if task.likelihood_path and os.path.exists(task.likelihood_path):
            lik = QgsRasterLayer(task.likelihood_path,
                                 task.basename + self.tr(" 対数尤度"))
            if lik.isValid():
                QgsProject.instance().addMapLayer(lik)

        self.iface.messageBar().pushMessage(
            self.tr("林況分類"), self.tr("完了しました: %s") % os.path.basename(task.result_path),
            level=Qgis.Success, duration=8)

    def _on_failed(self, task: ClassifyTask):
        if task.exception:
            QgsMessageLog.logMessage(task.exception, LOG_TAG, Qgis.Critical)
            QMessageBox.critical(self.iface.mainWindow(), self.tr("エラー"),
                                 task.exception)
        else:
            self.iface.messageBar().pushMessage(
                self.tr("林況分類"), self.tr("処理を中止しました。"), level=Qgis.Warning,
                duration=5)

    def _rewrite_labels(self, task: ClassifyTask, signature: Signature):
        """意味づけの結果をラスタ属性テーブルとシグネチャに書き戻す。"""
        from osgeo import gdal
        try:
            ds = gdal.Open(task.result_path, gdal.GA_Update)
            write_rat(ds, signature)
            ds.FlushCache()
            ds = None
        except RuntimeError as exc:
            QgsMessageLog.logMessage(
                self.tr("ラスタ属性テーブルを更新できません: %s") % exc,
                LOG_TAG, Qgis.Warning)
        if task.signature_path:
            with open(task.signature_path, "w", encoding="utf-8") as fh:
                fh.write(signature.to_json())

    # -- 意味づけのやり直し ------------------------------------------------
    def relabel(self):
        layer = self.iface.activeLayer()
        if not isinstance(layer, QgsRasterLayer):
            QMessageBox.information(
                self.iface.mainWindow(), self.tr("確認"),
                self.tr("分類結果のラスタレイヤを選んでから実行してください。"))
            return
        sig_path = os.path.splitext(layer.source())[0].replace(
            "_class", "") + "_signature.json"
        if not os.path.isfile(sig_path):
            QMessageBox.information(
                self.iface.mainWindow(), self.tr("確認"),
                self.tr("対応するシグネチャファイルが見つかりません:\n%s") % sig_path)
            return
        with open(sig_path, encoding="utf-8") as fh:
            signature = Signature.from_json(fh.read())

        dialog = LabelDialog(signature, self.iface.mainWindow())
        if not dialog.exec_():
            return
        from osgeo import gdal
        ds = gdal.Open(layer.source(), gdal.GA_Update)
        write_rat(ds, dialog.signature)
        ds.FlushCache()
        ds = None
        with open(sig_path, "w", encoding="utf-8") as fh:
            fh.write(dialog.signature.to_json())
        apply_signature_style(layer, dialog.signature)

    # -- 小班集計 ----------------------------------------------------------
    def zonal(self):
        from .gui.zonal_dialog import ZonalDialog
        dialog = ZonalDialog(self.iface.mainWindow())
        dialog.exec_()
