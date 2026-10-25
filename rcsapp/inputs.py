"""Parsing the things people upload: card-text spreadsheets (as partners send them) and screenshots (single files or a zip)."""
import io
import os
import re
import zipfile

import pandas as pd
from PIL import Image

IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def _norm(s):
    return re.sub(r"[^a-z]", "", str(s).lower())


def parse_template_content(data, filename="content.xlsx"):
    """Partners (for example Tanla) send a sheet with Template Name, Card Title, Card Description, CTA Phrase, CTA Type."""
    bio = io.BytesIO(data)
    df = pd.read_csv(bio, dtype=object) if filename.lower().endswith(".csv") else pd.read_excel(bio, dtype=object)
    cols = {c: _norm(c) for c in df.columns}
    pick = {}
    for key, tests in (("template", ("templatename", "template", "templateid")), ("title", ("cardtitle", "title")),
                       ("description", ("carddescription", "description", "body")), ("button", ("ctaphrase", "button", "cta", "ctalabel"))):
        for c, n in cols.items():
            if n in tests and key not in pick:
                pick[key] = c
    missing = [k for k in ("template", "title", "description") if k not in pick]
    if missing:
        raise ValueError(f"The card-text sheet needs columns for: {missing}. Columns found: {list(df.columns)}")
    ctype = next((c for c, n in cols.items() if n in ("ctatype", "buttontype")), None)
    out = []
    for _, r in df.iterrows():
        name = str(r[pick["template"]]).strip()
        if not name or name.lower() == "nan":
            continue
        btn = str(r[pick["button"]]).strip() if "button" in pick and str(r[pick["button"]]) != "nan" else ""
        if ctype and str(r[ctype]) not in ("nan", ""):
            btn = f"{btn} ({str(r[ctype]).strip()})" if btn else str(r[ctype]).strip()
        out.append({"template": name, "title": "" if str(r[pick["title"]]) == "nan" else str(r[pick["title"]]).strip(),
                    "description": "" if str(r[pick["description"]]) == "nan" else str(r[pick["description"]]).strip(), "button": btn})
    return out


def read_images(files):
    """files: list of (name, bytes). Zips are expanded. Returns {file_name: bytes} for image files only."""
    out = {}
    for name, data in files:
        low = name.lower()
        if low.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for zi in z.infolist():
                    base = os.path.basename(zi.filename)
                    if zi.is_dir() or not base or base.startswith(".") or "__MACOSX" in zi.filename:
                        continue
                    if base.lower().endswith(IMG_EXT):
                        out[base] = z.read(zi)
        elif low.endswith(IMG_EXT):
            out[os.path.basename(name)] = data
    return out


def match_images(template_names, images):
    """Match by file-name stem (case-insensitive). Returns {template: file_name}."""
    stems = {os.path.splitext(n)[0].lower(): n for n in images}
    res = {}
    for t in template_names:
        key = t.lower()
        if key in stems:
            res[t] = stems[key]
        else:
            hits = [n for s, n in stems.items() if key in s or s in key]
            if len(hits) == 1:
                res[t] = hits[0]
    return res


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)[:80]


def save_image(data, out_dir, stem, max_w=1200):
    """Normalise to an RGB JPEG of sensible size. Returns the file name."""
    im = Image.open(io.BytesIO(data))
    if im.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", im.size, "white")
        im = im.convert("RGBA")
        bg.paste(im, mask=im.split()[-1])
        im = bg
    else:
        im = im.convert("RGB")
    if im.width > max_w:
        im = im.resize((max_w, round(im.height * max_w / im.width)))
    os.makedirs(out_dir, exist_ok=True)
    name = safe_name(stem) + ".jpg"
    im.save(os.path.join(out_dir, name), quality=88)
    return name
