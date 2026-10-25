"""Drives the real Streamlit page headlessly. The harness cannot upload files, so file_uploader is replaced by a fake."""
import io
import os
import sys
import unittest

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("SOFFICE_CMD", "python /mnt/skills/public/docx/scripts/office/soffice.py")
import helpers as H  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

CONTENT = pd.DataFrame({"Template Name": [t["template"] for t in H.TEMPLATES], "Card Title": [t["title"] for t in H.TEMPLATES],
                        "Card Description": [t["description"] for t in H.TEMPLATES], "CTA Phrase": ["Shop Now"] * 6, "CTA Type": ["URL"] * 6})
_bio = io.BytesIO()
CONTENT.to_excel(_bio, index=False)

HARNESS = '''
import io, os, sys
sys.path.insert(0, %(root)r); sys.path.insert(0, %(tests)r)
import streamlit as st
import helpers as H
class Fake:
    def __init__(self, name, data): self.name, self._d = name, data
    def getvalue(self): return self._d
SHEET, CONTENT, IMAGES = %(sheet)r, %(content)r, %(images)r
def fake_uploader(label, type=None, accept_multiple_files=False, key=None, **kw):
    if key == "sheet": return Fake("demo.xlsx", SHEET)
    if key == "content": return Fake("content.xlsx", CONTENT)
    if key == "imgs": return [Fake(n, d) for n, d in IMAGES.items()]
    return [] if accept_multiple_files else None
st.file_uploader = fake_uploader
# The sandbox this was written in has a pyarrow/NumPy mismatch that breaks table widgets; a normal install does not.
st.dataframe = lambda *a, **k: None
st.data_editor = lambda df, **k: df
exec(compile(open(%(app)r, encoding="utf-8").read(), "app.py", "exec"))
'''


def make_app(sheet=None):
    script = HARNESS % {"root": ROOT, "tests": os.path.dirname(os.path.abspath(__file__)), "sheet": sheet or H.sheet_bytes(extra_brand=True),
                        "content": _bio.getvalue(), "images": H.images(), "app": os.path.join(ROOT, "app.py")}
    at = AppTest.from_string(script, default_timeout=120)
    at.secrets["APP_PASSWORD"] = "pw"
    return at


def login(at):
    at.run()
    at.text_input[0].input("pw").run()
    at.button[0].click().run()
    return at


class AppFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.at = login(make_app())

    def test_no_exception_and_brands_listed(self):
        self.assertEqual([e.value for e in self.at.exception], [])
        brand_box = next(s for s in self.at.selectbox if s.label == "Brand")
        self.assertEqual(sorted(brand_box.options), ["DEMO BRAND LTD", "OTHER BRAND LTD"])

    def test_generate_needs_an_issue_then_builds(self):
        at = make_app()
        login(at)
        brand_box = next(s for s in at.selectbox if s.label == "Brand")
        brand_box.select("DEMO BRAND LTD").run()
        gen = next(b for b in at.button if b.label == "Generate")
        gen.click().run()
        self.assertTrue(any("issue" in e.value for e in at.error), [e.value for e in at.error])
        issue = next(t for t in at.text_area if t.label.startswith("What did the brand"))
        issue.input("The brand sees lower click rates on some templates.").run()
        next(b for b in at.button if b.label == "Generate").click().run()
        self.assertEqual([e.value for e in at.exception], [])
        self.assertTrue(any("Files are ready" in s.value for s in at.success), [e.value for e in at.error])
        dl = [e for e in at.main if getattr(e, "type", "") == "download_button" or "download" in str(type(e)).lower()]
        self.assertTrue(len(at.get("download_button")) >= 4)

    def test_llm_engine_needs_config_and_consent(self):
        at = make_app()
        login(at)
        next(r for r in at.radio if r.label == "Engine").set_value("Open-source language model").run()
        self.assertTrue(any("No language model is configured" in w.value for w in at.sidebar.warning))


if __name__ == "__main__":
    unittest.main()
