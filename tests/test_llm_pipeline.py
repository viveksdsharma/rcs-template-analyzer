import io
import json
import os
import sys
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("SOFFICE_CMD", "python /mnt/skills/public/docx/scripts/office/soffice.py")
import helpers as H  # noqa: E402
from mock_llm import MockLLM  # noqa: E402
from rcsapp import pipeline as P  # noqa: E402
from rcsapp.engines import llm_engine as L  # noqa: E402


def make_input(**kw):
    base = dict(sheet_bytes=H.sheet_bytes(), sheet_name="demo.xlsx", brand="DEMO BRAND", brand_display="Demo Brand", templates=[dict(t) for t in H.TEMPLATES],
                period="August 2026 vs August 2025", period_labels={"2026": "August 2026", "2025": "August 2025"}, issue="The brand sees lower click rates on some templates.",
                images=H.images(), engine="rules", vertical="E-commerce", outputs=("docx", "pptx"))
    base.update(kw)
    return P.JobInput(**base)


class Setup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules_res = P.run_job(make_input(), log=lambda *a: None)
        assert cls.rules_res.status == "built", cls.rules_res.issues

    def cfg(self, url, vision=False):
        return L.LLMConfig(base_url=url, api_key="k", model="mock-model", vision=vision, retries=0, timeout=20)


class RulesPipeline(Setup):
    def test_builds_word_and_plain_deck_without_template(self):
        self.assertEqual(sorted(self.rules_res.files), ["Demo_Brand_RCS_Template_Analysis.docx", "Demo_Brand_RCS_Template_Deck.pptx"])
        self.assertIn("plain_deck", [i["code"] for i in self.rules_res.issues])

    def test_pdf_is_made(self):
        r = P.run_job(make_input(outputs=("pdf",)), log=lambda *a: None)
        self.assertTrue(r.files and next(iter(r.files)).endswith(".pdf"))
        self.assertTrue(next(iter(r.files.values())).startswith(b"%PDF"))

    def test_no_internal_marking_or_other_brand_in_files(self):
        for name, data in self.rules_res.files.items():
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                blob = " ".join(z.read(n).decode("utf8", "ignore") for n in z.namelist() if n.endswith(".xml"))
            for bad in ("Vodafone Idea Internal", "C2 \u2013", "Classification"):
                self.assertNotIn(bad, blob, f"{bad} in {name}")

    def test_numbers_in_word_come_from_the_sheet(self):
        from docx import Document
        d = Document(io.BytesIO(self.rules_res.files["Demo_Brand_RCS_Template_Analysis.docx"]))
        text = "\n".join(p.text for p in d.paragraphs) + "\n".join(c.text for t in d.tables for r in t.rows for c in r.cells)
        self.assertIn("1,000,000", text)           # demo_code_100 Messages Submitted
        self.assertIn("5.20%", text)               # its click rate, 5200 / 100000

    def test_preflight_gives_plain_words(self):
        r = P.run_job(make_input(issue="", templates=[{"template": "demo_code_100", "title": "", "description": ""}]), log=lambda *a: None)
        self.assertEqual(r.status, "stopped_inputs")
        msg = " ".join(i["message"] for i in r.issues)
        self.assertIn("no card title or description", msg)
        self.assertIn("issue", msg)

    def test_second_brand_must_be_chosen(self):
        r = P.run_job(make_input(sheet_bytes=H.sheet_bytes(extra_brand=True), brand=""), log=lambda *a: None)
        self.assertEqual(r.status, "stopped_inputs")

    def test_editing_runs_the_checks_again(self):
        bad = json.loads(json.dumps(self.rules_res.analysis))
        bad["summary"]["lead"] += " This is 99.99% certain."
        r = P.run_job(make_input(), log=lambda *a: None, analysis_override=bad)
        self.assertEqual(r.status, "blocked_by_checks")
        self.assertEqual(r.files, {})
        good = json.loads(json.dumps(self.rules_res.analysis))
        good["summary"]["lead"] += " A reviewer added this sentence."
        self.assertEqual(P.run_job(make_input(), log=lambda *a: None, analysis_override=good).status, "built")

    def test_history_is_returned_for_the_user_to_keep(self):
        import pandas as pd
        h = pd.read_csv(io.BytesIO(self.rules_res.history_csv))
        self.assertEqual(len(h), 6)
        self.assertIn("click_index", h.columns)
        r2 = P.run_job(make_input(history_bytes=self.rules_res.history_csv), log=lambda *a: None)
        self.assertEqual(len(pd.read_csv(io.BytesIO(r2.history_csv))), 6)      # same rows are not duplicated


