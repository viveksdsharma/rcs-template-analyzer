"""
The check layer. It runs on every analysis before any file is produced. Errors block the build; warnings are listed.

  1. Schema check      the analysis has every required part and covers every template in the sheet.
  2. Number check      every number in the text is computed from the sheet, quoted from the request / card text,
                       or declared in "external_numbers" with its source. A wrong or invented number blocks the build.
  3. Confidentiality   no internal classification text, and no other brand's name or image in this run.
  4. Mandatory caveat  if every template is a single send date, the analysis must say findings are directional.
  5. Guardrail IDs     a recommendation may only cite guardrail IDs that exist in the knowledge pack.

Limits (also stated in the README): the number check confirms that a number EXISTS in the computed results; it cannot
confirm the number is attached to the right thing. Whole-number percentages are the weakest part.
"""
import itertools
import math
import os
import re

from .metrics import rates_from_counts

RISK = {"Low", "Medium", "High"}
STATUS_CLASS = {"supported", "directional", "plausible", "not_driver", "unknown"}
PRIORITY = {"Now", "Next", "Test", "Ongoing"}

NUM_PATTERNS = [
    ("pct", re.compile(r"(?<![\w.])\d+(?:\.\d+)?%")),
    ("fold", re.compile(r"(?<![\w.])\d+(?:\.\d+)?x\b")),
    ("big", re.compile(r"(?<![\w.])\d{1,3}(?:,\d{3})+(?!\d)")),
    ("mk", re.compile(r"(?<![\w.])\d+(?:\.\d+)?[MK]\b")),
    ("pts", re.compile(r"(?<![\w.])\d+(?:\.\d+)?\s*(?:pts|points?)\b")),
]
SKIP_NUMBER_KEYS = {"external_numbers", "variations", "meta", "context", "external_tables"}


def _walk(obj, path=""):
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")


def _decimals(s):
    m = re.search(r"\.(\d+)", s)
    return len(m.group(1)) if m else 0


def _num(s):
    return float(re.sub(r"[^\d.]", "", s.replace(",", "")))


def _trunc(x, d):
    f = 10 ** d
    return math.floor(abs(x) * f + 1e-9) / f


def _matches(token_val, d, allowed):
    t = abs(token_val)
    for a in allowed:
        a = abs(a)
        if abs(round(a, d) - t) < 1e-9 or abs(_trunc(a, d) - t) < 1e-9:
            return True
    return False


def _subsets(items, cap=12):
    n = len(items)
    if n > cap:
        return [[x] for x in items]
    return [list(c) for r in range(1, n + 1) for c in itertools.combinations(items, r)]


