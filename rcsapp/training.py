"""
Training data: import a brand's portal sheet, validate it, derive the columns the model learns from, and fit the model.

Design decisions (each one fixes a problem seen in real brand data):

  * One row per template per send date. Counts (reads, clicks), not rates, so the click-rate base is known.
  * Click index = a row's click rate divided by the pooled click rate of the brand's OTHER texts in the same
    cohort and calendar year (leave-one-out). A template that holds most of the reads can no longer be its own baseline,
    and a cohort such as online / offline cannot leak into the text signal.
  * Templates that carry the same text (same template ID, or text that matches after removing variables and coupon codes)
    form one text group. Groups are never split between training and test.
  * Rows are weighted by their number of clicks, because a click rate resting on 11 clicks is not as reliable as one on 15,000.
  * The history file stores the card text, not features, so the feature definitions can improve without re-collecting.
  * Winners-only collections are flagged: a model cannot learn what separates good from bad if it only sees good.
  * Bundled seed workbooks (data/seed) are loaded at start-up, so the history survives a restart of a hosted app.
"""
import io
import json
import os
import re

import numpy as np
import pandas as pd

from . import SEED
from . import features as F
from . import metrics as M

VERTICALS = ["E-commerce", "Retail", "BFSI-Banking", "BFSI-Insurance", "BFSI-Lending", "Healthcare", "Travel", "Telecom", "Other"]
MIN_READS = 1000
MIN_OTHERS = 2
SIM_THRESHOLD = 0.85
GATE_GROUPS = 40
GATE_BRANDS = 4

HISTORY_COLS = ["brand", "vertical", "cohort", "template", "date", "year", "send_number", "text_group", "reads", "clicks", "click_rate",
                "loo_rate", "click_index", "n_baseline", "usable", "reason", "weight", "title", "description", "button", "category",
                "submitted", "sent", "delivered", "exclude", "exclude_reason", "source"]
RAW_COLS = ["brand", "vertical", "cohort", "template", "date", "reads", "clicks", "submitted", "sent", "delivered", "title", "description",
            "button", "category", "exclude", "exclude_reason", "source"]
TEXT_RAW = ("vertical", "cohort", "title", "description", "button", "category", "exclude", "exclude_reason", "source")


# --------------------------------------------------------------------------- brand names
def _bnorm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower().replace("\u2019", ""))


def brand_matches(history_brand, names):
    """True when a history brand is the same brand as any of the given names ('Axis' and 'Axis Bank' match)."""
    hb = _bnorm(history_brand)
    for n in _as_list(names):
        nb = _bnorm(n)
        if nb and hb and (nb in hb or hb in nb):
            return True
    return False


def brand_names(h):
    return [] if h is None or not len(h) else sorted(set(h["brand"].astype(str)))


def _as_list(x):
    if x is None:
        return []
    return [x] if isinstance(x, str) else [v for v in x if v]


# --------------------------------------------------------------------------- reading a brand's sheet
def parse_cohort_rules(text):
    """'offline=offline' per line (or comma separated): a template whose name contains the left side gets the label on the right."""
    rules = []
    for part in re.split(r"[\n,;]+", text or ""):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip():
                rules.append((k.strip().lower(), v.strip()))
    return rules


def cohort_of(template, rules, given=None):
    if given not in (None, "") and not (isinstance(given, float) and pd.isna(given)):
        return str(given).strip()
    low = str(template).lower()
    for sub, label in rules:
        if sub in low:
            return label
    return ""


_TITLE_RE = re.compile(r"(?s)^\s*Card Title\s*(.*?)\s*Card Description\s*(.*)$")
_DESC_LABEL = re.compile(r"^\s*Card Description\s*:?\s*", re.I)


def _s(x):
    return "" if x is None or (isinstance(x, float) and pd.isna(x)) else str(x).strip()


def split_card(title, body, exact):
    """Return (title, description). Prefers the 'exact text' column when it holds Card Title / Card Description labels."""
    title, body, exact = _s(title), _s(body), "" if _s(exact) == "" else str(exact)
    m = _TITLE_RE.match(exact)
    if m and m.group(1).strip() and m.group(2).strip():
        return m.group(1).strip(), m.group(2).strip()
    body = _DESC_LABEL.sub("", body).strip()
    if title and body.startswith(title):
        body = body[len(title):].strip()
    return title, body


