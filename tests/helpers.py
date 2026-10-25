"""Synthetic data for the tests. Nothing here is real brand data."""
import io

import openpyxl
from PIL import Image

HDR = ["Date", "Aggregator", "SubAggregator", "Brand", "AgentID", "GoogleID", "MessageType", "AgentName", "Template", "Messages Submitted", "Messages Sent",
       "Messages Delivered", "Messages Read", "Messages Failed", "Messages Expired", "Messages DeliveryRate", "ReadRate", "P2AResponses", "P2AResponseRate", "P2AMessages"]


def _row(date, brand, gid, tpl, sub, sent, dl, rd, p2a):
    fail, exp = sub - sent, sent - dl
    return [date, "PartnerX", "-", brand, "id1", gid, "Promo", "Demo", tpl, sub, sent, dl, rd, fail, exp,
            f"{dl / sent * 100:.2f} %", f"{rd / dl * 100:.2f} %", p2a, f"{p2a / rd * 100:.2f} %", 0]


def sheet_bytes(brand="DEMO BRAND LTD", extra_brand=False):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(HDR)
    g = "demo_abc123_agent"
    rows = [
        ("2026-08-05", g, "demo_code_100", 1000000, 600000, 540000, 100000, 5200),
        ("2026-08-12", g, "demo_generic", 1200000, 700000, 630000, 118000, 1900),
        ("2026-08-19", g, "demo_raffle", 900000, 500000, 440000, 90000, 800),
        ("2026-08-26", g, "demo_flat_min", 800000, 480000, 430000, 85000, 1500),
        ("2025-08-06", g, "old_code_50", 700000, 420000, 410000, 60000, 4200),
        ("2025-08-14", g, "old_generic", 600000, 360000, 350000, 52000, 1100),
    ]
    for r in rows:
        ws.append(_row(r[0], brand, r[1], r[2], r[3], r[4], r[5], r[6], r[7]))
    ws.append([None] * 20)
    ws.append([2025] + [None] * 19)
    if extra_brand:
        ws.append(_row("2026-08-09", "OTHER BRAND LTD", "oth_x_agent", "oth_t1", 500000, 300000, 270000, 50000, 900))
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


TEMPLATES = [
    {"template": "demo_code_100", "label": "Code offer", "short": "Code offer", "creative": "A bold banner reading FLAT 100 OFF with the product shown.",
     "title": "Flat \u20B9100 off with code SAVE100", "description": "Use code SAVE100 on orders above \u20B9499. Ends tonight.", "button": "Shop Now (URL)"},
    {"template": "demo_generic", "label": "Generic sale", "short": "Generic sale", "creative": "A colourful banner reading BIG SALE.",
     "title": "Big sale is here", "description": "Shop now for great deals on your favourites.", "button": "Shop Now (URL)"},
    {"template": "demo_raffle", "label": "Raffle", "short": "Raffle", "creative": "A prize photo with TAP TO ENTER.",
     "title": "[v1], you're eligible for the [v2] Giveaway!", "description": "It's your LUCKY DAY! Stand a chance to win exclusive gifts. WIN BIG! Hurry, the clock's ticking! Shop for \u20B93,000+ between 4 and 6 PM to be considered for the draw this evening only.", "button": "Shop Now (URL)"},
    {"template": "demo_flat_min", "label": "Minimum discount", "short": "Minimum discount", "creative": "A model with the text MIN 30% OFF.",
     "title": "PRICE DROP ALERT: MIN 30% OFF!", "description": "[v1], grab MIN 30% OFF on top picks! Hurry!", "button": "Shop Now (URL)"},
    {"template": "old_code_50", "label": "Old code offer", "short": "Old code", "creative": "(no graphic available)",
     "title": "[v2], you've unlocked a coupon", "description": "Get [v3] OFF with code [v4]. Valid only till [v5].", "button": "Shop Now (URL)"},
    {"template": "old_generic", "label": "Old generic", "short": "Old generic", "creative": "(no graphic available)",
     "title": "Festive picks for you", "description": "Great gifts for everyone you love.", "button": "Shop now (URL)"},
]


def png_bytes(color=(200, 60, 60), size=(800, 400)):
    bio = io.BytesIO()
    Image.new("RGB", size, color).save(bio, format="PNG")
    return bio.getvalue()


def images():
    return {t["template"] + ".png": png_bytes((40 + 30 * i, 80, 160)) for i, t in enumerate(TEMPLATES[:4])}
