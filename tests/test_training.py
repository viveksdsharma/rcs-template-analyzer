import io
import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
from rcsapp import training as T  # noqa: E402


def raw(rows):
    df = pd.DataFrame(rows)
    for c in T.RAW_COLS:
        if c not in df.columns:
            df[c] = ""
    return df[T.RAW_COLS]


def r(brand, tpl, date, reads, clicks, title="t", desc="d", cohort="", vertical="E-commerce", exclude=""):
    return {"brand": brand, "vertical": vertical, "cohort": cohort, "template": tpl, "date": date, "reads": reads, "clicks": clicks, "title": title, "description": desc, "button": "Go", "exclude": exclude}


class SplitCard(unittest.TestCase):
    def test_exact_text_wins(self):
        self.assertEqual(T.split_card("x", "x y", "Card Title Big sale! Card Description Buy now"), ("Big sale!", "Buy now"))

    def test_body_that_repeats_the_title_is_cleaned(self):
        self.assertEqual(T.split_card("Big sale", "Big sale Buy now and save", None), ("Big sale", "Buy now and save"))

    def test_leading_description_label_is_dropped(self):
        self.assertEqual(T.split_card("T", "Card Description Body here", None), ("T", "Body here"))

    def test_blank_and_nan(self):
        self.assertEqual(T.split_card(float("nan"), None, float("nan")), ("", ""))


class ImportSheet(unittest.TestCase):
    def test_reads_a_real_shaped_sheet(self):
        rows, issues = T.import_sheet(H.sheet_bytes(), "demo.xlsx", "Demo", "E-commerce")
        self.assertEqual((len(rows), set(rows["brand"])), (6, {"Demo"}))
        first = rows[rows["template"] == "demo_code_100"].iloc[0]
        self.assertEqual((first["reads"], first["clicks"]), (100000, 5200))
        self.assertEqual(first["title"], "Flat \u20B9100 off with code SAVE100")
        self.assertTrue(first["description"].startswith("Use code SAVE100"))
        self.assertNotIn("error", [i["level"] for i in issues])

    def test_sheet_with_two_brands_is_refused(self):
        from rcsapp import metrics as M
        with self.assertRaises(M.MultipleBrands):
            T.import_sheet(H.sheet_bytes(extra_brand=True), "demo.xlsx", "Demo", "E-commerce")

    def test_missing_vertical_is_an_error(self):
        self.assertIn("vertical_missing", [i["code"] for i in T.import_sheet(H.sheet_bytes(), "demo.xlsx", "Demo", "")[1]])

    def test_cohort_rules(self):
        self.assertEqual(T.parse_cohort_rules("offline=offline\nonline = online, vip=VIP"), [("offline", "offline"), ("online", "online"), ("vip", "VIP")])
        self.assertEqual(T.cohort_of("rcs_vs_b1g1_offline", [("offline", "off")]), "off")
        self.assertEqual(T.cohort_of("x", [("offline", "off")]), "")
        self.assertEqual(T.cohort_of("x", [], given="vip"), "vip")


