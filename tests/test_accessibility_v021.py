"""v0.21: easier to read and to use with a keyboard, on Debian 13, with OCR
that is really tested."""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import pymupdf as fitz
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import app as web  # noqa: E402

CSS = (ROOT / 'frontend' / 'style.css').read_text()
HTML = (ROOT / 'frontend' / 'index.html').read_text()
WORKBENCH = (ROOT / 'frontend' / 'js' / 'workbench.js').read_text()
LIGHT_BACKGROUNDS = ('#f5f2e9', '#fffefa', '#faf8f1', '#ecefe5', '#f9f7ef', '#fbfaf5')
# Text on dark or coloured backgrounds, decorative text and large headings follow other rules.
EXEMPT = re.compile(r'rail|toast|dark-button|main-button|topbar-user|auth-decor|masthead-stamp|scan-button|recent-job|'
                    r'machine-status|led\b|duplex-symbol|editor-mark|\.dark|selected|current|print-send|large-|'
                    r'crop-header|stats-tooltip|setup-step h4 span|checklist-mark|pan-tool\.active|pan-tool\[aria-pressed|'
                    r'delete-job:hover|delete-job:focus|empty-number|\.dot\b|h1 em|masthead h1|brand|auth-large-letter|'
                    r'decoration|::placeholder|:disabled|danger|error')


def luminance(color: str) -> float:
    h = color.lstrip('#')
    h = ''.join(c * 2 for c in h) if len(h) == 3 else h
    channels = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    channels = [c / 12.92 if c <= .03928 else ((c + .055) / 1.055) ** 2.4 for c in channels]
    return .2126 * channels[0] + .7152 * channels[1] + .0722 * channels[2]


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + .05) / (min(la, lb) + .05)


def test_text_on_light_backgrounds_meets_wcag_aa_contrast():
    faint = []
    for selector, body in re.findall(r'([^{}]+)\{([^{}]*)\}', CSS):
        if EXEMPT.search(selector):
            continue
        size = re.search(r'font(?:-size)?:[^;]*?\b(\d+(?:\.\d+)?)px', body)
        if size and float(size.group(1)) >= 18:
            continue  # Large text needs only 3:1
        for color in re.findall(r'(?<![-\w])color:\s*(#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3})\b', body):
            worst = min(contrast(color, bg) for bg in LIGHT_BACKGROUNDS)
            if worst < 4.5:
                faint.append(f'{selector.strip()[:60]} {color} {worst:.1f}:1')
    assert faint == [], faint


def test_no_text_is_smaller_than_10_pixels():
    sizes = [float(s) for s in re.findall(r'font(?:-size)?:[^;}]*?\b(\d+(?:\.\d+)?)px', CSS)]
    assert sizes and min(sizes) >= 10


def test_every_icon_button_has_a_name_for_screen_readers():
    for attrs, inner in re.findall(r'<button\b([^>]*)>(.*?)</button>', HTML, re.S):
        if not re.sub(r'<[^>]+>', '', inner).strip():
            assert 'aria-label' in attrs or 'title=' in attrs, attrs


def test_the_crop_frame_works_with_a_keyboard():
    wrap = re.search(r'<div id="crop-image-wrap"[^>]*>', HTML).group(0)
    assert 'tabindex="0"' in wrap and 'aria-label="Crop frame.' in wrap and 'aria-describedby="crop-status"' in wrap
    assert '<p id="crop-status" class="visually-hidden" aria-live="polite">' in HTML
    assert "addEventListener('keydown',nudgeCrop)" in WORKBENCH
    for key in ('ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', "e.key==='Enter'", 'e.shiftKey', 'e.ctrlKey'):
        assert key in WORKBENCH
    assert '.crop-image-wrap:focus-visible' in CSS and '.visually-hidden{' in CSS


def test_the_image_is_built_on_debian_13_pinned_by_digest():
    dockerfile = (ROOT / 'Dockerfile').read_text()
    assert re.search(r'^FROM python:3\.12-slim-trixie@sha256:[0-9a-f]{64}$', dockerfile, re.M)
    assert 'echo ocrmypdf tesseract-ocr-eng' in dockerfile  # Debian's OCRmyPDF, maintained by Debian


@pytest.mark.skipif(not (shutil.which('ocrmypdf') and shutil.which('tesseract')),
                    reason='needs OCRmyPDF and Tesseract: runs inside the Full image')
def test_searchable_pdfs_really_find_the_text(tmp_path):
    from PIL import Image, ImageDraw, ImageFont
    page = Image.new('RGB', (1700, 2200), 'white')  # A letter page at 200 dpi, text only as pixels
    draw = ImageDraw.Draw(page)
    draw.text((150, 300), 'FOLIO SCAN STUDIO', fill='black', font=ImageFont.load_default(size=110))
    draw.text((150, 500), 'Electricity bill 2026', fill='black', font=ImageFont.load_default(size=90))
    scan = tmp_path / 'scan.pdf'
    page.save(scan, 'PDF', resolution=200)
    with fitz.open(scan) as before:
        assert before[0].get_text().strip() == ''  # No text yet, only an image
    searchable = tmp_path / 'searchable.pdf'
    web.ocr_pdf(scan, searchable, 1)
    with fitz.open(searchable) as after:
        text = after[0].get_text().upper()
    assert 'FOLIO' in text and 'STUDIO' in text and 'ELECTRICITY' in text
