# -*- coding: utf-8 -*-
"""Prepare sticker PNGs with a progress dialog, then open ШК-style print preview."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
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
    finished_ok = pyqtSignal(object)  # payload dict
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
            from app.services.print_docs import prepare_supply_stickers

            def _prog(done: int, total: int, detail: str = "") -> None:
                if self.isInterruptionRequested():
                    return
                self.progress.emit(int(done), int(total), str(detail or ""))

            payload = prepare_supply_stickers(
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
            self.finished_ok.emit(payload)
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.failed.emit(str(exc) or exc.__class__.__name__)


class StickerLoadDialog(QDialog):
    """Modal progress while sticker PNGs are fetched to disk."""

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
    Download stickers with progress, then open the same PNG scroll preview used
    for supply QR / box stickers (print paints images directly — no WebEngine).
    """
    from app.ui.dialogs_extra import build_sticker_print_pixmaps, show_pixmap_print_preview

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
    result = {"payload": None, "error": None, "opened": False}  # type: Dict[str, Any]

    def _on_progress(done: int, total: int, detail: str) -> None:
        progress.set_progress(done, total, detail)

    def _on_fail(message: str) -> None:
        result["error"] = str(message or "Ошибка подготовки стикеров")
        progress.reject()

    def _on_ok(payload: object) -> None:
        result["payload"] = payload if isinstance(payload, dict) else {}

        def _show_preview() -> None:
            if progress.cancelled:
                return
            progress.lock_for_preview_open()
            groups = list((result["payload"] or {}).get("groups") or [])
            try:
                pixmaps = build_sticker_print_pixmaps(groups)
            except Exception as exc:
                progress.reject()
                QMessageBox.critical(parent, "Стикеры", str(exc))
                return
            progress.accept()
            if not pixmaps:
                QMessageBox.warning(parent, "Стикеры", "Нет стикеров для печати.")
                return
            show_pixmap_print_preview(
                pixmaps,
                "Стикеры поставки · {}".format(supply_id),
                parent,
            )
            result["opened"] = True

        QTimer.singleShot(0, _show_preview)

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
    ids = list((result.get("payload") or {}).get("ids") or [])
    from app.services.print_docs import sticker_print_html_path

    path = sticker_print_html_path(api_key, supply_id, ids)
    if path is not None:
        return path
    import tempfile

    return Path(tempfile.gettempdir()) / "feedpilot_stickers_{}.html".format(supply_id)
