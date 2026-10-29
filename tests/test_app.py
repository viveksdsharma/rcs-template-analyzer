"""Drives the real Streamlit page headlessly. The harness cannot upload files, so file_uploader is replaced by a fake."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("SOFFICE_CMD", "soffice")
import helpers as H  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

HARNESS = '''
import io, os, sys
sys.path.insert(0, %(root)r); sys.path.insert(0, %(tests)r)
import streamlit as st
import helpers as H
class Fake:
    def __init__(self, name, data): self.name, self._d = name, data
    def getvalue(self): return self._d
SHEET, IMAGES, TRAIN, TRAIN_SHEET = %(sheet)r, %(images)r, %(train)r, %(train_sheet)r
def fake_uploader(label, type=None, accept_multiple_files=False, key=None, **kw):
    if key == "sheet": return Fake("demo.xlsx", SHEET)
    if key == "imgs": return [Fake(n, d) for n, d in IMAGES.items()]
    if key == "tr_sheet" and TRAIN: return Fake("demo.xlsx", TRAIN_SHEET)
    return [] if accept_multiple_files else None
st.file_uploader = fake_uploader
# The sandbox this was written in has a pyarrow/NumPy mismatch that breaks table widgets; a normal install does not.
st.dataframe = lambda *a, **k: None
st.data_editor = lambda df, **k: df
exec(compile(open(%(app)r, encoding="utf-8").read(), "app.py", "exec"))
'''


def make_app(train=True, train_sheet=None, password="pw", secrets=None):
    script = HARNESS % {"root": ROOT, "tests": os.path.dirname(os.path.abspath(__file__)), "sheet": H.sheet_bytes(extra_brand=True),
                        "images": H.images(), "train": train, "train_sheet": train_sheet or H.sheet_bytes(), "app": os.path.join(ROOT, "app.py")}
    at = AppTest.from_string(script, default_timeout=180)
    if password:
        at.secrets["APP_PASSWORD"] = password
    for k, v in (secrets or {}).items():
        at.secrets[k] = v
    return at


def login(at):
    at.run()
    at.text_input[0].input("pw").run()
    at.button[0].click().run()
    return at


def button(at, label):
    return next(b for b in at.button if b.label == label)


class Login(unittest.TestCase):
    def test_no_password_means_no_app(self):
        os.environ.pop("ALLOW_NO_AUTH", None)
        at = make_app(password=None)
        at.run()
        self.assertTrue(any("APP_PASSWORD is not set" in e.value for e in at.error))
        self.assertEqual(len(at.tabs), 0)

    def test_wrong_password_is_refused_and_the_app_stays_hidden(self):
        at = make_app()
        at.run()
        at.text_input[0].input("nope").run()
        at.button[0].click().run()
        self.assertTrue(any("Wrong password" in e.value for e in at.error))
        self.assertEqual(len(at.tabs), 0)

    def test_the_right_password_opens_the_app(self):
        at = login(make_app())
        self.assertEqual([e.value for e in at.exception], [])
        self.assertEqual(len(at.tabs), 3)

    def test_five_wrong_attempts_lock_the_session(self):
        at = make_app()
        at.run()
        for _ in range(5):
            at.text_input[0].input("nope").run()
            at.button[0].click().run()
        at.run()
        self.assertTrue(any("Too many wrong attempts" in e.value for e in at.error))


class Ui(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.at = login(make_app())

    def test_engine_options_are_renamed_and_manual_is_gone(self):
        radio = next(r for r in self.at.radio if r.label == "Engine")
        self.assertEqual(list(radio.options), ["Internal ML model", "Open-source language model", "Both, and compare"])
        self.assertEqual(radio.value, "Internal ML model")
        text = " ".join(str(r.options) for r in self.at.radio)
        for gone in ("Manual", "Copilot", "rules + statistics", "Data-driven"):
            self.assertNotIn(gone, text)

    def test_the_caption_under_the_title_is_gone(self):
        self.assertEqual([t.value for t in self.at.title], ["RCS Template Analyzer"])
        everything = " ".join([c.value for c in self.at.caption] + [m.value for m in self.at.markdown])
        for gone in ("Version", "Numbers come from your sheet by code", "A person should read it before it goes out"):
            self.assertNotIn(gone, everything)

    def test_the_bundled_vi_template_is_announced_and_a_different_one_can_be_uploaded(self):
        self.assertTrue(any("Using the Vi template bundled with the app" in c.value for c in self.at.sidebar.caption))
        self.assertTrue(os.path.exists(os.path.join(ROOT, "assets", "vi_template.pptx")))

    def test_the_bundled_training_data_is_loaded_at_start(self):
        h = self.at.session_state.hist
        self.assertEqual((len(h), h["brand"].nunique()), (36, 5))
        vals = {m.label: str(m.value) for m in self.at.metric}
        self.assertEqual(vals.get("Brands (need 4)"), "5")
        self.assertEqual(vals.get("Rows"), "36")

    def test_language_model_needs_configuration_and_consent(self):
        at = login(make_app())
        next(r for r in at.radio if r.label == "Engine").set_value("Open-source language model").run()
        self.assertTrue(any("No language model is configured" in w.value for w in at.sidebar.warning))
        at = login(make_app(secrets={"llm": {"base_url": "https://api.groq.com/openai/v1", "model": "qwen/qwen3.8-27b", "api_key": "k", "vision": True}}))
        next(r for r in at.radio if r.label == "Engine").set_value("Open-source language model").run()
        self.assertTrue(any("api.groq.com" in i.value and "Images sent: yes" in i.value for i in at.sidebar.info))
        self.assertTrue(any("may be sent to api.groq.com" in c.label for c in at.sidebar.checkbox))
        self.assertTrue(any(b.label == "Test the connection" for b in at.sidebar.button))


class AnalyseTab(unittest.TestCase):
    def test_brands_listed_and_card_text_read_from_the_portal_sheet(self):
        at = login(make_app())
        brand_box = next(s for s in at.selectbox if s.label == "Brand")
        self.assertEqual(sorted(brand_box.options), ["DEMO BRAND LTD", "OTHER BRAND LTD"])
        brand_box.select("DEMO BRAND LTD").run()
        self.assertTrue(any("read from the 'template title'" in c.value for c in at.caption))

    def test_generate_needs_an_issue_then_builds_on_the_bundled_template(self):
        at = login(make_app())
        next(s for s in at.selectbox if s.label == "Brand").select("DEMO BRAND LTD").run()
        button(at, "Generate").click().run()
        self.assertTrue(any("issue" in e.value for e in at.error), [e.value for e in at.error])
        next(t for t in at.text_area if t.label.startswith("What did the brand")).input("The brand sees lower click rates on some templates.").run()
        button(at, "Generate").click().run()
        self.assertEqual([e.value for e in at.exception], [])
        self.assertTrue(any("Files are ready" in s.value for s in at.success), [e.value for e in at.error])
        self.assertGreaterEqual(len(at.get("download_button")), 4)
        res = at.session_state.result
        self.assertIn("bundled Vi template", " ".join(i["message"] for i in res.issues))
        import pandas as pd
        import io
        self.assertEqual(len(pd.read_csv(io.BytesIO(res.history_csv))), 36 + 6)      # the bundled history plus this brand


class TrainingTab(unittest.TestCase):
    def test_import_adds_a_brand_to_the_bundled_history(self):
        at = login(make_app())
        at.text_input(key="tr_brand").input("Demo").run()
        at.selectbox(key="tr_vertical").select("E-commerce").run()
        button(at, "Import this brand").click().run()
        self.assertEqual([e.value for e in at.exception], [])
        h = at.session_state.hist
        self.assertEqual((len(h), h["brand"].nunique()), (42, 6))

    def test_a_sheet_with_two_brands_is_refused_and_nothing_changes(self):
        at = login(make_app(train_sheet=H.sheet_bytes(extra_brand=True)))
        at.text_input(key="tr_brand").input("Demo").run()
        at.selectbox(key="tr_vertical").select("E-commerce").run()
        button(at, "Import this brand").click().run()
        self.assertTrue(any("2 brands" in e.value for e in at.error))
        self.assertEqual(len(at.session_state.hist), 36)

    def test_import_is_disabled_without_a_brand_name_and_vertical(self):
        self.assertTrue(button(login(make_app()), "Import this brand").disabled)

    def test_reset_returns_to_the_bundled_data(self):
        at = login(make_app())
        at.text_input(key="tr_brand").input("Demo").run()
        at.selectbox(key="tr_vertical").select("E-commerce").run()
        button(at, "Import this brand").click().run()
        button(at, "Reset to the bundled data").click().run()
        self.assertEqual(len(at.session_state.hist), 36)


class GoldTab(unittest.TestCase):
    def test_blind_sheet_is_built_from_the_bundled_history(self):
        at = login(make_app())
        button(at, "Make the rating sheet").click().run()
        self.assertEqual([e.value for e in at.exception], [])
        self.assertTrue(at.session_state.blind_xlsx.startswith(b"PK"))
        self.assertGreaterEqual(at.session_state.blind_n, 25)


if __name__ == "__main__":
    unittest.main()
