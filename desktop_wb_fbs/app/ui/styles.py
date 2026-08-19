# -*- coding: utf-8 -*-
"""Desktop UI stylesheet — calm, modern, laconic (no XP chrome)."""

APP_QSS = """
/* --- Design tokens (via repeated values) --- */
/* Accent: #2563eb  Surface: #ffffff  Canvas: #f4f7fb  Line: #e8eef5  Text: #0f172a */

QWidget {
    font-family: "Segoe UI", "Inter", "Tahoma", sans-serif;
    font-size: 14px;
    color: #0f172a;
}
QMainWindow, QDialog {
    background: #f4f7fb;
}
QDialog {
    min-width: 440px;
}
QStatusBar {
    background: #ffffff;
    color: #64748b;
    border-top: 1px solid #e8eef5;
    font-size: 12px;
    min-height: 28px;
    padding: 0 12px;
}
QMessageBox {
    font-size: 14px;
}
QMessageBox QLabel {
    font-size: 14px;
    min-width: 280px;
    color: #334155;
}

/* ========== Top navigation ========== */
QFrame#topBar {
    background: #ffffff;
    border-bottom: 1px solid #e8eef5;
    min-height: 64px;
}
QLabel#brandTitle {
    color: #0f172a;
    font-size: 15px;
    font-weight: 700;
    letter-spacing: -0.01em;
    padding: 0;
}
QLabel#brandSub {
    color: #94a3b8;
    font-size: 11px;
    padding: 0;
}
QPushButton#navBtn {
    background: transparent;
    color: #64748b;
    border: none;
    border-bottom: 2px solid transparent;
    border-radius: 0;
    padding: 16px 16px 14px 16px;
    min-height: 0;
    max-height: 64px;
    font-size: 14px;
    font-weight: 500;
    text-align: center;
}
QPushButton#navBtn:hover {
    color: #0f172a;
    background: transparent;
}
QPushButton#navBtn:checked {
    color: #0f172a;
    font-weight: 600;
    border-bottom: 2px solid #2563eb;
    background: transparent;
}
QPushButton#navBtn:pressed {
    background: transparent;
    color: #0f172a;
}

/* ========== Buttons ========== */
QPushButton {
    color: #ffffff;
    border: 1px solid #2563eb;
    border-radius: 8px;
    background: #2563eb;
    padding: 0 16px;
    min-height: 36px;
    font-size: 14px;
    font-weight: 600;
}
QPushButton:hover {
    background: #1d4ed8;
    border-color: #1d4ed8;
}
QPushButton:pressed {
    background: #1e40af;
    border-color: #1e40af;
}
QPushButton:disabled {
    background: #94a3b8;
    border-color: #94a3b8;
    color: #f8fafc;
}
QPushButton#secondary, QPushButton[class="secondary"] {
    color: #334155;
    border: 1px solid #dbe3ef;
    background: #ffffff;
}
QPushButton#secondary:hover, QPushButton[class="secondary"]:hover {
    border-color: #cbd5e1;
    background: #f8fafc;
    color: #0f172a;
}
QPushButton#secondary:pressed, QPushButton[class="secondary"]:pressed {
    background: #f1f5f9;
}
QPushButton#secondary:disabled, QPushButton[class="secondary"]:disabled {
    color: #94a3b8;
    border-color: #e2e8f0;
    background: #f8fafc;
}
QPushButton#danger, QPushButton[class="danger"] {
    color: #b91c1c;
    border: 1px solid #fecaca;
    background: #ffffff;
}
QPushButton#danger:hover, QPushButton[class="danger"]:hover {
    background: #fef2f2;
    border-color: #fca5a5;
}
QPushButton#iconBtn {
    min-width: 36px;
    max-width: 36px;
    min-height: 36px;
    padding: 0;
    color: #64748b;
    border: 1px solid #dbe3ef;
    background: #ffffff;
    border-radius: 8px;
    font-size: 14px;
    font-weight: 600;
}
QPushButton#iconBtn:hover {
    background: #f8fafc;
    color: #0f172a;
    border-color: #cbd5e1;
}
QPushButton#mgtBtn {
    color: #0f172a;
    border: 1px solid #dbe3ef;
    background: #ffffff;
    border-radius: 8px;
    min-height: 36px;
    padding: 0 16px;
    font-size: 13px;
    font-weight: 600;
}
QPushButton#mgtBtn:hover {
    background: #f8fafc;
    border-color: #cbd5e1;
}
QPushButton#mgtBtn:pressed {
    background: #f1f5f9;
}
QPushButton#bottomPrimary {
    color: #ffffff;
    border: 1px solid #2563eb;
    border-radius: 8px;
    background: #2563eb;
    min-height: 36px;
    padding: 0 16px;
    font-size: 14px;
    font-weight: 600;
}
QPushButton#bottomPrimary:hover {
    background: #1d4ed8;
    border-color: #1d4ed8;
}
QPushButton#bottomPrimary:pressed {
    background: #1e40af;
}
QPushButton#tabBtn {
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    border-radius: 0;
    padding: 12px 16px 10px 16px;
    min-height: 0;
    color: #64748b;
    font-size: 14px;
    font-weight: 500;
    text-align: center;
}
QPushButton#tabBtn:hover {
    color: #0f172a;
    background: transparent;
}
QPushButton#tabBtn:checked {
    color: #0f172a;
    font-weight: 600;
    border-bottom: 2px solid #2563eb;
    background: transparent;
}
QPushButton#tabBtn:pressed {
    background: transparent;
}
QPushButton#linkBtn {
    background: transparent;
    border: none;
    color: #2563eb;
    font-size: 13px;
    font-weight: 600;
    min-height: 0;
    padding: 4px 0;
}
QPushButton#linkBtn:hover {
    background: transparent;
    color: #1d4ed8;
    text-decoration: underline;
}
QPushButton#pagerBtn {
    color: #334155;
    border: 1px solid #dbe3ef;
    background: #ffffff;
    border-radius: 8px;
    min-width: 36px;
    max-width: 36px;
    min-height: 36px;
    padding: 0;
    font-size: 14px;
    font-weight: 600;
}
QPushButton#pagerBtn:hover {
    background: #f8fafc;
    border-color: #cbd5e1;
}
QPushButton#pagerBtn:disabled {
    color: #cbd5e1;
    border-color: #e8eef5;
    background: #f8fafc;
}
QToolButton {
    color: #334155;
    border: 1px solid #dbe3ef;
    background: #ffffff;
    border-radius: 8px;
    padding: 0 12px;
    min-height: 36px;
    min-width: 36px;
    font-size: 14px;
    font-weight: 600;
}
QToolButton:hover {
    border-color: #cbd5e1;
    background: #f8fafc;
}
QToolButton#secondary {
    color: #334155;
    border: 1px solid #dbe3ef;
    background: #ffffff;
}
QToolButton#dangerToolBtn {
    color: #b91c1c;
    border: 1px solid #fecaca;
    background: #ffffff;
    border-radius: 8px;
    min-width: 28px;
    max-width: 32px;
    min-height: 28px;
    max-height: 32px;
    padding: 0;
    font-size: 13px;
    font-weight: 700;
}
QToolButton#dangerToolBtn:hover {
    background: #fef2f2;
    border-color: #fca5a5;
}
QPushButton#filterChip {
    color: #334155;
    border: 1px solid #dbe3ef;
    background: #ffffff;
    border-radius: 999px;
    padding: 0 14px;
    min-height: 32px;
    font-size: 13px;
    font-weight: 600;
}
QPushButton#filterChip:hover {
    background: #f8fafc;
    border-color: #cbd5e1;
}
QPushButton#filterChip:checked {
    color: #ffffff;
    background: #2563eb;
    border-color: #2563eb;
}
QDialogButtonBox QPushButton {
    min-width: 100px;
    min-height: 36px;
    padding: 0 16px;
}

/* ========== Inputs — kill native XP look ========== */
QLineEdit, QComboBox, QSpinBox, QTextEdit, QPlainTextEdit {
    background: #ffffff;
    border: 1px solid #dbe3ef;
    border-radius: 8px;
    padding: 0 12px;
    min-height: 36px;
    color: #0f172a;
    font-size: 14px;
    selection-background-color: #dbeafe;
    selection-color: #0f172a;
}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover {
    border-color: #cbd5e1;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QTextEdit:focus {
    border: 1px solid #2563eb;
    background: #ffffff;
}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {
    background: #f8fafc;
    color: #94a3b8;
    border-color: #e8eef5;
}
QComboBox {
    padding-right: 28px;
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 28px;
    border: none;
    background: transparent;
}
QComboBox::down-arrow {
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #64748b;
    margin-right: 10px;
}
QComboBox QAbstractItemView {
    background: #ffffff;
    border: 1px solid #e8eef5;
    border-radius: 8px;
    outline: none;
    selection-background-color: #eff6ff;
    selection-color: #0f172a;
    padding: 4px;
    font-size: 14px;
}
QComboBox QAbstractItemView::item {
    min-height: 32px;
    padding: 4px 12px;
}
QComboBox#sourceCombo {
    min-width: 180px;
    max-width: 280px;
    background: #ffffff;
    border: 1px solid #dbe3ef;
    border-radius: 8px;
    padding-left: 12px;
    font-weight: 500;
}
QSpinBox {
    padding-right: 28px;
}
QSpinBox::up-button, QSpinBox::down-button {
    subcontrol-origin: border;
    width: 22px;
    border: none;
    background: transparent;
    border-left: 1px solid #e8eef5;
}
QSpinBox::up-button {
    subcontrol-position: top right;
    border-top-right-radius: 8px;
}
QSpinBox::down-button {
    subcontrol-position: bottom right;
    border-bottom-right-radius: 8px;
}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {
    background: #f1f5f9;
}
QSpinBox::up-arrow {
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-bottom: 5px solid #64748b;
}
QSpinBox::down-arrow {
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #64748b;
}
QComboBox#pageSizeCombo {
    min-width: 72px;
    max-width: 88px;
    min-height: 36px;
    padding-left: 12px;
    font-weight: 500;
}
QLabel#fieldLabel {
    font-size: 13px;
    font-weight: 600;
    color: #475569;
}

/* ========== Panels ========== */
QFrame#toolbarPanel {
    background: #ffffff;
    border: 1px solid #e8eef5;
    border-radius: 12px;
}
QFrame#syncInfo {
    background: #f8fafc;
    border: 1px solid #e8eef5;
    border-radius: 8px;
}
QFrame#syncInfo[state="ok"] {
    background: #f0fdf4;
    border: 1px solid #bbf7d0;
}
QFrame#syncInfo[state="error"] {
    background: #fef2f2;
    border: 1px solid #fecaca;
}
QLabel#sectionTitle {
    font-size: 20px;
    font-weight: 700;
    color: #0f172a;
    letter-spacing: -0.02em;
    padding: 0;
}
QLabel#syncInfoText {
    font-size: 13px;
    font-weight: 500;
    color: #475569;
    line-height: 1.4;
}
QFrame#syncInfo[state="ok"] QLabel#syncInfoText {
    color: #166534;
}
QFrame#syncInfo[state="error"] QLabel#syncInfoText {
    color: #b91c1c;
}
QLabel#syncPallets {
    font-size: 13px;
    font-weight: 500;
    color: inherit;
}
QFrame#tabsRow {
    border-bottom: 1px solid #e8eef5;
    background: transparent;
    min-height: 48px;
}
QFrame#bottomBar {
    background: #ffffff;
    border: 1px solid #e8eef5;
    border-radius: 12px;
    min-height: 52px;
}
QFrame#pagerBar {
    background: transparent;
    border: none;
    min-height: 40px;
}
QLabel#selectedLabel {
    font-size: 14px;
    font-weight: 600;
    color: #0f172a;
}
QLabel#hint, QLabel[class="hint"] {
    color: #64748b;
    font-size: 13px;
}
QLabel#pageMeta {
    color: #64748b;
    font-size: 13px;
    font-weight: 500;
    padding: 0 4px;
    min-width: 72px;
}
QLabel#dialogTitle {
    font-size: 18px;
    font-weight: 700;
    color: #0f172a;
}
QLabel#sdTitle {
    font-size: 20px;
    font-weight: 700;
    color: #0f172a;
}
QLabel#sdMeta {
    color: #64748b;
    font-size: 13px;
}
QLabel#sdChip {
    background: #f8fafc;
    border: 1px solid #e8eef5;
    border-radius: 8px;
    padding: 4px 10px;
    font-size: 12px;
    font-weight: 600;
    color: #475569;
    min-height: 24px;
}
QFrame#sdHeader {
    background: #ffffff;
    border-bottom: 1px solid #e8eef5;
}
QFrame#dialogBody {
    background: #f4f7fb;
}

/* ========== Tables ========== */
QTableWidget {
    background: #ffffff;
    border: 1px solid #e8eef5;
    border-radius: 12px;
    gridline-color: transparent;
    selection-background-color: #eff6ff;
    selection-color: #0f172a;
    alternate-background-color: #fafbfc;
    font-size: 14px;
    outline: none;
}
QTableWidget::item {
    padding: 8px 12px;
    border-bottom: 1px solid #f1f5f9;
}
QTableWidget::item:selected {
    background: #eff6ff;
    color: #0f172a;
}
QHeaderView::section {
    background: #f8fafc;
    border: none;
    border-bottom: 1px solid #e8eef5;
    border-right: 1px solid #f1f5f9;
    padding: 10px 12px;
    font-weight: 600;
    font-size: 12px;
    color: #64748b;
    min-height: 36px;
    text-transform: none;
}
QHeaderView::section:last {
    border-right: none;
}
QTableCornerButton::section {
    background: #f8fafc;
    border: none;
    border-bottom: 1px solid #e8eef5;
}

/* ========== Settings tabs ========== */
QTabWidget::pane {
    border: 1px solid #e8eef5;
    border-radius: 12px;
    background: #ffffff;
    top: 0px;
    padding: 16px;
}
QTabBar {
    qproperty-expanding: false;
    qproperty-drawBase: 0;
}
QTabBar::tab {
    background: transparent;
    color: #64748b;
    padding: 12px 16px 10px 16px;
    margin-right: 4px;
    border: none;
    border-bottom: 2px solid transparent;
    min-height: 0;
    font-size: 14px;
    font-weight: 500;
}
QTabBar::tab:selected {
    color: #0f172a;
    font-weight: 600;
    border-bottom: 2px solid #2563eb;
    background: transparent;
}
QTabBar::tab:hover {
    color: #0f172a;
}
QGroupBox {
    border: 1px solid #e8eef5;
    border-radius: 8px;
    margin-top: 16px;
    padding: 16px 12px 12px 12px;
    background: #ffffff;
    font-size: 14px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 4px;
    color: #475569;
    font-weight: 600;
}
QCheckBox {
    spacing: 8px;
    color: #0f172a;
    font-size: 14px;
    min-height: 24px;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid #cbd5e1;
    border-radius: 4px;
    background: #ffffff;
}
QCheckBox::indicator:checked {
    background: #2563eb;
    border-color: #2563eb;
}
QScrollArea {
    border: none;
    background: transparent;
}
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: #dbe3ef;
    border-radius: 5px;
    min-height: 32px;
}
QScrollBar::handle:vertical:hover {
    background: #cbd5e1;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar:horizontal {
    background: transparent;
    height: 10px;
}
QScrollBar::handle:horizontal {
    background: #dbe3ef;
    border-radius: 5px;
    min-width: 32px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0;
}
QMenu {
    background: #ffffff;
    border: 1px solid #e8eef5;
    border-radius: 8px;
    padding: 4px;
    font-size: 14px;
}
QMenu::item {
    padding: 8px 16px;
    border-radius: 6px;
    min-height: 28px;
}
QMenu::item:selected {
    background: #eff6ff;
    color: #0f172a;
}
QMenu::separator {
    height: 1px;
    background: #e8eef5;
    margin: 4px 8px;
}
"""
