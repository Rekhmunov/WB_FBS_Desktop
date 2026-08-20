# -*- coding: utf-8 -*-
"""Prepare sticker print docs with a visible progress dialog, then open preview."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from PyQt5.QtCore import QEventLoop, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.db import Database
from app.services.orders import OrdersService
from app.ui.dialog_utils import prepare_modal_dialog, standard_window_flags


class _StickerPrepareWorker(QThread):
    progress = pyqtSignal(int, int, str)  # done, total, detail
    finished_ok = pyqtSignal(object)  # Path
    failed = pyqtSignal(str)

    def __init__(
        self,
        db: Database,
        orders_svc: OrdersService,
        source_id: int,
        api_key: str,
        supply_id: str,
        order_ids: Optional[List[int]] = None,
        preloaded_stickers: Optional[Dict[int, Dict[str, Any]]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super(_StickerPrepareWorker, self).__init__(parent)
        self._db = db
        self._orders = orders_svc
        self._source_id = int(source_id)
        self._api_key = str(api_key or "")
        self._supply_id = str(supply_id or "")
        self._order_ids = list(order_ids) if order_ids is not None else None
        self._preloaded = preloaded_stickers

    def run(self) -> None:
        try:
            from app.services.print_docs import prepare_supply_stickers_html

            def _prog(done: int, total: int, detail: str = "") -> None:
                if self.isInterruptionRequested():
                    return
                self.progress.emit(int(done), int(total), str(detail or ""))

            path = prepare_supply_stickers_html(
                self._db,
                self._orders,
                self._source_id,
                self._api_key,
                self._supply_id,
                order_ids=self._order_ids,
                preloaded_stickers=self._preloaded,
                progress=_prog,
                should_abort=self.isInterruptionRequested,
            )
            if self.isInterruptionRequested():
                return
            self.finished_ok.emit(path)
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.failed.emit(str(exc) or exc.__class__.__name__)


class StickerLoadDialog(QDialog):
    """Modal progress while sticker PNGs are fetched and HTML is built."""

    def __init__(self, parent: Optional[QWidget] = None, *, title: str = "Стикеры") -> None:
        super(StickerLoadDialog, self).__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(standard_window_flags())
        prepare_modal_dialog(
            self,
            maximized=False,
            default_size=(480, 220),
            minimum_size=(420, 200),
        )
        self.setModal(True)
        self.setWindowModality(Qt.ApplicationModal)
        self._cancelled = False
        self._pulse = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)

        self.title_lab = QLabel("Подготовка стикеров к печати")
        self.title_lab.setObjectName("dialogTitle")
        root.addWidget(self.title_lab)

        self.detail = QLabel("Собираем файлы на этом компьютере…")
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet("color:#64748b;")
        root.addWidget(self.detail)

        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.setTextVisible(True)
        self.bar.setMinimumHeight(22)
        root.addWidget(self.bar)

        self.counter = QLabel("")
        self.counter.setStyleSheet("color:#334155;font-weight:600;")
        root.addWidget(self.counter)

        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_cancel = QPushButton("Отмена")
        self.btn_cancel.setObjectName("secondary")
        self.btn_cancel.clicked.connect(self._on_cancel)
        row.addWidget(self.btn_cancel)
        root.addLayout(row)

        self._anim = QTimer(self)
        self._anim.setInterval(450)
        self._anim.timeout.connect(self._tick)
        self._anim.start()

    def _tick(self) -> None:
        self._pulse = (self._pulse + 1) % 4
        dots = "." * self._pulse
        base = "Собираем файлы на этом компьютере"
        if self.bar.maximum() <= 0:
            self.detail.setText(base + dots)

    def _on_cancel(self) -> None:
        self._cancelled = True
        self.reject()

    def set_progress(self, done: int, total: int, detail: str = "") -> None:
        done = max(0, int(done))
        total = max(0, int(total))
        if total > 0:
            self.bar.setRange(0, total)
            self.bar.setValue(min(done, total))
            self.counter.setText("{} из {}".format(done, total))
        else:
            self.bar.setRange(0, 0)
            self.counter.setText("")
        if detail:
            self.detail.setText(detail)

    def lock_for_preview_open(self) -> None:
        self.btn_cancel.setEnabled(False)
        self._anim.stop()
        cur = max(self.bar.value(), 1)
        total = max(self.bar.maximum(), cur, 1)
        self.set_progress(total, total, "Открываем превью…")

    @property
    def cancelled(self) -> bool:
        return self._cancelled


def _resolve_order_ids(
    orders_svc: OrdersService,
    source_id: int,
    api_key: str,
    supply_id: str,
    order_ids: Optional[Sequence[int]],
) -> List[int]:
    if order_ids is not None:
        return [int(x) for x in order_ids]
    rows = orders_svc.orders_in_supply(source_id, supply_id, api_key="")
    if not rows and api_key:
        rows = orders_svc.orders_in_supply(source_id, supply_id, api_key=api_key)
    return [int(r["order_id"]) for r in rows if r.get("order_id") is not None]


def _open_ready_preview(
    path: Path,
    *,
    parent: Optional[QWidget],
    progress: StickerLoadDialog,
) -> Optional[Path]:
    """Warm WebEngine under the progress dialog, then open print-ready preview."""
    from app.services.print_docs import open_html_path
    from app.ui.html_print_dialog import HtmlPrintPreviewDialog, webengine_status

    progress.lock_for_preview_open()
    webengine_ok, status = webengine_status()
    if not webengine_ok:
        progress.accept()
        return open_html_path(
            path,
            parent=parent,
            title="Стикеры поставки",
            nested_print_preview=False,
            wait_images=True,
            webengine_hint=status,
        )

    dlg = HtmlPrintPreviewDialog(
        path,
        title="Стикеры поставки",
        parent=parent,
        nested_print_preview=False,
        wait_images=True,
    )
    # Preview loads behind the progress dialog; Print is enabled before we reveal it.
    dlg.setWindowModality(Qt.NonModal)
    loop = QEventLoop()
    ready = {"ok": False}

    def _on_ready() -> None:
        ready["ok"] = True
        if loop.isRunning():
            loop.quit()

    dlg.document_ready.connect(_on_ready)
    dlg.showMaximized()
    progress.raise_()
    progress.activateWindow()
    QTimer.singleShot(60000, loop.quit)
    if not ready["ok"]:
        loop.exec_()

    progress.accept()

    if not ready["ok"] and not bool(getattr(dlg, "_loaded", False)):
        dlg.close()
        return open_html_path(
            path,
            parent=parent,
            title="Стикеры поставки",
            nested_print_preview=False,
            wait_images=False,
            webengine_hint="таймаут загрузки превью",
        )

    dlg.setWindowModality(Qt.ApplicationModal)
    dlg.raise_()
    dlg.activateWindow()
    dlg.exec_()
    return path


def run_supply_sticker_print(
    parent: Optional[QWidget],
    db: Database,
    orders_svc: OrdersService,
    source_id: int,
    api_key: str,
    supply_id: str,
    *,
    order_ids: Optional[List[int]] = None,
    preloaded_stickers: Optional[Dict[int, Dict[str, Any]]] = None,
) -> Optional[Path]:
    """
    Download stickers with progress, warm the HTML preview, then open it already
    ready for «Печать…». Disk-cached supplies skip the network phase.
    """
    from app.services.print_docs import sticker_print_html_path
    from app.services.sticker_file_cache import existing_sticker_paths

    ids = _resolve_order_ids(orders_svc, source_id, api_key, supply_id, order_ids)
    cached_path = sticker_print_html_path(api_key, supply_id, ids)
    if cached_path is not None and ids and cached_path.is_file():
        on_disk = existing_sticker_paths(api_key, supply_id, ids)
        if len(on_disk) >= len(ids):
            progress = StickerLoadDialog(parent, title="Стикеры · {}".format(supply_id))
            progress.set_progress(len(ids), len(ids), "Файлы уже на диске — открываем…")
            progress.show()
            return _open_ready_preview(cached_path, parent=parent, progress=progress)

    progress = StickerLoadDialog(parent, title="Стикеры · {}".format(supply_id))
    worker = _StickerPrepareWorker(
        db,
        orders_svc,
        source_id,
        api_key,
        supply_id,
        order_ids=order_ids,
        preloaded_stickers=preloaded_stickers,
        parent=None,
    )
    result = {"path": None, "error": None, "opened": False}  # type: Dict[str, Any]

    def _on_progress(done: int, total: int, detail: str) -> None:
        progress.set_progress(done, total, detail)

    def _on_fail(message: str) -> None:
        result["error"] = str(message or "Ошибка подготовки стикеров")
        progress.reject()

    def _on_ok(path: object) -> None:
        result["path"] = Path(str(path))

        def _warm() -> None:
            if progress.cancelled:
                return
            opened = _open_ready_preview(
                Path(result["path"]), parent=parent, progress=progress
            )
            result["opened"] = opened is not None
            # If preview path returned early without accepting, force-close progress.
            if progress.isVisible():
                progress.accept()

        QTimer.singleShot(0, _warm)

    worker.progress.connect(_on_progress)
    worker.finished_ok.connect(_on_ok)
    worker.failed.connect(_on_fail)
    worker.start()
    progress.exec_()

    if progress.cancelled and not result["opened"]:
        worker.requestInterruption()
        if not worker.wait(8000):
            worker.terminate()
            worker.wait(2000)
        return None

    if result["error"] and not result["opened"]:
        worker.wait(1000)
        QMessageBox.critical(parent, "Стикеры", result["error"])
        return None

    worker.wait(2000)
    return Path(result["path"]) if result.get("path") else None
