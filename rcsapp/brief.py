"""
Prompts for the language-model engine, built from the request, the computed metrics and the knowledge pack.

The work is split in two so that the independence rule is enforced by the code, not just requested:
  STEP 1  one call per template. The model sees the card text (and the image if vision is on) and NO performance data.
          It rates content risk and writes the review and two rewritten variations.
  STEP 2  one call for everything else. The model now sees the computed numbers and the step 1 reviews.
"""
import json

from . import metrics as M
from .knowledge import knowledge_text

RULES = """RULES (follow all of them)
1. Use only numbers that are given to you. Do not calculate, estimate or round numbers yourself. If you need a figure you were not given, do not use it, or list it in "external_numbers" with its source.
2. P2AResponseRate is the CLICK RATE. Call it "click rate (P2AResponseRate)". Never call it P2A.
3. Use the sheet's own names: Messages Submitted, DeliveryRate, ReadRate, P2AResponseRate. Ratios marked * are calculated from the sheet counts.
4. Rate each template's content risk (Low, Medium, High) from its content alone. Never let a result decide the rating.
5. Every template is a single send date unless told otherwise. Say findings are directional, not proven. Do not claim causes the data cannot show: visits, orders, audience, send time and device split are not in this data. Brands do not share targeting, so never guess the audience.
6. Compare periods (if two are given) before concluding. A pattern seen in one period only is a hypothesis.
7. "Creative" means the image. "Text" means the card title and description. Do not use the word "copy".
8. The files go to the brand's partner. No internal classification text, no Vi commercial figures, no other brand's name, no section numbers in the text.
9. State platform behaviour only if it is in the knowledge pack. Cite guardrail IDs (G1, G2...) in "guardrails" on a recommendation when you rely on one.
10. Be specific and short. Each point: a short heading "h" and one or two sentences "b".
11. Proposed wording goes only in "variations". Use placeholders such as [v1] and [price] for variables. Do not invent offers, prices or dates.
12. If an image is attached, say what it shows and whether it agrees with the text (offer, code, button, deadline). If you cannot see an image, review the text alone and do not describe an image."""

SYSTEM = ("You are a senior RCS template analyst at a telecom messaging team. You write precise, evidence-based template reviews for "
          "brands and their messaging partners. You answer with one JSON object and nothing else.")


def _brand(request):
    return request.get("brand_display") or request.get("brand")


def request_block(request):
    L = ["THE REQUEST", f"- Brand: {_brand(request)}", f"- Period: {request.get('period', '')}"]
    if request.get("vertical"):
        L.append(f"- Vertical: {request['vertical']}")
    L.append(f"- What the brand or partner asked: {request.get('issue', '')}")
    return "\n".join(L)


def card_block(t):
    c = t.get("card", {})
    desc = c.get("description", "")
    desc = " ".join(desc) if isinstance(desc, list) else desc
    return "\n".join([f"- Template: {t['template']}", f"- Creative (image): {t.get('creative', '(see attached image)')}",
                      f"- Card title: {c.get('title', '')}", f"- Card description: {desc}", f"- Button: {c.get('button', '')}"])


def template_skeleton():
    return {"content_risk": "Low|Medium|High", "content_risk_basis": "what in the content drove the rating",
            "works": [{"h": "", "b": ""}], "watch_outs": [{"h": "", "b": ""}],
            "variations": [{"name": "Variation 1 - ...", "title": "", "description": [""], "why": ""},
                           {"name": "Variation 2 - ...", "title": "", "description": [""], "why": ""}]}


def step1_prompt(request, template, knowledge):
    return "\n\n".join([
        "STEP 1 OF 2: CONTENT REVIEW. You are given no performance data on purpose.",
        RULES, knowledge_text(knowledge, request.get("vertical")), request_block(request),
        "THE TEMPLATE\n" + card_block(template),
        "Return ONE JSON object with exactly this structure. Give 3 to 6 points in each of works and watch_outs (works may be shorter if little works). "
        "Use only figures that appear in the card text.\n```json\n" + json.dumps(template_skeleton(), indent=1) + "\n```"])


def global_skeleton():
    return {
        "summary": {"lead": "", "bullets": [""]},
        "numbers_commentary": [{"h": "", "b": ""}],
        "yoy_commentary": [{"h": "", "b": ""}],
        "findings": [{"h": "", "b": ""}],
        "hypotheses": [{"id": "H1", "hypothesis": "", "evidence": "", "status": "", "status_class": "supported|directional|plausible|not_driver|unknown", "next_step": ""}],
        "recommendations": [{"n": 1, "text": "", "why": "", "owner": "", "priority": "Now|Next|Test|Ongoing", "guardrails": ["G1"]}],
        "dos": [""], "donts": [""],
        "data_needed": [{"owner": "", "item": "", "why": "", "priority": "High|Medium"}],
        "deck": {"funnel_panel": [{"h": "", "b": ""}], "yoy_note": "", "ranking_panel": [{"h": "", "b": ""}],
                 "recommendations": [{"h": "", "b": ""}], "next_steps_test": [""], "data_asks": [{"owner": "", "text": ""}]},
        "external_numbers": [{"value": "", "source": ""}],
    }


