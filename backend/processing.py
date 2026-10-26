"""Non-destructive image processing for Folio Scan Studio.

Auto-straighten is intentionally conservative: on ambiguous page content we
return zero, rather than damaging a perfectly readable page. It needs OpenCV
and NumPy, which only the Full edition installs; everything else here uses
Pillow alone, so the Lite edition can import this module.
"""
from __future__ import annotations

import math

from PIL import Image, ImageOps, ImageEnhance

from backend import features


def find_deskew_angle(image: Image.Image) -> tuple[float, int]:
    """Estimate correction from repeated, mutually agreeing horizontal strokes.

    Returns (angle in PIL CCW degrees, independent supporting line groups).
    Photographs, sparse diagrams and uncertain scans are left unchanged.
    """
    features.require('auto_straighten')
    import cv2
    import numpy as np
    mono = ImageOps.grayscale(image)
    if mono.width < 250 or mono.height < 250:
        return 0.0, 0
    mono.thumbnail((1100, 1450), Image.Resampling.BILINEAR)
    arr = np.asarray(mono)
    # Omit the outer 3% of the page: shadows, platen edges, and scanned borders
    # are often straighter/longer than the actual text.
    h, w = arr.shape
    my, mx = max(2, int(h*.03)), max(2, int(w*.03))
    crop = arr[my:h-my, mx:w-mx]
    blurred = cv2.GaussianBlur(crop, (3,3), 0)
    binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    ink_coverage = np.count_nonzero(binary) / binary.size
    if ink_coverage < .002 or ink_coverage > .32:
        return 0.0, 0
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(12, binary.shape[1] // 80),1))
    merged = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    width = merged.shape[1]
    lines = cv2.HoughLinesP(merged, rho=1, theta=np.pi/720,
                            threshold=max(20,width//36),
                            minLineLength=max(42,width//9),
                            maxLineGap=max(10,width//35))
    if lines is None:
        return 0.0, 0
    candidates = []
    for x1,y1,x2,y2 in lines.reshape(-1,4):
        length = math.hypot(float(x2-x1), float(y2-y1))
        if length < width*.10 or length > width*.84:
            continue
        angle = math.degrees(math.atan2(float(y2-y1),float(x2-x1)))
        if abs(angle) <= 6.0:
            candidates.append((angle, length, (float(y1)+float(y2))/2))
    if len(candidates) < 8:
        return 0.0, len(candidates)
    median = float(np.median([a for a,_,_ in candidates]))
    aligned = [(a,weight,y) for a,weight,y in candidates if abs(a-median)<=.85]
    if len(aligned) < 7 or len(aligned)/len(candidates) < .72:
        return 0.0, len(aligned)
    # Near-identical top/bottom Hough segments from the same line count as one.
    rows = sorted(y for _,_,y in aligned)
    groups = [rows[0]]
    for y in rows[1:]:
        if y-groups[-1] >= max(6, merged.shape[0]*.008):
            groups.append(y)
    if len(groups) < 5 or groups[-1]-groups[0] < merged.shape[0]*.12:
        return 0.0, len(groups)
    values = np.array([a for a,_,_ in aligned])
    mad = float(np.median(np.abs(values-median)))
    if mad > .45:
        return 0.0, len(groups)
    weights = np.array([min(length,width*.6) for _,length,_ in aligned])
    correction = float(np.average(values, weights=weights))
    # Avoid applying imperceptible rotations and false positives > 5 degrees.
    if abs(correction) < .25 or abs(correction) > 5.75:
        return 0.0, len(groups)
    return round(correction,2), len(groups)


def transform_page(image: Image.Image, page: dict, *, uncropped: bool=False) -> Image.Image:
    rotation = int(page.get('rotation', 0))
    deskew = float(page.get('deskew', 0))
    scale = int(page.get('scale', 100))
    # Page geometry is intentionally ordered: right-angle orientation, then
    # the persistent crop, then fine deskew. Fine rotations must operate on
    # the CROPPED pixels or content outside the crop leaks back into view.
    if rotation:
        image = image.rotate(-rotation, expand=True, resample=Image.Resampling.BICUBIC, fillcolor='white')
    crop = page.get('crop')
    if crop and not uncropped:
        x, y, w, h = crop
        x1, y1 = round(x * image.width), round(y * image.height)
        x2, y2 = round((x + w) * image.width), round((y + h) * image.height)
        image = image.crop((x1, y1, max(x1 + 1, x2), max(y1 + 1, y2)))
    if abs(deskew) >= .01:
        image = image.rotate(deskew, expand=True, resample=Image.Resampling.BICUBIC, fillcolor='white')
    if scale != 100:
        image=image.resize((max(1,round(image.width*scale/100)),
                            max(1,round(image.height*scale/100))),Image.Resampling.LANCZOS)
    # Auto levels is deliberately conservative. Fine adjustments are applied
    # afterward; all transformations remain reversible in page metadata.
    if page.get('auto_levels', False):
        if image.mode not in ('RGB', 'L'):
            image = image.convert('RGB')
        image = ImageOps.autocontrast(image, cutoff=0.5, preserve_tone=True)
    brightness = int(page.get('brightness', 0) or 0)
    contrast = int(page.get('contrast', 0) or 0)
    if brightness:
        image = ImageEnhance.Brightness(image).enhance(max(.01, 1.0 + brightness / 100.0))
    if contrast:
        image = ImageEnhance.Contrast(image).enhance(max(.01, 1.0 + contrast / 100.0))
    if page.get('export_mode','Original')=='Gray':
        image=image.convert('L')
    return image