class Validation(unittest.TestCase):
    def test_duplicates_and_clicks_above_reads_are_errors(self):
        codes = [i["code"] for i in T.validate_rows(raw([r("B", "a", "2026-01-01", 5000, 50), r("B", "a", "2026-01-01", 5000, 60), r("B", "b", "2026-01-02", 100, 500)]))]
        self.assertIn("duplicate_rows", codes)
        self.assertIn("clicks_above_reads", codes)

    def test_small_base_warning(self):
        rows = raw([r("B", "a", "2026-01-01", 500, 5)] + [r("B", f"w{i}", f"2026-01-{i + 2:02d}", 50000, 1500 + i * 10, title=f"win{i} alpha{i}", desc=f"d{i} beta{i} gamma{i}") for i in range(5)])
        self.assertIn("small_base", [i["code"] for i in T.validate_rows(rows)])

    def test_winners_only_is_flagged(self):
        rows = raw([r("B", "big", "2026-01-01", 1000000, 10000, "big one", "huge text here")] + [r("B", f"w{i}", f"2026-01-{i + 2:02d}", 20000, 400, f"win{i} alpha{i}", f"d{i} beta{i} gamma{i}") for i in range(5)])
        self.assertIn("winners_only", [i["code"] for i in T.validate_rows(rows)])
        balanced = raw([r("B", f"x{i}", f"2026-01-{i + 1:02d}", 20000, 200 + 150 * (i % 3), f"w{i} a{i}", f"b{i} c{i}") for i in range(6)])
        self.assertNotIn("winners_only", [i["code"] for i in T.validate_rows(balanced)])

    def test_same_text_spread_is_reported(self):
        rows = raw([r("B", "a", "2026-01-01", 50000, 500, "Hello", "World offer"), r("B", "a", "2026-01-08", 50000, 1500, "Hello", "World offer"),
                    r("B", "c", "2026-01-09", 50000, 900, "Other words", "entirely different text"), r("B", "d", "2026-01-10", 50000, 950, "More words", "yet another thing")])
        self.assertIn("same_text_spread", [i["code"] for i in T.validate_rows(rows)])


class Derived(unittest.TestCase):
    def test_leave_one_out_baseline_is_hand_checkable(self):
        h = T.finalize(raw([r("B", "a", "2026-01-01", 10000, 100, "aa bb", "cc dd"), r("B", "b", "2026-01-02", 20000, 600, "ee ff", "gg hh"), r("B", "c", "2026-01-03", 10000, 200, "ii jj", "kk ll")])).set_index("template")
        self.assertAlmostEqual(h.loc["a", "loo_rate"], 100 * 800 / 30000)
        self.assertAlmostEqual(h.loc["a", "click_index"], (100 / 10000) / (800 / 30000))
        self.assertAlmostEqual(h.loc["b", "loo_rate"], 100 * 300 / 20000)

    def test_a_template_with_most_of_the_reads_is_not_its_own_baseline(self):
        h = T.finalize(raw([r("B", "big", "2026-01-01", 1000000, 10000, "big one", "huge text here"), r("B", "s1", "2026-01-02", 20000, 400, "small a", "tiny words"), r("B", "s2", "2026-01-03", 20000, 100, "small b", "other bits")])).set_index("template")
        self.assertAlmostEqual(h.loc["big", "loo_rate"], 100 * 500 / 40000)
        self.assertAlmostEqual(h.loc["big", "click_index"], 1.0 / 1.25)

    def test_cohorts_do_not_mix_in_the_baseline(self):
        rows = raw([r("B", f"on{i}", f"2026-01-0{i + 1}", 20000, 1000, f"on{i} aa{i}", f"bb{i} cc{i}", cohort="online") for i in range(3)] +
                   [r("B", f"off{i}", f"2026-02-0{i + 1}", 20000, 100, f"off{i} dd{i}", f"ee{i} ff{i}", cohort="offline") for i in range(3)])
        self.assertTrue(np.allclose(T.finalize(rows)["click_index"], 1.0))

    def test_text_groups_merge_same_text_with_different_codes_and_repeat_sends(self):
        h = T.finalize(raw([r("B", "a", "2026-01-01", 20000, 300, "[v1], First in line!", "Unlock Up To 150 Off using code: MISSEDYOU"),
                            r("B", "b", "2026-01-02", 20000, 200, "[v1], First in line!", "Unlock Up To 150 Off using code: HELLOAGAIN"),
                            r("B", "a", "2026-01-09", 20000, 250, "[v1], First in line!", "Unlock Up To 150 Off using code: MISSEDYOU"),
                            r("B", "z", "2026-01-03", 20000, 400, "Completely different", "words about something else entirely")]))
        self.assertEqual(h["text_group"].nunique(), 2)
        self.assertEqual(h[h["template"] == "a"]["send_number"].tolist(), [1, 2])

    def test_unusable_reasons_including_the_exclude_column(self):
        h = T.finalize(raw([r("B", "ok1", "2026-01-01", 20000, 300, "a b", "c d"), r("B", "ok2", "2026-01-02", 20000, 200, "e f", "g h"), r("B", "ok3", "2026-01-03", 20000, 250, "i j", "k l"),
                            r("B", "tiny", "2026-01-04", 500, 5, "m n", "o p"), r("B", "notext", "2026-01-05", 20000, 100, "", ""),
                            r("B", "skip", "2026-01-06", 20000, 100, "u v", "w x", exclude="Y"), r("C", "lonely", "2026-01-06", 20000, 100, "q r", "s t")])).set_index("template")
        self.assertEqual([h.loc[k, "reason"] for k in ("tiny", "notext", "skip", "lonely")], ["reads < 1000", "no text", "excluded", "group too small"])
        self.assertTrue(h.loc["ok1", "usable"])

    def test_weight_follows_clicks(self):
        h = T.finalize(raw([r("B", "a", "2026-01-01", 20000, 10, "a b", "c d"), r("B", "b", "2026-01-02", 20000, 250, "e f", "g h"), r("B", "c", "2026-01-03", 20000, 9000, "i j", "k l")])).set_index("template")
        self.assertEqual([h.loc[k, "weight"] for k in "abc"], [0.04, 0.5, 1.0])

    def test_merge_replaces_the_same_row_and_adds_new_ones(self):
        base = T.finalize(raw([r("B", "a", "2026-01-01", 20000, 100, "a b", "c d"), r("B", "b", "2026-01-02", 20000, 200, "e f", "g h"), r("B", "c", "2026-01-03", 20000, 300, "i j", "k l")]))
        m = T.merge(base, raw([r("B", "a", "2026-01-01", 20000, 150, "a b", "c d"), r("D", "x", "2026-01-01", 20000, 150, "m n", "o p")]))
        self.assertEqual(len(m), 4)
        self.assertEqual(m[(m["brand"] == "B") & (m["template"] == "a")]["clicks"].iloc[0], 150)


