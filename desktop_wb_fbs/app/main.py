# -*- coding: utf-8 -*-
from __future__ import annotations

import sys

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QPalette

# QtWebEngine must be imported before QApplication on Windows, otherwise
# QWebEngineView fails at runtime even when PyQtWebEngine is installed.
try:
    from PyQt5 import QtWebEngineWidgets  # noqa: F401
except ImportError:
    pass

from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app import APP_NAME, __version__
from app.diag_log import init as init_diag_log, write as diag_write
from app.db import Database
from app.services import SourceService
from app.services.auto_sync import AutoSyncSettings
from app.services.catalog import CategoryService, ProductService
from app.services.orders import OrdersService
from app.services.ozon_orders import OzonOrdersService
from app.ui.fbs_page import FbsPage
from app.ui.ozon_fbs_page import OzonFbsPage
from app.ui.fonts import body_font, load_app_fonts
from app.ui.layout_utils import fit_tab_button
from app.ui.settings_page import SettingsPage
from app.ui.styles import get_app_qss
from app.ui.text_copy import install_text_copy_support, walk_copyable_labels


class MainWindow(QMainWindow):
    def __init__(self, db: Database) -> None:
        super(MainWindow, self).__init__()
        self.db = db
        self.sources = SourceService(db)
        self.products = ProductService(db)
        self.categories = CategoryService(db)
        self.orders = OrdersService(db)

        self.setWindowTitle("{} — ВБ / Ozon FBS".format(APP_NAME))
        self.resize(1280, 800)
        self.setMinimumSize(960, 600)

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        top = QFrame()
        top.setObjectName("topBar")
        top_l = QHBoxLayout(top)
        top_l.setContentsMargins(16, 0, 24, 0)
        top_l.setSpacing(8)

        self.btn_fbs = QPushButton("ВБ ФБС")
        self.btn_fbs.setCheckable(True)
        self.btn_fbs.setObjectName("navBtn")
        self.btn_fbs.setCursor(Qt.PointingHandCursor)
        self.btn_fbs.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
        self.btn_ozon = QPushButton("ОЗОН ФБС")
        self.btn_ozon.setCheckable(True)
        self.btn_ozon.setObjectName("navBtn")
        self.btn_ozon.setCursor(Qt.PointingHandCursor)
        self.btn_ozon.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
        self.btn_settings = QPushButton("Настройки")
        self.btn_settings.setCheckable(True)
        self.btn_settings.setObjectName("navBtn")
        self.btn_settings.setCursor(Qt.PointingHandCursor)
        self.btn_settings.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
        top_l.addWidget(self.btn_fbs)
        top_l.addWidget(self.btn_ozon)
        top_l.addWidget(self.btn_settings)
        fit_tab_button(self.btn_fbs, h_pad=40)
        fit_tab_button(self.btn_ozon, h_pad=40)
        fit_tab_button(self.btn_settings, h_pad=52)
        top_l.addStretch(1)
        layout.addWidget(top)

        self.stack = QStackedWidget()
        self.fbs_page = FbsPage(db, self.sources, self.orders)
        self.ozon_orders = OzonOrdersService(db)
        self.ozon_fbs_page = OzonFbsPage(db, self.sources, self.ozon_orders)
        self.settings_page = SettingsPage(
            db, self.sources, self.products, self.categories
        )
        top_l.addWidget(self.fbs_page.source_combo, 0, Qt.AlignVCenter)
        top_l.addWidget(self.ozon_fbs_page.source_combo, 0, Qt.AlignVCenter)
        self.ozon_fbs_page.source_combo.hide()
        self.stack.addWidget(self.fbs_page)
        self.stack.addWidget(self.ozon_fbs_page)
        self.stack.addWidget(self.settings_page)
        layout.addWidget(self.stack, 1)

        self.btn_fbs.clicked.connect(lambda: self._show(0))
        self.btn_ozon.clicked.connect(lambda: self._show(1))
        self.btn_settings.clicked.connect(lambda: self._show(2))
        self.settings_page.sources_changed.connect(self._on_sources_changed)
        self._autosync = AutoSyncSettings(db)
        self._autosync_timer = QTimer(self)
        self._autosync_timer.setInterval(60 * 1000)
        self._autosync_timer.timeout.connect(self._on_autosync_tick)
        self._autosync_timer.start()
        self._show(0)
        self.statusBar().showMessage(
            "Готово · данные локально · API Wildberries и Ozon"
        )
        # First poll shortly after startup (web restores scheduler on boot).
        QTimer.singleShot(15 * 1000, self._on_autosync_tick)

    def _on_sources_changed(self) -> None:
        self.fbs_page.reload_sources()
        self.ozon_fbs_page.reload_sources()
        reload_ui = getattr(self.settings_page, "_reload_autosync_ui", None)
        if callable(reload_ui):
            reload_ui()

    def _show(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self.btn_fbs.setChecked(index == 0)
        self.btn_ozon.setChecked(index == 1)
        self.btn_settings.setChecked(index == 2)
        self.fbs_page.source_combo.setVisible(index == 0)
        self.ozon_fbs_page.source_combo.setVisible(index == 1)
        if index == 0:
            self.fbs_page.reload_sources()
        elif index == 1:
            self.ozon_fbs_page.reload_sources()

    def _on_autosync_tick(self) -> None:
        try:
            self._maybe_autosync_channel("wb")
            self._maybe_autosync_channel("ozon")
        except Exception:
            pass

    def _maybe_autosync_channel(self, channel: str) -> None:
        due, _reason = self._autosync.should_run_now(channel)
        if not due:
            return
        if channel == "wb":
            worker = getattr(self.fbs_page, "_worker", None)
            if worker is not None and worker.isRunning():
                return
            if not self.sources.list_fbs_enabled():
                return
            self.statusBar().showMessage("Автосинк ВБ ФБС…")
            self._autosync.mark_synced("wb")
            self.fbs_page.start_sync()
            return
        worker = getattr(self.ozon_fbs_page, "_sync_worker", None)
        if worker is not None and worker.isRunning():
            return
        if not self.sources.list_ozon_fbs_enabled():
            return
        self.statusBar().showMessage("Автосинк ОЗОН ФБС…")
        self._autosync.mark_synced("ozon")
        self.ozon_fbs_page.run_sync()


def run() -> int:
    try:
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    except Exception:
        pass
    log_path = init_diag_log()
    diag_write("app.run.begin", version=__version__, log=str(log_path))
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    # Apply light chrome immediately — before fonts/DB — so the first frame is
    # never a dark/black Fusion flash on Windows dark-mode hosts.
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor("#ffffff"))
    pal.setColor(QPalette.WindowText, QColor("#0f1f33"))
    pal.setColor(QPalette.Base, QColor("#ffffff"))
    pal.setColor(QPalette.AlternateBase, QColor("#f2f8ff"))
    pal.setColor(QPalette.Text, QColor("#0f1f33"))
    pal.setColor(QPalette.Button, QColor("#ffffff"))
    pal.setColor(QPalette.ButtonText, QColor("#0f1f33"))
    pal.setColor(QPalette.Highlight, QColor("#ddf1ff"))
    pal.setColor(QPalette.HighlightedText, QColor("#0f1f33"))
    pal.setColor(QPalette.ToolTipBase, QColor("#ffffff"))
    pal.setColor(QPalette.ToolTipText, QColor("#0f1f33"))
    pal.setColor(QPalette.PlaceholderText, QColor("#8aa0b8"))
    app.setPalette(pal)
    app.setStyleSheet(get_app_qss())
    load_app_fonts()
    app.setFont(body_font(14))
    install_text_copy_support(app)

    db = Database()
    db.init_schema()

    win = MainWindow(db)
    walk_copyable_labels(win)
    win.setAutoFillBackground(True)
    win.setPalette(pal)
    # Maximize before show so Windows does not paint an intermediate dark frame.
    win.setWindowState(win.windowState() | Qt.WindowMaximized)
    win.show()
    return app.exec_()
