# -*- coding: utf-8 -*-
"""
plugin - RinkyoClassifier のエントリポイント

ツールバーは小班JUMP3 などと共有する NationalForestToolbar に相乗りする。

License: GPL v2
"""

from __future__ import annotations

import os
from typing import Optional

from qgis.PyQt import sip
from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QMessageBox, QToolBar
from qgis.core import (
    Qgis, QgsApplication, QgsCoordinateTransformContext,
    QgsMessageLog, QgsProject, QgsRasterLayer, QgsVectorFileWriter,
)

from .core import pipeline, tif_labels
from .core.rinkyo_core import Signature
from .core.rinkyo_raster import write_rat
from .gui.label_dialog import (
    LabelDialog, apply_highlight_style, apply_signature_style,
)
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
        # 開いている意味づけ画面（非モーダルなので参照を持っておく）
        self._label_dialogs = []

    # -- QGIS インタフェース ----------------------------------------------
    def initGui(self):  # noqa: N802  QGIS の規約
        self.toolbar = self._shared_toolbar()

        run_action = QAction(self._icon("run_classify.png"),
                             self.tr("教師なし分類を実行"),
                             self.iface.mainWindow())
        run_action.triggered.connect(self.run)
        relabel_action = QAction(self._icon("label_scatter.png"),
                                 self.tr("分類結果の意味づけ…"),
                                 self.iface.mainWindow())
        relabel_action.triggered.connect(self.relabel)
        zonal_action = QAction(self.tr("小班別に集計…"),
                               self.iface.mainWindow())
        zonal_action.triggered.connect(self.zonal)

        for action in (run_action, relabel_action, zonal_action):
            self.iface.addPluginToRasterMenu(self.tr(MENU_TITLE), action)
            self.actions.append(action)
        self.toolbar.addAction(run_action)
        self.toolbar.addAction(relabel_action)

        self._register_provider()

    @staticmethod
    def _icon(filename: str) -> QIcon:
        """icons/ 以下のアイコン。無ければ（破損パッケージ等）空アイコン。"""
        path = os.path.join(os.path.dirname(__file__), "icons", filename)
        return QIcon(path) if os.path.exists(path) else QIcon()

    def unload(self):
        for dialog in list(self._label_dialogs):
            if not sip.isdeleted(dialog):
                dialog.done(0)
        self._label_dialogs = []
        for action in self.actions:
            self.iface.removePluginRasterMenu(self.tr(MENU_TITLE), action)
            if self.toolbar is not None and not sip.isdeleted(self.toolbar):
                self.toolbar.removeAction(action)
        self.actions = []
        self._unregister_provider()
        # ツールバーは他プラグインと共有しているので、空のときだけ片付ける
        if (self.toolbar is not None and not sip.isdeleted(self.toolbar)
                and not self.toolbar.actions()):
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
        registry = QgsApplication.processingRegistry()
        provider = RinkyoProvider()
        existing = registry.providerById(provider.id())
        if existing is not None:
            # 同じ id のプロバイダが既に登録されている（旧版や別フォルダの
            # 同じプラグインが同時に有効など）。addProvider に渡すと C++ 側で
            # provider が即座に delete され、unload 時に
            # "wrapped C/C++ object ... has been deleted" になるので登録しない。
            QgsMessageLog.logMessage(
                self.tr("プロセシングプロバイダ '%s' は既に登録されています。"
                        "同じプラグインが別フォルダに重複してインストール"
                        "されていないか確認してください。") % provider.id(),
                LOG_TAG, Qgis.Warning)
            return
        if registry.addProvider(provider):
            self.provider = provider
        else:
            # 失敗時は registry が provider を delete 済み。参照を持たない。
            QgsMessageLog.logMessage(
                self.tr("プロセシングプロバイダを登録できませんでした。"),
                LOG_TAG, Qgis.Warning)

    def _unregister_provider(self):
        provider, self.provider = self.provider, None
        if provider is None or sip.isdeleted(provider):
            return
        try:
            QgsApplication.processingRegistry().removeProvider(provider)
        except RuntimeError:
            # QGIS 終了処理などで既に破棄されている場合は無視する
            pass

    @staticmethod
    def tr(message: str) -> str:
        return QCoreApplication.translate("RinkyoClassifier", message)

    # -- 処理範囲（ポリゴン切り抜き用の一時ファイル書き出し） ----------------
    @staticmethod
    def _write_mask_layer(layer, out_dir: str, basename: str,
                          selected_only: bool) -> str:
        """切り抜き用ポリゴンを、GDAL から読める一時 GeoPackage に書き出す。

        「選択地物のみ」が指定されていて、実際に選択がなければ
        全地物を対象にする（うっかり空振りしないための保険）。
        """
        path = os.path.join(out_dir, basename + "_clip_mask.gpkg")
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.onlySelectedFeatures = bool(
            selected_only and layer.selectedFeatureCount())
        err = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer, path, QgsCoordinateTransformContext(), options)
        # writeAsVectorFormatV3 は (エラーコード, メッセージ, ...) のタプルを返す
        code = err[0] if isinstance(err, tuple) else err
        if code != QgsVectorFileWriter.NoError:
            raise OSError(str(err))
        return path

    # -- 実行 --------------------------------------------------------------
    def run(self):
        dialog = MainDialog(self.iface, self.iface.mainWindow())
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

        clip_mask_path = None
        mask_layer = params.get("clip_mask_layer")
        if mask_layer is not None:
            try:
                os.makedirs(params["out_dir"], exist_ok=True)
                clip_mask_path = self._write_mask_layer(
                    mask_layer, params["out_dir"],
                    params["basename"] or "rinkyo",
                    params["clip_selected_only"])
            except OSError as exc:
                QMessageBox.critical(
                    self.iface.mainWindow(), self.tr("エラー"),
                    self.tr("切り抜き用ポリゴンを書き出せません:\n%s") % exc)
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
            clip_extent=params.get("clip_extent"),
            clip_mask_path=clip_mask_path,
            mode=params["input_mode"],
            input_paths=params["input_paths"],
            input_root=params["input_root"],
            shared_signature=params["shared_signature"],
            existing=params["existing"],
            keep_tree=params["keep_tree"],
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
        if not task.results:
            # 既存の出力があるため全部スキップした場合
            self._push_summary(task, self.tr("処理したファイルはありません"))
            return
        if task.mode == pipeline.MODE_EACH:
            self._on_batch_done(task)
            return

        layer = QgsRasterLayer(task.result_path,
                               task.basename + self.tr(" 分類結果"))
        if not layer.isValid():
            QMessageBox.critical(self.iface.mainWindow(), self.tr("エラー"),
                                 self.tr("分類結果を読み込めませんでした。"))
            return

        # 先に地図へ載せておき、意味づけ画面の「適用」で色とラベルを更新する
        apply_signature_style(layer, task.signature)
        QgsProject.instance().addMapLayer(layer)
        layer_ids = [layer.id()]

        if task.likelihood_path and os.path.exists(task.likelihood_path):
            lik = QgsRasterLayer(task.likelihood_path,
                                 task.basename + self.tr(" 対数尤度"))
            if lik.isValid():
                QgsProject.instance().addMapLayer(lik)

        self._push_summary(task, os.path.basename(task.result_path))
        outcome = task.outcome

        def on_apply(signature):
            error = self._write_files(
                layer_ids, lambda: self._rewrite_labels(outcome, signature))
            self._restyle(layer_ids, signature)
            return error

        self._open_label_dialog(task.signature, on_apply, layer_ids,
                                subject=layer.name())

    def _on_batch_done(self, task: ClassifyTask):
        """ファイルごと処理の完了。結果はレイヤグループにまとめて追加する。

        共通シグネチャなら意味づけは 1 回だけ行い、全結果に書き戻す。
        個別シグネチャの場合は件数が多くなりうるのでダイアログは出さず、
        必要なレイヤだけ「分類結果の意味づけ…」でやり直してもらう。
        """
        project = QgsProject.instance()
        title = "%s %s" % (task.basename or self.tr("林況分類"),
                           self.tr("分類結果"))
        group = project.layerTreeRoot().insertGroup(0, title)
        layer_ids = []
        for res in task.results:
            name = res.basename + self.tr(" 分類結果")
            layer = QgsRasterLayer(res.result_path, name)
            if not layer.isValid():
                QgsMessageLog.logMessage(
                    self.tr("分類結果を読み込めませんでした: %s")
                    % res.result_path, LOG_TAG, Qgis.Warning)
                continue
            apply_signature_style(layer, res.signature)
            project.addMapLayer(layer, False)
            group.addLayer(layer)
            layer_ids.append(layer.id())
        if not layer_ids:
            project.layerTreeRoot().removeChildNode(group)
        self._push_summary(
            task, self.tr("%d 件（%s）") % (len(layer_ids), task.out_dir))

        outcome = task.outcome
        shared = outcome.shared_signature
        if shared is not None and outcome.results:
            def on_apply(signature):
                error = self._write_files(
                    layer_ids,
                    lambda: self._rewrite_labels(outcome, signature))
                self._restyle(layer_ids, signature)
                return error

            self._open_label_dialog(
                shared, on_apply, layer_ids,
                subject=self.tr("%s（%d 件）") % (title, len(layer_ids)))

    # -- 意味づけ画面 ------------------------------------------------------
    def _open_label_dialog(self, signature: Signature, on_apply, layer_ids,
                           subject: str = ""):
        """意味づけ画面を非モーダルで開く。

        開いたまま地図を拡大・移動して確認し、「適用」で何度でも反映できる。
        一覧で選んだクラスは、layer_ids のレイヤ上で黄色くハイライトする。
        subject には編集対象（レイヤ名やグループ名）を渡す。複数レイヤを
        同時に編集する場合でも、いま何を編集しているか画面で分かるようにする。
        """
        def on_highlight(indices, sig, dim):
            self._highlight(layer_ids, indices, sig, dim)

        dialog = LabelDialog(signature, self.iface.mainWindow(),
                             on_apply=on_apply, on_highlight=on_highlight,
                             subject=subject)
        dialog.setWindowModality(Qt.NonModal)
        dialog.setAttribute(Qt.WA_DeleteOnClose, True)
        self._label_dialogs.append(dialog)

        def forget(_result=None, d=dialog):
            if d in self._label_dialogs:
                self._label_dialogs.remove(d)

        dialog.finished.connect(forget)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return dialog

    def _highlight(self, layer_ids, indices, signature: Signature, dim: bool):
        """選んだクラスを黄色く表示する。indices が空なら通常表示に戻す。

        ファイルは書き換えず、レイヤの表示（レンダラ）だけを差し替える。
        """
        if not indices:
            self._restyle(layer_ids, signature)
            return
        project = QgsProject.instance()
        for layer_id in layer_ids:
            layer = project.mapLayer(layer_id)
            if layer is None or sip.isdeleted(layer):
                continue
            apply_highlight_style(layer, signature, indices, dim)

    def _write_files(self, layer_ids, write):
        """分類ラスタへの書き込み write() を、レイヤを読み直しながら行う。

        QGIS が開いているラスタは、閉じる際に古い内容の .aux.xml（RAT）を
        書き戻すことがある。書き込みの前に一度読み直して古い状態を
        吐き出させ、書き込み後にもう一度読み直して新しい内容を反映する。
        """
        self._reload(layer_ids)
        try:
            return write()
        finally:
            self._reload(layer_ids)

    def _reload(self, layer_ids):
        project = QgsProject.instance()
        for layer_id in layer_ids:
            layer = project.mapLayer(layer_id)
            if layer is None or sip.isdeleted(layer):
                continue
            try:
                layer.reload()
            except Exception as exc:  # noqa: BLE001  表示の更新だけなので続行
                QgsMessageLog.logMessage(
                    self.tr("レイヤを読み直せませんでした: %s") % exc,
                    LOG_TAG, Qgis.Warning)

    def _restyle(self, layer_ids, signature: Signature):
        """レイヤの色・凡例を更新する。途中で削除されたレイヤは飛ばす。"""
        project = QgsProject.instance()
        view = self.iface.layerTreeView()
        for layer_id in layer_ids:
            layer = project.mapLayer(layer_id)
            if layer is None or sip.isdeleted(layer):
                continue
            apply_signature_style(layer, signature)
            if view is not None:
                view.refreshLayerSymbology(layer_id)

    def _push_summary(self, task: ClassifyTask, what: str):
        outcome = task.outcome
        problems = (outcome.skipped + outcome.failures) if outcome else []
        if problems:
            self.iface.messageBar().pushMessage(
                self.tr("林況分類"),
                self.tr("完了しました: %s　スキップ・失敗 %d 件"
                        "（ログメッセージパネルを確認してください）")
                % (what, len(problems)),
                level=Qgis.Warning, duration=0)
            for text in problems:
                QgsMessageLog.logMessage(text, LOG_TAG, Qgis.Warning)
        else:
            self.iface.messageBar().pushMessage(
                self.tr("林況分類"), self.tr("完了しました: %s") % what,
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

    def _rewrite_labels(self, outcome: "pipeline.Outcome",
                        signature: Signature) -> Optional[str]:
        """意味づけの結果をラスタ属性テーブルとシグネチャに書き戻す。

        QgsTask は完了後に破棄されるので、task ではなく結果（outcome）を
        受け取る。失敗したファイルがあれば内容をまとめて返す（無ければ None）。
        """
        errors = []
        for res in outcome.results:
            error = self._write_labels(res.result_path, res.signature_path,
                                       signature)
            if error:
                errors.append(error)
            res.signature = signature
        if outcome.shared_signature is not None:
            outcome.shared_signature = signature
        return "\n".join(errors) or None

    def _write_labels(self, raster_path: str, sig_path: Optional[str],
                      signature: Signature) -> Optional[str]:
        from osgeo import gdal
        error = None
        try:
            ds = gdal.Open(raster_path, gdal.GA_Update)
            write_rat(ds, signature)
            ds.FlushCache()
            ds = None
        except RuntimeError as exc:
            error = self.tr("分類ラスタ（TIF）に意味づけを書き込めません: %s\n%s") % (
                raster_path, exc)
            QgsMessageLog.logMessage(error, LOG_TAG, Qgis.Warning)
        if sig_path:
            try:
                with open(sig_path, "w", encoding="utf-8") as fh:
                    fh.write(signature.to_json())
            except OSError as exc:
                msg = self.tr("シグネチャを保存できません: %s") % exc
                QgsMessageLog.logMessage(msg, LOG_TAG, Qgis.Warning)
                error = "%s\n%s" % (error, msg) if error else msg
        return error

    # -- 意味づけのやり直し ------------------------------------------------
    def relabel(self):
        layer = self.iface.activeLayer()
        if not isinstance(layer, QgsRasterLayer):
            QMessageBox.information(
                self.iface.mainWindow(), self.tr("確認"),
                self.tr("分類結果のラスタレイヤを選んでから実行してください。"))
            return
        raster_path = tif_labels.raster_file(layer.source())
        sig_path = tif_labels.signature_path_for(raster_path)
        # TIF に埋め込んだ意味づけ（0.3.0 以降）を優先し、
        # 無ければ隣の _signature.json を使う
        signature, _origin = tif_labels.load_signature(raster_path)
        if signature is None:
            QMessageBox.information(
                self.iface.mainWindow(), self.tr("確認"),
                self.tr("この分類ラスタには意味づけの情報がありません。\n"
                        "TIF への埋め込みも、対応するシグネチャファイルも"
                        "見つかりません:\n%s") % sig_path)
            return

        layer_ids = [layer.id()]

        def on_apply(sig):
            error = self._write_files(
                layer_ids,
                lambda: self._write_labels(raster_path, sig_path, sig))
            self._restyle(layer_ids, sig)
            return error

        self._open_label_dialog(signature, on_apply, layer_ids,
                                subject=layer.name())

    # -- 小班集計 ----------------------------------------------------------
    def zonal(self):
        from .gui.zonal_dialog import ZonalDialog
        dialog = ZonalDialog(self.iface.mainWindow())
        dialog.exec_()