class BrandNames(unittest.TestCase):
    def test_fuzzy_matching(self):
        self.assertTrue(T.brand_matches("Axis Bank", "Axis"))
        self.assertTrue(T.brand_matches("bigbasket", ["INNOVATIVE RETAIL CONCEPTS PRIVATE LIMITED", "bigbasket"]))
        self.assertTrue(T.brand_matches("Victoria's Secret", "Apparel Group Victoria\u2019s Secret India"))
        self.assertFalse(T.brand_matches("Myntra", ["Axis", "Axis Bank Limited"]))


class Readiness(unittest.TestCase):
    def test_few_rows_message_and_counts(self):
        h = T.finalize(T.import_sheet(H.sheet_bytes(), "demo.xlsx", "Demo", "E-commerce")[0])
        rd = T.readiness(h)
        self.assertFalse(rd["gate_ok"])
        self.assertEqual((rd["brands"], rd["groups"]), (1, 4))
        self.assertIn("more brands", rd["message"])
        self.assertFalse(T.readiness(None)["gate_ok"])

    def test_old_format_history_is_refused_with_a_reason(self):
        old = pd.DataFrame({"brand": ["B"], "template": ["a"], "date": ["2026-01-01"], "reads": [1], "clicks": [1], "has_number": [1]})
        with self.assertRaises(ValueError) as cm:
            T.read_history(old.to_csv(index=False).encode())
        self.assertIn("older version", str(cm.exception))

    def test_roundtrip_through_csv(self):
        h = T.finalize(H.synthetic_history_raw(n_brands=2, per=6))
        back = T.read_history(T.to_csv(h))
        self.assertEqual(len(back), len(h))
        self.assertTrue(np.allclose(back["click_index"].fillna(-1), h["click_index"].fillna(-1)))


