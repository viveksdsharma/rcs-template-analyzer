import copy
import io
import json
import os
import re
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
from rcsapp import CONFIG  # noqa: E402
from rcsapp import check as C  # noqa: E402
from rcsapp import features as F  # noqa: E402
from rcsapp import metrics as M  # noqa: E402
from rcsapp.engines import ml_engine as E  # noqa: E402
from rcsapp.engines import reconcile as R  # noqa: E402
from rcsapp.knowledge import load_knowledge, load_rules  # noqa: E402

STYLE = json.load(open(os.path.join(CONFIG, "style.json"), encoding="utf-8"))
OB = os.path.join(CONFIG, "other_brands.txt")
K, RULES = load_knowledge(), load_rules()


def request():
    cur, ref = [], []
    for t in H.TEMPLATES:
        e = {"template": t["template"], "label": t["label"], "short": t["short"], "creative": t["creative"], "card": {"title": t["title"], "description": t["description"], "button": t["button"]}}
        (ref if t["template"].startswith("old_") else cur).append(e)
    return {"brand": "DEMO BRAND", "brand_display": "Demo Brand", "period": "Aug 2026 vs Aug 2025", "period_labels": {"2026": "August 2026", "2025": "August 2025"},
            "issue": "The brand sees lower click rates on some templates.", "templates": cur, "reference_templates": ref, "vertical": "E-commerce"}


class Rules(unittest.TestCase):
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
        items = [{"year": 2026, "score": 0, "click": 3}, {"year": 2026, "score": 2, "click": 1}, {"year": 2025, "score": 9, "click": 99}]
        c = E.concordance(items)
        self.assertEqual((c["concordant"], c["discordant"], c["tied"]), (1, 0, 0))


class Analysis(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.metrics = M.run(io.BytesIO(H.sheet_bytes()), brand="DEMO", filename="x.xlsx")
        cls.req = request()
        cls.a, cls.info = E.build_analysis(cls.metrics, cls.req, K, RULES)

    def errs(self, a):
        return [i["code"] for i in C.run_all(a, self.req, self.metrics, STYLE, OB, K, extra_brands=["Axis"]) if i["level"] == "error"]

    def test_rules_output_passes_every_check(self):
        self.assertEqual(self.errs(self.a), [])

    def test_all_current_templates_reviewed(self):
        self.assertEqual({t["template"] for t in self.a["templates"]}, {"demo_code_100", "demo_generic", "demo_raffle", "demo_flat_min"})

    def test_recommendations_are_tested_against_the_runs_data(self):
        for r in self.a["recommendations"]:
            if "text rule" in r["why"]:
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

    def test_missing_template_and_caveat_caught(self):
        a = copy.deepcopy(self.a)
        a["templates"] = a["templates"][:3]
        self.assertIn("template_not_reviewed", self.errs(a))

        def strip(o):
            if isinstance(o, str):
                return re.sub(r"(?i)(single|one) send date|directional", "x", o)
            if isinstance(o, list):
                return [strip(x) for x in o]
            if isinstance(o, dict):
                return {k: (v if k in ("status_class", "content_risk", "priority") else strip(v)) for k, v in o.items()}
            return o
        self.assertIn("missing_caveat", self.errs(strip(self.a)))

    def test_unknown_guardrail_caught(self):
        a = copy.deepcopy(self.a)
        a["recommendations"][0]["guardrails"] = ["G99"]
        self.assertIn("unknown_guardrail", self.errs(a))

    def test_designed_with_brand_is_not_called_independent(self):
        rules = copy.deepcopy(RULES)
        rules["designed_with"] = ["DEMO"]
        a, _ = E.build_analysis(self.metrics, self.req, K, rules)
        h2 = next(h for h in a["hypotheses"] if h["id"] == "H2")
        self.assertIn("not an independent test", h2["evidence"])
        self.assertNotEqual(h2["status_class"], "directional")


def synthetic_history(n_brands=6, per=14, signal=True, seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    for b in range(n_brands):
        for i in range(per):
            f = {k: 0.0 for k in F.MODEL_FEATURES}
            f["has_number"], f["chance"], f["has_code"] = float(rng.integers(0, 2)), float(rng.integers(0, 2) * (rng.random() < 0.4)), float(rng.integers(0, 2))
            f["title_words"], f["desc_words"], f["n_vars"] = float(rng.integers(2, 9)), float(rng.integers(8, 45)), float(rng.integers(0, 6))
            y = (0.7 * f["has_number"] - 0.8 * f["chance"] + 0.3 * f["has_code"] + rng.normal(0, 0.15)) if signal else rng.normal(0, 0.6)
            rows.append({"run_id": "t", "brand": f"B{b}", "vertical": "", "template": f"t{b}_{i}", "date": "2026-01-01", "year": 2026, "reads": 50000,
                         "click_rate": 2.0, "click_index": float(np.exp(y)), **f})
    return pd.DataFrame(rows)


class TrainedModel(unittest.TestCase):
    def test_not_used_with_little_history(self):
        h = synthetic_history(n_brands=2, per=5)
        info = E.fit_model(h)
        self.assertEqual(info["status"], "insufficient")
        self.assertNotIn("model", info)

    def test_noise_is_declined(self):
        info = E.fit_model(synthetic_history(signal=False))
        self.assertEqual(info["status"], "not_predictive")

    def test_real_signal_is_used_and_validated_on_unseen_brands(self):
        info = E.fit_model(synthetic_history(signal=True))
        self.assertEqual(info["status"], "ok")
        self.assertGreater(info["r2"], 0.3)
        self.assertIn(info["drivers"][0][0], ("has_number", "chance"))

    def test_current_brand_excluded_from_training(self):
        h = synthetic_history()
        info = E.fit_model(h, exclude_brand="B0")
        self.assertEqual(info["brands"], 5)

    def test_model_findings_pass_the_number_check(self):
        metrics = M.run(io.BytesIO(H.sheet_bytes()), brand="DEMO", filename="x.xlsx")
        req = request()
        a, info = E.build_analysis(metrics, req, K, RULES, history=synthetic_history())
        self.assertEqual(info["model"]["status"], "ok")
        self.assertTrue(any(f["h"].startswith("Model view") for f in a["findings"]))
        self.assertEqual([i for i in C.run_all(a, req, metrics, STYLE, OB, K) if i["level"] == "error"], [])


class Reconcile(unittest.TestCase):
    def test_more_cautious_rating_wins_and_is_noted(self):
        ml = {"templates": [{"template": "a", "content_risk": "High"}, {"template": "b", "content_risk": "Low"}]}
        llm = {"templates": [{"template": "a", "content_risk": "Low"}, {"template": "b", "content_risk": "Medium"}]}
        rows, summ = R.agreement(ml, llm)
        self.assertEqual(summ, {"templates": 2, "agree": 0, "disagree": 2})
        final, notes = R.reconcile(ml, llm)
        self.assertEqual({t["template"]: t["content_risk"] for t in final["templates"]}, {"a": "High", "b": "Medium"})
        self.assertEqual(len(notes), 2)


if __name__ == "__main__":
    unittest.main()
