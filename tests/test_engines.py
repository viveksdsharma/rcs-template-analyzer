import copy
import io
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
from rcsapp import CONFIG  # noqa: E402
from rcsapp import check as C  # noqa: E402
from rcsapp import metrics as M  # noqa: E402
from rcsapp import training as T  # noqa: E402
from rcsapp.engines import ml_engine as E  # noqa: E402
from rcsapp.engines import reconcile as R  # noqa: E402
from rcsapp.knowledge import load_knowledge, load_rules  # noqa: E402

with open(os.path.join(CONFIG, "style.json"), encoding="utf-8") as _f:
    STYLE = json.load(_f)
OB = os.path.join(CONFIG, "other_brands.txt")
K, RULES = load_knowledge(), load_rules()


def request():
    cur, ref = [], []
    for t in H.TEMPLATES:
        e = {"template": t["template"], "label": t["label"], "short": t["short"], "creative": t["creative"], "card": {"title": t["title"], "description": t["description"], "button": t["button"]}}
        (ref if t["template"].startswith("old_") else cur).append(e)
    return {"brand": "DEMO BRAND", "brand_display": "Demo Brand", "period": "Aug 2026 vs Aug 2025", "period_labels": {"2026": "August 2026", "2025": "August 2025"},
            "issue": "The brand sees lower click rates on some templates.", "templates": cur, "reference_templates": ref, "vertical": "E-commerce"}


class Scoring(unittest.TestCase):
    def test_risk_follows_content_only(self):
        req = {t["template"]: t for t in request()["templates"] + request()["reference_templates"]}
        ev = {k: E.evaluate(v["card"], RULES) for k, v in req.items()}
        self.assertEqual(ev["demo_code_100"]["risk"], "Low")
        self.assertIn("chance_based", [w["id"] for w in ev["demo_raffle"]["watch"]])
        self.assertIn("floor_or_ceiling", [w["id"] for w in ev["demo_flat_min"]["watch"]])
        self.assertGreater(ev["demo_raffle"]["score"], ev["demo_code_100"]["score"])

    def test_score_never_reads_performance(self):
        import inspect
        src = inspect.getsource(E.evaluate)
        for banned in ("click", "rates", "counts", "metrics"):
            self.assertNotIn(banned, src)

    def test_concordance_within_year_only(self):
        c = E.concordance([{"year": 2026, "score": 0, "click": 3}, {"year": 2026, "score": 2, "click": 1}, {"year": 2025, "score": 9, "click": 99}])
        self.assertEqual((c["concordant"], c["discordant"], c["tied"]), (1, 0, 0))

    def test_rule_evidence_reads_the_direction_across_brands(self):
        ev = E.rule_evidence(T.finalize(H.synthetic_history_raw(signal=True)), RULES).set_index("check")
        self.assertEqual(ev.loc["chance_based", "reading"], "as expected (flagged rows click lower)")
        self.assertEqual(len(E.rule_evidence(None, RULES)), 0)


