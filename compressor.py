"""Core PDF compression logic, tuned for scanned documents.

Three modes:
  lossless  - pixel-identical output. Losslessly re-optimizes every image
              (mozjpeg Huffman/progressive for JPEGs, CCITT G4 for 1-bit
              images, colour reduction plus PNG predictors for the rest) and
              decodes each changed image again to prove it is unchanged.
              Recompresses every Flate stream with libdeflate, subsets
              fonts, drops page thumbnails and private editor data
              (/PieceInfo) and packs objects into object streams.
  smart     - recompress/downsample embedded images in place. Keeps any text
              layer (OCR), links and bookmarks. Runs the lossless pass
              first and only re-encodes an image when that beats its
              losslessly optimized size.
  rasterize - re-render every page as a single JPEG. Strongest compression,
              but drops selectable text. Best for pure image scans.

The "Visually lossless" preset picks the JPEG quality per image: the lowest
quality whose SSIM against the source stays above SSIM_TARGET, encoded with
full-resolution colour (4:4:4) so coloured text stays sharp.
"""

import io
import os
import shutil
import zlib

import fitz  # PyMuPDF
import numpy as np
from PIL import Image

try:  # optional: lossless JPEG re-optimization
    import mozjpeg_lossless_optimization as mozjpeg
except ImportError:  # pragma: no cover
    mozjpeg = None
try:  # optional: stronger deflate (libdeflate level 12)
    import deflate as libdeflate
except ImportError:  # pragma: no cover
    libdeflate = None

AUTO = None  # jpeg quality placeholder: search for the best quality per image

PRESETS = {
    # name: (target dpi, jpeg quality)
    "Lossless (identical quality)": (None, None),
    "Visually lossless (auto)": (None, AUTO),  # keep full resolution
    "Low (best quality)": (200, 80),
    "Medium": (150, 65),
    "High (smallest file)": (110, 50),
    "Extreme": (80, 40),
}
LOSSLESS = "Lossless (identical quality)"

SSIM_TARGET = 0.985
_LOSSY_FILTERS = ("DCT", "JPX", "JBIG2", "CCITT")


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


# ---------- JPEG encoding ----------

def _optimize_jpeg(data):
    """Lossless mozjpeg pass (optimal Huffman tables, progressive scans)."""
    if mozjpeg is None:
        return data
    try:
        out = mozjpeg.optimize(data)
    except Exception:
        return data
    return out if len(out) < len(data) else data


def _encode_jpeg(img, quality, subsampling=-1):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True, progressive=True,
             subsampling=subsampling)
    return _optimize_jpeg(buf.getvalue())


