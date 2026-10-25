"""
Metrics engine. Every number in a deliverable comes from here (or is quoted from the brand), never from a model.

Rules:
  1. Column names are the sheet's own. Ratios the sheet does not hold are "calculated" (shown with *).
  2. P2AResponseRate is the CLICK RATE (P2AResponses / Messages Read).
  3. One brand per run.
  4. A missing required column stops the run and is listed. Nothing is guessed.
  5. Stray rows (loose ratios, bare year cells, blanks) are ignored and reported.
"""
import io
import json
import os
import re
from datetime import date, datetime

import pandas as pd

from . import CONFIG

CONFIG_PATH = os.path.join(CONFIG, "sources.json")
COUNT_COLS = ["submitted", "sent", "delivered", "read", "failed", "expired", "p2a_responses"]

DEFINITIONS = [
    ("DeliveryRate", "Messages Delivered / Messages Sent", "Platform delivery once a message has left the platform", "sheet"),
    ("ReadRate", "Messages Read / Messages Delivered", "How many delivered messages were opened", "sheet"),
    ("P2AResponseRate (click rate)", "P2AResponses / Messages Read", "Click rate: share of read messages that were clicked", "sheet"),
    ("Pre-send failure rate*", "Messages Failed / Messages Submitted", "Sizing the messages lost before they leave the platform", "calculated"),
    ("End-to-end delivery rate*", "Messages Delivered / Messages Submitted", "Reconciling with a brand's own delivery rate, which often uses submitted as the base", "calculated"),
    ("Expiry rate*", "Messages Expired / Messages Sent", "Sent messages that expired before delivery; relevant to time-bound offers", "calculated"),
    ("Read rate, sent base*", "Messages Read / Messages Sent", "Comparison with a read rate quoted on a different base", "calculated"),
    ("Click rate, delivered base*", "P2AResponses / Messages Delivered", "Comparison with a brand's CTR, which is usually visits / delivered", "calculated"),
]


class PipelineError(Exception):
    """The run must stop and ask a human."""


class MissingColumns(PipelineError):
    pass


class MultipleBrands(PipelineError):
    pass


def _norm(s):
    return re.sub(r"\s+", " ", str(s)).strip().lower()


def parse_pct(x):
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).replace("%", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _parse_date(v):
    if isinstance(v, (datetime, pd.Timestamp)):
        return pd.Timestamp(v).normalize()
    if isinstance(v, date):
        return pd.Timestamp(v)
    if isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}", v.strip()):
        return pd.Timestamp(v.strip()[:10])
    return None


def _pct(a, b):
    return None if not b else 100.0 * a / b


def load_profile(name="valuefirst_portal", path=CONFIG_PATH):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    if name not in cfg:
        raise PipelineError(f"Source profile '{name}' not found. Available: {[k for k in cfg if not k.startswith('_')]}")
    return cfg[name]


def read_table(source, filename=None, sheet_name=0):
    """source: path, bytes or file-like. CSV and Excel are both accepted."""
    name = (filename or (source if isinstance(source, str) else "")).lower()
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    if name.endswith(".csv"):
        return pd.read_csv(source, dtype=object)
    return pd.read_excel(source, sheet_name=sheet_name, header=0, dtype=object)


