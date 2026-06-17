"""Generate placeholder hotel photos (valid JPEGs Booking will accept) for the DEMO
property, then patch the photos[] array into its JSON with absolute paths.

These are DUMMY images — a gradient background + the property name + a caption — so
the Photos step of the wizard has real files to upload. Replace with real photos
anytime by swapping the files (the JSON paths stay the same).

Run:  .venv/Scripts/python.exe scripts/gen_photos.py
"""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
JSON_PATH = ROOT / "examples" / "booking_engine" / "DEMO-HOTEL-01.json"
PHOTO_DIR = ROOT / "data" / "photos" / "DEMO-HOTEL-01"

W, H = 1600, 1067         # 3:2 landscape — comfortably above OTA minimums
HOTEL = "Maple Ridge Inn"
MIN_KB = 110              # MMT requires >=100KB; aim a little over

# (filename, caption, room_type|None, top-color, bottom-color)  — >=10 photos, all landscape
SHOTS = [
    ("exterior.jpg",      "Hotel exterior - mountain view", None,                 (38, 84, 124),  (96, 152, 180)),
    ("exterior_2.jpg",    "Front entrance",                 None,                 (44, 92, 116),  (110, 160, 176)),
    ("lobby.jpg",         "Reception & lobby",              None,                 (60, 72, 96),   (132, 144, 168)),
    ("standard_room.jpg", "Standard Room",                 "Standard Room",       (70, 110, 90),  (150, 190, 160)),
    ("deluxe_room.jpg",   "Deluxe Valley-View",            "Deluxe Valley-View",  (120, 96, 60),  (196, 168, 120)),
    ("family_suite.jpg",  "Family Suite",                  "Family Suite",        (96, 64, 110),  (170, 140, 190)),
    ("bathroom.jpg",      "Private bathroom",              None,                  (64, 96, 110),  (150, 184, 196)),
    ("restaurant.jpg",    "In-house restaurant",           None,                  (110, 70, 70),  (190, 150, 150)),
    ("garden.jpg",        "Garden & valley view",          None,                  (56, 104, 72),  (140, 196, 150)),
    ("corridor.jpg",      "Corridor",                      None,                  (80, 84, 100),  (160, 164, 184)),
    ("breakfast.jpg",     "Breakfast spread",              None,                  (124, 100, 58),  (210, 184, 128)),
    ("view.jpg",          "Mountain view from room",       None,                  (48, 96, 132),  (120, 168, 196)),
]


def _font(size: int):
    for name in ("arial.ttf", "DejaVuSans.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _gradient(top, bottom):
    img = Image.new("RGB", (W, H), top)
    draw = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        c = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.line([(0, y), (W, y)], fill=c)
    return img, draw


def _texture(img, sigma=30, alpha=0.16):
    """Blend fine random noise into the image. Smooth gradients compress to ~50KB —
    high-frequency texture pushes the JPEG above the 100KB minimum and looks less flat."""
    try:
        noise = Image.effect_noise((W, H), sigma).convert("RGB")
        return Image.blend(img, noise, alpha)
    except Exception:
        return img


def _centered(draw, text, font, y, fill):
    box = draw.textbbox((0, 0), text, font=font)
    w = box[2] - box[0]
    draw.text(((W - w) / 2, y), text, font=font, fill=fill)


def main():
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    title_f, cap_f, tag_f = _font(64), _font(40), _font(26)
    photos = []
    for fname, caption, room_type, top, bottom in SHOTS:
        img, draw = _gradient(top, bottom)
        # subtle frame
        draw.rectangle([24, 24, W - 24, H - 24], outline=(255, 255, 255, 60), width=3)
        _centered(draw, HOTEL, title_f, H * 0.36, (255, 255, 255))
        _centered(draw, caption, cap_f, H * 0.50, (235, 240, 248))
        _centered(draw, "DEMO • placeholder photo", tag_f, H * 0.60, (210, 220, 235))
        img = _texture(img)                            # add detail so the JPEG clears 100KB
        out = PHOTO_DIR / fname
        # guarantee >= MIN_KB: re-encode with more texture/quality until it's big enough
        for q, a in ((90, 0.16), (94, 0.22), (96, 0.30)):
            _texture(img, alpha=a).save(out, "JPEG", quality=q)
            if out.stat().st_size >= MIN_KB * 1024:
                break
        entry = {"path": str(out), "caption": caption}
        if room_type:
            entry["room_type"] = room_type
        photos.append(entry)
        print(f"  wrote {out}")

    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    data["photos"] = photos
    JSON_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n  patched {len(photos)} photos into {JSON_PATH.name}")


if __name__ == "__main__":
    main()
