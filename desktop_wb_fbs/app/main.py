# -*- coding: utf-8 -*-
from __future__ import annotations

import sys

from PyQt5.QtCore import Qt
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
    QLabel,
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
from app.services.catalog import CategoryService, ProductService
from app.services.orders import OrdersService
from app.ui.fbs_page import FbsPage
from app.ui.fonts import body_font, display_font, load_app_fonts
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

        self.setWindowTitle("{} — Поставки ВБ ФБС".format(APP_NAME))
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
        top_l.setContentsMargins(24, 0, 24, 0)
        top_l.setSpacing(8)

        self.page_title = QLabel("Поставки — ВБ ФБС")
        self.page_title.setObjectName("brandTitle")
        self.page_title.setFont(display_font(16))
        top_l.addWidget(self.page_title)
        top_l.addSpacing(12)

        self.btn_fbs = QPushButton("Поставки — ВБ ФБС")
        self.btn_fbs.setCheckable(True)
        self.btn_fbs.setObjectName("navBtn")
        self.btn_fbs.setCursor(Qt.PointingHandCursor)
        self.btn_fbs.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
        self.btn_settings = QPushButton("Настройки")
        self.btn_settings.setCheckable(True)
        self.btn_settings.setObjectName("navBtn")
        self.btn_settings.setCursor(Qt.PointingHandCursor)
        self.btn_settings.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
        top_l.addWidget(self.btn_fbs)
        top_l.addWidget(self.btn_settings)
        fit_tab_button(self.btn_fbs, h_pad=52)
        fit_tab_button(self.btn_settings, h_pad=52)
        top_l.addStretch(1)
        layout.addWidget(top)

        self.stack = QStackedWidget()
        self.fbs_page = FbsPage(db, self.sources, self.orders)
        self.settings_page = SettingsPage(
            db, self.sources, self.products, self.categories
        )
        # Source select sits in the top bar, opposite «Настройки».
        top_l.addWidget(self.fbs_page.source_combo, 0, Qt.AlignVCenter)
        self.stack.addWidget(self.fbs_page)
        self.stack.addWidget(self.settings_page)
        layout.addWidget(self.stack, 1)

        self.btn_fbs.clicked.connect(lambda: self._show(0))
        self.btn_settings.clicked.connect(lambda: self._show(1))
        self.settings_page.sources_changed.connect(self.fbs_page.reload_sources)
        self._show(0)
        self.statusBar().showMessage("Готово · данные локально · только API Wildberries")

    def _show(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self.btn_fbs.setChecked(index == 0)
        self.btn_settings.setChecked(index == 1)
        self.fbs_page.source_combo.setVisible(index == 0)
        if index == 0:
            self.page_title.setText("Поставки — ВБ ФБС")
            self.fbs_page.reload_sources()
        else:
            self.page_title.setText("Настройки")


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