def load_sheet(source, profile="valuefirst_portal", brand=None, templates=None, filename=None, allow_multi=False):
    """Return (clean DataFrame, issues). allow_multi=True keeps every brand (used by the UI to list brands)."""
    prof = load_profile(profile)
    raw = read_table(source, filename=filename)
    by_norm = {_norm(c): c for c in raw.columns}

    mapping, missing = {}, []
    for canon, aliases in prof["required"].items():
        hit = next((by_norm[_norm(a)] for a in aliases if _norm(a) in by_norm), None)
        if hit is None:
            missing.append(f"{canon} (looked for: {', '.join(aliases)})")
        else:
            mapping[canon] = hit
    if missing:
        raise MissingColumns("Required columns not found in the sheet: " + "; ".join(missing) + f". Columns present: {list(raw.columns)}")
    for canon, aliases in prof.get("optional", {}).items():
        hit = next((by_norm[_norm(a)] for a in aliases if _norm(a) in by_norm), None)
        if hit is not None:
            mapping[canon] = hit

    df = pd.DataFrame({canon: raw[col] for canon, col in mapping.items()})
    issues = []
    df["_date"] = df["date"].map(_parse_date)
    for c in COUNT_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    valid = df["_date"].notna() & df["template"].notna() & df["submitted"].notna()
    dropped = int((~valid & df.notna().any(axis=1)).sum())
    if dropped:
        issues.append({"level": "info", "code": "stray_rows_ignored",
                       "message": f"{dropped} row(s) ignored: no valid date, template or Messages Submitted (stray ratios, year markers or notes)."})
    df = df[valid].copy()
    if df.empty:
        raise PipelineError("No valid data rows found (a row needs a date, a template name and Messages Submitted).")

    df["date"] = df["_date"]
    df = df.drop(columns=["_date"])
    df["year"] = df["date"].dt.year.astype(int)
    for c in COUNT_COLS:
        df[c] = df[c].astype(float)
    df["template"] = df["template"].astype(str).str.strip()

    brands = sorted(df["brand"].dropna().astype(str).unique())
    if not allow_multi:
        if brand:
            sel = [b for b in brands if brand.lower() in b.lower()]
            if not sel:
                raise PipelineError(f"Brand '{brand}' not found. Brands in the sheet: {brands}")
            if len(sel) > 1:
                raise MultipleBrands(f"'{brand}' matches more than one brand: {sel}. Be more specific.")
            df = df[df["brand"].astype(str) == sel[0]].copy()
        elif len(brands) > 1:
            raise MultipleBrands(f"The sheet holds {len(brands)} brands: {brands}. Choose one brand per run.")
    if templates:
        want = {t.strip() for t in templates}
        missing_t = sorted(want - set(df["template"]))
        if missing_t:
            raise PipelineError(f"Templates not found in the sheet for this brand: {missing_t}")
        df = df[df["template"].isin(want)].copy()

    df["delivery_rate_sheet"] = df["delivery_rate_sheet"].map(parse_pct)
    df["read_rate_sheet"] = df["read_rate_sheet"].map(parse_pct)
    df["p2a_rate_sheet"] = df["p2a_rate_sheet"].map(parse_pct)
    return df.sort_values("date").reset_index(drop=True), issues


def rates_from_counts(c):
    return {
        "DeliveryRate": _pct(c["delivered"], c["sent"]),
        "ReadRate": _pct(c["read"], c["delivered"]),
        "P2AResponseRate": _pct(c["p2a_responses"], c["read"]),
        "pre_send_failure_rate": _pct(c["failed"], c["submitted"]),
        "end_to_end_delivery_rate": _pct(c["delivered"], c["submitted"]),
        "expiry_rate": _pct(c["expired"], c["sent"]),
        "read_rate_sent_base": _pct(c["read"], c["sent"]),
        "click_rate_delivered_base": _pct(c["p2a_responses"], c["delivered"]),
    }


def _counts(frame):
    return {c: float(frame[c].sum()) for c in COUNT_COLS}


def check_rows(df, tol_pts=0.011, recon_tol=0.002):
    issues = []
    for _, r in df.iterrows():
        name, d = r["template"], r["date"].strftime("%Y-%m-%d")
        rt = rates_from_counts({c: r[c] for c in COUNT_COLS})
        for label, sheet_v, mine in (("DeliveryRate", r["delivery_rate_sheet"], rt["DeliveryRate"]),
                                     ("ReadRate", r["read_rate_sheet"], rt["ReadRate"]),
                                     ("P2AResponseRate", r["p2a_rate_sheet"], rt["P2AResponseRate"])):
            if sheet_v is None or mine is None:
                continue
            sv = sheet_v * 100 if sheet_v <= 1.0 and mine > 2 else sheet_v
            if abs(sv - mine) > tol_pts:
                issues.append({"level": "warn", "code": "sheet_rate_mismatch", "template": name, "date": d,
                               "message": f"{label}: sheet shows {sv:.2f}% but the counts give {mine:.2f}%."})
        if r["delivered"] > r["sent"] or r["sent"] > r["submitted"] or r["read"] > r["delivered"]:
            issues.append({"level": "error", "code": "funnel_order", "template": name, "date": d,
                           "message": "Counts break the funnel order (read <= delivered <= sent <= submitted)."})
        gap_f = abs(r["failed"] - (r["submitted"] - r["sent"]))
        if r["submitted"] and gap_f / r["submitted"] > recon_tol:
            issues.append({"level": "warn", "code": "failed_not_submitted_minus_sent", "template": name, "date": d,
                           "message": f"Messages Failed differs from Submitted minus Sent by {gap_f:,.0f}; confirm what 'Failed' covers."})
        gap_e = abs(r["expired"] - (r["sent"] - r["delivered"]))
        if r["sent"] and gap_e / r["sent"] > recon_tol:
            issues.append({"level": "warn", "code": "expired_not_sent_minus_delivered", "template": name, "date": d,
                           "message": f"Messages Expired differs from Sent minus Delivered by {gap_e:,.0f}; confirm what 'Expired' covers."})
        if r["read"] < 1000:
            issues.append({"level": "warn", "code": "small_base", "template": name, "date": d,
                           "message": f"Only {r['read']:,.0f} reads: click rate is unstable at this size."})
    per = df.groupby("template")["date"].nunique()
    if (per == 1).all():
        issues.append({"level": "info", "code": "single_send_date",
                       "message": "Every template is a single send date, so audience, day of week and frequency cannot be separated from the creative. Findings are directional, not proven."})
    return issues


