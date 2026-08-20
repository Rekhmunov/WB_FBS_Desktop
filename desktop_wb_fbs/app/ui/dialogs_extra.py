# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPainter, QPixmap
from PyQt5.QtPrintSupport import QPrintDialog, QPrinter
from PyQt5.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.db import Database
from app.services.orders import OrdersService
from app.services.trbx_stickers import StickersService
from app.ui.dialog_utils import prepare_modal_dialog, standard_window_flags
from app.wb import default_mgt_supply_name


def _print_pixmaps(parent: QWidget, pixmaps: List[QPixmap]) -> None:
    valid = [p for p in pixmaps if p is not None and not p.isNull()]
    if not valid:
        QMessageBox.warning(parent, "Печать", "Нет изображений для печати.")
        return
    printer = QPrinter(QPrinter.HighResolution)
    dlg = QPrintDialog(printer, parent)
    dlg.setWindowFlags(standard_window_flags())
    if dlg.exec_() != QDialog.Accepted:
        return
    painter = QPainter()
    if not painter.begin(printer):
        QMessageBox.warning(parent, "Печать", "Не удалось начать печать.")
        return
    try:
        for index, pix in enumerate(valid):
            if index > 0:
                printer.newPage()
            page = printer.pageRect(QPrinter.DevicePixel)
            scaled = pix.scaled(
                page.size(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
            x = page.x() + max(0, (page.width() - scaled.width()) // 2)
            y = page.y() + max(0, (page.height() - scaled.height()) // 2)
            painter.drawPixmap(x, y, scaled)
    finally:
        painter.end()
    QMessageBox.information(parent, "Печать", "Стикер отправлен на печать.")


def show_png_list(
    pngs: List[bytes],
    title: str,
    parent: Optional[QWidget] = None,
    sheet_labels: Optional[List[str]] = None,
) -> None:
    pixmaps = []  # type: List[QPixmap]
    for raw in pngs:
        pix = QPixmap()
        if raw:
            pix.loadFromData(raw)
        pixmaps.append(pix)
    show_pixmap_print_preview(
        pixmaps, title, parent, sheet_labels=sheet_labels
    )


def show_pixmap_print_preview(
    pixmaps: List[QPixmap],
    title: str,
    parent: Optional[QWidget] = None,
    sheet_labels: Optional[List[str]] = None,
) -> None:
    """Scroll preview with one visual sheet per printed page."""
    valid = [p for p in pixmaps if p is not None and not p.isNull()]
    if not valid:
        QMessageBox.warning(
            parent,
            title or "Печать",
            "Нет изображений для предпросмотра.",
        )
        return

    total = len(valid)
    labels = list(sheet_labels or [])
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    prepare_modal_dialog(
        dlg,
        maximized=True,
        default_size=(640, 720),
        minimum_size=(480, 520),
    )
    root = QVBoxLayout(dlg)
    root.setContentsMargins(24, 20, 24, 20)
    root.setSpacing(12)
    heading = QLabel(title)
    heading.setObjectName("dialogTitle")
    heading.setWordWrap(True)
    root.addWidget(heading)
    tip = QLabel(
        "Предпросмотр по листам · {} стр. Каждый блок = один лист на печати.".format(
            total
        )
    )
    tip.setStyleSheet("color:#64748b;")
    tip.setWordWrap(True)
    root.addWidget(tip)

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(0)  # QFrame.NoFrame
    scroll.setStyleSheet("QScrollArea { background: #e8eef5; border: none; }")
    wrap = QWidget()
    wrap.setStyleSheet("background: #e8eef5;")
    lay = QVBoxLayout(wrap)
    lay.setContentsMargins(20, 16, 20, 24)
    lay.setSpacing(20)

    for index, pix in enumerate(valid, start=1):
        sheet = QFrame()
        sheet.setObjectName("printSheet")
        sheet.setStyleSheet(
            """
            QFrame#printSheet {
                background: #ffffff;
                border: 1px solid #cbd5e1;
                border-radius: 4px;
            }
            """
        )
        sheet_lay = QVBoxLayout(sheet)
        sheet_lay.setContentsMargins(14, 12, 14, 14)
        sheet_lay.setSpacing(8)

        extra = ""
        if index - 1 < len(labels) and str(labels[index - 1] or "").strip():
            extra = " · {}".format(str(labels[index - 1]).strip())
        meta = QLabel("Лист {} из {}{}".format(index, total, extra))
        meta.setStyleSheet(
            "color:#64748b;font-size:12px;font-weight:600;"
            "border:none;background:transparent;"
        )
        meta.setWordWrap(True)
        sheet_lay.addWidget(meta)

        rule = QFrame()
        rule.setFixedHeight(1)
        rule.setStyleSheet("background:#e2e8f0;border:none;")
        sheet_lay.addWidget(rule)

        img = QLabel()
        img.setAlignment(Qt.AlignCenter)
        img.setStyleSheet("border:none;background:transparent;")
        preview = pix.scaledToWidth(420, Qt.SmoothTransformation)
        img.setPixmap(preview)
        img.setMinimumHeight(preview.height())
        sheet_lay.addWidget(img, 0, Qt.AlignCenter)

        lay.addWidget(sheet, 0, Qt.AlignHCenter)

    lay.addStretch(1)
    scroll.setWidget(wrap)
    root.addWidget(scroll, 1)

    buttons = QDialogButtonBox()
    print_btn = buttons.addButton("Печать…", QDialogButtonBox.ActionRole)
    print_btn.setObjectName("bottomPrimary")
    print_btn.clicked.connect(lambda: _print_pixmaps(dlg, valid))
    close_btn = buttons.addButton(QDialogButtonBox.Close)
    close_btn.setObjectName("secondary")
    buttons.rejected.connect(dlg.reject)
    close_btn.clicked.connect(dlg.reject)
    root.addWidget(buttons)
    dlg.exec_()


def build_sticker_print_pixmaps(groups: List[Dict[str, Any]]) -> List[QPixmap]:
    """Build print pages: product separator sheet, then each order sticker PNG."""
    pages = []  # type: List[QPixmap]
    for g in groups or []:
        sep = _sticker_separator_pixmap(g)
        if sep is not None and not sep.isNull():
            pages.append(sep)
        for order in g.get("orders") or []:
            path = str(order.get("sticker_file_path") or "").strip()
            pix = QPixmap()
            if path:
                pix = QPixmap(path)
            if pix.isNull():
                # Placeholder so the sheet order stays aligned with the print job.
                pix = _sticker_missing_pixmap(order.get("order_id"))
            pages.append(pix)
    return pages


def _sticker_separator_pixmap(group: Dict[str, Any]) -> QPixmap:
    """58×40 mm style separator card (артикул для подбора)."""
    w, h = 580, 400
    pix = QPixmap(w, h)
    pix.fill(Qt.white)
    painter = QPainter(pix)
    try:
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.setPen(Qt.black)
        y = 28
        qty = int(group.get("qty") or 0)
        font = painter.font()
        font.setBold(True)
        font.setPointSize(22)
        painter.setFont(font)
        painter.drawText(24, y, 532, 40, Qt.AlignLeft | Qt.AlignVCenter, "{} шт.".format(qty))
        y += 48
        font.setPointSize(16)
        painter.setFont(font)
        name = str(group.get("product_name") or "—")
        painter.drawText(24, y, 532, 72, Qt.AlignLeft | Qt.TextWordWrap, name)
        y += 80
        font.setBold(False)
        font.setPointSize(11)
        painter.setFont(font)
        lines = []
        brand = str(group.get("brand") or "").strip()
        if brand:
            lines.append("Бренд: {}".format(brand))
        color = str(group.get("color") or "").strip()
        if color:
            lines.append("Цвет: {}".format(color))
        nm = group.get("nm_id")
        if nm not in (None, ""):
            lines.append("Артикул WB: {}".format(nm))
        barcodes = group.get("barcodes") or []
        if barcodes:
            lines.append("Баркод: {}".format(barcodes[0]))
        article = str(group.get("article") or "").strip()
        if article:
            lines.append("Артикул: {}".format(article))
        for line in lines:
            painter.drawText(24, y, 532, 28, Qt.AlignLeft | Qt.AlignVCenter, line)
            y += 26
        font.setPointSize(10)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(Qt.darkGray)
        painter.drawText(
            24,
            h - 40,
            532,
            24,
            Qt.AlignLeft | Qt.AlignVCenter,
            "Артикул для подбора · Не нужно клеить",
        )
    finally:
        painter.end()
    return pix


def _sticker_missing_pixmap(order_id: object) -> QPixmap:
    w, h = 580, 400
    pix = QPixmap(w, h)
    pix.fill(Qt.white)
    painter = QPainter(pix)
    try:
        painter.setPen(Qt.red)
        font = painter.font()
        font.setPointSize(14)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(
            pix.rect(),
            Qt.AlignCenter,
            "Нет стикера\nЗаказ {}".format(order_id),
        )
    finally:
        painter.end()
    return pix


def build_picking_list_pixmaps(
    supply_id: str,
    supply_name: str,
    groups: List[Dict[str, Any]],
    variant: str = "summary",
) -> List[QPixmap]:
    """Paginate picking list into A4-like sheets for sheet preview / print."""
    from collections import OrderedDict

    mode = "extended" if str(variant).lower() == "extended" else "summary"
    title = (
        "Расширенный лист подбора" if mode == "extended" else "Лист подбора"
    )
    total_orders = sum(int(g.get("qty") or 0) for g in groups or [])
    if total_orders % 10 == 1 and total_orders % 100 != 11:
        order_word = "заказ"
    elif 2 <= total_orders % 10 <= 4 and not (12 <= total_orders % 100 <= 14):
        order_word = "заказа"
    else:
        order_word = "заказов"

    blocks = []  # type: List[Tuple[str, Any, int]]
    if mode == "summary":
        summary_qty = OrderedDict()  # type: OrderedDict[str, int]
        for g in groups or []:
            name = str(g.get("product_name") or "—")
            summary_qty[name] = summary_qty.get(name, 0) + int(g.get("qty") or 0)
        blocks.append(("totals", "Всего {} {}".format(total_orders, order_word), 34))
        if not summary_qty:
            blocks.append(("summary", ("Нет заказов", 0), 32))
        for name, qty in summary_qty.items():
            blocks.append(("summary", (name, qty), 32))
    else:
        part_b_counts = {}  # type: Dict[str, int]
        for g in groups or []:
            for o in g.get("orders") or []:
                pb = str(o.get("sticker_part_b") or "").strip()
                if pb:
                    part_b_counts[pb] = part_b_counts.get(pb, 0) + 1
        dup = {pb for pb, n in part_b_counts.items() if n > 1}
        blocks.append(("totals_ext", "Всего {} {}".format(total_orders, order_word), 34))
        blocks.append(("colhead", None, 28))
        for g in groups or []:
            orders = list(g.get("orders") or [])
            qty = int(g.get("qty") or len(orders))
            meta_lines = [str(g.get("product_name") or "—")]
            if g.get("brand"):
                meta_lines.append(str(g.get("brand")))
            if g.get("article"):
                meta_lines.append(str(g.get("article")))
            for b in g.get("barcodes") or []:
                meta_lines.append(str(b))
            if g.get("color"):
                meta_lines.append("Цвет: {}".format(g.get("color")))
            meta_lines.append("{} шт".format(qty))
            blocks.append(("product", meta_lines, 18 + 16 * len(meta_lines)))
            for o in orders:
                pb = str(o.get("sticker_part_b") or "").strip()
                pa = str(o.get("sticker_part_a") or "").strip()
                sticker = (pa + pb) if (pa or pb) else "—"
                blocks.append(
                    (
                        "order",
                        {
                            "oid": o.get("order_id"),
                            "sticker": sticker,
                            "dup": pb in dup,
                        },
                        28,
                    )
                )

    page_w, page_h = 794, 1123
    margin = 36
    header_h = 78
    footer_h = 28
    content_top = margin + header_h
    content_bottom = page_h - margin - footer_h
    usable = content_bottom - content_top

    pages_blocks = []  # type: List[List[Tuple[str, Any, int]]]
    current = []  # type: List[Tuple[str, Any, int]]
    used = 0
    for block in blocks:
        h = int(block[2])
        if current and used + h > usable:
            pages_blocks.append(current)
            current = []
            used = 0
        current.append(block)
        used += h
    if current:
        pages_blocks.append(current)
    if not pages_blocks:
        pages_blocks = [[("summary", ("Нет заказов", 0), 32)]]

    pages = []  # type: List[QPixmap]
    page_count = len(pages_blocks)
    for page_idx, page_blocks in enumerate(pages_blocks, start=1):
        pix = QPixmap(page_w, page_h)
        pix.fill(Qt.white)
        painter = QPainter(pix)
        try:
            painter.setRenderHint(QPainter.TextAntialiasing, True)
            painter.setPen(Qt.black)
            font = painter.font()
            font.setFamily("Arial")
            font.setBold(True)
            font.setPointSize(16)
            painter.setFont(font)
            painter.drawText(
                margin, margin, page_w - 2 * margin, 32,
                Qt.AlignLeft | Qt.AlignVCenter, title,
            )
            font.setBold(False)
            font.setPointSize(10)
            painter.setFont(font)
            painter.setPen(Qt.darkGray)
            painter.drawText(
                margin, margin + 34, page_w - 2 * margin, 24,
                Qt.AlignLeft | Qt.AlignVCenter,
                "{} · ID {}".format(supply_name or supply_id, supply_id),
            )
            painter.setPen(Qt.black)
            y = content_top
            for kind, payload, h in page_blocks:
                if kind in ("totals", "totals_ext"):
                    font.setBold(True)
                    font.setPointSize(11)
                    painter.setFont(font)
                    painter.fillRect(margin, y, page_w - 2 * margin, h - 4, Qt.lightGray)
                    painter.drawText(
                        margin + 8, y, page_w - 2 * margin - 90, h - 4,
                        Qt.AlignLeft | Qt.AlignVCenter, str(payload),
                    )
                    painter.drawText(
                        page_w - margin - 80, y, 72, h - 4, Qt.AlignCenter, "Собрано",
                    )
                elif kind == "summary":
                    name, qty = payload
                    font.setBold(False)
                    font.setPointSize(11)
                    painter.setFont(font)
                    text = (
                        name if qty == 0 and name == "Нет заказов"
                        else "{} — {} шт.".format(name, qty)
                    )
                    painter.drawText(
                        margin + 8, y, page_w - 2 * margin - 90, h - 2,
                        Qt.AlignLeft | Qt.AlignVCenter, text,
                    )
                    painter.drawRect(page_w - margin - 52, y + 8, 14, 14)
                    painter.drawLine(margin, y + h - 2, page_w - margin, y + h - 2)
                elif kind == "colhead":
                    font.setBold(True)
                    font.setPointSize(10)
                    painter.setFont(font)
                    painter.drawText(margin + 8, y, 160, h, Qt.AlignVCenter, "Заказ")
                    painter.drawText(margin + 180, y, 280, h, Qt.AlignVCenter, "Стикер")
                    painter.drawText(
                        page_w - margin - 80, y, 72, h, Qt.AlignCenter, "Собрано",
                    )
                    painter.drawLine(margin, y + h - 2, page_w - margin, y + h - 2)
                elif kind == "product":
                    font.setBold(True)
                    font.setPointSize(11)
                    painter.setFont(font)
                    yy = y + 4
                    for i, line in enumerate(payload):
                        if i == 1:
                            font.setBold(False)
                            font.setPointSize(10)
                            painter.setFont(font)
                            painter.setPen(Qt.darkGray)
                        painter.drawText(
                            margin + 8, yy, page_w - 2 * margin - 16, 16,
                            Qt.AlignLeft | Qt.AlignVCenter, str(line),
                        )
                        yy += 16
                    painter.setPen(Qt.black)
                    painter.drawLine(margin, y + h - 2, page_w - margin, y + h - 2)
                elif kind == "order":
                    font.setBold(bool(payload.get("dup")))
                    font.setPointSize(10)
                    painter.setFont(font)
                    painter.setPen(Qt.red if payload.get("dup") else Qt.black)
                    painter.drawText(
                        margin + 8, y, 160, h - 2,
                        Qt.AlignLeft | Qt.AlignVCenter, str(payload.get("oid") or ""),
                    )
                    painter.drawText(
                        margin + 180, y, 280, h - 2,
                        Qt.AlignLeft | Qt.AlignVCenter, str(payload.get("sticker") or "—"),
                    )
                    painter.setPen(Qt.black)
                    font.setBold(False)
                    painter.setFont(font)
                    painter.drawRect(page_w - margin - 52, y + 6, 14, 14)
                    painter.drawLine(margin, y + h - 2, page_w - margin, y + h - 2)
                y += h

            font.setBold(False)
            font.setPointSize(9)
            painter.setFont(font)
            painter.setPen(Qt.darkGray)
            painter.drawText(
                margin, page_h - margin - 18, page_w - 2 * margin, 18,
                Qt.AlignRight | Qt.AlignVCenter,
                "Лист {} из {}".format(page_idx, page_count),
            )
        finally:
            painter.end()
        pages.append(pix)
    return pages


def show_order_stickers(
    api_key: str, order_ids: List[int], parent: Optional[QWidget] = None
) -> None:
    svc = StickersService(Database())  # stickers don't need DB
    items = svc.order_stickers_png(api_key, order_ids)
    pngs = [it["png"] for it in items if it.get("png")]
    if not pngs:
        raise RuntimeError("WB не вернул стикеры")
    show_png_list(pngs, "Стикеры заказов ({})".format(len(pngs)), parent)


def show_supply_qr(
    api_key: str,
    supply_id: str,
    parent: Optional[QWidget] = None,
    *,
    order_count: int = 0,
    city: str = "",
) -> None:
    """Show/print local supply QR sticker (WB-GI id). ``api_key`` kept for call-site compat."""
    from app.services.supply_qr import render_supply_qr_sticker_png

    _ = api_key  # local QR does not call WB /barcode
    png = render_supply_qr_sticker_png(
        supply_id,
        order_count=int(order_count or 0),
        city=str(city or ""),
    )
    show_png_list([png], "QR поставки {}".format(supply_id), parent)



def _fmt_order_ids(ids: List[Any], limit: int = 40) -> str:
    list_ids = [str(x) for x in (ids or [])]
    if not list_ids:
        return ""
    shown = ", ".join(list_ids[:limit])
    if len(list_ids) > limit:
        return "{} … (+{})".format(shown, len(list_ids) - limit)
    return shown


def show_collect_mgt_result(parent: Optional[QWidget], data: Dict[str, Any]) -> None:
    """Web ``_wbFbsCollectMgtShowResult`` — detailed outcome after collect."""
    ok = bool(data.get("ok"))
    title = "Готово" if ok else "Есть проблемы"
    lines = [str(data.get("message") or "")]
    for g in data.get("groups") or []:
        if isinstance(g, dict) and g.get("message"):
            lines.append("• {}".format(g.get("message")))
    created = data.get("created_supplies") or []
    if created:
        lines.append("")
        lines.append("Созданы поставки:")
        for s in created:
            if isinstance(s, dict):
                lines.append("• {}".format(s.get("name") or s.get("supply_id") or ""))
    errors = data.get("errors") or []
    if errors:
        lines.append("")
        lines.append("Ошибки:")
        for e in errors:
            lines.append("• {}".format(e))
    warnings = data.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append("Предупреждения:")
        for w in warnings:
            lines.append("• {}".format(w))
    remaining = data.get("remaining_in_new")
    if remaining is None:
        remaining = data.get("not_added") or []
    if remaining:
        lines.append("")
        lines.append(
            "Остались в «Новых» ({}): {}".format(
                len(remaining), _fmt_order_ids(remaining)
            )
        )
    skipped = data.get("skipped_cancelled") or []
    if skipped:
        lines.append("")
        lines.append(
            "Пропущено — уже не new / отмена на WB ({}): {}".format(
                len(skipped), _fmt_order_ids(skipped)
            )
        )
    text_msg = "\n".join(lines).strip() or "Готово"
    if ok:
        QMessageBox.information(parent, title, text_msg)
    else:
        QMessageBox.warning(parent, title, text_msg)


class CollectMgtDialog(QDialog):
    def __init__(
        self,
        db: Database,
        orders: OrdersService,
        source: Dict[str, Any],
        parent: Optional[QWidget] = None,
        *,
        preview: Optional[Dict[str, Any]] = None,
    ) -> None:
        super(CollectMgtDialog, self).__init__(parent)
        from app.services.collect_mgt import CollectMgtService

        self.db = db
        self.orders = orders
        self.source = source
        self.svc = CollectMgtService(db, orders)
        self.preview = preview if isinstance(preview, dict) else None
        self.result_payload = None  # type: Optional[Dict[str, Any]]
        self.setWindowTitle("Собрать все МГТ-заказы")
        prepare_modal_dialog(
            self,
            maximized=True,
            default_size=(720, 560),
            minimum_size=(560, 440),
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)
        title = QLabel("Собрать все МГТ-заказы")
        title.setObjectName("dialogTitle")
        root.addWidget(title)
        self.lead = QLabel("")
        self.lead.setObjectName("hint")
        self.lead.setWordWrap(True)
        root.addWidget(self.lead)

        self.err = QLabel("")
        self.err.setObjectName("hint")
        self.err.setStyleSheet("color:#b91c1c; font-size: 14px;")
        self.err.setWordWrap(True)
        self.err.hide()
        root.addWidget(self.err)

        self.scroll = QWidget()
        self.form = QVBoxLayout(self.scroll)
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setWidget(self.scroll)
        root.addWidget(scroll_area, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok = buttons.button(QDialogButtonBox.Ok)
        ok.setText("Собрать")
        ok.setObjectName("mgtBtn")
        cancel = buttons.button(QDialogButtonBox.Cancel)
        cancel.setText("Отмена")
        cancel.setObjectName("secondary")
        buttons.accepted.connect(self.do_collect)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self._ok_btn = ok
        self._group_widgets = []  # type: List[Dict[str, Any]]
        self._existing_names = set()  # type: set
        self._load()

    def _load(self) -> None:
        preview = self.preview or self.svc.preview(int(self.source["id"]))
        self.preview = preview
        groups = list(preview.get("groups") or [])
        self._existing_names = {
            str(x or "").strip()
            for x in (preview.get("existing_names") or [])
            if str(x or "").strip()
        }
        self.lead.setText(
            "Новых МГТ заказов: {}.".format(preview.get("mgt_count", 0))
        )
        while self.form.count():
            item = self.form.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._group_widgets = []
        if not groups:
            self.form.addWidget(QLabel("Нет МГТ-заказов для сбора"))
            if self._ok_btn:
                self._ok_btn.setEnabled(False)
            return
        for g in groups:
            box = QWidget()
            lay = QVBoxLayout(box)
            lay.setContentsMargins(0, 8, 0, 8)
            lay.setSpacing(8)
            label = QLabel(
                "<b>{}</b> — {} зак.".format(
                    g.get("label") or "",
                    g.get("order_count") or 0,
                )
            )
            label.setTextFormat(Qt.RichText)
            lay.addWidget(label)
            mode = str(g.get("mode") or "create")
            name_edit = None  # type: Optional[QLineEdit]
            warn = None  # type: Optional[QLabel]
            radio_group = None  # type: Optional[QButtonGroup]
            if mode == "create":
                name_lab = QLabel("Название новой поставки")
                name_lab.setObjectName("fieldLabel")
                lay.addWidget(name_lab)
                name_edit = QLineEdit(str(g.get("suggested_name") or ""))
                lay.addWidget(name_edit)
                warn = QLabel(
                    "Поставка с таким названием уже есть — измените название."
                )
                warn.setStyleSheet("color:#b45309; font-size: 13px;")
                warn.setWordWrap(True)
                conflict = (
                    str(g.get("suggested_name") or "").strip()
                    in self._existing_names
                )
                warn.setVisible(conflict)
                lay.addWidget(warn)

                def _on_name(text: str, w=warn) -> None:
                    name = str(text or "").strip()
                    w.setVisible(bool(name and name in self._existing_names))

                name_edit.textChanged.connect(_on_name)
            elif mode == "choose":
                choose_lab = QLabel("Выберите поставку")
                choose_lab.setObjectName("fieldLabel")
                lay.addWidget(choose_lab)
                radio_group = QButtonGroup(box)
                supplies = list(g.get("compatible_supplies") or [])
                for si, s in enumerate(supplies):
                    sid = str(s.get("supply_id") or "")
                    sname = str(s.get("name") or sid)
                    meta_parts = [
                        "пустая" if s.get("is_empty") else "МГТ",
                        "B2B" if s.get("is_b2b") else None,
                        "{} заказ.".format(int(s.get("orders_count") or 0)),
                    ]
                    meta = " · ".join(p for p in meta_parts if p)
                    rb = QRadioButton("{} — {}".format(sname, meta))
                    rb.setProperty("supply_id", sid)
                    radio_group.addButton(rb)
                    if si == 0:
                        rb.setChecked(True)
                    lay.addWidget(rb)
            else:
                sid = str(g.get("default_supply_id") or "")
                match = next(
                    (
                        s
                        for s in (g.get("compatible_supplies") or [])
                        if str(s.get("supply_id") or "") == sid
                    ),
                    None,
                )
                sname = (
                    str(match.get("name") or sid)
                    if isinstance(match, dict)
                    else sid
                )
                auto = QLabel("Будет добавлено в поставку «{}».".format(sname))
                auto.setObjectName("hint")
                auto.setWordWrap(True)
                lay.addWidget(auto)
            self.form.addWidget(box)
            self._group_widgets.append(
                {
                    "group": g,
                    "name_edit": name_edit,
                    "warn": warn,
                    "radio_group": radio_group,
                }
            )
        self.form.addStretch(1)

    def _collect_decisions(self) -> Tuple[List[Dict[str, Any]], List[str]]:
        decisions = []  # type: List[Dict[str, Any]]
        errors = []  # type: List[str]
        used_names = set()  # type: set
        for item in self._group_widgets:
            g = item["group"]
            is_b2b = bool(g.get("is_b2b"))
            gkey = str(g.get("group_key") or "")
            mode = str(g.get("mode") or "create")
            label = g.get("label") or ("B2B" if is_b2b else "не B2B")
            if mode == "create":
                name_edit = item.get("name_edit")
                name = ""
                if name_edit is not None:
                    name = name_edit.text().strip()
                name = name or str(g.get("suggested_name") or "").strip()
                if not name:
                    errors.append("{}: укажите название поставки".format(label))
                    continue
                if name in self._existing_names or name in used_names:
                    errors.append(
                        "{}: поставка «{}» уже есть — измените название".format(
                            label, name
                        )
                    )
                    continue
                used_names.add(name)
                decisions.append(
                    {
                        "group_key": gkey,
                        "is_b2b": is_b2b,
                        "action": "create",
                        "name": name,
                    }
                )
            elif mode == "choose":
                radio_group = item.get("radio_group")
                supply_id = ""
                if radio_group is not None:
                    checked = radio_group.checkedButton()
                    if checked is not None:
                        supply_id = str(
                            checked.property("supply_id") or ""
                        ).strip()
                if not supply_id:
                    errors.append("{}: выберите поставку".format(label))
                    continue
                decisions.append(
                    {
                        "group_key": gkey,
                        "is_b2b": is_b2b,
                        "action": "choose",
                        "supply_id": supply_id,
                    }
                )
            else:
                decisions.append(
                    {
                        "group_key": gkey,
                        "is_b2b": is_b2b,
                        "action": "add",
                        "supply_id": str(g.get("default_supply_id") or ""),
                    }
                )
        return decisions, errors

    def do_collect(self) -> None:
        decisions, errors = self._collect_decisions()
        if errors:
            self.err.setText("\n".join(errors))
            self.err.show()
            return
        self.err.hide()
        if self._ok_btn:
            self._ok_btn.setEnabled(False)
        try:
            result = self.svc.execute(
                int(self.source["id"]),
                str(self.source["api_key"]),
                decisions,
            )
            self.result_payload = result
            self.accept()
        except Exception as exc:
            self.err.setText(str(exc))
            self.err.show()
            if self._ok_btn:
                self._ok_btn.setEnabled(True)


class SelectionSupplyDialog(QDialog):
    def __init__(
        self,
        orders: OrdersService,
        source: Dict[str, Any],
        order_ids: List[int],
        mode: str = "create",
        parent: Optional[QWidget] = None,
    ) -> None:
        super(SelectionSupplyDialog, self).__init__(parent)
        self.orders = orders
        self.source = source
        self.order_ids = order_ids
        self.mode = mode
        self.setWindowTitle(
            "Новая поставка" if mode == "create" else "Добавить к поставке"
        )
        prepare_modal_dialog(
            self,
            maximized=True,
            default_size=(520, 400),
            minimum_size=(440, 320),
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)
        title = QLabel(
            "Новая поставка" if mode == "create" else "Добавить к существующей"
        )
        title.setObjectName("dialogTitle")
        root.addWidget(title)
        count = QLabel("Заказов: {}".format(len(order_ids)))
        count.setObjectName("hint")
        root.addWidget(count)

        # Load order traits
        sid = int(source["id"])
        with orders.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM wb_fbs_orders
                WHERE source_id = ? AND order_id IN ({})
                """.format(
                    ",".join("?" for _ in order_ids)
                ),
                [sid] + list(order_ids),
            ).fetchall()
        items = [dict(r) for r in rows]
        cargos = {int(r.get("cargo_type") or 0) for r in items}
        b2bs = {bool(int(r.get("is_b2b") or 0)) for r in items}
        whs = {r.get("warehouse_id") for r in items}
        errors = []
        if len(cargos) > 1:
            errors.append("Нельзя смешивать типы груза (МГТ/СГТ/КГТ+)")
        if len(b2bs) > 1:
            errors.append("Нельзя смешивать B2B и розницу")
        if len(whs) > 1:
            errors.append("Нельзя смешивать склады")
        self.err = QLabel("\n".join(errors))
        self.err.setObjectName("hint")
        self.err.setStyleSheet("color:#b91c1c; font-size: 15px;")
        self.err.setWordWrap(True)
        root.addWidget(self.err)

        self.name_edit = QLineEdit(
            default_mgt_supply_name(is_b2b=bool(next(iter(b2bs), False)))
        )
        self.supply_combo = QComboBox()
        if mode == "create":
            name_lab = QLabel("Название поставки")
            name_lab.setObjectName("fieldLabel")
            root.addWidget(name_lab)
            root.addWidget(self.name_edit)
        else:
            name_lab = QLabel("Открытая совместимая поставка")
            name_lab.setObjectName("fieldLabel")
            root.addWidget(name_lab)
            root.addWidget(self.supply_combo)
            cargo = next(iter(cargos), 0)
            is_b2b = bool(next(iter(b2bs), False))
            wh = next(iter(whs), None)
            for s in orders.open_compatible_supplies(sid, cargo, is_b2b, wh):
                self.supply_combo.addItem(
                    "{} · {} зак.".format(s.get("name") or s.get("supply_id"), s.get("order_count")),
                    str(s.get("supply_id")),
                )

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok = buttons.button(QDialogButtonBox.Ok)
        ok.setText("Создать" if mode == "create" else "Добавить")
        buttons.button(QDialogButtonBox.Cancel).setObjectName("secondary")
        buttons.accepted.connect(self.do_ok)
        buttons.rejected.connect(self.reject)
        if errors:
            ok.setEnabled(False)
        if mode == "add" and self.supply_combo.count() == 0:
            ok.setEnabled(False)
            self.err.setText((self.err.text() + "\nНет совместимых открытых поставок").strip())
        root.addStretch(1)
        root.addWidget(buttons)

    def do_ok(self) -> None:
        try:
            sid = int(self.source["id"])
            key = str(self.source["api_key"])
            if self.mode == "create":
                self.orders.create_supply_from_orders(
                    sid, key, self.order_ids, self.name_edit.text().strip()
                )
            else:
                supply_id = str(self.supply_combo.currentData() or "")
                if not supply_id:
                    raise ValueError("Выберите поставку")
                self.orders.add_orders_to_existing_supply(
                    sid, key, supply_id, self.order_ids
                )
            self.accept()
        except Exception as exc:
            QMessageBox.critical(self, "Поставка", str(exc))
