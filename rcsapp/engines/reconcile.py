"""
Two engines will not give word-for-word identical output, and pretending otherwise would hide disagreements.
They share one schema and the same checks, and this module shows where they differ.
"""
SEV = {"Low": 0, "Medium": 1, "High": 2}


def agreement(a, b, name_a="Internal ML model", name_b="Open-source LLM"):
    """a, b: analyses. Returns a table (list of dicts) and a summary."""
    ta = {t["template"]: t for t in a.get("templates", [])}
    tb = {t["template"]: t for t in b.get("templates", [])}
    rows, agree = [], 0
    for name in ta:
        if name not in tb:
            continue
        ra, rb = ta[name]["content_risk"], tb[name]["content_risk"]
        same = ra == rb
        agree += same
        rows.append({"template": name, name_a: ra, name_b: rb, "agree": same, "gap": abs(SEV[ra] - SEV[rb])})
    return rows, {"templates": len(rows), "agree": agree, "disagree": len(rows) - agree}


def reconcile(ml, llm):
    """Final analysis for 'both' mode: the language model's text, the more cautious content risk where they differ.
    Returns (analysis, list of notes for the internal review file)."""
    final = dict(llm)
    final["templates"] = [dict(t) for t in llm["templates"]]
    notes = []
    mlt = {t["template"]: t for t in ml.get("templates", [])}
    for t in final["templates"]:
        m = mlt.get(t["template"])
        if not m:
            continue
        if SEV[m["content_risk"]] > SEV[t["content_risk"]]:
            notes.append(f"{t['template']}: Internal ML model says {m['content_risk']}, the LLM said {t['content_risk']}; the higher rating is used. Reviewer to confirm.")
            t["content_risk"] = m["content_risk"]
        elif SEV[m["content_risk"]] < SEV[t["content_risk"]]:
            notes.append(f"{t['template']}: Internal ML model says {m['content_risk']}, the LLM said {t['content_risk']}; the LLM's rating is kept (the more cautious one).")
    for key in ("feature_matrix", "reference_templates"):
        if key in ml and key not in final:
            final[key] = ml[key]
    return final, notes
