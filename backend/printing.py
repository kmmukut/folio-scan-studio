"""Validated print options, PDF preparation and local preview helpers.

No printer host, queue name, shell commands, or arbitrary CUPS option is accepted
from an HTTP client. PDF page selections are applied before CUPS N-up layout.
"""
from __future__ import annotations

import io
import os
import re
from pathlib import Path
from PIL import Image, ImageOps
import fitz  # PyMuPDF

# A name/CUPS media combination is only shown as definitely supported when the
# actual queue advertises it (via lpoptions). These are safe optional presets.
MEDIA = {
    'default': (None, 'Printer default'),
    'letter': ('Letter', 'US Letter · 8.5 × 11 in (216 × 279 mm)'),
    'legal': ('Legal', 'US Legal · 8.5 × 14 in (216 × 356 mm)'),
    'a4': ('A4', 'A4 · 210 × 297 mm (8.27 × 11.69 in)'),
    'a5': ('A5', 'A5 · 148 × 210 mm (5.83 × 8.27 in)'),
    'a6': ('A6', 'A6 · 105 × 148 mm (4.13 × 5.83 in)'),
    'b5': ('B5', 'B5 (ISO) · 176 × 250 mm (6.93 × 9.84 in)'),
    'executive': ('Executive', 'Executive · 7.25 × 10.5 in (184 × 267 mm)'),
    'statement': ('Statement', 'Statement · 5.5 × 8.5 in (140 × 216 mm)'),
    'folio': ('Folio', 'Folio · 8.5 × 13 in (216 × 330 mm)'),
    'com10': ('COM10', '#10 envelope · 4⅛ × 9½ in (105 × 241 mm)'),
    'dl': ('DL', 'DL envelope · 110 × 220 mm (4.33 × 8.66 in)'),
    'c5': ('C5', 'C5 envelope · 162 × 229 mm (6.38 × 9.02 in)'),
    'c6': ('C6', 'C6 envelope · 114 × 162 mm (4.49 × 6.38 in)'),
    'monarch': ('Monarch', 'Monarch envelope · 3⅞ × 7½ in (98 × 191 mm)'),
}
# Only used to render custom-percentage PDF pages, not to guess a tray choice.
SHEET_POINTS = {
    'letter': (612, 792), 'legal': (612, 1008), 'a4': (595.276, 841.89),
    'a5': (419.528, 595.276), 'a6': (297.638, 419.528),
    'b5': (498.898, 708.661), 'executive': (522, 756),
    'statement': (396, 612), 'folio': (612, 936),
    'com10': (684, 297), 'dl': (623.622, 311.811), 'c5': (649.134, 459.213),
    'c6': (459.213, 323.15), 'monarch': (540, 279),
}
# Different PPDs and IPP queues use different tokens for identical sizes.
# Only explicitly recognized, driver-advertised tokens can be selected. Never
# fabricate a device capability based solely on a printer model name.
MEDIA_ALIASES = {
    'letter': ('Letter', 'na_letter_8.5x11in'),
    'legal': ('Legal', 'na_legal_8.5x14in'),
    'a4': ('A4', 'iso_a4_210x297mm'),
    'a5': ('A5', 'iso_a5_148x210mm'),
    'a6': ('A6', 'iso_a6_105x148mm'),
    'b5': ('B5', 'ISOB5', 'iso_b5_176x250mm'),
    'executive': ('Executive', 'na_executive_7.25x10.5in'),
    'statement': ('Statement', 'na_invoice_5.5x8.5in', 'na_statement_5.5x8.5in'),
    'folio': ('Folio', 'USFolio', 'na_folio_8.5x13in'),
    'com10': ('COM10', 'Env10', 'EnvNo10', 'No10', 'No.10',
              'na_number-10_4.125x9.5in', 'na_number10_4.125x9.5in'),
    'dl': ('DL', 'EnvDL', 'iso_dl_110x220mm'),
    'c5': ('C5', 'EnvC5', 'iso_c5_162x229mm'),
    'c6': ('C6', 'EnvC6', 'iso_c6_114x162mm'),
    'monarch': ('Monarch', 'EnvMonarch', 'na_monarch_3.875x7.5in'),
}
ENVELOPE_MEDIA = frozenset(('com10','dl','c5','c6','monarch'))


