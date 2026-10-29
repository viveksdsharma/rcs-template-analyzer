"""
Text features. Used by the Internal ML model (scoring and training). They look at the card text only.

Text is normalised first (NFKC), so styled Unicode letters such as the bold-italic forms of INR 100
are read as the plain words they imitate.
"""
import re
import unicodedata

# Variables: [v1], [0], [1], [price], [0_sh_code], {{name}}.  Icon placeholders: [gift icon].
VAR_RE = re.compile(r"\[\s*(?:v?\d+|price|name|amount|date|offer|code)[^\]]*\]|\{\{?[^{}]*\}?\}", re.I)
ICON_RE = re.compile(r"\[[^\]]*\bicons?\b[^\]]*\]", re.I)
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\U0001F1E6-\U0001F1FF\u2600-\u27BF\u2B50\u2B06\u2B1B\u2B1C\u2B55\u2705\u23E9-\u23F3\u231A\u231B\u2611\u2764]")
CURRENCY_RE = re.compile(r"[\u20B9$]|\bRs\.?\s*\d|\bINR\b", re.I)
DIGIT_RE = re.compile(r"\d")
DEADLINE_RE = re.compile(r"\btoday\b|\btonight\b|\bends?\b|\bvalid (?:till|until|only)\b|\bonly till\b|\bhurry\b|\blast day\b|\blimited time\b|\bbefore (?:they|it)(?:'|\u2019)?s? gone\b|\bexpires?\b|\bonly for\b|\bexpires? soon\b|\boffer valid\b", re.I)
CHANCE_RE = re.compile(r"\bwin\b|\bwinner\b|\bchance\b|\bgiveaway\b|\blucky\b|\braffle\b|\bcontest\b|\bsweepstake", re.I)
FLOOR_RE = re.compile(r"\bmin(?:imum)?\b|\bupto\b|\bup to\b", re.I)
BENEFIT_RE = re.compile(r"\bfree\b|\boff\b|\bcashback\b|\bgifts?\b|\bdiscounts?\b|\bsave\b|\bsavings?\b|\bdeals?\b|\bcoupons?\b|\brewards?\b|\bvouchers?\b|\bbonus\b|\bwaiver\b|\bno cost\b|\bzero\b|\bcredit\b|\boffers?\b|%", re.I)
CODE_RE = re.compile(r"\bcode\b|\bcoupon\b", re.I)
SECOND_ACTION_RE = re.compile(r"\b(?:type|send|reply|text)\s+[\"\u201c'\u2018]?hi\b", re.I)
GENERIC_CTA = {"shop now", "know more", "learn more", "click here", "explore more", "view more", "visit", "get now", "explore"}


def iconify(s):
    """Display text for brand-facing files: each emoji becomes [icon] (runs become [icons]), as in the approved reports."""
    t = str(s or "")
    t = ICON_RE.sub("[icon]", t)
    t = EMOJI_RE.sub("[icon]", t)
    t = t.replace("\ufe0f", "").replace("\u200d", "").replace("\U0001F3FB", "").replace("\U0001F3FC", "").replace("\U0001F3FD", "").replace("\U0001F3FE", "").replace("\U0001F3FF", "")
    t = re.sub(r"(?:\[icons?\][ \t]*){2,}", "[icons] ", t)
    return re.sub(r"[ \t]+", " ", t).strip() if "\n" not in t else "\n".join(re.sub(r"[ \t]+", " ", ln).strip() for ln in t.split("\n"))


def normalize(s):
    """NFKC: styled letters and digits become plain ones. Display text elsewhere is left untouched."""
    return unicodedata.normalize("NFKC", str(s or ""))


def strip_vars(s):
    return VAR_RE.sub(" ", ICON_RE.sub(" ", s or ""))


def _words(s):
    return re.findall(r"[A-Za-z0-9\u20B9%\u2019'\-]+", s)


def extract(card):
    """card: {title, description, button}. description may be a list of lines."""
    title = normalize(card.get("title", ""))
    desc = card.get("description", "")
    desc = normalize(" ".join(desc) if isinstance(desc, list) else desc)
    button = normalize(card.get("button", ""))
    full_raw = f"{title} {desc}"
    full = strip_vars(full_raw)
    t_clean, d_clean = strip_vars(title), strip_vars(desc)
    words = _words(full)
    caps_words = [w for w in words if len(w) >= 3 and w.isalpha() and w.isupper()]
    btn = re.sub(r"\(.*?\)", "", button).strip().lower()
    n_vars = len(VAR_RE.findall(ICON_RE.sub(" ", full_raw)))
    emoji = len(EMOJI_RE.findall(full_raw)) + len(ICON_RE.findall(full_raw))
    has_digit = bool(DIGIT_RE.search(full))
    has_currency = bool(CURRENCY_RE.search(full))
    has_percent = "%" in full
    has_code = bool(CODE_RE.search(full))
    quantified = has_digit or has_currency or has_percent or has_code
    return {
        "title_chars": len(t_clean.strip()),
        "title_words": len(_words(t_clean)),
        "desc_words": len(_words(d_clean)),
        "total_words": len(words),
        "has_number": int(has_digit or has_currency or has_percent),
        "has_currency": int(has_currency),
        "has_percent": int(has_percent),
        "has_code": int(has_code),
        "quantified": int(quantified),
        "names_benefit": int(bool(BENEFIT_RE.search(full))),
        "has_deadline": int(bool(DEADLINE_RE.search(full))),
        "chance": int(bool(CHANCE_RE.search(full))),
        "floor_or_ceiling": int(bool(FLOOR_RE.search(full))),
        "competing_actions": int(bool(SECOND_ACTION_RE.search(full_raw))),
        "n_vars": n_vars,
        "n_emoji": emoji,
        "caps_share": (len(caps_words) / len(words)) if words else 0.0,
        "exclaims": full_raw.count("!"),
        "title_has_digit": int(bool(DIGIT_RE.search(t_clean))),
        "cta_generic": int(btn in GENERIC_CTA),
        "cta_words": len(_words(btn)),
    }


MODEL_FEATURES = ["has_number", "has_code", "names_benefit", "has_deadline", "chance", "floor_or_ceiling", "competing_actions",
                  "cta_generic", "title_words", "desc_words", "n_vars", "n_emoji", "caps_share", "exclaims"]

_CODE_AFTER_WORD = re.compile(r"\bcode\s*:?\s*[a-z0-9]+", re.I)


def token_set(title, desc):
    """Words that identify the text. Variables, icons, emoji and coupon codes are removed so that
    sends differing only in the code count as the same text."""
    t = normalize(f"{title or ''} {' '.join(desc) if isinstance(desc, list) else desc or ''}")
    t = strip_vars(t)
    t = _CODE_AFTER_WORD.sub(" ", t)
    t = EMOJI_RE.sub(" ", t).lower()
    return frozenset(w for w in re.findall(r"[a-z0-9\u20B9%]+", t) if w)


def jaccard(a, b):
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)
