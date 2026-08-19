# -*- coding: utf-8 -*-
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.db import Database
from app.paths import photos_dir
from app.wb import utc_now


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
    ) -> int:
        name = str(name or "").strip()
        if not name:
            raise ValueError("Укажите название товара")
        article = str(supplier_article or "").strip()
        nmid = str(wb_nmid or "").strip()
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
                photo_path = str(existing["photo_path"]) if existing and existing["photo_path"] else None

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
                        name=?, supplier_article=?, wb_nmid=?, box_qty=?,
                        product_category=?, skip_kiz_gtin_check=?,
                        photo_path=COALESCE(?, photo_path), updated_at=?
                    WHERE id=?
                    """,
                    (
                        name,
                        article,
                        nmid,
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
                    name, supplier_article, wb_nmid, box_qty, product_category,
                    skip_kiz_gtin_check, photo_path, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    article,
                    nmid,
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