def normalized_media_name(token: str) -> str:
    return re.sub(r'[^a-z0-9]', '', str(token).lower())


def match_media_choices(advertised: list[str]) -> dict[str,str]:
    """Return Folio size IDs mapped to the actual queue-specific CUPS values."""
    available={normalized_media_name(token):token for token in advertised}
    mapping={}
    for key, names in MEDIA_ALIASES.items():
        for name in names:
            actual=available.get(normalized_media_name(name))
            if actual:
                mapping[key]=actual
                break
    return mapping


SIDES = {'one-sided','two-sided-long-edge','two-sided-short-edge'}
ORIENTATION = {'auto':None, 'portrait':3, 'landscape':4,
               'reverse-landscape':5, 'reverse-portrait':6}
NUP = {1,2,4,6,9,16}
MAX_PREVIEW_PAGES = 3000


class PrintFileError(ValueError):
    pass


def inspect_file(path: Path, filename: str, guest: bool = False) -> dict:
    """Inspect actual file content, with lenient but authenticated PDF parsing."""
    kind = filename.lower().rsplit('.',1)[-1]
    if kind == 'pdf':
        try:
            with fitz.open(path, filetype='pdf') as doc:
                if doc.needs_pass:
                    raise PrintFileError('This PDF requires a password. Unlock it before printing.')
                pages = doc.page_count
                if pages < 1 or pages > MAX_PREVIEW_PAGES:
                    raise PrintFileError('Unsupported PDF page count')
                if guest and pages > 20:
                    raise PrintFileError('Guest documents may have at most 20 pages')
                first = doc.load_page(0)
                return {'kind':'pdf', 'pages':pages, 'width':round(first.rect.width),
                        'height':round(first.rect.height)}
        except PrintFileError:
            raise
        except Exception as exc:
            raise PrintFileError('PDF could not be read. The file may be damaged or incomplete.') from exc
    if kind in ('png','jpg','jpeg'):
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                w,h=image.size
                if w * h > (22_000_000 if guest else 100_000_000):
                    raise PrintFileError('Image is too large to process safely')
                return {'kind':'image','pages':1,'width':w,'height':h}
        except PrintFileError:
            raise
        except Exception as exc:
            raise PrintFileError('The selected image could not be read') from exc
    raise PrintFileError('Only PDF, PNG and JPEG files are supported')


def render_preview(path: Path, kind: str, page: int) -> bytes:
    """Return a small page JPEG, never executed as browser HTML or PDF."""
    if kind == 'pdf':
        with fitz.open(path, filetype='pdf') as doc:
            if doc.needs_pass or not (1 <= page <= doc.page_count):
                raise PrintFileError('Page is unavailable')
            leaf=doc.load_page(page-1)
            # Limit dimensions and total render pixels for hostile PDF input.
            scale=min(1.45, 1200/max(leaf.rect.width,1), 1600/max(leaf.rect.height,1),
                      (2_000_000/max(leaf.rect.width*leaf.rect.height,1))**.5)
            pix=leaf.get_pixmap(matrix=fitz.Matrix(scale,scale), colorspace=fitz.csRGB,
                                alpha=False, annots=True)
            return pix.tobytes('jpeg', jpg_quality=82)
    if page != 1:
        raise PrintFileError('Page is unavailable')
    with Image.open(path) as src:
        image=ImageOps.exif_transpose(src).convert('RGB')
        image.thumbnail((1200,1600))
        buffer=io.BytesIO();image.save(buffer,'JPEG',quality=83,optimize=True)
        return buffer.getvalue()


