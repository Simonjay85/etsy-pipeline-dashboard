#!/usr/bin/env python3
"""Compatibility entrypoint for PivotAndPage furniture image preparation.

The old Pillow poster/compositor workflow was retired on 2026-09-01.  This
entrypoint now delegates to the ChatGPT built-in Image Generate manifest
workflow so it cannot accidentally create branded posters, collages, or local
pseudo-lifestyle composites.
"""
from __future__ import annotations

from pivotandpage_chatgpt_imagegen_batch import main


if __name__ == "__main__":
    raise SystemExit(main())
