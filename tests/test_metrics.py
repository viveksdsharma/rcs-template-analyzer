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

    def test_stray_rows_reported(self):
        self.assertIn("stray_rows_ignored", [i["code"] for i in self.m["issues"]])
        self.assertEqual(self.m["meta"]["n_templates"], 6)

    def test_year_groups_and_comparison(self):
        y = self.m["groups"]["year"]
        self.assertEqual(y["2026"]["n_templates"], 4)
        self.assertEqual(y["2025"]["n_templates"], 2)
        yoy = self.m["comparisons"]["year_over_year"]
        self.assertEqual((yoy["from_year"], yoy["to_year"]), (2025, 2026))
        self.assertAlmostEqual(yoy["metrics"]["P2AResponseRate"]["to"], y["2026"]["rates"]["P2AResponseRate"])

    def test_single_send_date_caveat(self):
        self.assertIn("single_send_date", [i["code"] for i in self.m["issues"]])

    def test_second_brand_stops_unless_chosen(self):
        data = H.sheet_bytes(extra_brand=True)
        with self.assertRaises(M.MultipleBrands):
            M.run(io.BytesIO(data), filename="x.xlsx")
        self.assertEqual(M.run(io.BytesIO(data), brand="DEMO", filename="x.xlsx")["meta"]["n_templates"], 6)

    def test_template_filter_and_unknown_template(self):
        r = M.run(io.BytesIO(H.sheet_bytes()), brand="DEMO", templates=["demo_code_100", "demo_generic"], filename="x.xlsx")
        self.assertEqual(r["meta"]["n_templates"], 2)
        with self.assertRaises(M.PipelineError):
            M.run(io.BytesIO(H.sheet_bytes()), brand="DEMO", templates=["nope"], filename="x.xlsx")

    def test_missing_column_named(self):
        raw = pd.read_excel(io.BytesIO(H.sheet_bytes()), dtype=object).drop(columns=["Messages Expired"])
        bio = io.BytesIO()
        raw.to_excel(bio, index=False)
        with self.assertRaises(M.MissingColumns) as cm:
            M.run(io.BytesIO(bio.getvalue()), brand="DEMO", filename="x.xlsx")
        self.assertIn("expired", str(cm.exception))

    def test_broken_funnel_blocks(self):
        raw = pd.read_excel(io.BytesIO(H.sheet_bytes()), dtype=object)
        raw.loc[raw["Template"] == "demo_raffle", "Messages Read"] = 99999999
        bio = io.BytesIO()
        raw.to_excel(bio, index=False)
        self.assertFalse(M.run(io.BytesIO(bio.getvalue()), brand="DEMO", filename="x.xlsx")["can_proceed"])

    def test_altered_count_flagged(self):
        raw = pd.read_excel(io.BytesIO(H.sheet_bytes()), dtype=object)
        raw.loc[raw["Template"] == "demo_raffle", "P2AResponses"] = 9999
        bio = io.BytesIO()
        raw.to_excel(bio, index=False)
        r = M.run(io.BytesIO(bio.getvalue()), brand="DEMO", filename="x.xlsx")
        self.assertIn("sheet_rate_mismatch", [i["code"] for i in r["issues"]])

    def test_csv_accepted(self):
        raw = pd.read_excel(io.BytesIO(H.sheet_bytes()), dtype=object)
        r = M.run(io.BytesIO(raw.to_csv(index=False).encode()), brand="DEMO", filename="x.csv")
        self.assertEqual(r["meta"]["n_templates"], 6)


class Features(unittest.TestCase):
    def f(self, title, desc, button="Shop Now (URL)"):
        return F.extract({"title": title, "description": desc, "button": button})

    def test_quantified_and_code(self):
        f = self.f("Flat \u20B9100 off with code SAVE100", "Use code SAVE100.")
        self.assertEqual((f["quantified"], f["has_code"], f["has_currency"]), (1, 1, 1))

    def test_variables_and_icons_are_not_content(self):
        f = self.f("[gift icon] FREE GIFTS for [v1]", "Valid till [v2] ONLY.")
        self.assertEqual(f["n_vars"], 2)
        self.assertEqual(f["quantified"], 0)
        self.assertEqual(f["names_benefit"], 1)

    def test_chance_floor_competing(self):
        self.assertEqual(self.f("Win big", "Stand a chance to win")["chance"], 1)
        self.assertEqual(self.f("MIN 35% OFF", "up to 50%")["floor_or_ceiling"], 1)
        self.assertEqual(self.f("Loans", 'Please type "Hi" to know more')["competing_actions"], 1)

    def test_deadline_without_benefit(self):
        f = self.f("Hurry", "Ends tonight, valid till Friday")
        self.assertEqual((f["has_deadline"], f["names_benefit"], f["quantified"]), (1, 0, 0))


class Inputs(unittest.TestCase):
    def test_partner_content_sheet(self):
        df = pd.DataFrame({"Template Name": ["a", "b"], "Card Title": ["T1", "T2"], "Card Description": ["D1", "D2"], "CTA Phrase": ["Go", "Buy"], "CTA Type": ["URL", "URL"]})
        bio = io.BytesIO()
        df.to_excel(bio, index=False)
        out = I.parse_template_content(bio.getvalue(), "c.xlsx")
        self.assertEqual(out[0], {"template": "a", "title": "T1", "description": "D1", "button": "Go (URL)"})

    def test_content_sheet_missing_columns(self):
        bio = io.BytesIO()
        pd.DataFrame({"x": [1]}).to_excel(bio, index=False)
        with self.assertRaises(ValueError):
            I.parse_template_content(bio.getvalue(), "c.xlsx")

    def test_images_zip_and_matching(self):
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("Images/demo_code_100.png", H.png_bytes())
            z.writestr("__MACOSX/junk.png", b"x")
        imgs = I.read_images([("Images.zip", buf.getvalue()), ("demo_generic.JPG", H.png_bytes())])
        self.assertEqual(sorted(imgs), ["demo_code_100.png", "demo_generic.JPG"])
        self.assertEqual(I.match_images(["demo_code_100", "demo_generic", "other"], imgs), {"demo_code_100": "demo_code_100.png", "demo_generic": "demo_generic.JPG"})


if __name__ == "__main__":
    unittest.main()