def allowed_numbers(m):
    """Numbers that may appear because they are computed from the sheet."""
    pct, fold, big, pts = set(), set(), set(), set()

    def add_rates(d):
        for v in d.values():
            if v is not None:
                pct.add(float(v))

    for t in m["templates"]:
        add_rates(t["rates"])
        add_rates({k: v for k, v in (t.get("sheet_values") or {}).items() if v is not None})
        for v in t["counts"].values():
            big.add(float(v))
        if t.get("click_index_vs_year"):
            fold.add(t["click_index_vs_year"])
            fold.add(1 / t["click_index_vs_year"])
    for gname in ("year", "year|google_id", "google_id"):
        for g in m["groups"].get(gname, {}).values():
            add_rates(g["rates"])
            for v in g["counts"].values():
                big.add(float(v))
    for comp in m["comparisons"].values():
        for met in comp["metrics"].values():
            pct.update([met["from"], met["to"]])
            pts.add(abs(met["change_pts"]))
            pct.add(abs(met["change_pts"]))
            if met["fold"]:
                fold.update([met["fold"], 1 / met["fold"]])
                pct.add(abs(met["fold"] - 1) * 100)
                pct.add(abs(1 / met["fold"] - 1) * 100)
        if comp.get("submitted_growth") is not None:
            pct.add(abs(comp["submitted_growth"]) * 100)

    by_y = {}
    for t in m["templates"]:
        if t["rates"]["P2AResponseRate"]:
            by_y.setdefault(t["year"], []).append(t["rates"]["P2AResponseRate"])
    for g in list(by_y.values()) + [[r for v in by_y.values() for r in v]]:
        if len(g) > 1 and min(g) > 0:
            fold.add(max(g) / min(g))

    by_year = {}
    for t in m["templates"]:
        by_year.setdefault(t["year"], []).append(t)
    for ts in by_year.values():
        tot_c = sum(t["counts"]["p2a_responses"] for t in ts)
        tot_r = sum(t["counts"]["read"] for t in ts)
        tot_s = sum(t["counts"]["submitted"] for t in ts)
        for sub in _subsets(ts):
            c = sum(t["counts"]["p2a_responses"] for t in sub)
            r = sum(t["counts"]["read"] for t in sub)
            s = sum(t["counts"]["submitted"] for t in sub)
            if tot_c:
                pct.add(100 * c / tot_c)
            if tot_r:
                pct.add(100 * r / tot_r)
            if tot_s:
                pct.add(100 * s / tot_s)
            if r:
                pct.add(100 * c / r)
            big.update([c, r, s])
            for k in ("sent", "delivered", "failed", "expired"):
                big.add(sum(t["counts"][k] for t in sub))

    # pooled figures for a template sent more than once, and its index against the period's blended click rate
    by_tpl = {}
    for t in m["templates"]:
        by_tpl.setdefault((t["year"], t["template"]), []).append(t)
    for (yr, _), ts in by_tpl.items():
        sums = {k: sum(t["counts"][k] for t in ts) for k in ts[0]["counts"]}
        big.update(sums.values())
        rt = rates_from_counts(sums)
        add_rates(rt)
        blended = m["groups"]["year"].get(str(yr), {}).get("rates", {}).get("P2AResponseRate")
        if blended and rt["P2AResponseRate"]:
            fold.update([rt["P2AResponseRate"] / blended, blended / rt["P2AResponseRate"]])
    return {"pct": pct, "fold": fold, "big": big, "pts": pts}


def _external_texts(analysis, request, knowledge=None):
    texts = []
    for item in analysis.get("external_numbers", []) or []:
        texts.append(item.get("value", "") if isinstance(item, dict) else str(item))
    if request:
        for key in ("brand_reported_tables", "context", "issue", "period"):
            if key in request:
                texts += [t for _, t in _walk(request[key])]
        for t in (request.get("templates") or []) + (request.get("reference_templates") or []):
            texts += [x for _, x in _walk({"card": t.get("card", {}), "creative": t.get("creative", "")})]
    if knowledge:
        texts += [g.get("text", "") for g in knowledge.get("guardrails", []) + knowledge.get("platform_facts", [])]
    return texts


def external_values(analysis, request=None, knowledge=None):
    """Numbers allowed from outside the sheet, typed by how they were written: a % only vouches for a %,
    an x only for an x. 'any' (every number) is used only for counts and M/K figures."""
    typed = {"pct": set(), "fold": set(), "big": set(), "mk": set(), "pts": set(), "any": set()}
    for txt in _external_texts(analysis, request, knowledge):
        txt = str(txt)
        for kind, rx in NUM_PATTERNS:
            for mt in rx.finditer(txt):
                typed[kind].add(_num(mt.group()))
        for mt in re.finditer(r"\d+(?:,\d{3})*(?:\.\d+)?", txt):
            typed["any"].add(_num(mt.group()))
    return typed


