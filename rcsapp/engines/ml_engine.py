"""
Internal ML model. No language model, no network.

  1. Scoring   scores each template's TEXT ONLY (never performance) -> content risk, works, watch-outs.
  2. Test      compares that text-only score with click rate inside the run, and tests each recommended check against the run's own data.
  3. Trained   a trained component is used ONLY when enough history exists (other brands, validated on unseen brands).
               With a few templates it would learn noise, so below the threshold the app says so and does not use it.
  4. Text      every sentence is built from computed numbers, so the number check passes by construction.
"""
import math
import re

import numpy as np
import pandas as pd

from .. import features as F
from .. import metrics as M
from .. import training as T

SEV = {"Low": 0, "Medium": 1, "High": 2}


# ------------------------------------------------------------------ scoring
def evaluate(card, rules):
    f = F.extract(card)
    quant, benefit = f["quantified"], f["names_benefit"]
    tests = {
        "no_benefit_named": not benefit and not quant,
        "not_quantified": bool(benefit) and not quant,
        "chance_based": f["chance"],
        "urgency_no_benefit": f["has_deadline"] and not benefit and not quant,
        "floor_or_ceiling": f["floor_or_ceiling"],
        "long_description": f["desc_words"] > rules["long_description_words"],
        "many_variables": f["n_vars"] >= rules["many_variables"],
        "shouty": f["caps_share"] > rules["shouty_caps_share"] and f["exclaims"] >= rules["shouty_exclaims"],
        "competing_actions": f["competing_actions"],
        "thin_title": f["title_words"] < rules["thin_title_words"] and not f["title_has_digit"],
    }
    works_tests = {
        "has_number": f["has_number"], "has_code": f["has_code"],
        "short_text": f["total_words"] <= 25 and f["total_words"] > 0,
        "single_action": (not f["competing_actions"]) and bool(card.get("button")),
    }
    watch = [r for r in rules["watch"] if tests.get(r["id"])]
    works = [r for r in rules["works"] if works_tests.get(r["id"])]
    score = sum(r["weight"] for r in watch)
    th = rules["thresholds"]
    risk = "High" if score >= th["high"] else "Medium" if score >= th["medium"] else "Low"
    return {"features": f, "watch": watch, "works": works, "score": score, "risk": risk}


def concordance(items):
    """items: list of dicts {year, score, click}. Pairs are formed within the same year only."""
    conc = disc = tied = 0
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i], items[j]
            if a["year"] != b["year"]:
                continue
            if a["score"] == b["score"] or abs(a["click"] - b["click"]) < 1e-12:
                tied += 1
            elif (a["score"] < b["score"]) == (a["click"] > b["click"]):
                conc += 1
            else:
                disc += 1
    return {"concordant": conc, "discordant": disc, "tied": tied, "comparable": conc + disc}


def _pooled(templates):
    r = sum(t["counts"]["read"] for t in templates)
    c = sum(t["counts"]["p2a_responses"] for t in templates)
    return 100 * c / r if r else None


def direction_support(flagged, templates, scored):
    """Does the click data in this run point the way a check says? Compares pooled click rate of flagged vs unflagged
    templates inside each period. Returns (periods_supporting, periods_contradicting)."""
    sup = con = 0
    for y in sorted({t["year"] for t in templates}):
        ts = [t for t in templates if t["year"] == y and t["template"] in scored]
        a = [t for t in ts if t["template"] in flagged]
        b = [t for t in ts if t["template"] not in flagged]
        if a and b:
            pa, pb = _pooled(a), _pooled(b)
            if pa is None or pb is None:
                continue
            if pa < pb:
                sup += 1
            elif pa > pb:
                con += 1
    return sup, con


def _support_text(sup, con):
    if sup > con:
        return "In this run the click data points the same way.", "supports"
    if con > sup:
        return "In this run the click data points the other way, so test it before acting.", "contradicts"
    if sup + con > 0:
        return "In this run the click data is mixed across periods, so test it.", "mixed"
    return "This run cannot test it: the templates are alike on this point.", "untested"


