"""Shared photo pre-processor — the one place every OTA upload path runs images through.

OTAs (Agoda, MakeMyTrip, Booking, …) reject or mangle raw MIS photos in the same ways:
  • phone photos carry an EXIF orientation tag and upload SIDEWAYS unless it's applied;
  • portrait/square photos get auto-rotated or letterboxed ugly — they want landscape;
  • files must sit inside a size window (commonly ≥100KB, ≤10MB) and be jpg/png.

`prepare_photo` normalizes one image for upload, in this exact order:
  1. apply EXIF orientation (stop the sideways/rotated uploads),
  2. convert to RGB (kills CMYK/odd-mode "blank tile" rejects),
  3. if it's portrait or square, pad WHITE bars on the left and right to make it landscape
     (we pad, never crop — no part of the photo is lost),
  4. guarantee a minimum size (upscale tiny images),
  5. resize / recompress so the JPEG lands between min_bytes and max_bytes,
  6. save a clean baseline JPEG.

Results are cached by (source path + parameters) so re-runs are instant. Returns the
prepared file path, or None if the image can't be made usable.
"""
from __future__ import annotations

import hashlib
import os
import tempfile

# OTA-friendly defaults. Agoda: ≥800×600, ≤10MB, jpg/png. MakeMyTrip: ≥100KB. 4:3 landscape.
DEFAULT_MIN_W = 800
DEFAULT_MIN_H = 600
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_MIN_BYTES = 100 * 1024
DEFAULT_LANDSCAPE_RATIO = 4 / 3          # target width:height when padding a portrait
DEFAULT_MAX_DIM = 2560                    # cap the long edge so files stay small & fast
WHITE = (255, 255, 255)


def _cache_dir() -> str:
    d = os.path.join(tempfile.gettempdir(), "accounts_pilot_photos", "prep")
    os.makedirs(d, exist_ok=True)
    return d


def prepare_photo(
    src: str,
    *,
    min_w: int = DEFAULT_MIN_W,
    min_h: int = DEFAULT_MIN_H,
    max_bytes: int = DEFAULT_MAX_BYTES,
    min_bytes: int = DEFAULT_MIN_BYTES,
    landscape_ratio: float = DEFAULT_LANDSCAPE_RATIO,
    max_dim: int = DEFAULT_MAX_DIM,
    bg=WHITE,
) -> str | None:
    """Normalize one image for OTA upload (see module docstring). Cached; returns path or None."""
    try:
        from PIL import Image, ImageOps
    except Exception:
        return None
    if not src or not os.path.exists(src):
        return None

    key = f"{src}|{os.path.getmtime(src)}|{min_w}x{min_h}|{min_bytes}-{max_bytes}|{landscape_ratio:.3f}|{max_dim}"
    dest = os.path.join(_cache_dir(), hashlib.sha1(key.encode("utf-8")).hexdigest() + ".jpg")
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest

    try:
        im = Image.open(src)
        im = ImageOps.exif_transpose(im)              # 1. honour EXIF orientation (un-rotate)
        im = im.convert("RGB")                         # 2. RGB (fixes blank/white tiles)

        w, h = im.size
        # 3. portrait or square → pad white bars left+right so it becomes landscape
        if h >= w:
            new_w = max(w + 2, int(round(h * landscape_ratio)))
            canvas = Image.new("RGB", (new_w, h), bg)
            canvas.paste(im, ((new_w - w) // 2, 0))
            im = canvas
            w, h = im.size

        # 4. enforce a minimum size (upscale images that are too small for the OTA)
        if w < min_w or h < min_h:
            scale = max(min_w / w, min_h / h)
            im = im.resize((max(min_w, int(round(w * scale))), max(min_h, int(round(h * scale)))))
            w, h = im.size

        # 5. cap the long edge, then a quality/size loop to land under max_bytes
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            im = im.resize((int(round(w * scale)), int(round(h * scale))))

        quality = 88
        im.save(dest, "JPEG", quality=quality, optimize=True)
        guard = 0
        while os.path.getsize(dest) > max_bytes and guard < 8:
            guard += 1
            w, h = im.size
            im = im.resize((max(min_w, int(w * 0.85)), max(min_h, int(h * 0.85))))
            quality = max(68, quality - 4)
            im.save(dest, "JPEG", quality=quality, optimize=True)

        # ensure we clear the lower bound too (bump quality for very small outputs)
        if os.path.getsize(dest) < min_bytes:
            im.save(dest, "JPEG", quality=96, optimize=True)

        return dest if os.path.getsize(dest) <= max_bytes else None
    except Exception:
        return None


def prepare_many(paths, **opts) -> list[str]:
    """Run a list of images through prepare_photo, dropping any that come back unusable."""
    out: list[str] = []
    for p in (paths or []):
        q = prepare_photo(p, **opts)
        if q and q not in out:
            out.append(q)
    return out
