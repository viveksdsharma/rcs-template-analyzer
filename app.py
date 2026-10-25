"""
RCS Template Analyzer: upload the portal sheet, the card text and the screenshots, choose an engine, download Word / PDF / PowerPoint.

Run locally:   streamlit run app.py
Deploy:        see README.md (Streamlit Community Cloud, or the Dockerfile for hosting inside your own tenant).
"""
import hmac
import io
import json
import os
import time
import zipfile

import pandas as pd
import streamlit as st

from rcsapp import __version__
from rcsapp import inputs as I
from rcsapp import metrics as M
from rcsapp import pipeline as P
from rcsapp.engines import llm_engine as L
from rcsapp.knowledge import load_knowledge, unvetted

st.set_page_config(page_title="RCS Template Analyzer", page_icon=None, layout="wide")

VERTICALS = ["", "E-commerce", "BFSI", "Healthcare", "Retail", "Travel", "Other"]
ENGINES = {
    "Data-driven engine (rules + statistics, no language model)": "rules",
    "Open-source language model": "llm",
    "Both, and compare": "both",
    "Manual: I paste Copilot's answer": "manual",
}


def secret(name, default=None):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name.upper(), default)


def llm_config():
    try:
        section = dict(st.secrets["llm"]) if "llm" in st.secrets else {}
    except Exception:
        section = {}
    return L.config_from(section)


# --------------------------------------------------------------------------- access control
def require_login():
    pw = secret("APP_PASSWORD")
    if not pw:
        if os.environ.get("ALLOW_NO_AUTH") == "1":
            st.warning("No password is set (ALLOW_NO_AUTH=1). Anyone with the link can use this app.")
            return
        st.error("APP_PASSWORD is not set, so the app will not start. Set it in .streamlit/secrets.toml (local) or in the app's Secrets (cloud). See the README.")
        st.stop()
    if st.session_state.get("auth"):
        return
    if st.session_state.get("fails", 0) >= 5:
        st.error("Too many wrong attempts in this session. Close the tab and try again later.")
        st.stop()
    st.title("RCS Template Analyzer")
    with st.form("login"):
        entered = st.text_input("Password", type="password")
        ok = st.form_submit_button("Enter")
    if ok:
        if hmac.compare_digest(entered.encode(), str(pw).encode()):
            st.session_state.auth = True
            st.rerun()
        st.session_state.fails = st.session_state.get("fails", 0) + 1
        time.sleep(1.0)
        st.error("Wrong password.")
    st.stop()


# --------------------------------------------------------------------------- helpers
@st.cache_data(show_spinner=False)
def read_sheet(data, name):
    df, issues = M.load_sheet(io.BytesIO(data), filename=name, allow_multi=True)
    return df, issues