def rule_evidence(hist, rules):
    """Cross-brand evidence for each scoring check, from the training history. One row per check.
    'flagged' rows are those the check fires on (text only). click_index < 1 means below the brand's other texts."""
    cols = ["check", "flagged", "not_flagged", "median_index_flagged", "median_index_not_flagged", "share_flagged_below_1", "reading"]
    if hist is None or not len(hist):
        return pd.DataFrame(columns=cols)
    u = hist[hist["usable"]].copy()
    if not len(u):
        return pd.DataFrame(columns=cols)
    ev = [evaluate({"title": r.title, "description": r.description, "button": r.button}, rules) for r in u.itertuples()]
    u["_fired"] = [{w["id"] for w in e["watch"]} for e in ev]
    rows = []
    for rule in rules["watch"]:
        m = u["_fired"].map(lambda s, rid=rule["id"]: rid in s)
        a, b = u[m], u[~m]
        if len(a) < 5:
            reading = "too few flagged rows to judge"
        elif len(b) < 5:
            reading = "too few unflagged rows to judge"
        else:
            ma, mb = float(a["click_index"].median()), float(b["click_index"].median())
            reading = "as expected (flagged rows click lower)" if ma < mb else "OPPOSITE (flagged rows click higher)"
        rows.append({"check": rule["id"], "flagged": int(len(a)), "not_flagged": int(len(b)),
                     "median_index_flagged": (round(float(a["click_index"].median()), 2) if len(a) else None),
                     "median_index_not_flagged": (round(float(b["click_index"].median()), 2) if len(b) else None),
                     "share_flagged_below_1": (round(float((a["click_index"] < 1).mean()), 2) if len(a) else None), "reading": reading})
    return pd.DataFrame(rows, columns=cols)


# ------------------------------------------------------------------ text helpers
def _names(items, n=3):
    items = list(items)
    s = ", ".join(items[:n])
    return s + (f" and {len(items) - n} more" if len(items) > n else "")


def _bold_head(text):
    m = re.search(r"\*\*(.+?)\*\*", text)
    return (m.group(1) if m else text).rstrip(". ")


def _brand(request):
    return request.get("brand_display") or request.get("brand") or "The brand"


def _conc_sentence(c):
    if c["comparable"] == 0:
        return "There were too few comparable template pairs to test whether the text-only content score matches click rate."
    return (f"Scored from the text alone, the content score ordered {c['concordant']} of {c['comparable']} comparable template pairs "
            f"the same way as click rate ({c['discordant']} the other way, {c['tied']} tied).")