def rows_from_canonical(df, brand_display="", vertical="", cohort_rules=None, source=""):
    """df comes from metrics.canonicalise(). Returns raw history rows (before finalize)."""
    rules = cohort_rules or []
    out = []
    has = lambda c: c in df.columns  # noqa: E731
    for _, r in df.iterrows():
        title, desc = split_card(r["title"] if has("title") else None, r["body"] if has("body") else None, r["exact_text"] if has("exact_text") else None)
        out.append({"brand": brand_display or str(r["brand"]), "vertical": vertical, "cohort": cohort_of(r["template"], rules, r["cohort"] if has("cohort") else None),
                    "template": r["template"], "date": r["date"].strftime("%Y-%m-%d"), "reads": r["read"], "clicks": r["p2a_responses"],
                    "submitted": r["submitted"], "sent": r["sent"], "delivered": r["delivered"], "title": title, "description": desc,
                    "button": _s(r["cta"]) if has("cta") else "", "category": _s(r["category"]) if has("category") else "",
                    "exclude": _s(r["exclude"]) if has("exclude") else "", "exclude_reason": _s(r["exclude_reason"]) if has("exclude_reason") else "", "source": source})
    return pd.DataFrame(out, columns=RAW_COLS)


def import_sheet(source, filename, brand_display, vertical, cohort_rules_text="", profile="valuefirst_portal"):
    """Read one brand's sheet. Returns (raw history rows, issues). Several brands in one sheet are not allowed here."""
    raw = M.read_table(source, filename=filename)
    df, issues = M.canonicalise(raw, profile)
    brands = sorted(df["brand"].dropna().astype(str).unique())
    if len(brands) > 1:
        raise M.MultipleBrands(f"The sheet holds {len(brands)} brands: {brands}. Import one brand per sheet.")
    issues = list(issues) + M.check_rows(df)
    rows = rows_from_canonical(df, brand_display, vertical, parse_cohort_rules(cohort_rules_text), source=filename or "")
    issues += validate_rows(rows)
    return rows, issues


# --------------------------------------------------------------------------- validation
def validate_rows(rows):
    """Errors stop the import. Warnings keep the row but flag it."""
    issues = []

    def add(level, code, msg, **kw):
        issues.append({"level": level, "code": code, "message": msg, **kw})

    if rows.empty:
        return [{"level": "error", "code": "empty", "message": "No rows to import."}]
    if rows["vertical"].eq("").any():
        add("error", "vertical_missing", "Choose the brand's vertical.")
    elif not set(rows["vertical"]).issubset(set(VERTICALS)):
        add("error", "vertical_unknown", f"Unknown vertical: {sorted(set(rows['vertical']) - set(VERTICALS))}. Use one of {VERTICALS}.")
    dup = rows.duplicated(["brand", "template", "date"], keep=False)
    if dup.any():
        names = sorted(set(rows.loc[dup, "template"]))
        add("error", "duplicate_rows", f"Duplicate rows for the same template and send date: {names[:6]}. Remove or merge them.")
    for _, r in rows.iterrows():
        t = r["template"]
        if not (str(r["title"]).strip() or str(r["description"]).strip()):
            add("warn", "no_text", f"'{t}': no card title or description, so it cannot be used for training.", template=t)
        if r["clicks"] > r["reads"]:
            add("error", "clicks_above_reads", f"'{t}': clicks are higher than reads.", template=t)
        if r["reads"] < MIN_READS:
            add("warn", "small_base", f"'{t}': only {r['reads']:,.0f} reads, so it is kept out of training.", template=t)
    fin = finalize(rows)
    if len(fin):
        valid = fin[(fin["reads"] > 0)]
        pooled = valid["clicks"].sum() / max(valid["reads"].sum(), 1)
        share_below = float((valid["clicks"] / valid["reads"] < pooled).mean()) if len(valid) else 1.0
        if len(valid) >= 4 and share_below < 0.30:
            add("warn", "winners_only", f"Only {share_below:.0%} of this brand's rows click below its own average. This looks like a collection of winners; "
                                         "a model cannot learn what separates good from bad without the weaker templates. Include every template in the period.")
        for g, sub in fin.groupby("text_group"):
            if len(sub) >= 2 and sub["click_rate"].min() > 0 and sub["click_rate"].max() / sub["click_rate"].min() > 2:
                add("info", "same_text_spread", f"The same text ({sub['template'].iloc[0]} and {len(sub) - 1} more) shows a click-rate spread of "
                                                 f"{sub['click_rate'].max() / sub['click_rate'].min():.1f}x across sends. Audience or timing, not wording, probably explains it.")
        few = fin[(fin["reason"] == "group too small") & (fin["reads"] >= MIN_READS)]
        if len(few):
            add("warn", "group_too_small", f"{len(few)} row(s) have fewer than {MIN_OTHERS} other rows in their brand, cohort and year to compare with, so they are kept out of training.")
    return issues


