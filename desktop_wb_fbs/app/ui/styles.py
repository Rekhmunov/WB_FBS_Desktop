# -*- coding: utf-8 -*-
"""QSS aligned with web WB FBS block — readable desktop sizing (16px body, 4px grid)."""

APP_QSS = """
/* --- Base: readable on Windows 10/11 at 100–150% DPI --- */
QWidget {
    font-family: "Segoe UI", "Tahoma", sans-serif;
    font-size: 15px;
    color: #0f1f33;
}
QMainWindow, QDialog {
    background: #f7fbff;
}
QDialog {
    min-width: 420px;
}
QStatusBar {
    background: #edf5ff;
    color: #5f7691;
    border-top: 1px solid #d9e8f7;
    font-size: 13px;
    min-height: 28px;
}
QMessageBox {
    font-size: 15px;
}
QMessageBox QLabel {
    font-size: 15px;
    min-width: 280px;
}

/* --- Top chrome --- */
QFrame#topBar {
    background: #ffffff;
    border-bottom: 1px solid #d9e8f7;
    min-height: 56px;
}
QLabel#brandTitle {
    color: #0f172a;
    font-size: 16px;
    font-weight: 700;
    padding: 0 4px 0 0;
}
QLabel#brandSub {
    color: #5f7691;
    font-size: 12px;
}
QPushButton#navBtn {
    background: transparent;
    color: #5f7691;
    border: none;
    border-bottom: 2px solid transparent;
    border-radius: 0;
    padding: 12px 16px;
    min-height: 44px;
    font-size: 15px;
    font-weight: 500;
    text-align: center;
}
QPushButton#navBtn:hover {
    color: #0f172a;
    background: transparent;
}
QPushButton#navBtn:checked {
    color: #0f172a;
    font-weight: 700;
    border-bottom: 2px solid #2495ee;
    background: transparent;
}

/* --- Buttons: tap-friendly, clear hierarchy --- */
QPushButton {
    color: #dbeafe;
    border: 1px solid rgba(37, 99, 235, 0.9);
    border-radius: 8px;
    background: #2563eb;
    padding: 8px 16px;
    min-height: 40px;
    font-size: 15px;
    font-weight: 600;
}
QPushButton:hover {
    background: #1d4ed8;
    border-color: #1d4ed8;
}
QPushButton:pressed {
    background: #1e40af;
}
QPushButton:disabled {
    opacity: 0.45;
    color: #dbeafe;
}
QPushButton#secondary, QPushButton[class="secondary"] {
    color: #1e40af;
    border: 1px solid rgba(147, 197, 253, 0.7);
    background: #ffffff;
}
QPushButton#secondary:hover, QPushButton[class="secondary"]:hover {
    border-color: rgba(59, 130, 246, 0.72);
    background: #eff6ff;
}
QPushButton#secondary:pressed, QPushButton[class="secondary"]:pressed {
    background: #e0f2fe;
}
QPushButton#danger, QPushButton[class="danger"] {
    color: #b91c1c;
    border: 1px solid #fca5a5;
    background: #ffffff;
}
QPushButton#danger:hover, QPushButton[class="danger"]:hover {
    background: #fef2f2;
    border-color: #f87171;
}
QPushButton#iconBtn {
    min-width: 40px;
    max-width: 44px;
    min-height: 40px;
    padding: 0;
    color: #1e40af;
    border: 1px solid rgba(147, 197, 253, 0.7);
    background: #ffffff;
    border-radius: 8px;
    font-size: 16px;
}
QPushButton#iconBtn:hover {
    background: #eff6ff;
}
QPushButton#mgtBtn {
    color: #ffffff;
    border: 1px solid #9810fa;
    background: #9810fa;
    border-radius: 8px;
    min-height: 40px;
    padding: 8px 16px;
    font-size: 15px;
    font-weight: 600;
}
QPushButton#mgtBtn:hover {
    background: #8609e0;
    border-color: #8609e0;
    color: #ffffff;
}
QPushButton#mgtBtn:pressed {
    background: #7208c0;
    border-color: #7208c0;
}
QPushButton#bottomPrimary {
    color: #0f172a;
    border: 1px solid #d7e0ef;
    border-radius: 8px;
    background: #eef2f7;
    min-height: 40px;
    padding: 8px 16px;
    font-size: 15px;
    font-weight: 600;
}
QPushButton#bottomPrimary:hover {
    background: #e2e8f0;
    border-color: #cbd5e1;
}
QPushButton#bottomPrimary:pressed {
    background: #dbe3ee;
}
QPushButton#tabBtn {
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    border-radius: 0;
    padding: 10px 14px;
    min-height: 44px;
    color: #64748b;
    font-size: 15px;
    font-weight: 500;
    text-align: left;
}
QPushButton#tabBtn:hover {
    color: #0f172a;
    background: transparent;
}
QPushButton#tabBtn:checked {
    color: #0f172a;
    font-weight: 700;
    border-bottom: 2px solid #2495ee;
    background: transparent;
}
QPushButton#linkBtn {
    background: transparent;
    border: none;
    color: #0f172a;
    font-size: 15px;
    font-weight: 600;
    min-height: 0;
    padding: 4px 0;
    text-decoration: underline;
}
QPushButton#linkBtn:hover {
    background: transparent;
    color: #2495ee;
}
QToolButton {
    color: #1e40af;
    border: 1px solid rgba(147, 197, 253, 0.7);
    background: #ffffff;
    border-radius: 8px;
    padding: 8px 12px;
    min-height: 40px;
    min-width: 40px;
    font-size: 15px;
    font-weight: 600;
}
QToolButton:hover {
    border-color: rgba(59, 130, 246, 0.72);
    background: #eff6ff;
}
QToolButton#secondary {
    color: #1e40af;
    border: 1px solid rgba(147, 197, 253, 0.7);
    background: #ffffff;
}
QToolButton#dangerToolBtn {
    color: #b91c1c;
    border: 1px solid #fca5a5;
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
    border-color: #f87171;
}
QToolButton#dangerToolBtn:pressed {
    background: #fee2e2;
}
QToolButton#dangerToolBtn:focus {
    border-color: #ef4444;
}
QToolButton#dangerToolBtn:disabled {
    color: #fca5a5;
    border-color: #fecaca;
    background: #ffffff;
}
QPushButton#filterChip {
    color: #1e40af;
    border: 1px solid rgba(147, 197, 253, 0.7);
    background: #ffffff;
    border-radius: 999px;
    padding: 6px 14px;
    min-height: 32px;
    font-size: 13px;
    font-weight: 600;
}
QPushButton#filterChip:hover {
    background: #eff6ff;
    border-color: rgba(59, 130, 246, 0.72);
}
QPushButton#filterChip:pressed {
    background: #dbeafe;
}
QPushButton#filterChip:checked {
    color: #ffffff;
    background: #2563eb;
    border-color: #2563eb;
}
QPushButton#filterChip:checked:hover {
    background: #1d4ed8;
    border-color: #1d4ed8;
}
QPushButton#filterChip:focus {
    border-color: #2563eb;
}
QPushButton#filterChip:disabled {
    color: #94a3b8;
    border-color: #e2e8f0;
    background: #f8fafc;
}
QDialogButtonBox {
    button-layout: 0;
}
QDialogButtonBox QPushButton {
    min-width: 100px;
    min-height: 40px;
    padding: 8px 16px;
}

/* --- Inputs --- */
QLineEdit, QComboBox, QSpinBox, QTextEdit, QPlainTextEdit {
    background: #ffffff;
    border: 1px solid #d7e0ef;
    border-radius: 8px;
    padding: 8px 12px;
    min-height: 40px;
    color: #0f172a;
    font-size: 15px;
    selection-background-color: #ddf1ff;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QTextEdit:focus {
    border: 1px solid rgba(37, 99, 235, 0.74);
}
QComboBox::drop-down {
    border: none;
    width: 28px;
}
QComboBox QAbstractItemView {
    background: #ffffff;
    border: 1px solid #d7e0ef;
    selection-background-color: #ddf1ff;
    selection-color: #0f172a;
    font-size: 15px;
    padding: 4px;
}
QFormLayout {
    spacing: 12px;
}
QLabel#fieldLabel {
    font-size: 14px;
    font-weight: 600;
    color: #334155;
}

/* --- Panels / section --- */
QFrame#toolbarPanel {
    background: #ffffff;
    border: 1px solid #d9e8f7;
    border-radius: 12px;
}
QFrame#syncInfo {
    background: #f8fafc;
    border: 1px solid #e2e8f0;
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
    font-size: 22px;
    font-weight: 700;
    color: #0f172a;
    letter-spacing: -0.01em;
    padding: 0;
}
QLabel#syncInfoText {
    font-size: 15px;
    font-weight: 600;
    color: #475569;
}
QFrame#syncInfo[state="ok"] QLabel#syncInfoText {
    color: #166534;
}
QFrame#syncInfo[state="error"] QLabel#syncInfoText {
    color: #b91c1c;
}
QLabel#syncPallets {
    font-size: 15px;
    font-weight: 500;
    color: inherit;
}
QFrame#tabsRow {
    border-bottom: 1px solid #e2e8f0;
    background: transparent;
    min-height: 48px;
}
QFrame#bottomBar {
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 12px;
    min-height: 56px;
}
QLabel#selectedLabel {
    font-size: 15px;
    font-weight: 600;
    color: #0f172a;
}
QLabel#hint, QLabel[class="hint"] {
    color: #64748b;
    font-size: 14px;
}
QLabel#dialogTitle {
    font-size: 20px;
    font-weight: 700;
    color: #0f172a;
}
QLabel#sdTitle {
    font-size: 22px;
    font-weight: 700;
    color: #0f172a;
}
QLabel#sdMeta {
    color: #475569;
    font-size: 14px;
}
QLabel#sdChip {
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    padding: 6px 12px;
    font-size: 13px;
    font-weight: 600;
    color: #334155;
    min-height: 28px;
}
QFrame#sdHeader {
    background: #ffffff;
    border-bottom: 1px solid #e2e8f0;
}
QFrame#dialogBody {
    background: #f7fbff;
}

/* --- Tables --- */
QTableWidget {
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    gridline-color: #f1f5f9;
    selection-background-color: rgba(239, 246, 255, 0.92);
    selection-color: #0f172a;
    alternate-background-color: #fafbfd;
    font-size: 15px;
}
QTableWidget::item {
    padding: 10px 12px;
}
QHeaderView::section {
    background: #f4f8ff;
    border: none;
    border-bottom: 1px solid #e2e8f0;
    border-right: 1px solid #eef2f7;
    padding: 10px 12px;
    font-weight: 600;
    font-size: 13px;
    color: #334155;
    min-height: 36px;
}

/* --- Settings tabs --- */
QTabWidget::pane {
    border: 1px solid #d9e8f7;
    border-radius: 12px;
    background: #ffffff;
    top: -1px;
    padding: 12px;
}
QTabBar::tab {
    background: transparent;
    color: #64748b;
    padding: 10px 16px;
    margin-right: 4px;
    border: none;
    border-bottom: 2px solid transparent;
    min-height: 40px;
    font-size: 15px;
    font-weight: 500;
}
QTabBar::tab:selected {
    color: #0f172a;
    font-weight: 700;
    border-bottom: 2px solid #2495ee;
    background: transparent;
}
QTabBar::tab:hover {
    color: #0f172a;
}
QGroupBox {
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    margin-top: 16px;
    padding: 16px 12px 12px 12px;
    background: #ffffff;
    font-size: 15px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 4px;
    color: #334155;
    font-weight: 600;
}
QCheckBox {
    spacing: 8px;
    color: #0f172a;
    font-size: 15px;
    min-height: 24px;
}
QScrollArea {
    border: none;
    background: transparent;
}
QMenu {
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    padding: 4px;
    font-size: 15px;
}
QMenu::item {
    padding: 8px 16px;
    border-radius: 4px;
    min-height: 28px;
}
QMenu::item:selected {
    background: #eff6ff;
    color: #0f172a;
}
"""