def check_numbers(analysis, metrics, request=None, knowledge=None):
    allowed = allowed_numbers(metrics)
    ext = external_values(analysis, request, knowledge)
    problems = []
    for path, text in _walk({k: v for k, v in analysis.items() if k not in SKIP_NUMBER_KEYS}):
        if re.search(r"\.variations\[", path):
            continue
        for kind, rx in NUM_PATTERNS:
            for mt in rx.finditer(text):
                tok = mt.group()
                val, d = _num(tok), _decimals(tok)
                if kind == "mk":
                    sc = 1e6 if tok.endswith("M") else 1e3
                    pool = [x / sc for x in allowed["big"]]
                    extpool = [x / sc for x in ext["any"]] + list(ext["mk"])
                elif kind == "big":
                    pool, extpool = list(allowed["big"]), list(ext["big"]) + [x for x in ext["any"] if x >= 1000]
                elif kind == "fold":
                    pool, extpool = list(allowed["fold"]), list(ext["fold"])
                elif kind == "pts":
                    pool, extpool = list(allowed["pts"]), list(ext["pts"])
                else:
                    pool, extpool = list(allowed["pct"]), list(ext["pct"])
                if _matches(val, d, pool) or _matches(val, d, extpool):
                    continue
                problems.append({"level": "error", "code": "unverified_number", "where": path,
                                 "message": f"'{tok}' is not in the sheet-derived numbers and is not declared in external_numbers. Context: ...{text[max(0, mt.start()-40):mt.end()+25].strip()}..."})
    return problems


def _alnum(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower().replace("\u2019", ""))


