import io
import os
import sys
import unittest

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import helpers as H  # noqa: E402
from rcsapp import features as F  # noqa: E402
from rcsapp import inputs as I  # noqa: E402
from rcsapp import metrics as M  # noqa: E402


class Metrics(unittest.TestCase):
    def setUp(self):
        self.m = M.run(io.BytesIO(H.sheet_bytes()), brand="DEMO", filename="x.xlsx")

    def test_rates_equal_counts(self):
        t = next(x for x in self.m["templates"] if x["template"] == "demo_code_100")
        self.assertAlmostEqual(t["rates"]["DeliveryRate"], 540000 / 600000 * 100)
        self.assertAlmostEqual(t["rates"]["ReadRate"], 100000 / 540000 * 100)
        self.assertAlmostEqual(t["rates"]["P2AResponseRate"], 5200 / 100000 * 100)

    def test_both_header_styles_are_read(self):
        plain = M.run(io.BytesIO(H.sheet_bytes(plain_headers=True)), brand="DEMO", filename="x.xlsx")
        self.assertEqual(plain["groups"]["year"]["2026"]["counts"], self.m["groups"]["year"]["2026"]["counts"])

    def test_fused_page_footer_is_stripped_and_stray_rows_reported(self):
        self.assertIn("stray_rows_ignored", [i["code"] for i in self.m["issues"]])
        raw = M.read_table(io.BytesIO(H.sheet_bytes()), filename="x.xlsx")
        df, _ = M.canonicalise(raw)
        self.assertNotIn("Vodafone", " ".join(df.astype(str).values.ravel()))
        self.assertEqual(M.clean_cell("abc&8C1 \u2013 Vodafone Idea External"), "abc")
        self.assertIsNone(M.clean_cell("&8C2 \u2013 Vodafone Idea Internal"))

    def test_year_groups_and_comparison(self):
        y = self.m["groups"]["year"]
        self.assertEqual((y["2026"]["n_templates"], y["2025"]["n_templates"]), (4, 2))
        self.assertAlmostEqual(self.m["comparisons"]["year_over_year"]["metrics"]["P2AResponseRate"]["to"], y["2026"]["rates"]["P2AResponseRate"])

    def test_single_send_date_caveat(self):
        self.assertIn("single_send_date", [i["code"] for i in self.m["issues"]])
        rep = M.run(io.BytesIO(H.sheet_bytes(repeat_send=True)), brand="DEMO", filename="x.xlsx")
        self.assertNotIn("single_send_date", [i["code"] for i in rep["issues"]])

    def test_repeated_sends_pool_per_template(self):
        rep = M.run(io.BytesIO(H.sheet_bytes(repeat_send=True)), brand="DEMO", filename="x.xlsx")
        rows = M.distinct(rep, 2026)["demo_code_100"]
        st = M.card_stats(rows)
        self.assertEqual(st["sends"], 2)
        self.assertAlmostEqual(st["cr"], 100 * (5200 + 3600) / (100000 + 90000))
        self.assertEqual(M.card_stats(M.distinct(rep, 2026)["demo_generic"])["sends"], 1)

    def test_google_id_short_form(self):
        self.assertEqual([M.short_agent_id(x) for x in ("axis_bank_yvyv0sd7_agent", "victoria_s_secret_india_xo8lhgva_agent", "nykaa_1tvds9nq_agent")],
                         ["yvyv0sd7", "xo8lhgva", "1tvds9nq"])

    def test_second_brand_stops_unless_chosen(self):
        data = H.sheet_bytes(extra_brand=True)
        with self.assertRaises(M.MultipleBrands):
            M.run(io.BytesIO(data), filename="x.xlsx")
        self.assertEqual(M.run(io.BytesIO(data), brand="DEMO", filename="x.xlsx")["meta"]["n_templates"], 6)

    def test_template_filter_and_unknown_template(self):
        self.assertEqual(M.run(io.BytesIO(H.sheet_bytes()), brand="DEMO", templates=["demo_code_100", "demo_generic"], filename="x.xlsx")["meta"]["n_templates"], 2)
        with self.assertRaises(M.PipelineError):
            M.run(io.BytesIO(H.sheet_bytes()), brand="DEMO", templates=["nope"], filename="x.xlsx")

    def test_missing_column_named(self):
        raw = pd.read_excel(io.BytesIO(H.sheet_bytes()), dtype=object).drop(columns=["Messages Expired"])
        bio = io.BytesIO()
        raw.to_excel(bio, index=False)
        with self.assertRaises(M.MissingColumns) as cm:
            M.run(io.BytesIO(bio.getvalue()), brand="DEMO", filename="x.xlsx")
        self.assertIn("expired", str(cm.exception))

    def _mutate(self, template, column, value):
        raw = pd.read_excel(io.BytesIO(H.sheet_bytes()), dtype=object)
        raw.loc[raw["Template"] == template, column] = value
        bio = io.BytesIO()
        raw.to_excel(bio, index=False)
        return M.run(io.BytesIO(bio.getvalue()), brand="DEMO", filename="x.xlsx")

    def test_broken_funnel_and_clicks_above_reads_block(self):
        self.assertFalse(self._mutate("demo_raffle", "Messages Read", 99999999)["can_proceed"])
        r = self._mutate("demo_raffle", "P2AResponses=clicks", 99999999)
        self.assertIn("clicks_above_reads", [i["code"] for i in r["issues"]])
        self.assertFalse(r["can_proceed"])

    def test_altered_count_flagged(self):
        self.assertIn("sheet_rate_mismatch", [i["code"] for i in self._mutate("demo_raffle", "P2AResponses=clicks", 9999)["issues"]])

    def test_csv_accepted(self):
        raw = pd.read_excel(io.BytesIO(H.sheet_bytes()), dtype=object)
        self.assertEqual(M.run(io.BytesIO(raw.to_csv(index=False).encode()), brand="DEMO", filename="x.csv")["meta"]["n_templates"], 6)