def page_numbers(spec: str, count: int) -> list[int]:
    """Source document pages, before number-up (always ascending, unique)."""
    if not spec or spec.strip().lower() in ('all','*'):
        return list(range(count))
    if len(spec)>500 or not re.fullmatch(r'[0-9,\-\s]+',spec):
        raise PrintFileError('Use page numbers like 1,3-5,8')
    values=set()
    for part in spec.split(','):
        part=part.strip()
        if not part:
            raise PrintFileError('Page ranges cannot contain an empty entry')
        match=re.fullmatch(r'(\d+)(?:\s*-\s*(\d+))?',part)
        if not match:
            raise PrintFileError('Use page numbers like 1,3-5,8')
        start=int(match.group(1)); end=int(match.group(2) or start)
        if start<1 or end<start or end>count:
            raise PrintFileError(f'Page range must be between 1 and {count}')
        values.update(range(start-1,end))
    return sorted(values)


def prepare_pdf(path: Path, kind: str, output: Path, *, pages: str = 'all',
                media: str = 'default', scale: str = 'fit', color_mode: str = 'auto') -> int:
    """Generate safe PDF with the selected source pages and optional precise scaling.

    We apply page selection ourselves, *before* CUPS number-up, because CUPS
    page-ranges otherwise refers to output sheets on some filter chains.
    """
    if kind == 'image':
        with Image.open(path) as image:
            ImageOps.exif_transpose(image).convert('RGB').save(output.with_name('image-source.pdf'),'PDF',resolution=150)
        src_path=output.with_name('image-source.pdf')
    else:
        src_path=path
    with fitz.open(src_path, filetype='pdf') as src:
        if src.needs_pass:
            raise PrintFileError('Password-protected PDF cannot be printed')
        ids=page_numbers(pages,src.page_count)
        if not ids:
            raise PrintFileError('Choose at least one page')
        # Create a fresh document so page numbers and N-up are predictable.
        with fitz.open() as selected:
            for index in ids:
                selected.insert_pdf(src, from_page=index,to_page=index,links=False,annots=True)
            if scale.isdigit() and int(scale)!=100:
                factor=int(scale)/100
                with fitz.open() as adjusted:
                    for n in range(selected.page_count):
                        page=selected[n]
                        w,h=SHEET_POINTS.get(media,(page.rect.width,page.rect.height))
                        if w<1 or h<1:
                            raise PrintFileError('Invalid page size')
                        target=adjusted.new_page(width=w,height=h)
                        dw=page.rect.width*factor;dh=page.rect.height*factor
                        rect=fitz.Rect((w-dw)/2,(h-dh)/2,(w+dw)/2,(h+dh)/2)
                        target.show_pdf_page(rect,selected,n,keep_proportion=True)
                    adjusted.save(output,garbage=3,deflate=True)
            else:
                selected.save(output,garbage=3,deflate=True)
        count=len(ids)
    if color_mode in ('gray','bw'):
        convert_color(output,color_mode)
    return count


def convert_color(path: Path, mode: str) -> None:
    """Convert chosen PDF pages to true grayscale or 1-bit before CUPS processing.

    This is a deliberate raster conversion for explicit print-color overrides.
    Auto and Color preserve vector PDFs instead. Resolution is bounded to
    prevent multi-megapixel PDF pages from exhausting a small Docker VM.
    """
    if mode not in ('gray','bw'):
        return
    converted=path.with_suffix('.converted.pdf')
    try:
        with fitz.open(path) as original, fitz.open() as pdf:
            for item in original:
                box=item.rect
                scale=min((220 if mode=='bw' else 200)/72,
                          (18_000_000/max(box.width*box.height,1))**.5)
                pix=item.get_pixmap(matrix=fitz.Matrix(scale,scale),colorspace=fitz.csGRAY,alpha=False)
                image=Image.frombytes('L',(pix.width,pix.height),pix.samples)
                if mode=='bw':
                    # Threshold may be tuned in future driver presets.
                    image=image.point(lambda value:255 if value>=170 else 0).convert('1')
                stream=io.BytesIO()
                image.save(stream,format='PNG',optimize=True)
                page=pdf.new_page(width=box.width,height=box.height)
                page.insert_image(page.rect,stream=stream.getvalue())
            pdf.save(converted,garbage=3,deflate=True)
        os.replace(converted,path)
    finally:
        converted.unlink(missing_ok=True)
