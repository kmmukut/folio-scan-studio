"""Which optional features this installation can offer.

Folio ships as one codebase in two editions, chosen when the image is built:

* **Full**: everything, including searchable PDFs (OCRmyPDF + Tesseract) and
  automatic straightening suggestions (OpenCV + NumPy).
* **Lite**: for Raspberry Pi–class hosts. Scanning, editing, PDF export,
  printing, printer management and accounts work exactly as in Full; the two
  heavy features above are left out of the image.

Features are detected from what is actually installed, never assumed from
the edition name, so a Lite image with OCR added by hand offers OCR.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
from functools import lru_cache

OPTIONAL = {
    'ocr': 'Searchable PDF (text recognition)',
    'auto_straighten': 'Automatic straightening suggestions',
}


class Unavailable(Exception):
    """An optional feature was requested that this installation does not include."""

    def __init__(self, feature: str):
        super().__init__(f'{OPTIONAL[feature]} is not included in the Lite edition of Folio. '
                         'Use the Full edition image to enable it.')
        self.feature = feature


@lru_cache(maxsize=1)
def detect() -> dict[str, bool]:
    return {
        'ocr': shutil.which('ocrmypdf') is not None,
        'auto_straighten': all(importlib.util.find_spec(m) is not None for m in ('cv2', 'numpy')),
    }


def available(feature: str) -> bool:
    return detect()[feature]


def require(feature: str) -> None:
    if not available(feature):
        raise Unavailable(feature)


def edition() -> str:
    """'full' or 'lite' as built (FOLIO_EDITION), else inferred from the features present."""
    built = os.environ.get('FOLIO_EDITION', '').strip().lower()
    if built in ('full', 'lite'):
        return built
    return 'full' if all(detect().values()) else 'lite'


def summary() -> dict:
    return {'edition': edition(), 'features': detect(),
            'labels': OPTIONAL}