def md_table(header, rows):
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(out)


def performance_block(metrics):
    hdr = ["Template", "Date", "GoogleID", "Messages Submitted", "DeliveryRate", "ReadRate", "P2AResponseRate (click rate)",
           "Pre-send failure rate*", "End-to-end delivery rate*", "Expiry rate*", "Click rate, delivered base*"]
    rows = []
    for t in metrics["templates"]:
        r = t["rates"]
        rows.append([t["template"], M.nice_date(t["date"]), t["google_id_short"], M.fmt_int(t["counts"]["submitted"]),
                     M.fmt_pct(M.shown_rate(t, "DeliveryRate")), M.fmt_pct(M.shown_rate(t, "ReadRate")), M.fmt_pct(M.shown_rate(t, "P2AResponseRate")),
                     M.fmt_pct(r["pre_send_failure_rate"], 1), M.fmt_pct(r["end_to_end_delivery_rate"], 1), M.fmt_pct(r["expiry_rate"], 1),
                     M.fmt_pct(r["click_rate_delivered_base"])])
    L = [md_table(hdr, rows), "", "Totals by year (recomputed from summed counts):"]
    hdr2 = ["Year", "Templates", "Messages Submitted", "DeliveryRate", "ReadRate", "P2AResponseRate", "Pre-send failure*", "End-to-end*", "Expiry*", "Click rate, delivered base*"]
    rows2 = []
    for y, g in sorted(metrics["groups"]["year"].items()):
        r = g["rates"]
        rows2.append([y, g["n_templates"], M.fmt_int(g["counts"]["submitted"]), M.fmt_pct(r["DeliveryRate"]), M.fmt_pct(r["ReadRate"]), M.fmt_pct(r["P2AResponseRate"]),
                      M.fmt_pct(r["pre_send_failure_rate"], 1), M.fmt_pct(r["end_to_end_delivery_rate"], 1), M.fmt_pct(r["expiry_rate"], 2), M.fmt_pct(r["click_rate_delivered_base"])])
    L.append(md_table(hdr2, rows2))
    yoy = metrics["comparisons"].get("year_over_year")
    if yoy:
        L.append(f"\nYear on year ({yoy['from_year']} to {yoy['to_year']}):")
        rows3 = [[k, M.fmt_pct(v["from"]), M.fmt_pct(v["to"]), f"{v['change_pts']:+.1f} pts", f"{v['fold']:.2f}x" if v["fold"] else "n/a"] for k, v in yoy["metrics"].items()]
        L.append(md_table(["Metric", "From", "To", "Change", "Fold (to / from)"], rows3))
        L.append(f"\nMessages Submitted growth: {yoy['submitted_growth'] * 100:+.0f}%")
    L.append("\nData checks:")
    for i in metrics["issues"]:
        L.append(f"- [{i['level']}] {i.get('template', '')} {i['message']}")
    L.append("\nDefinitions:\n" + md_table(["Name", "Calculation", "Used for"], [[d["name"], d["formula"], d["used_for"]] for d in metrics["definitions"]]))
    return "\n".join(L)


def step2_prompt(request, metrics, template_reviews, knowledge):
    cur = max(metrics["meta"]["years"])
    reviews = []
    for name, rv in template_reviews.items():
        reviews.append(f"### {name}\n- Content risk (rated from content alone): {rv.get('content_risk')}\n- Basis: {rv.get('content_risk_basis', '')}\n"
                       + "\n".join(f"- Works: {x['h']}. {x['b']}" for x in rv.get("works", []))
                       + "\n" + "\n".join(f"- Watch-out: {x['h']}. {x['b']}" for x in rv.get("watch_outs", [])))
    refs = ""
    if request.get("reference_templates"):
        refs = "\n\nEarlier-period templates (text only):\n" + "\n".join(
            f"- {t['template']}: title \"{t['card'].get('title', '')}\"; description \"{t['card'].get('description', '')}\"" for t in request["reference_templates"])
    return "\n\n".join([
        "STEP 2 OF 2: FINDINGS, HYPOTHESES AND RECOMMENDATIONS.",
        RULES, knowledge_text(knowledge, request.get("vertical")), request_block(request),
        f"CONTENT REVIEWS FROM STEP 1 (current period {cur}; ratings were made before any result was seen)\n" + "\n\n".join(reviews) + refs,
        "PERFORMANCE (computed by code; the only numbers you may use)\n" + performance_block(metrics),
        "Return ONE JSON object with exactly this structure. Compare the content ratings with the click rates honestly: say where they agree and where they do not. "
        "Leave yoy_commentary empty if there is only one period. Write 4 to 9 findings and 8 to 11 recommendations; every recommendation needs text, why, owner, priority.\n```json\n"
        + json.dumps(global_skeleton(), indent=1) + "\n```"])
