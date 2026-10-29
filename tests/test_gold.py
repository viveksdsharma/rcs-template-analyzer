import io
import os
import sys
import unittest

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
from rcsapp import gold as G  # noqa: E402
from rcsapp import training as T  # noqa: E402
from rcsapp.knowledge import load_rules  # noqa: E402

RULES = load_rules()


def history():
    return T.finalize(H.synthetic_history_raw(n_brands=6, per=14, signal=True))


def rated_gold(h, agree=True):
    g = G.blind_sheet(h, RULES)
    es = G.engine_scores(g, RULES)
    g["content_risk"] = es["engine_risk"].values
    g["confidence"] = "High"
    for fc in G.flag_cols(RULES):
        g[fc] = ["Y" if fc in f else "" for f in es["engine_flags"]]
    if not agree:
        g.loc[g.index[:30], "content_risk"] = "High"
    return g


class BlindSheet(unittest.TestCase):
    def test_no_results_anywhere_and_order_is_shuffled(self):
        h = history()
        g = G.blind_sheet(h, RULES, seed=1)
        for banned in ("reads", "clicks", "click_rate", "click_index", "loo_rate", "weight"):
            self.assertNotIn(banned, g.columns)
        self.assertTrue(g["outcome_click_index"].eq("").all())
        self.assertTrue(g["saw_performance"].eq("N").all())
        self.assertEqual(len(g), h["text_group"].nunique())
        self.assertNotEqual(g["template_id"].tolist(), h.drop_duplicates("text_group")["template"].tolist())

    def test_a_brand_stays_in_one_split(self):
        g = G.blind_sheet(history(), RULES)
        self.assertEqual(set(g["split"]), {"dev", "test"})
        self.assertTrue(g.groupby("brand")["split"].nunique().eq(1).all())

    def test_workbook_has_dropdowns_rubric_and_instructions(self):
        g = G.blind_sheet(history(), RULES)
        wb = openpyxl.load_workbook(io.BytesIO(G.template_workbook(g, RULES)))
        self.assertEqual(wb.sheetnames, ["Rate here", "How to", "Rubric"])
        self.assertEqual([c.value for c in wb["Rate here"][1]], list(g.columns))
        self.assertGreater(len(wb["Rate here"].data_validations.dataValidation), 5)
        self.assertIn("BEFORE", wb["How to"]["A2"].value)

    def test_the_real_bundled_history_makes_a_blind_sheet(self):
        h, _ = T.load_seed()
        g = G.blind_sheet(h, RULES)
        self.assertGreaterEqual(len(g), 25)
        self.assertNotIn("clicks", g.columns)

    def test_read_back(self):
        g = rated_gold(history())
        back = G.read_gold(G.template_workbook(g, RULES))
        self.assertEqual(len(back), len(g))
        with self.assertRaises(ValueError):
            G.read_gold(H.sheet_bytes())


class Scoring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = history()

    def test_perfect_agreement(self):
        res = G.score(rated_gold(self.h), RULES, hist=self.h)
        self.assertAlmostEqual(res["engine_vs_human"][0]["qwk"], 1.0)
        self.assertIn("close enough", res["verdict"])

    def test_disagreement_lowers_the_score(self):
        self.assertLess(G.score(rated_gold(self.h, agree=False), RULES, hist=self.h)["engine_vs_human"][0]["qwk"], 0.7)

    def test_rows_that_saw_results_or_low_confidence_are_left_out(self):
        g = rated_gold(self.h)
        g.loc[g.index[:10], "saw_performance"] = "Y"
        g.loc[g.index[10:15], "confidence"] = "Low"
        c = G.score(g, RULES)["counts"]
        self.assertEqual((c["excluded_saw_performance"], c["excluded_low_confidence"], c["headline"]), (10, 5, len(g) - 15))

    def test_flags_are_judged_only_with_enough_positives(self):
        g = rated_gold(self.h)
        self.assertEqual(G.score(g, RULES)["flags"].set_index("check").loc["chance_based", "verdict"], "keep")
        g2 = g.copy()
        g2["chance_based"] = ""
        g2.loc[g2.index[:2], "chance_based"] = "Y"
        self.assertIn("too few", G.score(g2, RULES)["flags"].set_index("check").loc["chance_based", "verdict"])

    def test_human_ceiling_llm_and_outcomes(self):
        g = rated_gold(self.h)
        g["second_risk"] = g["content_risk"]
        self.assertAlmostEqual(G.score(g, RULES)["human_vs_human"]["qwk"], 1.0)
        self.assertAlmostEqual(G.score(g, RULES, llm_risk=dict(zip(g["template_id"], g["content_risk"])))["llm_vs_human"]["qwk"], 1.0)
        res = G.score(rated_gold(self.h), RULES, hist=self.h)
        self.assertGreaterEqual(res["outcomes"]["brands_with_5_or_more"], 1)
        self.assertLess(res["outcomes"]["mean_spearman"], 0.1)

    def test_a_small_set_says_so(self):
        g = rated_gold(self.h).head(12)
        self.assertTrue(any("minimum for a first read" in n for n in G.score(g, RULES)["notes"]))
        self.assertIn("scorecard", G.scorecard_markdown(G.score(g, RULES)).lower())


if __name__ == "__main__":
    unittest.main()