class Features(unittest.TestCase):
    def f(self, title, desc, button="Shop Now (URL)"):
        return F.extract({"title": title, "description": desc, "button": button})

    def test_quantified_and_code(self):
        f = self.f("Flat \u20B9100 off with code SAVE100", "Use code SAVE100.")
        self.assertEqual((f["quantified"], f["has_code"], f["has_currency"]), (1, 1, 1))

    def test_variables_and_icons_are_not_content(self):
        f = self.f("[gift icon] FREE GIFTS for [v1]", "Valid till [v2] ONLY.")
        self.assertEqual((f["n_vars"], f["quantified"], f["names_benefit"]), (2, 0, 1))

    def test_numeric_placeholders_are_variables_not_numbers(self):
        f = self.f("Hi [0]!", "Your offer, [1], ends [0_sh_code]")
        self.assertEqual((f["quantified"], f["n_vars"], f["has_number"]), (0, 3, 0))

    def test_styled_unicode_letters_read_as_plain_words(self):
        styled = "Grab \U0001D408\U0001D40D\U0001D411 \U0001D7CF\U0001D7CE\U0001D7CE \U0001D41C\U0001D41A\U0001D42C\U0001D421\U0001D41B\U0001D41A\U0001D41C\U0001D424 on UPI payments!"
        f = self.f(styled, "")
        self.assertEqual((f["has_currency"], f["has_number"], f["names_benefit"]), (1, 1, 1))

    def test_chance_floor_competing_deadline(self):
        self.assertEqual(self.f("Win big", "Stand a chance to win")["chance"], 1)
        self.assertEqual(self.f("MIN 35% OFF", "up to 50%")["floor_or_ceiling"], 1)
        self.assertEqual(self.f("Loans", 'Please type "Hi" to know more')["competing_actions"], 1)
        f = self.f("Hurry", "Ends tonight, valid till Friday")
        self.assertEqual((f["has_deadline"], f["names_benefit"], f["quantified"]), (1, 0, 0))

    def test_token_set_ignores_codes_and_variables(self):
        a = F.token_set("[v1], First in line!", "Unlock Up To \u20B9150 Off using code: MISSEDYOU \U0001F381")
        b = F.token_set("[v1], First in line!", "Unlock Up To \u20B9150 Off using code: HELLOAGAIN")
        self.assertEqual(a, b)

    def test_iconify_turns_emoji_into_icon_placeholders(self):
        out = F.iconify("Rakhi celebrations start here! \U0001F389\U0001F389 \U0001F1EE\U0001F1F3 \u2705 Flat 25% \U0001F4B0 \u23F3 ok")
        self.assertEqual(out, "Rakhi celebrations start here! [icons] Flat 25% [icons] ok")
        self.assertEqual(F.iconify("Plain text"), "Plain text")
        self.assertIn("[icon]", F.iconify("Hi \U0001F44B"))
        self.assertNotIn("\U0001F44B", F.iconify("Hi \U0001F44B"))


class Inputs(unittest.TestCase):
    def test_partner_content_sheet(self):
        df = pd.DataFrame({"Template Name": ["a", "b"], "Card Title": ["T1", "T2"], "Card Description": ["D1", "D2"], "CTA Phrase": ["Go", "Buy"], "CTA Type": ["URL", "URL"]})
        bio = io.BytesIO()
        df.to_excel(bio, index=False)
        self.assertEqual(I.parse_template_content(bio.getvalue(), "c.xlsx")[0], {"template": "a", "title": "T1", "description": "D1", "button": "Go (URL)"})

    def test_content_sheet_missing_columns(self):
        bio = io.BytesIO()
        pd.DataFrame({"x": [1]}).to_excel(bio, index=False)
        with self.assertRaises(ValueError):
            I.parse_template_content(bio.getvalue(), "c.xlsx")

    def test_images_zip_matching_and_unreadable_image(self):
        import tempfile
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("Images/demo_code_100.png", H.png_bytes())
            z.writestr("__MACOSX/junk.png", b"x")
        imgs = I.read_images([("Images.zip", buf.getvalue()), ("demo_generic.JPG", H.png_bytes())])
        self.assertEqual(sorted(imgs), ["demo_code_100.png", "demo_generic.JPG"])
        self.assertEqual(I.match_images(["demo_code_100", "demo_generic", "other"], imgs), {"demo_code_100": "demo_code_100.png", "demo_generic": "demo_generic.JPG"})
        with self.assertRaises(Exception):
            I.save_image(b"not an image", tempfile.mkdtemp(), "x")


if __name__ == "__main__":
    unittest.main()