def _box_mean(a, k=8):
    """Mean over every k x k window (valid region) via summed-area table."""
    s = np.pad(a, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    return (s[k:, k:] - s[:-k, k:] - s[k:, :-k] + s[:-k, :-k]) / (k * k)


def _ssim(x, y):
    if min(x.shape) < 8:
        return 1.0
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    mx, my = _box_mean(x), _box_mean(y)
    vx = _box_mean(x * x) - mx * mx
    vy = _box_mean(y * y) - my * my
    cxy = _box_mean(x * y) - mx * my
    m = ((2 * mx * my + c1) * (2 * cxy + c2)) / ((mx * mx + my * my + c1) * (vx + vy + c2))
    return float(m.mean())


def _encode_jpeg_auto(img, target=SSIM_TARGET):
    """Smallest JPEG (4:4:4) whose luma SSIM vs. img is >= target."""
    ref = np.asarray(img.convert("L"), dtype=np.float64)
    lo, hi = 40, 95
    best = _encode_jpeg(img, hi, subsampling=0)
    while lo <= hi:
        q = (lo + hi) // 2
        data = _encode_jpeg(img, q, subsampling=0)
        got = np.asarray(Image.open(io.BytesIO(data)).convert("L"), dtype=np.float64)
        if _ssim(ref, got) >= target:
            best, hi = data, q - 1
        else:
            lo = q + 1
    return best


def _encode(img, quality):
    if quality is AUTO:
        return _encode_jpeg_auto(img)
    return _encode_jpeg(img, quality)


# ---------- lossless image optimization ----------

def _png_stream(img):
    """Encode img as PNG and return (zlib data with PNG predictors, bpc, PLTE).

    PNG's IDAT payload is exactly a PDF FlateDecode stream with
    /DecodeParms <</Predictor 15>>, so it can be dropped in as-is.
    """
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    b = buf.getvalue()
    pos, idat, ihdr, plte = 8, [], b"", None
    while pos < len(b):
        n = int.from_bytes(b[pos:pos + 4], "big")
        typ, data = b[pos + 4:pos + 8], b[pos + 8:pos + 8 + n]
        pos += 12 + n
        if typ == b"IHDR":
            ihdr = data
        elif typ == b"PLTE":
            plte = data
        elif typ == b"IDAT":
            idat.append(data)
    return b"".join(idat), ihdr[8], plte


def _resolve(doc, kind, val):
    """Turn an xref_get_key result into a PDF object string."""
    if kind == "xref":
        return doc.xref_object(int(val.split()[0]), compressed=True).strip()
    return val


def _colorspace(doc, xref):
    """(component count, colorspace string, is plain Device{Gray,RGB})."""
    kind, val = doc.xref_get_key(xref, "ColorSpace")
    if kind == "null":
        return None, None, False
    obj = _resolve(doc, kind, val)
    if obj in ("/DeviceGray", "/CalGray"):
        return 1, val, obj == "/DeviceGray"
    if obj in ("/DeviceRGB", "/CalRGB"):
        return 3, val, obj == "/DeviceRGB"
    if obj == "/DeviceCMYK":
        return 4, val, False
    if obj.startswith("[") and "/ICCBased" in obj:
        icc = int(obj.split("/ICCBased")[1].split()[0])
        n = int(doc.xref_get_key(icc, "N")[1])
        return n, val if kind == "xref" else obj, False
    return None, None, False  # Indexed, Separation, Lab, ... leave alone


def _to_gray(img):
    """RGB image whose channels are all equal -> exact grayscale copy."""
    a = np.asarray(img)
    if (a[..., 0] == a[..., 1]).all() and (a[..., 1] == a[..., 2]).all():
        return Image.fromarray(np.ascontiguousarray(a[..., 0]))
    return None


def _to_palette(img, base):
    """Image with <= 256 distinct colours -> (exact "P" image, /Indexed space).

    The lookup table holds the original sample values in the original colour
    space (base), so colour management is untouched.
    """
    if not img.getcolors(256):
        return None
    a = np.asarray(img)
    if img.mode == "RGB":
        keys = (a[..., 0].astype(np.uint32) << 16) | (a[..., 1].astype(np.uint32) << 8) | a[..., 2]
        pal = np.unique(keys)
        lookup = np.stack([(pal >> 16) & 255, (pal >> 8) & 255, pal & 255], 1).astype(np.uint8)
    else:
        keys = a
        pal = np.unique(keys)
        lookup = pal.astype(np.uint8)[:, None]
    p = Image.fromarray(np.searchsorted(pal, keys).astype(np.uint8)).convert("P")
    p.putpalette(np.repeat(lookup, 3 // lookup.shape[1], 1).ravel().tolist())
    cs = f"[/Indexed {base} {len(pal) - 1} <{lookup.tobytes().hex()}>]"
    return p, cs


def _parms(width, colors, bpc):
    return f"<</Predictor 15/Colors {colors}/BitsPerComponent {bpc}/Columns {width}>>"


def _deflate(data):
    """Smallest standard zlib stream we can make for data."""
    best = zlib.compress(data, 9)
    if libdeflate is not None:
        out = libdeflate.zlib_compress(data, 12)
        if len(out) < len(best):
            best = out
    return best


def _ccitt_g4(img):
    """1-bit image -> raw CCITT Group 4 data (single strip, via libtiff)."""
    buf = io.BytesIO()
    img.save(buf, "TIFF", compression="group4", tiffinfo={278: img.height})
    tif = Image.open(io.BytesIO(buf.getvalue()))
    offsets, counts = tif.tag_v2[273], tif.tag_v2[279]
    if len(offsets) != 1:
        return None
    return buf.getvalue()[offsets[0]:offsets[0] + counts[0]]


_IMAGE_KEYS = ("Filter", "DecodeParms", "ColorSpace", "BitsPerComponent")


def _pixels(doc, xref):
    pix = fitz.Pixmap(doc, xref)
    if pix.colorspace and pix.colorspace.n == 1 and not pix.alpha:
        pix = fitz.Pixmap(fitz.csRGB, pix)  # compare gray and RGB on equal terms
    return pix.width, pix.height, pix.n, pix.samples


def _replace_verified(doc, xref, data, keys):
    """Swap in a new encoding, keeping it only if it decodes identically."""
    old_raw = doc.xref_stream_raw(xref)
    old_keys = {k: doc.xref_get_key(xref, k) for k in _IMAGE_KEYS}
    before = _pixels(doc, xref)
    doc.update_stream(xref, data, compress=0)
    for k, v in keys.items():
        doc.xref_set_key(xref, k, v)
    try:
        same = _pixels(doc, xref) == before
    except Exception:
        same = False
    if not same:
        doc.update_stream(xref, old_raw, compress=0)
        for k, (kind, val) in old_keys.items():
            doc.xref_set_key(xref, k, "null" if kind == "null" else val)
    return same


def _optimize_flate_image(doc, xref, protected):
    filt = doc.xref_get_key(xref, "Filter")[1]
    if any(f in filt for f in _LOSSY_FILTERS):
        return
    w = int(doc.xref_get_key(xref, "Width")[1])
    h = int(doc.xref_get_key(xref, "Height")[1])
    bpc_key = doc.xref_get_key(xref, "BitsPerComponent")
    bpc = int(bpc_key[1]) if bpc_key[0] == "int" else 1
    is_mask = doc.xref_get_key(xref, "ImageMask")[1] == "true"
    if is_mask:
        n, cs, device = 1, None, False
    else:
        n, cs, device = _colorspace(doc, xref)
        if n is None:
            return
    raw = doc.xref_stream(xref)
    if raw is None or len(raw) < h * ((w * n * bpc + 7) // 8):
        return
    raw = raw[:h * ((w * n * bpc + 7) // 8)]
    old_len = len(doc.xref_stream_raw(xref))

    # Rank Flate layouts by their zlib size, then deflate only the winner
    # with libdeflate. Entries: (zlib size, payload, bpc, parms, space).
    # Plain samples (no predictor) are the always-valid baseline.
    flate = [(len(zlib.compress(raw, 9)), raw, bpc, "null", cs)]
    g4 = None

    mode = {(1, 1): "1", (1, 8): "L", (3, 8): "RGB"}.get((n, bpc))
    if mode:
        img = Image.frombytes(mode, (w, h), raw)
        cands = [(img, n, cs)]
        if mode == "1":
            g4 = _ccitt_g4(img)
        # Changing the sample layout is only exact when nothing else (Decode,
        # colour-key Mask, use as an SMask) depends on it; RGB->gray also
        # needs a plain device space so no ICC profile is bypassed.
        if (bpc == 8 and n in (1, 3) and xref not in protected
                and doc.xref_get_key(xref, "Decode")[0] == "null"
                and doc.xref_get_key(xref, "Mask")[0] != "array"):
            base = cs
            gray = _to_gray(img) if n == 3 and device else None
            if gray is not None:
                img, base, cands = gray, "/DeviceGray", [(gray, 1, "/DeviceGray")]
            # Gray always has <= 256 levels; a palette only helps if it
            # lowers the bit depth.
            pal = _to_palette(img, base) if img.mode == "RGB" or len(img.getcolors(256)) <= 16 else None
            if pal:
                cands.append((pal[0], 1, pal[1]))
        for im, colors, space in cands:
            data, out_bpc, _ = _png_stream(im)
            flate.append((len(data), zlib.decompress(data), out_bpc,
                          _parms(w, colors, out_bpc), space))

    _, payload, out_bpc, parms, space = min(flate, key=lambda c: c[0])
    data, filt = _deflate(payload), "/FlateDecode"
    if g4 and len(g4) < len(data):
        data, filt, out_bpc = g4, "/CCITTFaxDecode", 1
        parms = f"<</K -1/Columns {w}/Rows {h}/BlackIs1 true>>"
        space = cs
    if len(data) >= old_len:
        return
    keys = {"Filter": filt, "DecodeParms": parms, "BitsPerComponent": str(out_bpc)}
    if not is_mask:
        keys["ColorSpace"] = space
    _replace_verified(doc, xref, data, keys)


def _optimize_jpeg_image(doc, xref):
    if mozjpeg is None or doc.xref_get_key(xref, "Filter")[1] != "/DCTDecode":
        return
    raw = doc.xref_stream_raw(xref)
    new = _optimize_jpeg(raw)
    if len(new) < len(raw):
        # Verification also catches marker loss (e.g. Adobe APP14 transform).
        _replace_verified(doc, xref, new, {"Filter": "/DCTDecode", "DecodeParms": "null"})


def _image_xrefs(doc):
    out, protected = [], set()
    for x in range(1, doc.xref_length()):
        try:
            if doc.xref_get_key(x, "Subtype")[1] != "/Image":
                continue
        except Exception:
            continue
        out.append(x)
        for key in ("SMask", "Mask"):
            kind, val = doc.xref_get_key(x, key)
            if kind == "xref":
                protected.add(int(val.split()[0]))
    return out, protected


def _lossless_pass(doc, progress=None):
    """Pixel-identical size optimizations on an open document."""
    xrefs, protected = _image_xrefs(doc)
    for i, xref in enumerate(xrefs):
        try:
            _optimize_jpeg_image(doc, xref)
            _optimize_flate_image(doc, xref, protected)
        except Exception:
            pass  # unusual image: keep it exactly as it was
        if progress:
            progress(i + 1, len(xrefs))
    for x in range(1, doc.xref_length()):
        # Page thumbnails and private app data (Illustrator/Photoshop
        # /PieceInfo) are never drawn by viewers.
        for key in ("Thumb", "PieceInfo"):
            try:
                if doc.xref_get_key(x, key)[0] != "null":
                    doc.xref_set_key(x, key, "null")
            except Exception:
                pass
    try:
        doc.subset_fonts()
    except Exception:
        pass


def _recompress_streams(doc):
    """Re-deflate every non-image Flate stream harder (fonts, page contents,
    ICC profiles...); the decoded bytes are unchanged. Images were already
    handled by the lossless pass."""
    for x in range(1, doc.xref_length()):
        try:
            if (not doc.xref_is_stream(x)
                    or doc.xref_get_key(x, "Subtype")[1] == "/Image"
                    or doc.xref_get_key(x, "Filter")[1] != "/FlateDecode"):
                continue
            raw = doc.xref_stream_raw(x)
            data = _deflate(zlib.decompress(raw))
            if len(data) < len(raw):
                parms = doc.xref_get_key(x, "DecodeParms")
                doc.update_stream(x, data, compress=0)
                doc.xref_set_key(x, "Filter", "/FlateDecode")
                if parms[0] != "null":
                    doc.xref_set_key(x, "DecodeParms", parms[1])
        except Exception:
            continue


def _save(doc, dst):
    # Clean and deflate with MuPDF first, then squeeze every stream harder
    # and write without "clean" so the stronger streams are kept.
    data = doc.tobytes(garbage=4, deflate=True, deflate_images=True,
                       deflate_fonts=True, clean=True)
    out = fitz.open("pdf", data)
    _recompress_streams(out)
    out.save(dst, garbage=4, deflate=True, use_objstms=1, compression_effort=100)
    out.close()


# ---------- modes ----------

def _open(src):
    doc = fitz.open(src)
    if doc.needs_pass:
        raise ValueError("PDF is password protected")
    return doc


def _image_to_pil(doc, xref):
    pix = fitz.Pixmap(doc, xref)
    if pix.alpha:
        return None  # transparency - leave untouched
    if pix.n >= 4 or (pix.colorspace and pix.colorspace.n not in (1, 3)):
        pix = fitz.Pixmap(fitz.csRGB, pix)  # CMYK / other -> RGB
    mode = "L" if pix.n == 1 else "RGB"
    return Image.frombytes(mode, (pix.width, pix.height), pix.samples)


def _compress_lossless(src, dst, progress=None):
    doc = _open(src)
    _lossless_pass(doc, progress)
    _save(doc, dst)
    doc.close()


def _compress_smart(src, dst, dpi, quality, grayscale, progress=None):
    doc = _open(src)
    _lossless_pass(doc)
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
            if dpi and cur_dpi > dpi * 1.1:
                scale = dpi / cur_dpi
                size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
                img = img.resize(size, Image.LANCZOS)
            if grayscale and img.mode != "L":
                img = img.convert("L")
            data = _encode(img, quality)
            # The lossless pass already ran, so this is the size the image
            # would have untouched: re-encode only if it beats that.
            if len(data) < len(doc.xref_stream_raw(xref)):
                page.replace_image(xref, stream=data)
        if progress:
            progress(i + 1, total)
    _save(doc, dst)
    doc.close()


def _compress_rasterize(src, dst, dpi, quality, grayscale, progress=None):
    doc = _open(src)
    out = fitz.open()
    cs = fitz.csGRAY if grayscale else fitz.csRGB
    total = doc.page_count
    for i, page in enumerate(doc):
        pix = page.get_pixmap(dpi=dpi, colorspace=cs, alpha=False)
        mode = "L" if grayscale else "RGB"
        img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
        new_page = out.new_page(width=page.rect.width, height=page.rect.height)
        new_page.insert_image(new_page.rect, stream=_encode(img, quality))
        if progress:
            progress(i + 1, total)
    out.set_metadata(doc.metadata or {})
    _save(out, dst)
    out.close()
    doc.close()


def compress_pdf(src, dst, preset=LOSSLESS, mode="smart",
                 grayscale=False, progress=None):
    """Compress src into dst. Returns (original_size, new_size).

    mode is "lossless", "smart" or "rasterize"; the lossless preset forces
    lossless mode. If compression would make the file bigger, the original
    is copied as-is.
    """
    dpi, quality = PRESETS[preset]
    if preset == LOSSLESS:
        mode = "lossless"
    tmp = dst + ".tmp"
    try:
        if mode == "lossless":
            _compress_lossless(src, tmp, progress)
        elif mode == "rasterize":
            _compress_rasterize(src, tmp, dpi or 300, quality, grayscale, progress)
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
