# -*- coding: utf-8 -*-
"""Desktop UI stylesheet — web-archive SaaS look (Inter + soft blue tokens)."""

_APP_QSS_TEMPLATE = """
/* --- Design tokens (web archive parity) --- */
/* Primary: #3ba4f7  Soft: #ddf1ff  Canvas: #f7fbff  Line: #d9e8f7  Text: #0f1f33 */

QWidget {
    font-family: __FONT_BODY__;
    font-size: 14px;
    color: #0f1f33;
}
QMainWindow, QDialog {
    background: #f7fbff;
}
QDialog {
    min-width: 440px;
}
QStatusBar {
    background: #ffffff;
    color: #5f7691;
    border-top: 1px solid #d9e8f7;
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
    color: #5f7691;
}

/* ========== Top navigation ========== */
QFrame#topBar {
    background: #ffffff;
    border-bottom: 1px solid #d9e8f7;
    min-height: 72px;
}
QLabel#brandTitle {
    font-family: __FONT_DISPLAY__;
    color: #0f1f33;
    font-size: 16px;
    font-weight: 700;
    letter-spacing: -0.02em;
    padding: 0;
}
QLabel#brandSub {
    color: #8aa0b8;
    font-size: 11px;
    letter-spacing: 0.02em;
    padding: 0;
}
QPushButton#navBtn {
    background: transparent;
    color: #5f7691;
    border: none;
    border-bottom: 3px solid transparent;
    border-radius: 0;
    padding: 12px 24px 14px 24px;
    min-height: 52px;
    font-size: 14px;
    font-weight: 600;
    text-align: center;
}
QPushButton#navBtn:hover {
    color: #0f1f33;
    background: rgba(59, 164, 247, 0.06);
}
QPushButton#navBtn:checked {
    color: #0f1f33;
    border-bottom: 3px solid #2495ee;
    padding: 12px 24px 11px 24px;
    background: transparent;
}
QPushButton#navBtn:pressed {
    background: rgba(59, 164, 247, 0.1);
    color: #0f1f33;
}

/* ========== Buttons ========== */
QPushButton {
    color: #ffffff;
    border: 1px solid #3ba4f7;
    border-radius: 10px;
    background: #3ba4f7;
    padding: 0 16px;
    min-height: 36px;
    font-size: 14px;
    font-weight: 600;
}
QPushButton:hover {
    background: #2495ee;
    border-color: #2495ee;
}
QPushButton:pressed {
    background: #1687e1;
    border-color: #1687e1;
}
QPushButton:disabled {
    background: #b7c9db;
    border-color: #b7c9db;
    color: #f2f8ff;
}
QPushButton#secondary, QPushButton[class="secondary"] {
    color: #0f1f33;
    border: 1px solid #d9e8f7;
    background: #ffffff;
}
QPushButton#secondary:hover, QPushButton[class="secondary"]:hover {
    border-color: #79c3ff;
    background: #ddf1ff;
    color: #0f1f33;
}
QPushButton#secondary:pressed, QPushButton[class="secondary"]:pressed {
    background: #cfe8ff;
    border-color: #3ba4f7;
}
QPushButton#secondary:disabled, QPushButton[class="secondary"]:disabled {
    color: #8aa0b8;
    border-color: #d9e8f7;
    background: #f2f8ff;
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
    color: #5f7691;
    border: 1px solid #d9e8f7;
    background: #ffffff;
    border-radius: 10px;
    font-size: 14px;
    font-weight: 600;
}
QPushButton#iconBtn:hover {
    background: #f2f8ff;
    color: #0f1f33;
    border-color: #c2d9f2;
}
QPushButton#mgtBtn {
    color: #ffffff;
    border: 1px solid #9810fa;
    background: #9810fa;
    border-radius: 10px;
    min-height: 36px;
    padding: 0 16px;
    font-size: 14px;
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
    color: #ffffff;
}
QPushButton#mgtBtn:disabled {
    opacity: 0.55;
}
QPushButton#bottomPrimary {
    color: #ffffff;
    border: 1px solid #3ba4f7;
    border-radius: 10px;
    background: #3ba4f7;
    min-height: 36px;
    padding: 0 16px;
    font-size: 14px;
    font-weight: 600;
}
QPushButton#bottomPrimary:hover {
    background: #2495ee;
    border-color: #2495ee;
}
QPushButton#bottomPrimary:pressed {
    background: #1687e1;
}
QPushButton#tabBtn {
    background: transparent;
    border: none;
    border-bottom: 3px solid transparent;
    border-radius: 0;
    padding: 0;
    min-height: 44px;
    color: #5f7691;
    font-size: 14px;
    font-weight: 500;
    text-align: left;
}
QPushButton#tabBtn:hover {
    color: #0f1f33;
    background: rgba(59, 164, 247, 0.06);
}
QPushButton#tabBtn:checked {
    color: #0f1f33;
    font-weight: 700;
    border-bottom: 3px solid #2495ee;
    background: #ddf1ff;
}
QPushButton#tabBtn:pressed {
    background: transparent;
}
QPushButton#tabBtn QLabel#tabBtnLabel {
    color: #5f7691;
    font-size: 14px;
    font-weight: 500;
    background: transparent;
    border: none;
}
QPushButton#tabBtn:checked QLabel#tabBtnLabel {
    color: #0f1f33;
    font-weight: 700;
}
QLabel#tabCount {
    min-width: 26px;
    min-height: 20px;
    padding: 0 8px;
    border-radius: 6px;
    background: #e8eef7;
    color: #5f7691;
    font-size: 12px;
    font-weight: 600;
}
QPushButton#tabBtn:checked QLabel#tabCount {
    background: #ddf1ff;
    color: #2495ee;
}
QPushButton#linkBtn {
    background: transparent;
    border: none;
    color: #3ba4f7;
    font-size: 13px;
    font-weight: 600;
    min-height: 0;
    padding: 4px 0;
}
QPushButton#linkBtn:hover {
    background: transparent;
    color: #2495ee;
    text-decoration: underline;
}
QPushButton#pagerBtn {
    color: #2495ee;
    border: 1px solid #d9e8f7;
    background: #ffffff;
    border-radius: 10px;
    min-height: 32px;
    min-width: 0;
    max-width: 16777215;
    padding: 0 12px;
    font-size: 13px;
    font-weight: 600;
}
QPushButton#pagerBtn:hover {
    background: #ddf1ff;
    border-color: #79c3ff;
}
QPushButton#pagerBtn:disabled {
    color: #8aa0b8;
    border-color: #d9e8f7;
    background: #f2f8ff;
}
QToolButton {
    color: #5f7691;
    border: 1px solid #d9e8f7;
    background: #ffffff;
    border-radius: 10px;
    padding: 0 12px;
    min-height: 36px;
    min-width: 36px;
    font-size: 14px;
    font-weight: 600;
}
QToolButton:hover {
    border-color: #8aa0b8;
    background: #eaf4ff;
    color: #0f1f33;
}
QToolButton#secondary {
    color: #5f7691;
    border: 1px solid #d9e8f7;
    background: #ffffff;
}
QToolButton#secondary:hover {
    border-color: #8aa0b8;
    background: #eaf4ff;
    color: #0f1f33;
}
QToolButton#secondary:pressed {
    background: #d9e8f7;
    border-color: #5f7691;
}
QToolButton#dangerToolBtn {
    color: #b91c1c;
    border: 1px solid #fecaca;
    background: #ffffff;
    border-radius: 10px;
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
    color: #5f7691;
    border: 1px solid #d9e8f7;
    background: #ffffff;
    border-radius: 999px;
    padding: 0 14px;
    min-height: 32px;
    font-size: 13px;
    font-weight: 600;
}
QPushButton#filterChip:hover {
    background: #f2f8ff;
    border-color: #c2d9f2;
}
QPushButton#filterChip:checked {
    color: #ffffff;
    background: #3ba4f7;
    border-color: #3ba4f7;
}
QDialogButtonBox QPushButton {
    min-width: 100px;
    min-height: 36px;
    padding: 0 16px;
}

/* ========== Inputs — kill native XP look ========== */
QLineEdit, QComboBox, QSpinBox, QTextEdit, QPlainTextEdit {
    background: #ffffff;
    border: 1px solid #d9e8f7;
    border-radius: 10px;
    padding: 0 12px;
    min-height: 36px;
    color: #0f1f33;
    font-size: 14px;
    selection-background-color: #ddf1ff;
    selection-color: #0f1f33;
}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover {
    border-color: #79c3ff;
    background: #ffffff;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QTextEdit:focus {
    border: 1px solid #3ba4f7;
    background: #ffffff;
}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {
    background: #f2f8ff;
    color: #8aa0b8;
    border-color: #d9e8f7;
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
    border-top: 5px solid #5f7691;
    margin-right: 10px;
}
QComboBox QAbstractItemView {
    background: #ffffff;
    border: 1px solid #d9e8f7;
    border-radius: 10px;
    outline: none;
    selection-background-color: #ddf1ff;
    selection-color: #0f1f33;
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
    min-height: 36px;
    background: #ffffff;
    border: 1px solid #d9e8f7;
    border-radius: 10px;
    padding-left: 12px;
    font-weight: 500;
}
QFrame#topBar QComboBox#sourceCombo {
    margin: 0;
}
QSpinBox {
    padding-right: 28px;
}
QSpinBox::up-button, QSpinBox::down-button {
    subcontrol-origin: border;
    width: 22px;
    border: none;
    background: transparent;
    border-left: 1px solid #d9e8f7;
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
    background: #f2f8ff;
}
QSpinBox::up-arrow {
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-bottom: 5px solid #5f7691;
}
QSpinBox::down-arrow {
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #5f7691;
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
    color: #5f7691;
}

/* ========== Panels ========== */
QFrame#toolbarPanel {
    background: #ffffff;
    border: 1px solid #d9e8f7;
    border-radius: 14px;
}
QFrame#syncInfo {
    background: #f2f8ff;
    border: 1px solid #d9e8f7;
    border-radius: 12px;
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
    font-family: __FONT_DISPLAY__;
    font-size: 20px;
    font-weight: 700;
    color: #0f1f33;
    letter-spacing: -0.02em;
    padding: 0;
}
QLabel#syncInfoText {
    font-size: 13px;
    font-weight: 500;
    color: #5f7691;
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
    border-bottom: 1px solid #d9e8f7;
    background: transparent;
    min-height: 56px;
}
QFrame#bottomBar {
    background: #ffffff;
    border: 1px solid #d9e8f7;
    border-radius: 14px;
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
    color: #0f1f33;
}
QLabel#hint, QLabel[class="hint"] {
    color: #5f7691;
    font-size: 13px;
}
QLabel#pageMeta {
    color: #0f1f33;
    font-size: 13px;
    font-weight: 600;
    padding: 0 4px;
    min-width: 56px;
}
QLabel#pagerTotal {
    color: #5f7691;
    font-size: 13px;
    font-weight: 500;
}
QLabel#dialogTitle {
    font-size: 18px;
    font-weight: 700;
    color: #0f1f33;
}
QLabel#sdTitle {
    font-size: 24px;
    font-weight: 700;
    color: #0f1f33;
    line-height: 1.25;
}
QLabel#sdMeta {
    color: #5f7691;
    font-size: 13px;
}
QLabel#sdWarehouse {
    color: #5f7691;
    font-size: 14px;
    padding-left: 2px;
}
QLabel#sdLoadStatus {
    color: #2495ee;
    font-size: 13px;
    font-weight: 600;
    background: #ddf1ff;
    border: 1px solid #b3ddff;
    border-radius: 10px;
    padding: 8px 12px;
}
QLabel#sdChip {
    background: #f2f8ff;
    border: 1px solid #d9e8f7;
    border-radius: 10px;
    padding: 4px 10px;
    font-size: 13px;
    font-weight: 600;
    color: #5f7691;
    min-height: 28px;
}
QFrame#sdChipQr {
    background: #f2f8ff;
    border: 1px solid #d9e8f7;
    border-radius: 10px;
    min-height: 28px;
}
QLabel#sdChipQrText {
    font-size: 13px;
    font-weight: 600;
    color: #5f7691;
}
QToolButton#sdQrPrint {
    border: none;
    background: transparent;
    color: #5f7691;
    min-width: 28px;
    max-width: 28px;
    min-height: 28px;
    padding: 0;
    font-size: 14px;
}
QToolButton#sdQrPrint:hover {
    background: #d9e8f7;
    border-radius: 6px;
    color: #0f1f33;
}
QFrame#sdHeader {
    background: #ffffff;
    border-bottom: 1px solid #d9e8f7;
}
QFrame#sdBody {
    background: #ffffff;
}
QLineEdit#sdSearch {
    min-height: 36px;
    max-width: 280px;
    border-radius: 10px;
    padding: 0 12px;
    background: #f2f8ff;
    border: 1px solid #d9e8f7;
}
QPushButton#portalBtn {
    color: #6b21a8;
    border: 1px solid rgba(147, 51, 234, 0.35);
    background: #f3e8ff;
    border-radius: 10px;
    padding: 0 14px;
    min-height: 40px;
    font-size: 14px;
    font-weight: 600;
}
QPushButton#portalBtn:hover {
    background: #e9d5ff;
    border-color: #a855f7;
    color: #581c87;
}
QPushButton#portalBtn:pressed {
    background: #d8b4fe;
    border-color: #9333ea;
}
QPushButton[waitOrders="true"],
QToolButton[waitOrders="true"],
QCheckBox[waitOrders="true"],
QLineEdit[waitOrders="true"] {
    opacity: 0.55;
}
QPushButton#secondary:disabled,
QToolButton#splitCaret:disabled,
QToolButton#secondary:disabled {
    color: #8aa0b8;
    border-color: #d9e8f7;
    background: #f2f8ff;
}
QFrame#sdHeader QPushButton#secondary,
QFrame#sdHeader QPushButton#portalBtn {
    min-height: 40px;
    padding: 0 14px;
}
/* Supply-detail action row: clear hover (white header needs stronger contrast). */
QFrame#sdHeader QPushButton#secondary:hover,
QFrame#sdHeader QToolButton#secondary:hover,
QFrame#sdHeader QToolButton#splitCaret:hover {
    background: #ddf1ff;
    border-color: #79c3ff;
    color: #0b6cb8;
}
QFrame#sdHeader QToolButton#secondary:hover,
QFrame#sdHeader QToolButton#splitCaret:hover {
    border-left: none;
}
QFrame#sdHeader QPushButton#secondary:pressed,
QFrame#sdHeader QToolButton#secondary:pressed,
QFrame#sdHeader QToolButton#splitCaret:pressed {
    background: #ddf1ff;
    border-color: #2495ee;
    color: #0b6cb8;
}
QFrame#sdHeader QToolButton#secondary:pressed,
QFrame#sdHeader QToolButton#splitCaret:pressed {
    border-left: none;
}
QFrame#sdHeader QPushButton#iconBtn:hover {
    background: #ddf1ff;
    border-color: #79c3ff;
    color: #0b6cb8;
}
QFrame#sdHeader QPushButton#iconBtn:pressed {
    background: #ddf1ff;
    border-color: #2495ee;
}
QFrame#sdHeader QPushButton#portalBtn:hover {
    background: #e9d5ff;
    border-color: #a855f7;
    color: #581c87;
}
QWidget#splitPair,
QWidget#kizSplitPair,
QWidget#pickSplitPair {
    min-height: 40px;
}
QWidget#splitPair QPushButton#secondary,
QWidget#kizSplitPair QPushButton#secondary,
QWidget#pickSplitPair QPushButton#secondary {
    border-top-right-radius: 0;
    border-bottom-right-radius: 0;
    min-height: 40px;
    max-height: 40px;
    padding: 0 14px;
}
QToolButton#splitCaret {
    color: #5f7691;
    border: 1px solid #d9e8f7;
    border-left: none;
    background: #ffffff;
    border-top-left-radius: 0;
    border-bottom-left-radius: 0;
    border-top-right-radius: 8px;
    border-bottom-right-radius: 8px;
    min-width: 36px;
    max-width: 36px;
    min-height: 40px;
    max-height: 40px;
    padding: 0;
}
QToolButton#splitCaret::menu-indicator {
    image: none;
    width: 0px;
    height: 0px;
    border: none;
}
QToolButton#splitCaret:hover {
    background: #ddf1ff;
    border-color: #79c3ff;
    border-left: none;
    color: #0b6cb8;
}
QToolButton#splitCaret:pressed {
    background: #ddf1ff;
    border-color: #2495ee;
    border-left: none;
}
QWidget#splitPair QToolButton#secondary,
QWidget#kizSplitPair QToolButton#secondary,
QWidget#pickSplitPair QToolButton#secondary {
    border-left: none;
    border-top-left-radius: 0;
    border-bottom-left-radius: 0;
    border-top-right-radius: 8px;
    border-bottom-right-radius: 8px;
    min-width: 40px;
    max-width: 40px;
    min-height: 40px;
    max-height: 40px;
    padding: 0;
}
/* КИЗ refresh tone — web `.wb-fbs-kiz-split.is-ok` / `.is-error` */
QWidget#kizSplitPair[kizTone="ok"] QPushButton#secondary,
QWidget#kizSplitPair[kizTone="ok"] QToolButton#secondary {
    color: #166534;
    border-color: #86efac;
    background: #dcfce7;
}
QWidget#kizSplitPair[kizTone="ok"] QPushButton#secondary:hover:enabled,
QWidget#kizSplitPair[kizTone="ok"] QToolButton#secondary:hover:enabled {
    background: #bbf7d0;
    border-color: #4ade80;
}
QWidget#kizSplitPair[kizTone="ok"] QPushButton#secondary:pressed:enabled,
QWidget#kizSplitPair[kizTone="ok"] QToolButton#secondary:pressed:enabled {
    background: #86efac;
}
QWidget#kizSplitPair[kizTone="error"] QPushButton#secondary,
QWidget#kizSplitPair[kizTone="error"] QToolButton#secondary {
    color: #991b1b;
    border-color: #fca5a5;
    background: #fee2e2;
}
QWidget#kizSplitPair[kizTone="error"] QPushButton#secondary:hover:enabled,
QWidget#kizSplitPair[kizTone="error"] QToolButton#secondary:hover:enabled {
    background: #fecaca;
    border-color: #f87171;
}
QWidget#kizSplitPair[kizTone="error"] QPushButton#secondary:pressed:enabled,
QWidget#kizSplitPair[kizTone="error"] QToolButton#secondary:pressed:enabled {
    background: #fca5a5;
}
QWidget#kizSplitPair[kizTone="ok"] QToolButton#secondary,
QWidget#kizSplitPair[kizTone="error"] QToolButton#secondary {
    border-left: 1px solid rgba(15, 23, 42, 0.12);
}
/* Pick (без маркировки) — green only when all stickers + ШК complete */
QWidget#pickSplitPair[pickTone="ok"] QPushButton#secondary,
QWidget#pickSplitPair[pickTone="ok"] QToolButton#secondary {
    color: #166534;
    border-color: #86efac;
    background: #dcfce7;
}
QWidget#pickSplitPair[pickTone="ok"] QPushButton#secondary:hover:enabled,
QWidget#pickSplitPair[pickTone="ok"] QToolButton#secondary:hover:enabled {
    background: #bbf7d0;
    border-color: #4ade80;
}
QWidget#pickSplitPair[pickTone="ok"] QPushButton#secondary:pressed:enabled,
QWidget#pickSplitPair[pickTone="ok"] QToolButton#secondary:pressed:enabled {
    background: #86efac;
}
QWidget#pickSplitPair[pickTone="ok"] QToolButton#secondary {
    border-left: 1px solid rgba(15, 23, 42, 0.12);
}
QTableWidget#sdTable {
    border: none;
    border-radius: 0;
    background: #ffffff;
}
QTableWidget#sdTable::item {
    padding: 0;
    border-bottom: 1px solid #f2f8ff;
}
QLabel#sdOrderId {
    font-size: 14px;
    font-weight: 700;
    color: #0f1f33;
}
QLabel#sdOrderMeta {
    font-size: 12px;
    color: #5f7691;
}
QLabel#sdSticker {
    color: #0f1f33;
}
QLabel#sdProductName {
    font-size: 14px;
    font-weight: 700;
    color: #0f1f33;
}
QLabel#sdProductSub {
    font-size: 12px;
    color: #5f7691;
}
QLabel#sdBarcode {
    font-size: 22px;
    font-weight: 700;
    color: #0f1f33;
    letter-spacing: 0.02em;
    min-height: 28px;
    padding: 0;
    margin: 0;
}
QLabel#sdKizBadge {
    padding: 2px 8px;
    border-radius: 6px;
    font-size: 11px;
    font-weight: 700;
    background: #f2f8ff;
    color: #5f7691;
    max-width: 96px;
}
QLabel#sdKizBadge[kizState="pending"] {
    background: #fef3c7;
    color: #92400e;
}
QLabel#sdKizBadge[kizState="ok"] {
    background: #dcfce7;
    color: #166534;
}
QLabel#sdKizBadge[kizState="error"] {
    background: #fee2e2;
    color: #b91c1c;
}
QLabel#sdKizBadge[kizState="empty"] {
    background: #f2f8ff;
    color: #8aa0b8;
}
QFrame#dialogBody {
    background: #f7fbff;
}

/* ========== Tables ========== */
QTableWidget {
    background: #ffffff;
    border: 1px solid #d9e8f7;
    border-radius: 12px;
    gridline-color: transparent;
    selection-background-color: #ddf1ff;
    selection-color: #0f1f33;
    alternate-background-color: #fafbfc;
    font-size: 14px;
    outline: none;
}
QTableWidget::item {
    padding: 10px 12px;
    border-bottom: 1px solid #f2f8ff;
}
QTableWidget QWidget {
    background: transparent;
}
QTableWidget::item:selected {
    background: #ddf1ff;
    color: #0f1f33;
}
QHeaderView::section {
    background: #f2f8ff;
    border: none;
    border-bottom: 1px solid #d9e8f7;
    border-right: 1px solid #d9e8f7;
    padding: 12px 12px;
    font-weight: 700;
    font-size: 11px;
    letter-spacing: 0.04em;
    color: #5f7691;
    min-height: 40px;
    text-transform: uppercase;
}
QHeaderView::section:last {
    border-right: none;
}
QTableCornerButton::section {
    background: #eef4fb;
    border: none;
    border-bottom: 1px solid #d9e8f7;
}
QLabel#supplyLink {
    color: #2495ee;
    font-size: 16px;
    font-weight: 700;
    background: transparent;
}
QLabel#supplyMeta {
    color: #5f7691;
    font-size: 12px;
    background: transparent;
}
QLabel#supplyQr {
    color: #0f1f33;
    font-size: 14px;
    font-weight: 600;
    background: transparent;
}
QLabel#supplyOrders {
    color: #0f1f33;
    font-size: 16px;
    font-weight: 700;
    background: transparent;
}
QLabel#whName {
    color: #0f1f33;
    font-size: 14px;
    font-weight: 700;
    background: transparent;
}
QLabel#orderIdLabel {
    color: #0f1f33;
    font-size: 16px;
    font-weight: 700;
    background: transparent;
}
QLabel#productName {
    color: #0f1f33;
    font-size: 14px;
    font-weight: 600;
    background: transparent;
}
QLabel#productSub {
    color: #5f7691;
    font-size: 12px;
    background: transparent;
}
QLabel#barcodeLine {
    color: #5f7691;
    font-size: 12px;
    font-family: "Cascadia Mono", "Consolas", "Courier New", monospace;
    background: transparent;
}

/* ========== Settings tabs ========== */
QTabWidget::pane {
    border: 1px solid #d9e8f7;
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
    color: #5f7691;
    padding: 12px 16px 10px 16px;
    margin-right: 4px;
    border: none;
    border-bottom: 2px solid transparent;
    min-height: 0;
    font-size: 14px;
    font-weight: 500;
}
QTabBar::tab:selected {
    color: #0f1f33;
    font-weight: 600;
    border-bottom: 2px solid #3ba4f7;
    background: transparent;
}
QTabBar::tab:hover {
    color: #0f1f33;
}
QGroupBox {
    border: 1px solid #d9e8f7;
    border-radius: 10px;
    margin-top: 16px;
    padding: 16px 12px 12px 12px;
    background: #ffffff;
    font-size: 14px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 4px;
    color: #5f7691;
    font-weight: 600;
}
QCheckBox {
    spacing: 8px;
    color: #0f1f33;
    font-size: 14px;
    min-height: 24px;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid #c2d9f2;
    border-radius: 4px;
    background: #ffffff;
}
QCheckBox::indicator:checked {
    background: #3ba4f7;
    border-color: #3ba4f7;
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
    background: #d9e8f7;
    border-radius: 5px;
    min-height: 32px;
}
QScrollBar::handle:vertical:hover {
    background: #c2d9f2;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar:horizontal {
    background: transparent;
    height: 10px;
}
QScrollBar::handle:horizontal {
    background: #d9e8f7;
    border-radius: 5px;
    min-width: 32px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0;
}
QMenu {
    background-color: #ffffff;
    color: #0f1f33;
    border: 1px solid #d9e8f7;
    border-radius: 10px;
    padding: 4px;
    font-size: 14px;
}
QMenu::item {
    background-color: transparent;
    color: #0f1f33;
    padding: 8px 16px;
    border-radius: 6px;
    min-height: 28px;
}
QMenu::item:selected {
    background-color: #ddf1ff;
    color: #0f1f33;
}
QMenu::item:disabled {
    color: #8aa0b8;
}
QMenu::separator {
    height: 1px;
    background: #d9e8f7;
    margin: 4px 8px;
}
QToolTip {
    background-color: #ffffff;
    color: #0f1f33;
    border: 1px solid #d9e8f7;
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 13px;
}

/* ========== КИЗ marking modal (web parity) ========== */
QDialog#kizModal {
    background: #ffffff;
    min-width: 0;
}
QFrame#kizHeader {
    background: #ffffff;
    border-bottom: 1px solid #d9e8f7;
}
QLabel#kizTitle {
    color: #0f1f33;
    font-size: 28px;
    font-weight: 800;
    background: transparent;
    padding: 0;
    margin: 0;
}
QLabel#kizSub {
    color: #5f7691;
    font-size: 14px;
    background: transparent;
    padding: 0;
    margin: 0;
}
QFrame#kizHeader QPushButton#bottomPrimary {
    min-width: 0;
    max-width: 140px;
    min-height: 40px;
    max-height: 40px;
    padding: 8px 16px;
}
QToolButton#iconBtn {
    min-width: 36px;
    max-width: 36px;
    min-height: 36px;
    max-height: 36px;
    padding: 0;
    color: #5f7691;
    border: 1px solid #d9e8f7;
    background: #ffffff;
    border-radius: 10px;
    font-size: 16px;
    font-weight: 600;
}
QToolButton#iconBtn:hover {
    background: #f2f8ff;
    color: #0f1f33;
    border-color: #c2d9f2;
}
QToolButton#kizRowMenu {
    min-width: 32px;
    max-width: 32px;
    min-height: 32px;
    max-height: 32px;
    padding: 0;
    color: #5f7691;
    border: 1px solid #d9e8f7;
    background: #ffffff;
    border-radius: 10px;
    font-size: 16px;
    font-weight: 600;
}
QToolButton#kizRowMenu:hover {
    background: #f2f8ff;
    color: #0f1f33;
    border-color: #c2d9f2;
}
QToolButton#kizRowMenu::menu-indicator {
    image: none;
    width: 0;
}
QFrame#kizToolbar {
    background: #ffffff;
    border-bottom: 1px solid #d9e8f7;
}
QCheckBox#kizFilterCheck {
    color: #5f7691;
    font-size: 14px;
    spacing: 8px;
}
QLineEdit#kizSearch {
    min-height: 36px;
    max-width: 280px;
    border-radius: 10px;
    padding: 0 12px;
    background: #f2f8ff;
    border: 1px solid #d9e8f7;
    font-size: 14px;
}
QLineEdit#kizSearch:focus {
    border-color: #c2d9f2;
    background: #ffffff;
}
QLabel#kizScanCount {
    color: #0f1f33;
    font-size: 14px;
    font-weight: 600;
    background: transparent;
}
QFrame#kizScanBar {
    background: #f2f8ff;
    border-bottom: 1px solid #d9e8f7;
}
QLabel#kizScanLabel {
    color: #5f7691;
    font-size: 14px;
    font-weight: 600;
    background: transparent;
}
QLineEdit#kizScanInput {
    min-height: 40px;
    padding: 8px 12px;
    border: 1px solid #c2d9f2;
    border-radius: 10px;
    background: #ffffff;
    font-size: 16px;
}
QLineEdit#kizScanInput:focus {
    border-color: #79c3ff;
}
QToolButton#kizScanClear {
    min-width: 40px;
    max-width: 40px;
    min-height: 40px;
    max-height: 40px;
    padding: 0;
    color: #5f7691;
    border: 1px solid #c2d9f2;
    background: #ffffff;
    border-radius: 10px;
    font-size: 16px;
}
QToolButton#kizScanClear:hover {
    color: #0f1f33;
    border-color: #8aa0b8;
    background: #f2f8ff;
}
QFrame#kizInfo {
    background: #fef2f2;
    border-bottom: 1px solid #fecaca;
}
QFrame#kizInfo[state="ok"] {
    background: #dcfce7;
    border-bottom-color: #86efac;
}
QLabel#kizInfoText {
    color: #b91c1c;
    font-size: 14px;
    background: transparent;
}
QFrame#kizInfo[state="ok"] QLabel#kizInfoText {
    color: #166534;
}
QTableWidget#kizTable {
    border: none;
    border-radius: 0;
    background: #ffffff;
    alternate-background-color: #ffffff;
    selection-background-color: #ddf1ff;
}
QTableWidget#kizTable QHeaderView::section {
    background: #f2f8ff;
    color: #5f7691;
    font-size: 13px;
    font-weight: 600;
    text-transform: uppercase;
    border: none;
    border-bottom: 1px solid #d9e8f7;
    border-right: none;
    padding: 12px 16px;
}
QTableWidget#kizTable::item {
    padding: 0;
    border-bottom: 1px solid #d9e8f7;
}
QFrame#kizRowCell {
    background: transparent;
}
QFrame#kizRowCell[state="active"] {
    background: #ddf1ff;
}
QLabel#kizOrderId {
    color: #0f1f33;
    font-size: 14px;
    font-weight: 700;
    background: transparent;
}
QLabel#kizStickerHead {
    color: #0f1f33;
    font-size: 14px;
    font-weight: 700;
    background: transparent;
}
QLabel#kizStickerTail {
    color: #0f1f33;
    font-size: 20px;
    font-weight: 700;
    background: transparent;
}
QLabel#kizOrderDate {
    color: #5f7691;
    font-size: 13px;
    background: transparent;
}
QLabel#kizProductName {
    color: #0f1f33;
    font-size: 14px;
    font-weight: 600;
    background: transparent;
}
QLabel#kizProductSub {
    color: #5f7691;
    font-size: 13px;
    background: transparent;
}
QLabel#kizBarcode {
    color: #0f1f33;
    font-size: 16px;
    font-weight: 600;
    font-family: __FONT_BODY__;
    background: transparent;
}
QLabel#kizCodeIdx {
    color: #8aa0b8;
    font-size: 13px;
    font-weight: 600;
    background: transparent;
}
QLineEdit#kizCodeInput {
    min-height: 36px;
    padding: 6px 10px;
    border: 1px solid #c2d9f2;
    border-radius: 10px;
    background: #ffffff;
    font-size: 14px;
}
QLineEdit#kizCodeInput:focus {
    border-color: #79c3ff;
}
QLineEdit#kizCodeInput[state="error"] {
    border-color: #f87171;
    background: #fef2f2;
}
QToolButton#kizCodeRemove {
    min-width: 32px;
    max-width: 32px;
    min-height: 32px;
    max-height: 32px;
    padding: 0;
    color: #5f7691;
    border: 1px solid #d9e8f7;
    background: #ffffff;
    border-radius: 10px;
    font-size: 18px;
}
QToolButton#kizCodeRemove:hover {
    color: #b91c1c;
    border-color: #fecaca;
    background: #fef2f2;
}
QLabel#kizCodeStatus {
    padding: 6px 10px;
    border-radius: 10px;
    font-size: 12px;
    font-weight: 600;
    background: transparent;
    min-height: 28px;
}
QLabel#kizCodeStatus[state="pending"] {
    color: #92400e;
    background: #fef3c7;
    border: 1px solid #fcd34d;
}
QLabel#kizCodeStatus[state="ok"] {
    color: #166534;
    background: #dcfce7;
    border: 1px solid #86efac;
}
QLabel#kizCodeStatus[state="error"] {
    color: #991b1b;
    background: #fee2e2;
    border: 1px solid #fca5a5;
}
QPushButton#kizAddBtn {
    color: #3ba4f7;
    border: none;
    background: transparent;
    padding: 4px 10px;
    min-height: 32px;
    font-size: 14px;
    font-weight: 600;
    text-align: left;
}
QPushButton#kizAddBtn:hover {
    color: #2495ee;
    background: #ddf1ff;
}
QPushButton#kizAddBtn:pressed {
    color: #1687e1;
    background: #ddf1ff;
}
QLabel#kizRowError {
    color: #b91c1c;
    font-size: 13px;
    background: transparent;
}
QLabel#kizPromptTitle {
    color: #0f1f33;
    font-size: 20px;
    font-weight: 700;
    background: transparent;
}
QLabel#kizPromptMeta {
    color: #5f7691;
    font-size: 14px;
    background: transparent;
}

/* ========== Грузоместа (TRBX) ========== */
QTableWidget#trbxTable {
    border: 1px solid #d9e8f7;
    border-radius: 10px;
    background: #ffffff;
    gridline-color: transparent;
}
QTableWidget#trbxTable::item {
    padding: 0;
    border-bottom: 1px solid #f2f8ff;
}
QTableWidget#trbxTable QHeaderView::section {
    background: #f2f8ff;
    color: #5f7691;
    font-size: 12px;
    font-weight: 600;
    border: none;
    border-bottom: 1px solid #d9e8f7;
    padding: 10px 16px;
}
QLabel#trbxBoxId {
    font-size: 14px;
    font-weight: 600;
    color: #0f1f33;
    background: transparent;
}
QToolButton#trbxBoxBtn,
QToolButton#trbxBoxDeleteBtn {
    min-width: 40px;
    max-width: 40px;
    min-height: 40px;
    max-height: 40px;
    padding: 0;
    color: #0f1f33;
    background: #f2f8ff;
    border: 1px solid #d9e8f7;
    border-radius: 10px;
    font-size: 13px;
    font-weight: 600;
}
QToolButton#trbxBoxBtn:hover,
QToolButton#trbxBoxDeleteBtn:hover {
    background: #f2f8ff;
}
QToolButton#trbxBoxDeleteBtn {
    color: #b91c1c;
}
QToolButton#trbxBoxDeleteBtn:hover {
    background: #fef2f2;
    border-color: #fecaca;
}
"""


def get_app_qss() -> str:
    """Build QSS with bundled Inter / InterDisplay font stacks."""
    from app.ui.fonts import display_css_stack, font_css_stack, load_app_fonts

    load_app_fonts()
    return (
        _APP_QSS_TEMPLATE.replace("__FONT_BODY__", font_css_stack()).replace(
            "__FONT_DISPLAY__", display_css_stack()
        )
    )


# Back-compat for imports that still read APP_QSS at module load (fonts may
# not be registered yet — prefer get_app_qss() after QApplication exists).
APP_QSS = _APP_QSS_TEMPLATE.replace(
    "__FONT_BODY__", '"Inter", "Segoe UI", system-ui, sans-serif'
).replace(
    "__FONT_DISPLAY__", '"Inter Display", "Inter", "Segoe UI", system-ui, sans-serif'
)
