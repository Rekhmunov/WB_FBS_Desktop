# -*- coding: utf-8 -*-
"""Local supply QR sticker (web parity: WB-GI id → QR, no /barcode API)."""
from __future__ import annotations

import io
from typing import Tuple

from PyQt5.QtCore import QBuffer, QByteArray, QRectF, Qt
from PyQt5.QtGui import QFont, QImage, QPainter, QPixmap


def supply_qr_payload(supply_id: str) -> str:
    return str(supply_id or "").strip()


def _qr_png_bytes(text: str, box_px: int = 280) -> bytes:
    import qrcode
    from PIL import Image

    value = str(text or "").strip()
    if not value:
        raise ValueError("Нет кода поставки для QR")
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(value)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    if not isinstance(img, Image.Image):
        img = img.get_image()
    img = img.convert("RGB").resize((int(box_px), int(box_px)), Image.NEAREST)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_supply_qr_sticker_png(
    supply_id: str,
    *,
    order_count: int = 0,
    city: str = "",
    size_mm: Tuple[float, float] = (58.0, 40.0),
    dpi: int = 203,
) -> bytes:
    """Compose official-like 58×40 mm sticker: id | QR | qty+city."""
    sid = supply_qr_payload(supply_id)
    if not sid:
        raise ValueError("Нет кода поставки для QR")
    width_mm, height_mm = size_mm
    w = max(200, int(round(width_mm / 25.4 * dpi)))
    h = max(140, int(round(height_mm / 25.4 * dpi)))
    rail = max(40, int(round(11.0 / 25.4 * dpi)))
    pad = max(6, int(round(1.5 / 25.4 * dpi)))
    qr_side = max(80, min(h - 2 * pad, w - 2 * rail - 4 * pad))

    qr_raw = _qr_png_bytes(sid, box_px=qr_side)
    qr_pix = QPixmap()
    if not qr_pix.loadFromData(qr_raw):
        raise RuntimeError("Не удалось сформировать QR-код поставки")

    image = QImage(w, h, QImage.Format_RGB32)
    image.fill(Qt.white)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)

        qr_x = (w - qr_side) // 2
        qr_y = (h - qr_side) // 2
        painter.drawPixmap(qr_x, qr_y, qr_side, qr_side, qr_pix)

        font = QFont("Arial")
        font.setPixelSize(max(10, int(round(h * 0.09))))
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(Qt.black)

        left = QRectF(0, pad, rail, h - 2 * pad)
        painter.save()
        painter.translate(left.center())
        painter.rotate(-90)
        text_rect = QRectF(
            -left.height() / 2, -left.width() / 2, left.height(), left.width()
        )
        painter.drawText(text_rect, int(Qt.AlignCenter | Qt.TextWordWrap), sid)
        painter.restore()

        qty = max(0, int(order_count or 0))
        qty_label = "{} шт.".format(qty) if qty > 0 else ""
        city_label = str(city or "").strip()
        right_text = "\n".join(x for x in (qty_label, city_label) if x) or " "
        right = QRectF(w - rail, pad, rail, h - 2 * pad)
        painter.save()
        painter.translate(right.center())
        painter.rotate(90)
        text_rect = QRectF(
            -right.height() / 2, -right.width() / 2, right.height(), right.width()
        )
        painter.drawText(text_rect, int(Qt.AlignCenter | Qt.TextWordWrap), right_text)
        painter.restore()
    finally:
        painter.end()

    out = QPixmap.fromImage(image)
    buf = QByteArray()
    qbuf = QBuffer(buf)
    qbuf.open(QBuffer.WriteOnly)
    out.save(qbuf, "PNG")
    qbuf.close()
    return bytes(buf)
