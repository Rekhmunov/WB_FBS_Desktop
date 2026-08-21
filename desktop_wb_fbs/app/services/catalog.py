# -*- coding: utf-8 -*-
from __future__ import annotations

import csv
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.db import Database
from app.paths import photos_dir
from app.wb import utc_now


def _truthy_flag(value: object) -> bool:
    text = str(value or "").strip().lower()
    return text in (
        "1",
        "true",
        "yes",
        "y",
        "да",
        "д",
        "+",
        "on",
        "истина",
    )


def _norm_header(value: object) -> str:
    return " ".join(str(value or "").strip().lower().replace("ё", "е").split())


class ProductService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def list_all(self) -> List[Dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM product_photos ORDER BY name COLLATE NOCASE"
            ).fetchall()
        out = Database.rows_to_dicts(rows)
        for d in out:
            d["skip_kiz_gtin_check"] = bool(int(d.get("skip_kiz_gtin_check") or 0))
        return out

    def get(self, product_id: int) -> Optional[Dict[str, Any]]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM product_photos WHERE id = ?", (product_id,)
            ).fetchone()
        d = Database.row_to_dict(row)
        if d:
            d["skip_kiz_gtin_check"] = bool(int(d.get("skip_kiz_gtin_check") or 0))
        return d

    def save(
        self,
        product_id: Optional[int],
        name: str,
        supplier_article: str,
        wb_nmid: str,
        box_qty: Optional[int],
        product_category: str,
        skip_kiz_gtin_check: bool,
        photo_src: Optional[str] = None,
        ozon_sku: str = "",
        yandex_offer_id: str = "",
    ) -> int:
        name = str(name or "").strip()
        if not name:
            raise ValueError("Укажите название товара")
        article = str(supplier_article or "").strip()
        nmid = str(wb_nmid or "").strip()
        ozon = str(ozon_sku or "").strip()
        yandex = str(yandex_offer_id or "").strip()
        category = str(product_category or "").strip()
        skip = 1 if skip_kiz_gtin_check else 0
        now = utc_now()
        photo_path = None  # type: Optional[str]

        with self.db.connect() as conn:
            if product_id:
                existing = conn.execute(
                    "SELECT photo_path FROM product_photos WHERE id = ?",
                    (product_id,),
                ).fetchone()
                photo_path = (
                    str(existing["photo_path"])
                    if existing and existing["photo_path"]
                    else None
                )

            if photo_src:
                src = Path(photo_src)
                if src.is_file():
                    dest = photos_dir() / "{}_{}".format(
                        product_id or "new", src.name
                    )
                    shutil.copy2(str(src), str(dest))
                    photo_path = str(dest)

            if product_id:
                conn.execute(
                    """
                    UPDATE product_photos SET
                        name=?, supplier_article=?, wb_nmid=?,
                        ozon_sku=?, yandex_offer_id=?, box_qty=?,
                        product_category=?, skip_kiz_gtin_check=?,
                        photo_path=COALESCE(?, photo_path), updated_at=?
                    WHERE id=?
                    """,
                    (
                        name,
                        article,
                        nmid,
                        ozon,
                        yandex,
                        box_qty,
                        category,
                        skip,
                        photo_path,
                        now,
                        product_id,
                    ),
                )
                conn.commit()
                return int(product_id)

            cur = conn.execute(
                """
                INSERT INTO product_photos(
                    name, supplier_article, wb_nmid, ozon_sku, yandex_offer_id,
                    box_qty, product_category, skip_kiz_gtin_check, photo_path,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    article,
                    nmid,
                    ozon,
                    yandex,
                    box_qty,
                    category,
                    skip,
                    photo_path,
                    now,
                    now,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def find_id_by_article_or_nmid(
        self, supplier_article: str = "", wb_nmid: str = ""
    ) -> Optional[int]:
        article = str(supplier_article or "").strip()
        nmid = str(wb_nmid or "").strip()
        with self.db.connect() as conn:
            if article:
                row = conn.execute(
                    """
                    SELECT id FROM product_photos
                    WHERE lower(trim(supplier_article)) = lower(?)
                    ORDER BY id ASC LIMIT 1
                    """,
                    (article,),
                ).fetchone()
                if row:
                    return int(row["id"])
            if nmid:
                row = conn.execute(
                    """
                    SELECT id FROM product_photos
                    WHERE trim(wb_nmid) = ?
                    ORDER BY id ASC LIMIT 1
                    """,
                    (nmid,),
                ).fetchone()
                if row:
                    return int(row["id"])
        return None

    def import_csv(self, path: str) -> Dict[str, int]:
        """Import products from CSV (header row + data). Returns counts."""
        file_path = Path(path)
        if not file_path.is_file():
            raise ValueError("Файл не найден")

        raw = None  # type: Optional[str]
        last_err = None  # type: Optional[Exception]
        for enc in ("utf-8-sig", "utf-8", "cp1251"):
            try:
                raw = file_path.read_text(encoding=enc)
                break
            except Exception as exc:
                last_err = exc
        if raw is None:
            raise ValueError(
                "Не удалось прочитать CSV ({})".format(last_err or "encoding")
            )

        sample = raw[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
        except Exception:
            dialect = csv.excel
            dialect.delimiter = ";" if sample.count(";") >= sample.count(",") else ","

        reader = csv.reader(raw.splitlines(), dialect)
        rows = [list(r) for r in reader if any(str(c or "").strip() for c in r)]
        if not rows:
            raise ValueError("CSV пустой")

        header = [_norm_header(c) for c in rows[0]]

        def _col(*needles: str) -> Optional[int]:
            for i, h in enumerate(header):
                for n in needles:
                    if n in h:
                        return i
            return None

        col_name = _col("наименование товара", "наименование", "название")
        col_article = _col("артикул продавца")
        if col_article is None:
            for i, h in enumerate(header):
                if "артикул" not in h:
                    continue
                if any(
                    x in h
                    for x in ("wb", "вб", "ozon", "озон", "яндекс", "yandex", "offer")
                ):
                    continue
                col_article = i
                break
        col_nmid = _col("артикул wb", "артикул вб", "nmid")
        col_ozon = _col("sku ozon", "sku озон", "артикул ozon", "артикул озон")
        col_yandex = _col("яндекс", "yandex", "offerid", "offer id", "offer_id")
        col_box = _col("кратность", "коробе", "в коробе")
        col_cat = _col("категория")
        col_skip = _col("без проверки gtin", "без проверки gtin маркировки", "gtin")

        if col_name is None:
            raise ValueError(
                "В CSV нет столбца «Наименование товара»"
            )

        created = 0
        updated = 0
        skipped = 0
        for row in rows[1:]:
            def cell(idx: Optional[int]) -> str:
                if idx is None or idx >= len(row):
                    return ""
                return str(row[idx] or "").strip()

            name = cell(col_name)
            article = cell(col_article)
            nmid = cell(col_nmid)
            ozon = cell(col_ozon)
            yandex = cell(col_yandex)
            category = cell(col_cat)
            skip = _truthy_flag(cell(col_skip)) if col_skip is not None else False
            box_raw = cell(col_box)
            box_qty = None  # type: Optional[int]
            if box_raw:
                try:
                    box_qty = int(float(box_raw.replace(",", ".")))
                    if box_qty <= 0:
                        box_qty = None
                except (TypeError, ValueError):
                    box_qty = None

            if not name and not article and not nmid:
                skipped += 1
                continue
            if not name:
                name = article or nmid or ozon or yandex
            if not name:
                skipped += 1
                continue

            existing_id = self.find_id_by_article_or_nmid(article, nmid)
            self.save(
                existing_id,
                name,
                article,
                nmid,
                box_qty,
                category,
                skip,
                None,
                ozon_sku=ozon,
                yandex_offer_id=yandex,
            )
            if existing_id:
                updated += 1
            else:
                created += 1

        return {
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "total": created + updated,
        }

    def delete(self, product_id: int) -> None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT photo_path FROM product_photos WHERE id = ?", (product_id,)
            ).fetchone()
            conn.execute("DELETE FROM product_photos WHERE id = ?", (product_id,))
            conn.commit()
        if row and row["photo_path"]:
            try:
                Path(str(row["photo_path"])).unlink(missing_ok=True)  # type: ignore[call-arg]
            except TypeError:
                # Python 3.8 Path.unlink has no missing_ok
                p = Path(str(row["photo_path"]))
                if p.exists():
                    p.unlink()
            except Exception:
                pass

    def skip_gtin_map(self) -> Dict[str, bool]:
        """Keys: supplier_article lower + nm_id string."""
        out = {}  # type: Dict[str, bool]
        for p in self.list_all():
            if not p.get("skip_kiz_gtin_check"):
                continue
            art = str(p.get("supplier_article") or "").strip().lower()
            nm = str(p.get("wb_nmid") or "").strip()
            if art:
                out[art] = True
            if nm:
                out[nm] = True
        return out


class CategoryService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def list_all(self) -> List[Dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM product_categories
                ORDER BY sort_order ASC, name COLLATE NOCASE
                """
            ).fetchall()
        return Database.rows_to_dicts(rows)

    def save_all(self, items: List[Dict[str, Any]]) -> None:
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute("DELETE FROM product_categories")
            for i, item in enumerate(items):
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                boxes = item.get("boxes_per_pallet")
                try:
                    boxes_i = int(boxes) if boxes not in (None, "") else None
                except (TypeError, ValueError):
                    boxes_i = None
                conn.execute(
                    """
                    INSERT INTO product_categories(
                        name, boxes_per_pallet, sort_order, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (name, boxes_i, i, now, now),
                )
            conn.commit()

    def names(self) -> List[str]:
        return [str(c.get("name") or "") for c in self.list_all() if c.get("name")]
