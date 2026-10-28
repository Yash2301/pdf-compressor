"""Core PDF compression logic, tuned for scanned documents.

Two modes:
  smart     - recompress/downsample embedded images in place. Keeps any text
              layer (OCR), links and bookmarks. Good default.
  rasterize - re-render every page as a single JPEG. Strongest compression,
              but drops selectable text. Best for pure image scans.
"""

import io
import os
import shutil

import fitz  # PyMuPDF
from PIL import Image

PRESETS = {
    # name: (target dpi, jpeg quality)
    "Low (best quality)": (200, 80),
    "Medium (recommended)": (150, 65),
    "High (smallest file)": (110, 50),
    "Extreme": (80, 40),
}


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _image_to_pil(doc, xref):
    pix = fitz.Pixmap(doc, xref)
    if pix.alpha:
        return None  # transparency - leave untouched
    if pix.n >= 4 or (pix.colorspace and pix.colorspace.n not in (1, 3)):
        pix = fitz.Pixmap(fitz.csRGB, pix)  # CMYK / other -> RGB
    mode = "L" if pix.n == 1 else "RGB"
    return Image.frombytes(mode, (pix.width, pix.height), pix.samples)


def _compress_smart(src, dst, dpi, quality, grayscale, progress=None):
    doc = fitz.open(src)
    if doc.needs_pass:
        raise ValueError("PDF is password protected")
    done = set()
    total = doc.page_count
    for i, page in enumerate(doc):
        for info in page.get_image_info(xrefs=True):
            xref = info.get("xref")
            if not xref or xref in done:
                continue
            done.add(xref)
            meta = doc.extract_image(xref)
            # 1-bit scans (CCITT/JBIG2) are already tiny; JPEG would bloat them.
            if not meta or meta.get("bpc", 8) == 1 or meta.get("smask"):
                continue
            img = _image_to_pil(doc, xref)
            if img is None:
                continue
            bbox = fitz.Rect(info["bbox"])
            if bbox.width <= 0 or bbox.height <= 0:
                continue
            cur_dpi = img.width / (bbox.width / 72)
            if cur_dpi > dpi * 1.1:
                scale = dpi / cur_dpi
                size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
                img = img.resize(size, Image.LANCZOS)
            if grayscale and img.mode != "L":
                img = img.convert("L")
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=quality, optimize=True)
            data = buf.getvalue()
            if len(data) < len(meta["image"]):
                page.replace_image(xref, stream=data)
        if progress:
            progress(i + 1, total)
    doc.save(dst, garbage=4, deflate=True, clean=True)
    doc.close()


def _compress_rasterize(src, dst, dpi, quality, grayscale, progress=None):
    doc = fitz.open(src)
    if doc.needs_pass:
        raise ValueError("PDF is password protected")
    out = fitz.open()
    cs = fitz.csGRAY if grayscale else fitz.csRGB
    total = doc.page_count
    for i, page in enumerate(doc):
        pix = page.get_pixmap(dpi=dpi, colorspace=cs, alpha=False)
        mode = "L" if grayscale else "RGB"
        img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, optimize=True)
        new_page = out.new_page(width=page.rect.width, height=page.rect.height)
        new_page.insert_image(new_page.rect, stream=buf.getvalue())
        if progress:
            progress(i + 1, total)
    out.set_metadata(doc.metadata or {})
    out.save(dst, garbage=4, deflate=True)
    out.close()
    doc.close()


def compress_pdf(src, dst, preset="Medium (recommended)", mode="smart",
                 grayscale=False, progress=None):
    """Compress src into dst. Returns (original_size, new_size).

    If compression would make the file bigger, the original is copied as-is.
    """
    dpi, quality = PRESETS[preset]
    tmp = dst + ".tmp"
    try:
        if mode == "rasterize":
            _compress_rasterize(src, tmp, dpi, quality, grayscale, progress)
        else:
            _compress_smart(src, tmp, dpi, quality, grayscale, progress)
        old, new = os.path.getsize(src), os.path.getsize(tmp)
        if new >= old:
            shutil.copyfile(src, dst)
            new = old
        else:
            os.replace(tmp, dst)
        return old, new
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
