#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Entry point: FeedPilot Desktop — Поставки ВБ ФБС."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.main import run  # noqa: E402


if __name__ == "__main__":
    sys.exit(run())