# --------------------------------------------------------------------------- derived columns
class _UF:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, a):
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")[:24]


def _blank(x):
    return x is None or (isinstance(x, float) and pd.isna(x)) or str(x).strip() == ""


def finalize(h, min_reads=MIN_READS, min_others=MIN_OTHERS, sim=SIM_THRESHOLD):
    """Recompute every derived column from the raw ones. Safe to call again after rows are added."""
    h = h.copy()
    for c in RAW_COLS:
        if c not in h.columns:
            h[c] = "" if c in TEXT_RAW else np.nan
    h["date"] = pd.to_datetime(h["date"], errors="coerce")
    h = h[h["date"].notna() & h["template"].notna()].copy()
    for c in ("reads", "clicks"):
        h[c] = pd.to_numeric(h[c], errors="coerce").fillna(0.0)
    h["brand"] = h["brand"].astype(str)
    h["template"] = h["template"].astype(str)
    for c in TEXT_RAW:
        h[c] = h[c].fillna("").astype(str)
    h = h.drop_duplicates(["brand", "template", "date"], keep="last").sort_values(["brand", "date", "template"]).reset_index(drop=True)
    h["year"] = h["date"].dt.year.astype(int)
    h["click_rate"] = np.where(h["reads"] > 0, 100.0 * h["clicks"] / h["reads"].where(h["reads"] > 0, 1), np.nan)
    h["send_number"] = h.groupby(["brand", "template"])["date"].rank(method="first").astype(int)

    h["text_group"] = ""
    for brand, idx in h.groupby("brand").groups.items():
        idx = list(idx)
        tmpl = [h.at[i, "template"] for i in idx]
        toks = [F.token_set(h.at[i, "title"], h.at[i, "description"]) for i in idx]
        uf = _UF(len(idx))
        for a in range(len(idx)):
            for b in range(a + 1, len(idx)):
                if tmpl[a] == tmpl[b] or (toks[a] and toks[b] and F.jaccard(toks[a], toks[b]) >= sim):
                    uf.union(a, b)
        roots = {}
        for k in range(len(idx)):
            roots.setdefault(uf.find(k), []).append(k)
        for members in roots.values():
            label = f"{_slug(brand)}:{min(tmpl[m] for m in members)}"
            for m in members:
                h.at[idx[m], "text_group"] = label

    valid = (h["reads"] > 0) & (h["exclude"].str.upper() != "Y")
    h["loo_rate"], h["click_index"], h["n_baseline"] = np.nan, np.nan, 0
    for (brand, cohort, year), idx in h.groupby(["brand", "cohort", "year"]).groups.items():
        idx = [i for i in idx if valid[i]]
        for i in idx:
            others = [j for j in idx if h.at[j, "text_group"] != h.at[i, "text_group"]]
            h.at[i, "n_baseline"] = len(others)
            if len(others) >= min_others:
                r = h.loc[others, "reads"].sum()
                c = h.loc[others, "clicks"].sum()
                if r > 0 and c > 0:
                    loo = c / r
                    h.at[i, "loo_rate"] = 100 * loo
                    h.at[i, "click_index"] = (h.at[i, "clicks"] / h.at[i, "reads"]) / loo

    reason = []
    for i, r in h.iterrows():
        if str(r["exclude"]).upper() == "Y":
            reason.append("excluded")
        elif _blank(r["title"]) and _blank(r["description"]):
            reason.append("no text")
        elif r["reads"] < min_reads:
            reason.append(f"reads < {min_reads}")
        elif pd.isna(r["click_index"]):
            reason.append("group too small")
        else:
            reason.append("ok")
    h["reason"] = reason
    h["usable"] = h["reason"].eq("ok")
    h["weight"] = (h["clicks"].clip(20, 500) / 500.0).round(4)
    for c in HISTORY_COLS:
        if c not in h.columns:
            h[c] = np.nan
    return h[HISTORY_COLS]