def zip_files(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in files.items():
            z.writestr(k, v)
    return buf.getvalue()


def level_box(i):
    msg = f"{i.get('where') or i.get('template') or ''} {i['message']}".strip()
    {"error": st.error, "warn": st.warning}.get(i["level"], st.info)(msg)


# --------------------------------------------------------------------------- page
require_login()
st.title("RCS Template Analyzer")
st.caption(f"Version {__version__}. Numbers come from your sheet by code. The content review comes from the engine you choose. A person should read it before it goes out.")

knowledge = load_knowledge()
cfg = llm_config()

with st.sidebar:
    st.header("Settings")
    engine_label = st.radio("Engine", list(ENGINES), index=0)
    engine = ENGINES[engine_label]
    outputs = st.multiselect("Files to make", ["Word", "PDF", "PowerPoint"], default=["Word", "PDF", "PowerPoint"])
    analyst = st.text_input("Your team's name in the files", value="Vi")
    consent = False
    if engine in ("llm", "both"):
        if cfg.ready():
            st.info(f"Language model: {cfg.model}\n\nHost: {cfg.host}\n\nImages sent: {'yes' if cfg.vision else 'no (text and numbers only)'}")
            consent = st.checkbox(f"I confirm this brand's text and numbers may be sent to {cfg.host}")
        else:
            st.warning("No language model is configured. Add base_url and model under [llm] in Secrets, or use another engine.")
    vi_tpl = st.file_uploader("Vi PowerPoint template (optional)", type=["pptx"], help="Without it the deck uses a plain design. The template carries a company sensitivity label, so it is uploaded per session and is not stored.")
    hist = st.file_uploader("Training history (optional)", type=["csv"], help="The file this app gave you after earlier runs. A trained model is used only when it holds enough templates from other brands.")
    extra_brands = st.text_area("Other brands to block (one per line)", help="Any name listed here, in the OTHER_BRANDS secret or in config/other_brands.txt stops the run if it appears in this brand's text.")
    with st.expander("Knowledge pack status"):
        un = unvetted(knowledge)
        st.write(f"{len(knowledge['guardrails'])} guardrails, {len(knowledge['platform_facts'])} platform facts.")
        if un:
            st.warning(f"Guardrails {', '.join(un)} are marked to_be_vetted. Confirm them with their owner before using this app for real brands.")

# ---- step 1
st.header("1. Portal sheet")
up = st.file_uploader("Upload the template performance export (.xlsx or .csv)", type=["xlsx", "csv"], key="sheet")
if not up:
    st.info("Start by uploading the sheet.")
    st.stop()
data = up.getvalue()
try:
    df_all, load_issues = read_sheet(data, up.name)
except M.PipelineError as e:
    st.error(str(e))
    st.stop()
brands = sorted(df_all["brand"].dropna().astype(str).unique())
brand = st.selectbox("Brand", brands, help="One brand per run.")
dfb = df_all[df_all["brand"].astype(str) == brand].copy()
dfb["click rate %"] = (100 * dfb["p2a_responses"] / dfb["read"]).round(2)
show = dfb[["template", "date", "google_id", "submitted", "read", "p2a_responses", "click rate %"]].copy()
show["date"] = show["date"].dt.strftime("%Y-%m-%d")
st.dataframe(show, use_container_width=True, hide_index=True)
names = list(dict.fromkeys(dfb["template"]))
picked = st.multiselect("Templates to analyse", names, default=names[:8])
if len(picked) > 6:
    st.warning("More than 6 templates makes a long report and a long deck. Consider splitting the request.")
for i in load_issues:
    st.caption(i["message"])
if not picked:
    st.stop()

# ---- step 2
st.header("2. Card text and screenshots")
c1, c2 = st.columns(2)
with c1:
    content_up = st.file_uploader("Card-text sheet from the partner (optional)", type=["xlsx", "csv"], key="content", help="Columns: Template Name, Card Title, Card Description, CTA Phrase, CTA Type.")
with c2:
    img_up = st.file_uploader("Screenshots (images or a zip)", type=["png", "jpg", "jpeg", "webp", "gif", "zip"], accept_multiple_files=True, key="imgs")
images = I.read_images([(f.name, f.getvalue()) for f in img_up]) if img_up else {}
matched = I.match_images(picked, images)
if images:
    st.caption(f"{len(images)} image(s) read; {len(matched)} matched to a template by file name.")
content = {}
if content_up:
    try:
        content = {c["template"].lower(): c for c in I.parse_template_content(content_up.getvalue(), content_up.name)}
    except Exception as e:
        st.error(f"The card-text sheet could not be read: {e}")

sig = (brand, tuple(picked), content_up.name if content_up else "", tuple(sorted(images)))
if st.session_state.get("cards_sig") != sig:
    rows = []
    for t in picked:
        c = content.get(t.lower(), {})
        rows.append({"template": t, "label": "", "short": "", "creative": "", "title": c.get("title", ""), "description": c.get("description", ""), "button": c.get("button", ""), "image": matched.get(t, "")})
    st.session_state.cards_df = pd.DataFrame(rows)
    st.session_state.cards_sig = sig
st.caption("Edit or paste the live card text. For each template, add one line describing what the image says and shows (needed unless the model can read images).")
edited = st.data_editor(
    st.session_state.cards_df, use_container_width=True, hide_index=True, num_rows="fixed",
    column_config={"template": st.column_config.TextColumn(disabled=True), "image": st.column_config.SelectboxColumn(options=[""] + sorted(images)),
                   "description": st.column_config.TextColumn(width="large"), "creative": st.column_config.TextColumn(width="large")}, key="cards_editor")

# ---- step 3
st.header("3. The request")
years = sorted(int(y) for y in dfb[dfb["template"].isin(picked)]["year"].unique())
a, b = st.columns(2)
with a:
    brand_display = st.text_input("Brand name as it should appear", value=brand.split(" ")[0].title())
    vertical = st.selectbox("Vertical", VERTICALS, help="Selects which Vi guardrails apply (for example BFSI).")
    requester = st.text_input("Who asked (optional)")
with b:
    period = st.text_input("Period text", value=" vs ".join(str(y) for y in reversed(years)))
    labels = {str(y): st.text_input(f"Label for {y}", value=str(y), key=f"lab{y}") for y in years}
issue = st.text_area("What did the brand or partner ask for? What complaint or goal sits behind it?", height=110,
                     help="Be specific: 'lower read rates and click rates on some templates, plus quality concerns', not 'performance issues'.")

# ---- engine specific
manual_json = ""
if engine == "manual":
    st.subheader("Copilot step")
    st.write("1) Make the brief. 2) Paste it into Copilot chat with the screenshots. 3) Paste Copilot's JSON answer below.")


def build_input(engine_mode, manual=""):
    tpls = []
    for r in edited.to_dict("records"):
        tpls.append({k: ("" if v is None else v) for k, v in r.items()})
    return P.JobInput(sheet_bytes=data, sheet_name=up.name, brand=brand, templates=tpls, brand_display=brand_display, vertical=vertical, period=period,
                      period_labels=labels, issue=issue, requester=requester, images=images, outputs=tuple({"Word": "docx", "PDF": "pdf", "PowerPoint": "pptx"}[o] for o in outputs),
                      engine=engine_mode, llm_cfg=cfg, llm_consent=consent, manual_json=manual,
                      vi_template_bytes=vi_tpl.getvalue() if vi_tpl else None, history_bytes=hist.getvalue() if hist else None,
                      extra_blocked_brands=[x.strip() for x in str(secret("OTHER_BRANDS", "")).split(",") if x.strip()] + extra_brands.splitlines(), analyst=analyst or "Vi")


if engine == "manual":
    if st.button("Make the brief"):
        try:
            st.session_state.brief = P.prepare_brief(build_input("rules"))
        except Exception as e:
            st.error(str(e))
    if st.session_state.get("brief"):
        st.download_button("Download brief (.md)", st.session_state.brief, file_name="analysis_brief.md")
        with st.expander("Show the brief"):
            st.code(st.session_state.brief, language="markdown")
    manual_json = st.text_area("Copilot's JSON answer", height=200)

# ---- step 4
st.header("4. Generate")
if not outputs:
    st.warning("Choose at least one file type in the sidebar.")
go = st.button("Generate", type="primary", disabled=not outputs)
if go:
    inp = build_input(engine, manual_json)
    with st.status("Working...", expanded=True) as status:
        lines = st.empty()
        buf = []

        def say(m):
            buf.append(m)
            lines.write("\n\n".join(buf[-8:]))
        res = P.run_job(inp, log=say)
        status.update(label=f"Finished: {res.status}", state="complete" if res.status == "built" else "error")
    st.session_state.result, st.session_state.last_inp = res, inp

res = st.session_state.get("result")
if res:
    st.divider()
    st.header("Result")
    {"built": st.success, "blocked_by_checks": st.error}.get(res.status, st.error)(
        {"built": "Files are ready. Have a person read the review before anything goes to the brand.",
         "blocked_by_checks": "The checks found errors, so no files were built. Fix them below or edit the analysis.",
         "stopped_inputs": "Something is missing.", "stopped_metrics": "The sheet could not be used.", "stopped_data": "The data failed its checks."}.get(res.status, res.status))
    t_dl, t_chk, t_eng, t_edit, t_num = st.tabs(["Downloads", "Checks and review", "Engines", "Edit analysis", "Numbers"])
    with t_dl:
        if res.files:
            for k, v in res.files.items():
                st.download_button(f"Download {k}", v, file_name=k, key=f"dl{k}")
            st.download_button("Download all as a zip", zip_files(res.files), file_name="rcs_analysis_files.zip")
        else:
            st.write("No files were built.")
        if res.analysis:
            st.download_button("Download analysis.json", json.dumps(res.analysis, indent=1, ensure_ascii=False), file_name="analysis.json")
        if res.history_csv:
            st.download_button("Download updated training history (.csv)", res.history_csv, file_name="history.csv",
                               help="Keep this file yourself and upload it next time. The app does not store it.")
        if res.review_notes:
            st.download_button("Download internal review notes (.md). Do not send to the brand.", res.review_notes, file_name="review_notes.md")
    with t_chk:
        for i in sorted(res.issues, key=lambda i: {"error": 0, "warn": 1}.get(i["level"], 2)):
            level_box(i)
        if not res.issues:
            st.success("No issues.")
        if res.review_notes:
            with st.expander("Review notes"):
                st.markdown(res.review_notes)
    with t_eng:
        info = res.engine_info.get("rules", {})
        mi = info.get("model", {})
        st.write(f"**Trained model:** {mi.get('status', 'none')}. {mi.get('reason', '')}")
        c = info.get("concordance")
        if c:
            st.write(f"**Text-only content score vs click rate (same period pairs):** {c['concordant']} concordant, {c['discordant']} discordant, {c['tied']} tied.")
        if res.agreement:
            st.write("**Content risk by engine**")
            st.dataframe(pd.DataFrame(res.agreement), hide_index=True, use_container_width=True)
        for n_ in res.notes:
            st.caption(n_)
        if info.get("scores"):
            st.write("**Rules engine scores**")
            st.dataframe(pd.DataFrame([{"template": k, **{kk: (", ".join(vv) if isinstance(vv, list) else vv) for kk, vv in v.items()}} for k, v in info["scores"].items()]), hide_index=True, use_container_width=True)
    with t_edit:
        if res.analysis:
            st.write("Reviewers can correct wording here. On rebuild, the number and confidentiality checks run again on the edited text.")
            txt = st.text_area("analysis.json", json.dumps(res.analysis, indent=1, ensure_ascii=False), height=420, key="edit_json")
            if st.button("Re-check and rebuild files"):
                try:
                    new = json.loads(txt)
                except json.JSONDecodeError as e:
                    st.error(f"Not valid JSON: {e}")
                else:
                    with st.spinner("Rebuilding..."):
                        st.session_state.result = P.run_job(st.session_state.last_inp, analysis_override=new)
                    st.rerun()
        else:
            st.write("Nothing to edit yet.")
    with t_num:
        if res.metrics:
            rows = []
            for t in res.metrics["templates"]:
                r = t["rates"]
                rows.append({"template": t["template"], "date": t["date"], "Messages Submitted": int(t["counts"]["submitted"]), "DeliveryRate": M.shown_rate(t, "DeliveryRate"),
                             "ReadRate": M.shown_rate(t, "ReadRate"), "P2AResponseRate (click rate)": M.shown_rate(t, "P2AResponseRate"),
                             "Pre-send failure rate*": round(r["pre_send_failure_rate"], 1), "Expiry rate*": round(r["expiry_rate"], 1)})
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
            st.caption("* calculated from the sheet counts.")