# ------------------------------------------------------------------ the analysis
def build_analysis(metrics, request, knowledge, rules, history=None, analyst="Vi"):
    brand = _brand(request)
    designed = any(str(d).lower() in str(metrics["meta"]["brand"]).lower() for d in rules.get("designed_with", []))
    years = metrics["meta"]["years"]
    cur = max(years)
    labels = request.get("period_labels", {})
    cur_label = labels.get(str(cur), str(cur))
    gy = metrics["groups"]["year"]
    g = gy[str(cur)]
    gr = g["rates"]
    cur_ts = [t for t in metrics["templates"] if t["year"] == cur]       # one row per send
    dist = M.distinct(metrics, cur)                                      # one entry per template
    n_t = len(dist)
    req = {t["template"]: t for t in (request.get("templates") or []) + (request.get("reference_templates") or [])}
    short = {k: (v.get("short") or k) for k, v in req.items()}
    yoy = metrics["comparisons"].get("year_over_year")
    declared = []

    def declare(value, source):
        declared.append({"value": value, "source": source})

    ev = {}
    for t in metrics["templates"]:
        r = req.get(t["template"])
        if r:
            ev[t["template"]] = evaluate(r.get("card", {}), rules)
    items = []
    for y in years:
        for name, rows_ in M.distinct(metrics, y).items():
            if name in ev:
                items.append({"year": y, "score": ev[name]["score"], "click": M.card_stats(rows_)["cr_pooled"]})
    conc = concordance(items)

    def lbl(t):
        """Template name, with the send date when the template was sent more than once."""
        return t["template"] if len(dist.get(t["template"], [t])) == 1 else f"{t['template']} ({M.nice_date(t['date']).rsplit(' ', 1)[0]})"

    own_names = [metrics["meta"]["brand"], brand, request.get("brand")]
    model_info = T.fit_model(history, exclude_brand=own_names) if history is not None else {"status": "none", "reason": "No training history is loaded.", "n": 0, "brands": 0}
    model_findings = []
    if model_info["status"] == "ok":
        names = [n_ for n_ in dist if n_ in ev]
        preds = T.predict_index(model_info, [ev[n_]["features"] for n_ in names])
        yr_rate = gr["P2AResponseRate"]
        rows = []
        for nm, pv in zip(names, preds):
            act = (M.card_stats(dist[nm])["cr_pooled"] / yr_rate) if yr_rate else None
            if act:
                rows.append((abs(math.log(act / pv)), nm, pv, act))
        for _, nm, pv, act in sorted(rows, reverse=True)[:3]:
            declare(f"{pv:.2f}x", f"predicted click index for {nm} from the trained component")
            model_findings.append({"h": f"Trained component: {short.get(nm, nm)}",
                                   "b": f"A component trained on other brands (validated on unseen brands, R-squared {model_info['r2']:.2f}) expected a click index of {pv:.2f}x for this text; the actual index was {act:.2f}x."})

    tmpl_out = []
    pooled_cr = {nm: M.card_stats(rows_)["cr_pooled"] for nm, rows_ in dist.items()}
    rank = {nm: i + 1 for i, nm in enumerate(sorted(dist, key=lambda k: -pooled_cr[k]))}
    for nm, rows_ in dist.items():
        e = ev.get(nm)
        if not e:
            continue
        st = M.card_stats(rows_)
        works = [{"h": w["h"], "b": w["b"]} for w in e["works"]][:3] or [{"h": "No text flags", "b": "None of the text checks fired for this template."}]
        watch = [{"h": w["h"], "b": w["b"]} for w in e["watch"]][:6]
        basis = ("Checks fired: " + ", ".join(w["id"] for w in e["watch"]) + f" (score {e['score']:.1f}).") if e["watch"] else "No text check fired (score 0.0)."
        sr = M.fmt_pct(st["cr"])
        sends = "" if st["sends"] == 1 else f" over {st['sends']} sends"
        tmpl_out.append({
            "template": nm, "content_risk": e["risk"], "content_risk_basis": basis, "works": works, "watch_outs": watch,
            "strip": f"Click rate {sr}{sends}: rank {rank[nm]} of {n_t} in {cur_label}; ReadRate {M.fmt_pct(st['rr'])}",
            "variations": [],
            "speaker_note": f"Content risk {e['risk']} is scored from the text alone ({basis}). Click rate {sr}{sends} is shown for comparison.",
        })

    drs = [M.shown_rate(t, "DeliveryRate") for t in cur_ts]
    rrs = [M.shown_rate(t, "ReadRate") for t in cur_ts]
    crs = [t["rates"]["P2AResponseRate"] for t in cur_ts]
    srt = sorted(cur_ts, key=lambda x: -x["rates"]["P2AResponseRate"])
    top, bot = srt[0], srt[-1]
    spread = (max(crs) / min(crs)) if min(crs) > 0 else None
    uniform = (max(drs) - min(drs) < 3.0) and (max(rrs) - min(rrs) < 3.0)
    fail = gr["pre_send_failure_rate"]
    exp = gr["expiry_rate"]

    def rng(vals):
        return f"{min(vals):.2f}\u2013{max(vals):.2f}%"

    numbers = [{"h": "Delivery and read within " + cur_label,
                "b": f"DeliveryRate is {rng(drs)} and ReadRate {rng(rrs)} across the {n_t} templates. " +
                     ("Nothing here separates a stronger template from a weaker one on delivery or read." if uniform else "The spread is wide enough to look at template by template.")}]
    lowrr = min(cur_ts, key=lambda t: M.shown_rate(t, "ReadRate"))
    med = float(np.median(rrs))
    if len(cur_ts) >= 3 and med - M.shown_rate(lowrr, "ReadRate") >= 1.5:
        numbers.append({"h": f"{lbl(lowrr)} has the lowest ReadRate", "b": f"At {M.fmt_pct(M.shown_rate(lowrr, 'ReadRate'))} it is clearly below the median template."})
    if spread:
        numbers.append({"h": "Click rate varies " + f"{spread:.1f}x",
                        "b": f"P2AResponseRate runs from {M.fmt_pct(M.shown_rate(bot, 'P2AResponseRate'))} ({lbl(bot)}) to {M.fmt_pct(M.shown_rate(top, 'P2AResponseRate'))} ({lbl(top)}). Each row is a single send date, so audience, day and offer are mixed in; treat the gap as a signal, not proof."})
    numbers.append({"h": "Pre-send failure", "b": f"{fail:.1f}% of Messages Submitted failed before sending, and a further {exp:.1f}% of Messages Sent expired. Reason codes are needed to know why."})
    numbers.append({"h": "Click rate on a delivered base", "b": f"Click rate, delivered base* is {M.fmt_pct(gr['click_rate_delivered_base'])} overall. Compare it with a brand's CTR only on that base, and only as a reconciliation point."})

    yoy_c = []
    if yoy:
        m = yoy["metrics"]
        a, b = (labels.get(str(yoy["from_year"]), str(yoy["from_year"])), labels.get(str(yoy["to_year"]), str(yoy["to_year"])))
        cr, rr, dr, ex, pf, cd = (m[k] for k in ("P2AResponseRate", "ReadRate", "DeliveryRate", "expiry_rate", "pre_send_failure_rate", "click_rate_delivered_base"))
        dirn = "lower" if cr["fold"] < 1 else "higher"
        fold_c = (1 / cr["fold"]) if cr["fold"] < 1 else cr["fold"]
        yoy_c.append({"h": f"Click rate {dirn} year on year", "b": f"Click rate (P2AResponseRate) moved from {M.fmt_pct(cr['from'])} in {a} to {M.fmt_pct(cr['to'])} in {b}, {fold_c:.1f}x {dirn}. On a delivered base it moved {abs(cd['fold'] - 1) * 100:.0f}% ({M.fmt_pct(cd['from'])} to {M.fmt_pct(cd['to'])})."})
        yoy_c.append({"h": "Reads and clicks", "b": f"ReadRate went from {M.fmt_pct(rr['from'])} to {M.fmt_pct(rr['to'])} ({abs(rr['change_pts']):.1f} points {'higher' if rr['change_pts'] > 0 else 'lower'}) while click rate moved the other way or by a different amount. A rise in reads with a fall in clicks is what a broader, colder audience would produce, but the sheet has no audience data to confirm it."})
        yoy_c.append({"h": "Delivery", "b": f"DeliveryRate went from {M.fmt_pct(dr['from'])} to {M.fmt_pct(dr['to'])} ({abs(dr['change_pts']):.1f} points) and the expiry rate from {M.fmt_pct(ex['from'])} to {M.fmt_pct(ex['to'])}. The sheet cannot say why."})
        yoy_c.append({"h": "Pre-send failure", "b": f"Pre-send failure was {M.fmt_pct(pf['from'], 1)} and then {M.fmt_pct(pf['to'], 1)}. " + ("It is similar in both periods, so it is a separate leak, not the year-on-year driver." if abs(pf["change_pts"]) < 5 else "The change is large enough to look at as a driver.")})
        yoy_c.append({"h": "Limits", "b": "Few templates per period and one send date each, with different offer types. Treat the comparison as directional."})

    fired = {}
    for nm in dist:
        for w in ev.get(nm, {}).get("watch", []):
            fired.setdefault(w["id"], {"rule": w, "names": []})["names"].append(nm)
    q_yes = [t for t in cur_ts if t["template"] in ev and ev[t["template"]]["features"]["quantified"]]
    q_no = [t for t in cur_ts if t["template"] in ev and not ev[t["template"]]["features"]["quantified"]]

    findings = [{"h": "Delivery and read " + ("are uniform" if uniform else "differ") + f" within {cur_label}",
                 "b": f"DeliveryRate is {rng(drs)} and ReadRate {rng(rrs)}. " + ("Template differences show up in the content and in the click rate, not in delivery." if uniform else "Look at the lower templates first.")}]
    if spread:
        findings.append({"h": "Click rate separates the templates", "b": f"It varies {spread:.1f}x within {cur_label}, from {lbl(bot)} to {lbl(top)}, while ReadRate stays within a narrow band." if uniform else f"It varies {spread:.1f}x within {cur_label}."})
    findings.append({"h": "Does the text-only content score match the result?", "b": _conc_sentence(conc) + (" The scoring was built while analysing this brand, so this match is not an independent test of it." if designed else " The scoring checks are starting checks that are not yet validated across brands; the match is directional, not proven.")})
    if fired:
        worst = sorted(fired.values(), key=lambda v: -len(v["names"]))[0]
        findings.append({"h": "The most common text flag", "b": f"\"{worst['rule']['h']}\" fired on {len(worst['names'])} of {n_t} templates ({_names(worst['names'])})."})
    if q_yes and q_no:
        qy, qn = _pooled(q_yes), _pooled(q_no)
        declare(f"{qy:.2f}%", "pooled click rate of templates with a number, price or code in the text")
        declare(f"{qn:.2f}%", "pooled click rate of templates with no number, price or code in the text")
        findings.append({"h": "Offers quantified in the text", "b": f"Templates with a number, price or code in the text have a pooled click rate of {qy:.2f}%, against {qn:.2f}% for those without. " + ("That is the opposite of the expectation that numbers help, so test it here; a figure may sit in the image instead of the text. " if qy < qn else "") + "Few templates and single send dates: this is a signal to test."})
    findings += model_findings
    if fail >= 10:
        findings.append({"h": "A large share of messages never leaves the platform", "b": f"{fail:.1f}% of Messages Submitted failed before sending. This is separate from template content."})
    findings.append({"h": "Post-click is the remaining gap", "b": "The sheet shows clicks, not what followed. Whether the higher-click templates also drove more visits and orders needs per-template data from the brand."})

    hyp = []
    if yoy:
        m = yoy["metrics"]
        hit = m["DeliveryRate"]["change_pts"] <= -3 or (m["expiry_rate"]["fold"] or 0) >= 2
        hyp.append({"id": "H1", "hypothesis": "Delivery and expiry changed between the periods",
                    "evidence": f"DeliveryRate {M.fmt_pct(m['DeliveryRate']['from'])} to {M.fmt_pct(m['DeliveryRate']['to'])}; expiry {M.fmt_pct(m['expiry_rate']['from'])} to {M.fmt_pct(m['expiry_rate']['to'])}; ReadRate {M.fmt_pct(m['ReadRate']['from'])} to {M.fmt_pct(m['ReadRate']['to'])}.",
                    "status": "Supported" if hit else "Not supported", "status_class": "supported" if hit else "not_driver", "next_step": "Explain the shift. Platform partner / Google" if hit else "Monitor."})
    else:
        hyp.append({"id": "H1", "hypothesis": "Delivery or read differs between templates",
                    "evidence": f"DeliveryRate {rng(drs)} and ReadRate {rng(rrs)} across the templates.",
                    "status": "Not supported" if uniform else "Plausible, needs data", "status_class": "not_driver" if uniform else "plausible", "next_step": "Monitor." if uniform else "Check the lower templates. Platform partner"})
    nontied = conc["comparable"]
    strong = nontied >= 4 and conc["concordant"] / nontied >= 0.75 and not designed
    hyp.append({"id": "H2", "hypothesis": "Offer specificity and content",
                "evidence": _conc_sentence(conc) + (" The scoring was built while analysing this brand, so this is not an independent test." if designed else "") + " Visits and orders after the click are not visible in the sheet.",
                "status": "Supported (directional)" if strong else ("Plausible, needs data" if nontied >= 2 else "Unknown"),
                "status_class": "directional" if strong else ("plausible" if nontied >= 2 else "unknown"), "next_step": f"Per-template visits and orders; A/B test. {brand} / Platform partner / {analyst}"})
    dil = yoy and yoy["metrics"]["ReadRate"]["change_pts"] > 0 and (yoy["metrics"]["P2AResponseRate"]["fold"] or 1) < 1
    hyp.append({"id": "H3", "hypothesis": "Scale dilution and audience mix",
                "evidence": ("Reads rose while clicks fell, which fits a broader audience; the sheet holds no audience data." if dil else "The sheet holds no audience data."),
                "status": "Plausible, needs data" if dil else "Unknown", "status_class": "plausible" if dil else "unknown", "next_step": f"Audience tier and engagement by campaign. {brand} / Google"})
    hyp.append({"id": "H4", "hypothesis": "Pre-send failures",
                "evidence": f"{fail:.1f}% of Messages Submitted failed before sending in {cur_label}.",
                "status": "Open, high priority" if fail >= 20 else "Not a main driver", "status_class": "plausible" if fail >= 20 else "not_driver", "next_step": "Failure reason codes. Platform partner"})
    hyp.append({"id": "H5", "hypothesis": "Send time and offer timing",
                "evidence": f"Expiry rate* is {exp:.1f}% of Messages Sent.",
                "status": "Plausible, needs data" if exp >= 5 else "Unknown", "status_class": "plausible" if exp >= 5 else "unknown", "next_step": "Send-time logs. Platform partner / " + brand})

    recs = []
    seen = {}
    for rid, v in fired.items():
        rule = v["rule"]
        flagged_all = {n_ for n_, e_ in ev.items() if any(w["id"] == rid for w in e_["watch"])}
        key = rule["rec"]
        if key in seen:
            seen[key]["names"] += [x for x in v["names"] if x not in seen[key]["names"]]
            seen[key]["all"] |= flagged_all
            continue
        seen[key] = {"rule": rule, "names": list(v["names"]), "all": set(flagged_all)}
    for key, v in sorted(seen.items(), key=lambda kv: -len(kv[1]["names"])):
        sup, con = direction_support(v["all"], metrics["templates"], ev)
        sent, verdict = _support_text(sup, con)
        pr = {"supports": "Now", "untested": "Next", "mixed": "Test", "contradicts": "Test"}[verdict]
        recs.append({"text": key, "why": f"The text check \"{v['rule']['h']}\" fired on {len(v['names'])} of {n_t} templates: {_names(v['names'])}. {sent}",
                     "owner": brand, "priority": pr, "guardrails": v["rule"].get("guardrails", [])})
    vert = (request.get("vertical") or "").lower()
    if "bfsi" in vert or "bank" in vert or "insur" in vert or "financ" in vert or "lend" in vert:
        recs.append({"text": "**Use a formal, reassuring tone and a low-commitment CTA** such as Check Eligibility for loans and insurance.", "why": "Vi guardrails G2 and G5 for BFSI; check the current CTAs against them.", "owner": brand, "priority": "Next", "guardrails": ["G2", "G5"]})
    elif "health" in vert or "retail" in vert:
        recs.append({"text": "**Use an assuring tone with a specific CTA** for service and reminder messages.", "why": "Vi guardrails G1 and G8 for retail and healthcare.", "owner": brand, "priority": "Next", "guardrails": ["G1", "G8"]})
    recs.append({"text": "**Add a per-template tracking tag to the button URL** and report visits and orders per template.", "why": "The sheet shows clicks but not what happens after; tags tie each template to visits and orders.", "owner": brand, "priority": "Now", "guardrails": []})
    if yoy and hyp[0]["status_class"] == "supported":
        recs.append({"text": "**Explain the delivery and expiry shift** between the two periods.", "why": "See hypothesis H1.", "owner": "Platform partner / Google", "priority": "Now", "guardrails": []})
    if fail >= 20:
        recs.append({"text": "**Diagnose pre-send failures** and add a reachability check before submission.", "why": f"{fail:.1f}% of Messages Submitted failed before sending.", "owner": "Platform partner", "priority": "Now", "guardrails": []})
    recs.append({"text": "**Cap frequency, rotate creatives and scale in tiers.**", "why": "Vi guardrail G6; frequency and audience data are needed to see whether it applies.", "owner": f"{brand} / Platform partner", "priority": "Next", "guardrails": ["G6"]})
    recs.append({"text": "**Run a clean A/B test:** the current text against a version with a specific offer, same audience and send time.", "why": "Isolates the content effect that single-send-date data cannot.", "owner": f"{brand} / Platform partner / {analyst}", "priority": "Test", "guardrails": ["G6"]})
    recs.append({"text": f"**Pre-launch template review by {analyst},** at least a week before launch.", "why": "Closes the loop on content, messaging, CTA placement and flow.", "owner": analyst, "priority": "Ongoing", "guardrails": []})
    order = {"Now": 0, "Next": 1, "Test": 2, "Ongoing": 3}
    recs.sort(key=lambda r: order[r["priority"]])
    for i, r in enumerate(recs, 1):
        r["n"] = i

    needs = [{"owner": brand, "item": "Visits and orders per template (tagged URLs)", "why": "Shows what happens after the click and tests whether content explains the click rate", "priority": "High"},
             {"owner": "Google / Platform partner", "item": "Audience tier or demographics, send time and frequency per campaign", "why": "Separates content from audience and timing; Vi has no visibility", "priority": "High"}]
    if fail >= 20:
        needs.append({"owner": "Platform partner", "item": "Failure reason codes by GoogleID and template", "why": "The largest loss in the funnel; may skew who is reached", "priority": "High"})
    if yoy and hyp[0]["status_class"] == "supported":
        needs.append({"owner": "Platform partner", "item": "Why DeliveryRate and expiry changed between the periods", "why": "Tests H1; the sheet cannot explain it", "priority": "High"})
    if request.get("reference_templates"):
        needs.append({"owner": brand, "item": "Graphics for the earlier-period templates", "why": "Completes the period comparison", "priority": "Medium"})

    def pooled_pair(fn):
        a = [t for t in cur_ts if t["template"] in ev and fn(ev[t["template"]]["features"])]
        b = [t for t in cur_ts if t["template"] in ev and not fn(ev[t["template"]]["features"])]
        return (_pooled(a), _pooled(b)) if a and b else (None, None)

    srt_d = sorted(dist.items(), key=lambda kv: -pooled_cr[kv[0]])
    cols = [short.get(nm, nm) + f" ({M.fmt_pct(pooled_cr[nm])})" for nm, _ in srt_d]
    rows = []
    for label, fn, good in (("Number, price or code in the text", lambda f: f["quantified"], True), ("Deadline in the text", lambda f: f["has_deadline"], None),
                            ("Chance-based wording", lambda f: f["chance"], False), ("Minimum or maximum wording", lambda f: f["floor_or_ceiling"], False)):
        cells = ["Yes" if (nm in ev and fn(ev[nm]["features"])) else ("No" if nm in ev else "n/a") for nm, _ in srt_d]
        a_, b_ = pooled_pair(fn)
        if a_ is None or good is None:
            verdict = {"t": "**Not testable.** All templates are the same on this feature." if a_ is None and good is not None else "**No clear direction** to test.", "fill": "amber"}
        else:
            ok = (a_ > b_) if good else (a_ < b_)
            verdict = {"t": "**Yes.** Templates with this feature click " + ("higher." if good else "lower.") if ok else "**No.** The direction does not match.", "fill": "green" if ok else "red"}
        rows.append([label] + cells + [verdict])
    rows.append(["Content score (text only)"] + [f"{ev[nm]['score']:.1f} ({ev[nm]['risk']})" if nm in ev else "n/a" for nm, _ in srt_d] +
                [{"t": "**" + ("Mostly." if conc["comparable"] and conc["concordant"] / conc["comparable"] >= 0.6 else "Weakly.") + "** " + _conc_sentence(conc), "fill": "amber"}])
    matrix = {"header": ["Feature"] + cols + ["Lines up with click rate?"], "rows": rows,
              "bullets": [f"**{_conc_sentence(conc)}**", "**Limits.** Few templates and single send dates: a feature that lines up here is a lead for the A/B test, not a cause."]}

    c_sent = _conc_sentence(conc)
    issue = (request.get("issue", "").strip() or f"{brand} asked for a template review.")
    issue = issue if issue.endswith((".", "!", "?")) else issue + "."
    n_all = len({t["template"] for t in metrics["templates"]})
    lead = (f"{issue} We analysed {n_t} templates for {cur_label} from the partner portal export"
            + (f", with {n_all - n_t} earlier-period templates for comparison" if n_all > n_t else "")
            + (f" (the {len(cur_ts)} send dates of {n_t} templates)" if len(cur_ts) > n_t else "")
            + ". The numbers come from the sheet; the content review comes from Vi's internal model, which scores the card text only and never sees click data. "
            + ("Each send has its own audience and timing, which the sheet does not show, so findings are directional, not proven. " if len(cur_ts) > n_t else "Every template is a single send date, so findings are directional, not proven. ")
            + "What the data cannot show is what happened after the click (visits and orders), which sits with the brand.")
    bullets = [f"**Delivery and read are {'uniform' if uniform else 'not uniform'} within {cur_label}.** DeliveryRate is {rng(drs)} and ReadRate {rng(rrs)}."]
    if spread:
        bullets.append(f"**Click rate varies {spread:.1f}x.** P2AResponseRate (click rate) runs from {M.fmt_pct(M.shown_rate(bot, 'P2AResponseRate'))} ({lbl(bot)}) to {M.fmt_pct(M.shown_rate(top, 'P2AResponseRate'))} ({lbl(top)}).")
    bullets.append(f"**{c_sent}**")
    if yoy:
        m = yoy["metrics"]
        bullets.append(f"**Year on year,** click rate moved from {M.fmt_pct(m['P2AResponseRate']['from'])} to {M.fmt_pct(m['P2AResponseRate']['to'])}, ReadRate from {M.fmt_pct(m['ReadRate']['from'])} to {M.fmt_pct(m['ReadRate']['to'])} and DeliveryRate from {M.fmt_pct(m['DeliveryRate']['from'])} to {M.fmt_pct(m['DeliveryRate']['to'])}.")
    bullets.append(f"**{fail:.1f}% of submitted messages failed before sending** in {cur_label}; this is separate from template content.")
    bullets.append("**Not yet answerable from this data:** visits and orders after the click, audience mix, frequency, send time and device split.")

    top_w = sorted({w["h"] for nm, _ in srt_d[:max(1, n_t // 2)] for w in ev.get(nm, {}).get("works", [])})
    bot_w = sorted({w["h"] for nm, _ in srt_d[-max(1, n_t // 2):] for w in ev.get(nm, {}).get("watch", [])})
    deck = {
        "funnel_title": "Where messages are lost along the way",
        "funnel_panel": [{"h": "Lost before sending", "b": f"{fail:.1f}% of Messages Submitted failed before sending in {cur_label}. This happens before the platform sends, so it is separate from template content."},
                         {"h": "After sending", "b": f"DeliveryRate is {gr['DeliveryRate']:.2f}% of Messages Sent and the expiry rate* is {exp:.1f}%."},
                         {"h": "Ask", "b": "Failure reason codes by GoogleID and template."}],
        "yoy_note": (f"**Delivered base.** The click rate on a delivered base moved from {M.fmt_pct(yoy['metrics']['click_rate_delivered_base']['from'])} to {M.fmt_pct(yoy['metrics']['click_rate_delivered_base']['to'])}. Compare it with a brand's CTR only as a consistency check.") if yoy else "",
        "ranking_title": "Click rate by template",
        "ranking_panel": [{"h": "Highest click rates", "b": f"{_names([short.get(nm, nm) + ' (' + M.fmt_pct(pooled_cr[nm]) + ')' for nm, _ in srt_d[:2]], 2)}." + (f" Text strengths: {', '.join(top_w[:3])}." if top_w else "")},
                          {"h": "Lowest click rates", "b": f"{_names([short.get(nm, nm) + ' (' + M.fmt_pct(pooled_cr[nm]) + ')' for nm, _ in srt_d[-2:]], 2)}." + (f" Text flags: {', '.join(bot_w[:3])}." if bot_w else "")},
                          {"h": "Text score vs result", "b": c_sent}],
        "recommendations_title": "Changes to make in the next sends",
        "recommendations": [{"h": _bold_head(r["text"]), "b": r["why"]} for r in recs if r["priority"] in ("Now", "Next")][:6],
        "next_steps_test": ["A specific offer against the current text, same audience and send time for both arms", "A tracking tag on every button URL so visits and orders are reported per template", f"{analyst} reviews the next templates at least a week before launch"],
        "data_asks": [{"owner": o, "text": "; ".join(x["item"] for x in needs if x["owner"] == o and x["priority"] == "High")} for o in dict.fromkeys(x["owner"] for x in needs if x["priority"] == "High")][:3],
    }

    analysis = {
        "meta": {"title": f"{brand}: Template Analysis and Input to the RCA", "subtitle": f"Analysis of {n_t} templates for {cur_label}",
                 "deck_title": "RCS Template Analysis", "deck_subtitle": request.get("period", "")},
        "summary": {"lead": lead, "bullets": bullets}, "numbers_commentary": numbers, "yoy_commentary": yoy_c,
        "templates": tmpl_out, "feature_matrix": matrix, "findings": findings, "hypotheses": hyp, "recommendations": recs,
        "dos": rules.get("dos", []), "donts": rules.get("donts", []), "data_needed": needs, "deck": deck,
        "reference_templates": [{"template": r["template"], "tag": r.get("short") or r["template"],
                                 "bullets": [f"{w['h']}" for w in (ev.get(r["template"], {}).get("watch") or ev.get(r["template"], {}).get("works") or [])][:2]} for r in (request.get("reference_templates") or [])],
        "external_numbers": declared,
    }
    info = {"model": {k: v for k, v in model_info.items() if k != "model"}, "concordance": conc,
            "scores": {k: {"score": v["score"], "risk": v["risk"], "flags": [w["id"] for w in v["watch"]]} for k, v in ev.items()}}
    return analysis, info