def merge(existing, new_rows):
    """Add rows to an existing history file. Rows with the same brand, template and date are replaced by the new ones."""
    parts = []
    if existing is not None and len(existing):
        parts.append(existing[[c for c in RAW_COLS if c in existing.columns]])
    parts.append(new_rows[RAW_COLS])
    return finalize(pd.concat(parts, ignore_index=True))


def to_csv(h):
    return h.to_csv(index=False).encode("utf-8")


def read_history(data):
    df = pd.read_csv(io.BytesIO(data), dtype=object) if isinstance(data, (bytes, bytearray)) else pd.read_csv(data, dtype=object)
    missing = [c for c in ("brand", "template", "date", "reads", "clicks", "title", "description") if c not in df.columns]
    if missing:
        raise ValueError(f"This is not a training history file made by this app. Missing columns: {missing}. "
                         "Files from an older version of the app held features, not text, and cannot be used; re-import the brands.")
    for c in ("reads", "clicks", "submitted", "sent", "delivered"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return finalize(df)


def rows_from_run(metrics, request, vertical="", cohort_rules_text=""):
    rules = parse_cohort_rules(cohort_rules_text)
    cards = {t["template"]: t for t in (request.get("templates") or []) + (request.get("reference_templates") or [])}
    out = []
    for t in metrics["templates"]:
        c = cards.get(t["template"], {}).get("card", {})
        desc = c.get("description", "")
        desc = " ".join(desc) if isinstance(desc, list) else desc
        out.append({"brand": request.get("brand_display") or request.get("brand"), "vertical": vertical, "cohort": cohort_of(t["template"], rules),
                    "template": t["template"], "date": t["date"], "reads": t["counts"]["read"], "clicks": t["counts"]["p2a_responses"],
                    "submitted": t["counts"]["submitted"], "sent": t["counts"]["sent"], "delivered": t["counts"]["delivered"],
                    "title": c.get("title", ""), "description": desc, "button": c.get("button", ""), "category": "", "exclude": "", "exclude_reason": "", "source": "analysis run"})
    return pd.DataFrame(out, columns=RAW_COLS)


# --------------------------------------------------------------------------- bundled seed data
def load_seed(seed_dir=None):
    """Import every workbook listed in data/seed/seed.json. Returns (history or None, report lines)."""
    seed_dir = seed_dir or SEED
    manifest = os.path.join(seed_dir, "seed.json")
    if not os.path.exists(manifest):
        return None, ["No seed.json found: the training history starts empty."]
    with open(manifest, encoding="utf-8") as f:
        items = json.load(f).get("files", [])
    hist, report = None, []
    for it in items:
        path = os.path.join(seed_dir, it["file"])
        if not os.path.exists(path):
            report.append(f"{it['file']}: not found, skipped.")
            continue
        try:
            rows, issues = import_sheet(path, it["file"], it["brand"], it["vertical"], it.get("cohort_rules", ""))
        except Exception as e:  # a bad seed file must not stop the app
            report.append(f"{it['file']}: could not be read ({e}).")
            continue
        errs = [i for i in issues if i["level"] == "error"]
        if errs:
            report.append(f"{it['file']}: skipped, {errs[0]['message']}")
            continue
        hist = merge(hist, rows)
        report.append(f"{it['brand']}: {len(rows)} rows loaded.")
    return hist, report


# --------------------------------------------------------------------------- readiness and the model
def _others(h, exclude_brand):
    u = h[h["usable"]]
    names = _as_list(exclude_brand)
    if names:
        u = u[~u["brand"].map(lambda b: brand_matches(b, names))]
    return u


def readiness(h, exclude_brand=None):
    if h is None or not len(h):
        return {"rows": 0, "usable": 0, "templates": 0, "groups": 0, "brands": 0, "per_brand": [], "gate_ok": False,
                "message": f"No history yet. The trained model needs at least {GATE_GROUPS} distinct texts from at least {GATE_BRANDS} brands."}
    u = _others(h, exclude_brand)
    per = []
    for b, sub in h.groupby("brand"):
        s = sub[sub["usable"]]
        per.append({"brand": b, "vertical": sub["vertical"].iloc[0], "rows": int(len(sub)), "usable": int(len(s)), "distinct_texts": int(s["text_group"].nunique()),
                    "excluded_for": ", ".join(sorted(set(sub.loc[~sub["usable"], "reason"])))})
    g, b = int(u["text_group"].nunique()), int(u["brand"].nunique())
    ok = g >= GATE_GROUPS and b >= GATE_BRANDS
    miss = []
    if g < GATE_GROUPS:
        miss.append(f"{GATE_GROUPS - g} more distinct texts")
    if b < GATE_BRANDS:
        miss.append(f"{GATE_BRANDS - b} more brands")
    return {"rows": int(len(h)), "usable": int(len(u)), "templates": int(u[["brand", "template"]].drop_duplicates().shape[0]), "groups": g, "brands": b,
            "verticals": sorted(set(u["vertical"])), "per_brand": per, "gate_ok": ok,
            "message": ("The history is large enough to try the trained component. It is used only if it beats guessing on brands it has not seen." if ok
                        else f"Not yet: the trained component needs {GATE_GROUPS} distinct texts from {GATE_BRANDS} brands (other than the brand being analysed). Missing: {' and '.join(miss)}.")}


def _xy(u):
    X = np.array([[float(F.extract({"title": r.title, "description": r.description, "button": r.button})[k]) for k in F.MODEL_FEATURES] for r in u.itertuples()])
    y = np.log(u["click_index"].astype(float).clip(0.05, 20).values)
    return X, y, u["weight"].astype(float).values, u["brand"].values


def fit_model(h, exclude_brand=None, gate_groups=GATE_GROUPS, gate_brands=GATE_BRANDS):
    base = {"status": "none", "n": 0, "brands": 0}
    if h is None or not len(h):
        return dict(base, reason="No training history is loaded.")
    u = _others(h, exclude_brand)
    n_groups, n_brands = int(u["text_group"].nunique()), int(u["brand"].nunique())
    base.update(n=int(len(u)), brands=n_brands, groups=n_groups)
    if n_groups < gate_groups or n_brands < gate_brands:
        return dict(base, status="insufficient",
                    reason=f"The history holds {n_groups} distinct texts from {n_brands} other brands; the trained component needs at least {gate_groups} texts from {gate_brands} brands, so it is not used.")
    from sklearn.linear_model import Ridge
    from sklearn.model_selection import GroupKFold
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    X, y, w, groups = _xy(u)
    folds = min(5, n_brands)
    pred = np.zeros_like(y)
    for tr, te in GroupKFold(n_splits=folds).split(X, y, groups):
        m = make_pipeline(StandardScaler(), Ridge(alpha=10.0))
        m.fit(X[tr], y[tr], ridge__sample_weight=w[tr])
        pred[te] = m.predict(X[te])
    mean = np.average(y, weights=w)
    ss_res = float(np.sum(w * (y - pred) ** 2))
    ss_tot = float(np.sum(w * (y - mean) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    mae = float(np.average(np.abs(y - pred), weights=w))
    base_mae = float(np.average(np.abs(y - mean), weights=w))
    info = dict(base, r2=float(r2), mae=mae, baseline_mae=base_mae)
    if r2 < 0.10 or mae >= base_mae:
        return dict(info, status="not_predictive",
                    reason=f"Tested on brands it had not seen, the trained component explains {r2:.2f} of the variation in click index (needs 0.10) and is no better than guessing the average, so it is not used.")
    pipe = make_pipeline(StandardScaler(), Ridge(alpha=10.0))
    pipe.fit(X, y, ridge__sample_weight=w)
    coef = pipe[-1].coef_
    drivers = sorted(zip(F.MODEL_FEATURES, coef), key=lambda kv: -abs(kv[1]))[:4]
    return dict(info, status="ok", model=pipe, drivers=[(k, float(v)) for k, v in drivers], reason="Trained on other brands and validated on unseen brands.")


def predict_index(model_info, feature_dicts):
    X = np.array([[float(f[k]) for k in F.MODEL_FEATURES] for f in feature_dicts])
    return np.exp(model_info["model"].predict(X))