class Model(unittest.TestCase):
    def test_not_used_with_little_history(self):
        info = T.fit_model(T.finalize(H.synthetic_history_raw(n_brands=2, per=5)))
        self.assertEqual(info["status"], "insufficient")
        self.assertNotIn("model", info)

    def test_noise_is_declined(self):
        self.assertEqual(T.fit_model(T.finalize(H.synthetic_history_raw(signal=False)))["status"], "not_predictive")

    def test_real_signal_is_used_and_validated_on_unseen_brands(self):
        info = T.fit_model(T.finalize(H.synthetic_history_raw(signal=True)))
        self.assertEqual(info["status"], "ok")
        self.assertGreater(info["r2"], 0.3)
        self.assertIn(info["drivers"][0][0], ("has_number", "chance", "has_code"))

    def test_the_brand_being_analysed_is_excluded_even_under_another_spelling(self):
        raw_ = H.synthetic_history_raw()
        raw_["brand"] = raw_["brand"].replace({"Brand0": "Brand0 Limited"})
        self.assertEqual(T.fit_model(T.finalize(raw_), exclude_brand=["Brand0", "Brand1"])["brands"], 4)

    def test_distinct_texts_not_rows_decide_the_gate(self):
        raw_ = H.synthetic_history_raw(n_brands=6, per=14)
        raw_["title"], raw_["description"] = "Same words", "every single row"
        self.assertEqual(T.fit_model(T.finalize(raw_))["status"], "insufficient")


class BundledSeed(unittest.TestCase):
    """The real brand workbooks shipped in data/seed."""

    @classmethod
    def setUpClass(cls):
        cls.h, cls.report = T.load_seed()

    def test_all_five_brands_load_without_errors(self):
        self.assertEqual(len(self.report), 5, self.report)
        self.assertTrue(all("rows loaded" in x for x in self.report), self.report)
        self.assertEqual(sorted(T.brand_names(self.h)), ["Axis Bank", "Myntra", "Nykaa", "Victoria's Secret", "bigbasket"])
        self.assertEqual(len(self.h), 36)

    def test_the_truncated_nykaa_template_is_excluded_with_a_reason(self):
        row = self.h[self.h["template"] == "RCSNYKAACOSMETICS_22ndAug"].iloc[0]
        self.assertEqual(row["reason"], "excluded")
        self.assertIn("truncated", row["exclude_reason"])

    def test_victorias_secret_cohorts_come_from_the_confirmed_meaning_of_online_and_offline(self):
        vs = self.h[self.h["brand"] == "Victoria's Secret"].set_index("template")
        self.assertEqual({t: vs.loc[t, "cohort"] for t in vs.index}, {"rcs_vs_bridal_offline": "offline", "rcs_vs_b1g1_offline": "offline", "rcs_vs_b1g1_off_pink": "offline",
                                                                      "rcs_vs_bridal_online": "online", "rcs_vs_b1g1_online": "online", "rcs_vs_online_pink": "online"})

    def test_text_columns_were_read_for_every_brand(self):
        usable = self.h[self.h["reason"] != "excluded"]
        self.assertTrue((usable["title"].str.len() + usable["description"].str.len() > 20).all())
        myntra = self.h[self.h["template"] == "p4_aff2_helloagain_rcs"].iloc[0]
        self.assertTrue(myntra["description"].startswith("Unlock Up To"))          # the stray 'Card Description' label is gone

    def test_the_trained_component_stays_off_until_the_history_is_big_enough(self):
        rd = T.readiness(self.h)
        self.assertFalse(rd["gate_ok"])
        self.assertEqual(rd["brands"], 5)
        self.assertEqual(T.fit_model(self.h)["status"], "insufficient")
        self.assertEqual(T.fit_model(self.h, exclude_brand="Axis")["brands"], 4)

    def test_repeated_axis_sends_are_four_texts(self):
        ax = self.h[self.h["brand"] == "Axis Bank"]
        self.assertEqual((len(ax), ax["text_group"].nunique()), (9, 4))


if __name__ == "__main__":
    unittest.main()