def same_brand(a, b):
    """True when two names plausibly refer to the same brand ('Axis' and 'Axis Bank Limited', 'bigbasket' and its legal entity)."""
    na, nb = _alnum(a), _alnum(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    fa = re.findall(r"[a-z0-9]+", str(a).lower().replace("\u2019", ""))
    fb = re.findall(r"[a-z0-9]+", str(b).lower().replace("\u2019", ""))
    return bool(fa and fb and len(fa[0]) >= 4 and fa[0] == fb[0])


def _blocked_brands(request, other_brands_path, extra=None):
    own_names = [str(request.get("brand", "")), str(request.get("brand_display", ""))]
    own_names = [o for o in own_names if o.strip()]
    brands = list(extra or [])
    if other_brands_path and os.path.exists(other_brands_path):
        with open(other_brands_path, encoding="utf-8") as f:
            brands += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    if not own_names:
        return []
    out, seen = [], set()
    for b in brands:
        b = str(b).strip()
        if not b or b.lower() in seen or any(same_brand(b, o) for o in own_names):
            continue
        seen.add(b.lower())
        out.append(b)
    return out


def check_confidentiality(analysis, request, style, other_brands_path=None, extra_brands=None):
    problems = []
    blocked = list(style.get("blocked_phrases", []))
    brands = _blocked_brands(request, other_brands_path, extra_brands)
    texts = list(_walk(analysis))
    for t in request.get("templates", []):
        if t.get("image"):
            texts.append((f"request.templates[{t.get('template')}].image", str(t["image"])))
    for path, text in texts:
        low = text.lower()
        for ph in blocked:
            if ph.lower() in low:
                problems.append({"level": "error", "code": "internal_marker", "where": path, "message": f"Internal marking found: '{ph}'. Files for the brand must not carry it."})
        for b in brands:
            if re.search(r"(?<![A-Za-z])" + re.escape(b) + r"(?![A-Za-z])", text, flags=re.I):
                problems.append({"level": "error", "code": "other_brand", "where": path, "message": f"Another brand's name ('{b}') appears in a run for {request.get('brand_display') or request.get('brand')}."})
    return problems


def check_schema(analysis, request, metrics, known_guardrails=None):
    p = []

    def err(code, msg, where=""):
        p.append({"level": "error", "code": code, "where": where, "message": msg})

    for key in ("meta", "summary", "templates", "findings", "hypotheses", "recommendations", "data_needed"):
        if key not in analysis:
            err("missing_part", f"The analysis has no '{key}'.", key)
    if p:
        return p

    cur_year = max(metrics["meta"]["years"])
    sheet_cur = {t["template"] for t in metrics["templates"] if t["year"] == cur_year}
    an = {t.get("template") for t in analysis["templates"]}
    for name in sorted(sheet_cur - an):
        err("template_not_reviewed", f"'{name}' is in the sheet for {cur_year} but has no review.", "templates")
    for name in sorted(an - {t['template'] for t in metrics['templates']}):
        err("template_not_in_sheet", f"'{name}' is reviewed but is not in the sheet.", "templates")
    req_names = {t.get("template") for t in request.get("templates", [])}
    for name in sorted(sheet_cur - req_names):
        err("template_without_card_text", f"'{name}' has no card text in the request.", "request.templates")

    for t in analysis["templates"]:
        w = t.get("template")
        if t.get("content_risk") not in RISK:
            err("bad_content_risk", f"{w}: content_risk must be Low, Medium or High.", f"templates[{w}]")
        if not (t.get("works") or t.get("watch_outs")):
            err("empty_review", f"{w}: needs at least one 'works' or 'watch_outs' point.", f"templates[{w}]")
        for item in (t.get("works") or []) + (t.get("watch_outs") or []):
            if not isinstance(item, dict) or not item.get("h") or not item.get("b"):
                err("bad_point", f"{w}: each point needs 'h' (short heading) and 'b' (text).", f"templates[{w}]")
                break
    for h in analysis["hypotheses"]:
        if h.get("status_class") not in STATUS_CLASS:
            err("bad_status", f"Hypothesis {h.get('id')}: status_class must be one of {sorted(STATUS_CLASS)}.", "hypotheses")
    for r in analysis["recommendations"]:
        if r.get("priority") not in PRIORITY:
            err("bad_priority", f"Recommendation {r.get('n')}: priority must be one of {sorted(PRIORITY)}.", "recommendations")
        if not (r.get("text") and r.get("why") and r.get("owner")):
            err("incomplete_recommendation", f"Recommendation {r.get('n')}: needs text, why and owner.", "recommendations")
        if known_guardrails is not None:
            for gid in r.get("guardrails", []) or []:
                if gid not in known_guardrails:
                    err("unknown_guardrail", f"Recommendation {r.get('n')} cites guardrail '{gid}', which is not in the knowledge pack.", "recommendations")

    if any(i.get("code") == "single_send_date" for i in metrics.get("issues", [])):
        enum_keys = (".status_class", ".content_risk", ".priority")
        blob = " ".join(t for path, t in _walk({k: v for k, v in analysis.items() if k != "meta"}) if not path.endswith(enum_keys)).lower()
        if not re.search(r"(single|one) send date|directional", blob):
            err("missing_caveat", "Every template is a single send date. The analysis must say that findings are directional, not proven.", "summary")
    return p


def check_template_fragment(frag, template_name, request, style, other_brands_path=None, extra_brands=None, card_text=""):
    """Validate one template review returned by a model (step 1 of the language-model engine). Returns a list of problem strings."""
    p = []
    if frag.get("content_risk") not in RISK:
        p.append(f"content_risk must be Low, Medium or High (got {frag.get('content_risk')!r}).")
    if not (frag.get("works") or frag.get("watch_outs")):
        p.append("needs at least one 'works' or 'watch_outs' point.")
    for item in (frag.get("works") or []) + (frag.get("watch_outs") or []):
        if not isinstance(item, dict) or not item.get("h") or not item.get("b"):
            p.append("each point needs 'h' (short heading) and 'b' (text).")
            break
    wrapped = {"templates": [dict(frag, template=template_name)]}
    for i in check_confidentiality(wrapped, request, style, other_brands_path, extra_brands):
        p.append(i["message"])
    # Step 1 has no performance data. Any figure with a % or an x must already appear in the card text.
    card_nums = set(re.findall(r"\d+(?:\.\d+)?", card_text))
    for path, text in _walk({k: frag.get(k) for k in ("works", "watch_outs", "content_risk_basis")}):
        for mt in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:%|x\b)", text):
            if mt.group(1) not in card_nums:
                p.append(f"'{mt.group(0)}' is not in the card text. Step 1 has no performance data: use only figures that appear in the card text.")
    return p


def run_all(analysis, request, metrics, style, other_brands_path=None, knowledge=None, extra_brands=None):
    issues = []
    known = {g["id"] for g in knowledge.get("guardrails", [])} if knowledge else None
    issues += check_schema(analysis, request, metrics, known)
    if not any(i["code"] == "missing_part" for i in issues):
        issues += check_numbers(analysis, metrics, request, knowledge)
        issues += check_confidentiality(analysis, request, style, other_brands_path, extra_brands)
    return issues