class LLMEngine(Setup):
    def run_llm(self, mode="good", engine="llm", consent=True, vision=False):
        with MockLLM(self.rules_res.analysis, mode) as srv:
            r = P.run_job(make_input(engine=engine, llm_cfg=self.cfg(srv.url, vision), llm_consent=consent, extra_blocked_brands=["Axis"]), log=lambda *a: None)
            return r, srv

    def test_two_step_flow_and_files(self):
        r, srv = self.run_llm()
        self.assertEqual(r.status, "built", [i["message"] for i in r.issues if i["level"] == "error"])
        self.assertEqual(len(srv.calls), 5)                       # 4 current templates + 1 global
        self.assertTrue(all("STEP 1" in c["first"] for c in srv.calls[:4]))
        self.assertIn("STEP 2", srv.calls[4]["first"])

    def test_step1_prompt_has_no_performance_numbers(self):
        from rcsapp import brief as B
        from rcsapp.knowledge import load_knowledge
        req = {"brand": "DEMO BRAND", "brand_display": "Demo", "period": "", "issue": "x", "vertical": ""}
        t = {"template": "demo_code_100", "creative": "banner", "card": {"title": "Flat off", "description": "d", "button": "b"}}
        p = B.step1_prompt(req, t, load_knowledge())
        for banned in ("PERFORMANCE", "| Template |", "5.20%", "1,000,000", "Totals by year"):
            self.assertNotIn(banned, p)
        self.assertIn("Flat off", p)          # the card text is there

    def test_wrong_number_is_sent_back_and_corrected(self):
        r, srv = self.run_llm("bad_number_once")
        self.assertEqual(r.status, "built")
        self.assertEqual([c["repair"] for c in srv.calls[4:]], [False, True])

    def test_fenced_answer_is_parsed(self):
        r, _ = self.run_llm("fenced")
        self.assertEqual(r.status, "built")

    def test_other_brand_in_answer_blocks_the_run(self):
        r, _ = self.run_llm("other_brand")
        self.assertNotEqual(r.status, "built")
        self.assertEqual(r.files, {})
        self.assertIn("Axis", " ".join(i["message"] for i in r.issues))

    def test_endpoint_down_is_a_plain_error_not_a_crash(self):
        r, _ = self.run_llm("down")
        self.assertEqual(r.status, "blocked_by_checks" if r.issues else "no_analysis")
        self.assertIn("model endpoint", " ".join(i["message"] for i in r.issues))

    def test_consent_is_required(self):
        r, srv = self.run_llm(consent=False)
        self.assertEqual(r.status, "stopped_inputs")
        self.assertEqual(srv.calls, [])

    def test_both_mode_flags_disagreement_and_uses_the_cautious_rating(self):
        r, _ = self.run_llm("flip", engine="both")
        self.assertEqual(r.status, "built")
        row = next(x for x in r.agreement if x["template"] == "demo_code_100")
        self.assertFalse(row["agree"])
        self.assertIn("demo_code_100", " ".join(r.notes))
        self.assertEqual(next(t for t in r.analysis["templates"] if t["template"] == "demo_code_100")["content_risk"], "High")
        self.assertIn("differ", r.review_notes)

    def test_both_mode_falls_back_to_rules_if_the_model_is_down(self):
        r, _ = self.run_llm("down", engine="both")
        self.assertEqual(r.status, "built")
        self.assertIn("fallback", [i["code"] for i in r.issues])

    def test_images_are_sent_only_when_vision_is_on(self):
        _, off = self.run_llm(vision=False)
        _, on = self.run_llm(vision=True)
        self.assertFalse(any(c["has_image"] for c in off.calls))
        self.assertTrue(any(c["has_image"] for c in on.calls[:4]))

    def test_manual_route(self):
        j = json.dumps({**{k: v for k, v in self.rules_res.analysis.items() if k not in ("meta",)}})
        r = P.run_job(make_input(engine="manual", manual_json=j), log=lambda *a: None)
        self.assertEqual(r.status, "built")
        r = P.run_job(make_input(engine="manual", manual_json="not json"), log=lambda *a: None)
        self.assertNotEqual(r.status, "built")

    def test_extract_json_variants(self):
        self.assertEqual(L.extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(L.extract_json('Here you go: {"a": {"b": 2}} thanks'), {"a": {"b": 2}})
        with self.assertRaises(ValueError):
            L.extract_json("no json here")

    def test_config_from_env_and_secrets(self):
        c = L.config_from({"base_url": "https://x.example/v1", "model": "m", "vision": "true"}, env={})
        self.assertTrue(c.ready() and c.vision)
        self.assertEqual(c.host, "x.example")
        self.assertFalse(L.config_from({}, env={}).ready())


if __name__ == "__main__":
    unittest.main()