class Analysis(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.metrics = M.run(io.BytesIO(H.sheet_bytes()), brand="DEMO", filename="x.xlsx")
        cls.req = request()
        cls.a, cls.info = E.build_analysis(cls.metrics, cls.req, K, RULES)

    def errs(self, a, metrics=None):
        return [i["code"] for i in C.run_all(a, self.req, metrics or self.metrics, STYLE, OB, K, extra_brands=["Axis"]) if i["level"] == "error"]

    def test_output_passes_every_check(self):
        self.assertEqual(self.errs(self.a), [])

    def test_no_rule_based_wording_reaches_the_brand_facing_text(self):
        blob = json.dumps({k: v for k, v in self.a.items() if k != "meta"}).lower()
        for banned in ("rule-based", "rules engine", "rules-engine", "text rule"):
            self.assertNotIn(banned, blob)
        self.assertIn("internal model", self.a["summary"]["lead"])

    def test_repeated_sends_make_one_review_per_template_and_pass_the_checks(self):
        m = M.run(io.BytesIO(H.sheet_bytes(repeat_send=True)), brand="DEMO", filename="x.xlsx")
        a, _ = E.build_analysis(m, self.req, K, RULES)
        names = [t["template"] for t in a["templates"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("over 2 sends", next(t for t in a["templates"] if t["template"] == "demo_code_100")["strip"])
        self.assertEqual(self.errs(a, metrics=m), [])

    def test_recommendations_are_tested_against_the_runs_data(self):
        for r in self.a["recommendations"]:
            if "text check" in r["why"]:
                self.assertRegex(r["why"], r"In this run|This run cannot test")

    def test_planted_number_is_caught(self):
        a = copy.deepcopy(self.a)
        a["summary"]["bullets"][1] = re.sub(r"(\d+)\.(\d+)%", lambda m: f"{m.group(1)}.{(int(m.group(2)) + 3) % 100:02d}%", a["summary"]["bullets"][1], count=1)
        self.assertIn("unverified_number", self.errs(a))

    def test_other_brand_and_internal_marking_caught(self):
        a = copy.deepcopy(self.a)
        a["findings"][0]["b"] += " Axis Bank saw the same."
        self.assertIn("other_brand", self.errs(a))
        a = copy.deepcopy(self.a)
        a["summary"]["lead"] += " C2 \u2013 Vodafone Idea Internal"
        self.assertIn("internal_marker", self.errs(a))

    def test_the_brands_own_name_is_not_blocked_even_when_spelled_differently(self):
        req = dict(self.req, brand="Axis", brand_display="Axis")
        a = copy.deepcopy(self.a)
        a["findings"][0]["b"] += " Axis Bank asked for this."
        codes = [i["code"] for i in C.check_confidentiality(a, req, STYLE, OB, extra_brands=["Axis Bank", "Nykaa"])]
        self.assertNotIn("other_brand", codes)
        a["findings"][0]["b"] += " Nykaa did better."
        self.assertIn("other_brand", [i["code"] for i in C.check_confidentiality(a, req, STYLE, OB, extra_brands=["Axis Bank", "Nykaa"])])

    def test_missing_template_caveat_and_guardrail_caught(self):
        a = copy.deepcopy(self.a)
        a["templates"] = a["templates"][:3]
        self.assertIn("template_not_reviewed", self.errs(a))
        a = copy.deepcopy(self.a)
        a["recommendations"][0]["guardrails"] = ["G99"]
        self.assertIn("unknown_guardrail", self.errs(a))

        def strip(o):
            if isinstance(o, str):
                return re.sub(r"(?i)(single|one) send date|directional", "x", o)
            if isinstance(o, list):
                return [strip(x) for x in o]
            if isinstance(o, dict):
                return {k: (v if k in ("status_class", "content_risk", "priority") else strip(v)) for k, v in o.items()}
            return o
        self.assertIn("missing_caveat", self.errs(strip(self.a)))

    def test_designed_with_brand_is_not_called_independent(self):
        rules = copy.deepcopy(RULES)
        rules["designed_with"] = ["DEMO"]
        a, _ = E.build_analysis(self.metrics, self.req, K, rules)
        h2 = next(h for h in a["hypotheses"] if h["id"] == "H2")
        self.assertIn("not an independent test", h2["evidence"])
        self.assertNotEqual(h2["status_class"], "directional")

    def test_trained_component_findings_pass_the_number_check(self):
        a, info = E.build_analysis(self.metrics, self.req, K, RULES, history=T.finalize(H.synthetic_history_raw(signal=True)))
        self.assertEqual(info["model"]["status"], "ok")
        self.assertTrue(any(f["h"].startswith("Trained component") for f in a["findings"]))
        self.assertEqual(self.errs(a), [])

    def test_the_analysed_brand_is_left_out_of_training_even_when_named_differently(self):
        raw = H.synthetic_history_raw(signal=True)
        raw.loc[raw["brand"] == "Brand0", "brand"] = "Demo Brand Ltd"
        self.assertEqual(E.build_analysis(self.metrics, self.req, K, RULES, history=T.finalize(raw))[1]["model"]["brands"], 5)


class Reconcile(unittest.TestCase):
    def test_more_cautious_rating_wins_and_is_noted(self):
        ml = {"templates": [{"template": "a", "content_risk": "High"}, {"template": "b", "content_risk": "Low"}]}
        llm = {"templates": [{"template": "a", "content_risk": "Low"}, {"template": "b", "content_risk": "Medium"}]}
        rows, summ = R.agreement(ml, llm)
        self.assertEqual(summ, {"templates": 2, "agree": 0, "disagree": 2})
        self.assertIn("Internal ML model", rows[0])
        final, notes = R.reconcile(ml, llm)
        self.assertEqual({t["template"]: t["content_risk"] for t in final["templates"]}, {"a": "High", "b": "Medium"})
        self.assertEqual(len(notes), 2)


if __name__ == "__main__":
    unittest.main()