def short_agent_id(google_id):
    s = re.sub(r"_agent$", "", str(google_id))
    parts = s.split("_", 1)
    return parts[1] if len(parts) == 2 else s


def analyse(df):
    out = {"templates": [], "groups": {}, "comparisons": {}}
    for _, r in df.iterrows():
        c = {k: float(r[k]) for k in COUNT_COLS}
        out["templates"].append({
            "template": r["template"], "date": r["date"].strftime("%Y-%m-%d"), "year": int(r["year"]),
            "google_id": r["google_id"], "google_id_short": short_agent_id(r["google_id"]), "message_type": r["message_type"],
            "counts": c, "rates": rates_from_counts(c),
            "sheet_values": {"DeliveryRate": r["delivery_rate_sheet"], "ReadRate": r["read_rate_sheet"], "P2AResponseRate": r["p2a_rate_sheet"]},
        })

    def group(by):
        res = {}
        for key, frame in df.groupby(by):
            k = "|".join(str(x) for x in (key if isinstance(key, tuple) else (key,)))
            c = _counts(frame)
            res[k] = {"n_templates": int(len(frame)), "counts": c, "rates": rates_from_counts(c)}
        return res

    out["groups"]["year"] = group("year")
    out["groups"]["year|google_id"] = group(["year", "google_id"])
    out["groups"]["google_id"] = group("google_id")
    for t in out["templates"]:
        yr = out["groups"]["year"][str(t["year"])]["rates"]["P2AResponseRate"]
        t["click_index_vs_year"] = None if not yr else t["rates"]["P2AResponseRate"] / yr

    years = sorted(out["groups"]["year"])
    if len(years) == 2:
        a, b = out["groups"]["year"][years[0]]["rates"], out["groups"]["year"][years[1]]["rates"]
        comp = {"from_year": int(years[0]), "to_year": int(years[1]), "metrics": {}}
        for m in a:
            if a[m] is None or b[m] is None:
                continue
            comp["metrics"][m] = {"from": a[m], "to": b[m], "change_pts": b[m] - a[m], "fold": (b[m] / a[m]) if a[m] else None}
        sa = out["groups"]["year"][years[0]]["counts"]["submitted"]
        sb = out["groups"]["year"][years[1]]["counts"]["submitted"]
        comp["submitted_growth"] = sb / sa - 1 if sa else None
        out["comparisons"]["year_over_year"] = comp
    out["blended_all_rows"] = rates_from_counts(_counts(df))
    return out


def run(source, brand=None, templates=None, profile="valuefirst_portal", filename=None):
    df, load_issues = load_sheet(source, profile=profile, brand=brand, templates=templates, filename=filename)
    issues = load_issues + check_rows(df)
    result = analyse(df)
    result["meta"] = {
        "source_file": filename or (os.path.basename(source) if isinstance(source, str) else "upload"),
        "profile": profile, "brand": sorted(df["brand"].astype(str).unique())[0],
        "n_templates": int(len(df)), "years": sorted(int(y) for y in df["year"].unique()),
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    result["definitions"] = [{"name": n, "formula": f, "used_for": u, "source": s} for n, f, u, s in DEFINITIONS]
    result["issues"] = issues
    result["can_proceed"] = not any(i["level"] == "error" for i in issues)
    return result


# ---------------------------------------------------------------- display helpers shared by the renderers
def fmt_pct(v, d=2):
    return "n/a" if v is None else f"{v:.{d}f}%"


def fmt_int(v):
    return f"{int(round(v)):,}"


def shown_rate(t, key):
    """What a report shows for a template: the sheet's own value for sheet columns, the recalculation otherwise."""
    sv = (t.get("sheet_values") or {}).get(key)
    if key in ("DeliveryRate", "ReadRate", "P2AResponseRate") and sv is not None:
        return sv
    return t["rates"][key] if key in t["rates"] else None


def nice_date(iso):
    d = datetime.strptime(iso, "%Y-%m-%d")
    return f"{d.day} {d.strftime('%b %Y')}"
