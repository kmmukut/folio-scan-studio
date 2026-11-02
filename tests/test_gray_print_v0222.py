"""v0.22.2: printing in Grayscale keeps text as text. Folio used to turn each
page into a 200 dpi picture, whose text a laser printer prints light and soft."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pymupdf as fitz
import pytest
from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import printing  # noqa: E402

needs_gs = pytest.mark.skipif(shutil.which('gs') is None, reason='Ghostscript (in both images) is not installed')


def colourful(path: Path) -> None:
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 100), 'Invoice 2026-114: total due', fontsize=11, color=(.8, .1, .1))
        page.draw_rect(fitz.Rect(72, 120, 300, 160), color=(0, 0, 1), fill=(.2, .6, 1))
        doc.new_page(width=842, height=595).insert_text((72, 100), 'A landscape page', fontsize=11)
        doc.save(path)


def greatest_tint(page) -> int:
    """How far any pixel is from gray (0: the page has no colour at all)."""
    pix = page.get_pixmap(colorspace=fitz.csRGB, alpha=False)
    red, green, blue = Image.frombytes('RGB', (pix.width, pix.height), pix.samples).split()
    return max(ImageChops.difference(red, green).getextrema()[1], ImageChops.difference(green, blue).getextrema()[1])


@needs_gs
def test_grayscale_keeps_text_as_text(tmp_path):
    source, target = tmp_path / 'source.pdf', tmp_path / 'gray.pdf'
    colourful(source)
    assert printing.prepare_pdf(source, 'pdf', target, color_mode='gray') == 2
    with fitz.open(target) as doc:
        first, second = doc
        assert 'Invoice 2026-114: total due' in first.get_text() and not first.get_images()
        assert greatest_tint(first) <= 2
        pix = first.get_pixmap(colorspace=fitz.csGRAY)
        assert min(pix.samples) < 160  # The coloured parts are gray now, not gone
        assert (second.rect.width, second.rect.height) == (842, 595) and 'A landscape page' in second.get_text()


@needs_gs
def test_black_text_stays_solid_black(tmp_path):
    """Black prints solid, whichever way the file wrote it; grays keep their shade."""
    source, target = tmp_path / 'source.pdf', tmp_path / 'gray.pdf'
    with fitz.open() as doc:
        page = doc.new_page()
        for line, colour in enumerate([(0, 0, 0), (0, 0, 0, 1), (.6, .4, .4, 1), (.2, .2, .2)]):
            page.insert_text((72, 100 + 30 * line), 'Hamburgefonstiv', fontsize=14, color=colour)
        doc.save(source)
    printing.prepare_pdf(source, 'pdf', target, color_mode='gray')
    with fitz.open(target) as doc:
        darkest = []
        for line in range(4):
            pix = doc[0].get_pixmap(matrix=fitz.Matrix(4, 4), colorspace=fitz.csGRAY,
                                    clip=fitz.Rect(70, 86 + 30 * line, 220, 104 + 30 * line))
            darkest.append(min(pix.samples))
    assert darkest[:3] == [0, 0, 0]  # RGB black, CMYK black (K only), rich black
    assert 45 <= darkest[3] <= 56  # 20% gray stays 20% gray


@pytest.mark.parametrize('ghostscript', ['missing', 'failing'])
def test_grayscale_falls_back_to_pictures(tmp_path, monkeypatch, ghostscript):
    false = shutil.which('false')
    monkeypatch.setattr(printing.shutil, 'which', lambda name: None if ghostscript == 'missing' else false)
    source, target = tmp_path / 'source.pdf', tmp_path / 'gray.pdf'
    colourful(source)
    assert printing.prepare_pdf(source, 'pdf', target, color_mode='gray') == 2
    with fitz.open(target) as doc:
        assert doc.page_count == 2 and doc[0].get_images() and not doc[0].get_text().strip()
        assert greatest_tint(doc[0]) <= 2
    assert not list(tmp_path.glob('*.converted.pdf'))


@needs_gs
def test_cancelling_a_print_stops_the_conversion(tmp_path):
    source, target = tmp_path / 'source.pdf', tmp_path / 'gray.pdf'
    colourful(source)

    def cancelled(done, total):
        raise RuntimeError('Cancelled')
    with pytest.raises(RuntimeError, match='Cancelled'):
        printing.prepare_pdf(source, 'pdf', target, color_mode='gray', progress=cancelled)
    assert not list(tmp_path.glob('*.converted.pdf'))


@needs_gs
def test_black_and_white_is_still_a_1_bit_picture(tmp_path):
    source, target = tmp_path / 'source.pdf', tmp_path / 'bw.pdf'
    colourful(source)
    printing.prepare_pdf(source, 'pdf', target, color_mode='bw')
    with fitz.open(target) as doc:
        image = doc[0].get_images(full=True)
        assert image and fitz.Pixmap(doc, image[0][0]).n == 1
