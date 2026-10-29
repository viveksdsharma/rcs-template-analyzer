"""
RCS Template Analyzer.

  Tab 1  Analyse a brand   upload the sheet, card text and screenshots, choose an engine, download Word / PDF / PowerPoint
  Tab 2  Training data     the bundled brands, import more, see whether the trained component can be switched on
  Tab 3  Gold set          make a blind rating sheet, score the engines against human ratings

Run locally:   streamlit run app.py
Deploy:        see README.md
"""
import hmac
import io
import json
import os
import time
import zipfile

import pandas as pd
import streamlit as st

from rcsapp import ASSETS, CONFIG
from rcsapp import gold as G
from rcsapp import inputs as I
from rcsapp import metrics as M
from rcsapp import pipeline as P
from rcsapp import training as T
from rcsapp.engines import llm_engine as L
from rcsapp.engines import ml_engine as E
from rcsapp.knowledge import load_knowledge, load_rules, unvetted

st.set_page_config(page_title="RCS Template Analyzer", page_icon=None, layout="wide")

ENGINES = {
    "Internal ML model": "ml",
    "Open-source language model": "llm",
    "Both, and compare": "both",
}
VERTICAL_OPTIONS = [""] + T.VERTICALS
BUNDLED = os.path.join(ASSETS, "vi_template.pptx")


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


@st.cache_data(show_spinner=False)
def read_sheet(data, name):
    return M.load_sheet(io.BytesIO(data), filename=name, allow_multi=True)


@st.cache_data(show_spinner="Loading the bundled training data...")
def seed_history():
    return T.load_seed()


def zip_files(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for k, v in files.items():
            z.writestr(k, v)
    return buf.getvalue()


def level_box(i):
    msg = f"{i.get('where') or i.get('template') or ''} {i['message']}".strip()
    {"error": st.error, "warn": st.warning}.get(i["level"], st.info)(msg)


def show_issues(issues):
    order = {"error": 0, "warn": 1}
    for i in sorted(issues, key=lambda i: order.get(i["level"], 2)):
        level_box(i)


require_login()
st.title("RCS Template Analyzer")

knowledge, rules = load_knowledge(), load_rules()
cfg = llm_config()
with open(os.path.join(CONFIG, "style.json"), encoding="utf-8") as _f:
    STYLE = json.load(_f)

if "hist" not in st.session_state:
    _h, _rep = seed_history()
    st.session_state.hist = _h
    st.session_state.seed_report = _rep


def current_history():
    return st.session_state.get("hist")


# --------------------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Settings")
    engine_label = st.radio("Engine", list(ENGINES), index=0)
    engine = ENGINES[engine_label]
    outputs = st.multiselect("Files to make", ["Word", "PDF", "PowerPoint"], default=["Word", "PDF", "PowerPoint"])
    analyst = st.text_input("Your team's name in the files", value="Vi")
    consent = False
    if engine in ("llm", "both"):
        if cfg.ready():
            st.info(f"Model: {cfg.model}\n\nHost: {cfg.host}\n\nImages sent: {'yes' if cfg.vision else 'no (text and numbers only)'}")
            consent = st.checkbox(f"I confirm this brand's text, numbers and images may be sent to {cfg.host}")
            if st.button("Test the connection"):
                with st.spinner("Calling the model..."):
                    ok, msg = L.ping(cfg, with_image=cfg.vision)
                (st.success if ok else st.error)(msg)
        else:
            st.warning("No language model is configured. Add base_url, model and api_key under [llm] in Secrets, or use the Internal ML model.")
    st.subheader("Deck template")
    tpl_up = st.file_uploader("Use a different PowerPoint template (optional)", type=["pptx"], key="tpl_up",
                              help="Leave empty to use the Vi template bundled with the app. A different file must have the same layouts as the Vi template.")
    st.caption("Using the Vi template bundled with the app." if (tpl_up is None and os.path.exists(BUNDLED)) else
               ("Using the template you uploaded." if tpl_up is not None else "No bundled template found: the deck will use a plain design."))
    extra_brands = st.text_area("Other brands to block (one per line)",
                                help="Names listed here, in the OTHER_BRANDS secret or in config/other_brands.txt must never appear in this brand's generated text. Every brand in the training data is blocked automatically.")
    with st.expander("Knowledge pack status"):
        un = unvetted(knowledge)
        st.write(f"{len(knowledge['guardrails'])} guardrails, {len(knowledge['platform_facts'])} platform facts.")
        if un:
            st.warning(f"Guardrails {', '.join(un)} are marked to_be_vetted. Confirm them with their owner before using this app for real brands.")

tab_a, tab_t, tab_g = st.tabs(["1. Analyse a brand", "2. Training data", "3. Gold set"])


# =========================================================================== TAB 1
def render_analyse():
    st.header("Portal sheet")
    up = st.file_uploader("Upload the template performance export (.xlsx or .csv)", type=["xlsx", "csv"], key="sheet")
    if not up:
        st.info("Start by uploading the sheet.")
    else:
        data = up.getvalue()
        try:
            df_all, load_issues = read_sheet(data, up.name)
        except M.PipelineError as e:
            st.error(str(e))
            return
        brands = sorted(df_all["brand"].dropna().astype(str).unique())
        brand = st.selectbox("Brand", brands, help="One brand per run.")
        dfb = df_all[df_all["brand"].astype(str) == brand].copy()
        dfb["click rate %"] = (100 * dfb["p2a_responses"] / dfb["read"]).round(2)
        show = dfb[["template", "date", "google_id", "submitted", "read", "p2a_responses", "click rate %"]].copy()
        show["date"] = show["date"].dt.strftime("%Y-%m-%d")
        st.dataframe(show, hide_index=True)
        names = list(dict.fromkeys(dfb["template"]))
        picked = st.multiselect("Templates to analyse", names, default=names[:8])
        if len(picked) > 6:
            st.warning("More than 6 templates makes a long report and a long deck. Consider splitting the request.")
        for i in load_issues:
            st.caption(i["message"])
        if picked:
            st.header("Card text and screenshots")
            c1, c2 = st.columns(2)
            with c1:
                content_up = st.file_uploader("Card-text sheet from the partner (optional)", type=["xlsx", "csv"], key="content",
                                              help="Columns: Template Name, Card Title, Card Description, CTA Phrase, CTA Type. If your portal sheet already has 'template title', 'template body' and 'CTA' columns, it is read automatically.")
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
            elif "title" in dfb.columns or "body" in dfb.columns:
                tmp_rows = T.rows_from_canonical(dfb, brand_display=brand)
                content = {r.template.lower(): {"title": r.title, "description": r.description, "button": r.button} for r in tmp_rows.drop_duplicates("template").itertuples()}
                if any(v["title"] or v["description"] for v in content.values()):
                    st.caption("Card text was read from the 'template title', 'template body' and 'CTA' columns of the portal sheet.")
            sig = (brand, tuple(picked), content_up.name if content_up else "", tuple(sorted(images)))
            if st.session_state.get("cards_sig") != sig:
                rows = []
                for t in picked:
                    c = content.get(t.lower(), {})
                    rows.append({"template": t, "label": "", "short": "", "creative": "", "title": c.get("title", ""), "description": c.get("description", ""),
                                 "button": c.get("button", ""), "image": matched.get(t, "")})
                st.session_state.cards_df = pd.DataFrame(rows)
                st.session_state.cards_sig = sig
            st.caption("Edit or paste the live card text. For each template, add one line describing what the image says and shows (not needed when the model reads images).")
            edited = st.data_editor(st.session_state.cards_df, hide_index=True, num_rows="fixed",
                                    column_config={"template": st.column_config.TextColumn(disabled=True), "image": st.column_config.SelectboxColumn(options=[""] + sorted(images)),
                                                   "description": st.column_config.TextColumn(width="large"), "creative": st.column_config.TextColumn(width="large")}, key="cards_editor")

            st.header("The request")
            years = sorted(int(y) for y in dfb[dfb["template"].isin(picked)]["year"].unique())
            a, b = st.columns(2)
            with a:
                brand_display = st.text_input("Brand name as it should appear", value=brand.split(" ")[0].title())
                vertical = st.selectbox("Vertical", VERTICAL_OPTIONS, help="Selects which Vi guardrails apply (for example BFSI).")
                requester = st.text_input("Who asked (optional)")
            with b:
                period = st.text_input("Period text", value=" vs ".join(str(y) for y in reversed(years)))
                labels = {str(y): st.text_input(f"Label for {y}", value=str(y), key=f"lab{y}") for y in years}
                cohort_rules = st.text_input("Cohort rules (optional)", help="For the training history only: 'offline=offline' puts every template whose name contains 'offline' in the cohort 'offline'.")
            issue = st.text_area("What did the brand or partner ask for? What complaint or goal sits behind it?", height=110,
                                 help="Be specific: 'lower read rates and click rates on some templates, plus quality concerns', not 'performance issues'.")

            def build_input(engine_mode):
                tpls = [{k: ("" if v is None else v) for k, v in r.items()} for r in edited.to_dict("records")]
                hist = current_history()
                blocked = [x.strip() for x in str(secret("OTHER_BRANDS", "")).split(",") if x.strip()] + extra_brands.splitlines() + T.brand_names(hist)
                return P.JobInput(sheet_bytes=data, sheet_name=up.name, brand=brand, templates=tpls, brand_display=brand_display, vertical=vertical, period=period,
                                  period_labels=labels, issue=issue, requester=requester, images=images,
                                  outputs=tuple({"Word": "docx", "PDF": "pdf", "PowerPoint": "pptx"}[o] for o in outputs),
                                  engine=engine_mode, llm_cfg=cfg, llm_consent=consent,
                                  template_bytes=tpl_up.getvalue() if tpl_up is not None else None, history=hist,
                                  cohort_rules=cohort_rules, extra_blocked_brands=blocked, analyst=analyst or "Vi")

            st.header("Generate")
            if not outputs:
                st.warning("Choose at least one file type in the sidebar.")
            if st.button("Generate", type="primary", disabled=not outputs):
                inp = build_input(engine)
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
        {"built": st.success}.get(res.status, st.error)(
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
                st.download_button("Download history including this brand (.csv)", res.history_csv, file_name="history.csv",
                                   help="The bundled training data plus this run's templates. Load it in the Training data tab, or add the workbook to data/seed to keep it.")
            if res.review_notes:
                st.download_button("Download internal review notes (.md). Do not send to the brand.", res.review_notes, file_name="review_notes.md")
        with t_chk:
            show_issues(res.issues)
            if not res.issues:
                st.success("No issues.")
            if res.review_notes:
                with st.expander("Review notes"):
                    st.markdown(res.review_notes)
        with t_eng:
            info = res.engine_info.get("ml", {})
            mi = info.get("model", {})
            st.write("**Internal ML model:** text scoring is always on. "
                     f"**Trained component:** {mi.get('status', 'none')}. {mi.get('reason', '')}")
            c = info.get("concordance")
            if c:
                st.write(f"**Text-only content score vs click rate (same period pairs):** {c['concordant']} concordant, {c['discordant']} discordant, {c['tied']} tied.")
            if "llm" in res.engine_info:
                e = res.engine_info["llm"]
                st.write(f"**Open-source LLM:** {e['model']} at {e['host']}, images {'sent' if e.get('vision') else 'not sent'}, {e['calls']} call(s).")
            if res.agreement:
                st.write("**Content risk by engine**")
                st.dataframe(pd.DataFrame(res.agreement), hide_index=True)
            for n_ in res.notes:
                st.caption(n_)
            if info.get("scores"):
                st.write("**Internal ML model scores**")
                st.dataframe(pd.DataFrame([{"template": k, **{kk: (", ".join(vv) if isinstance(vv, list) else vv) for kk, vv in v.items()}} for k, v in info["scores"].items()]), hide_index=True)
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
                st.dataframe(pd.DataFrame(rows), hide_index=True)
                st.caption("* calculated from the sheet counts.")


# =========================================================================== TAB 2
def render_training():
    st.header("Training data")
    st.write("The brands in the app's `data/seed` folder are loaded every time the app starts. Import more here: use the same export as for an analysis, with the card text columns "
             "(template title, template body, CTA). Include every template in the period, not only the good ones. Screenshots are not needed for training.")
    with st.expander("What was loaded at start-up"):
        for line in st.session_state.get("seed_report", []):
            st.write("- " + line)
    st.caption("Brands imported here last for this session. To keep a brand permanently, add its workbook to data/seed and list it in seed.json.")
    hist_up = st.file_uploader("Add a saved training history (optional)", type=["csv"], key="tr_hist",
                               help="A history file downloaded from this app. Its rows are merged into the current history.")
    if hist_up is not None and st.session_state.get("hist_loaded") != hist_up.name:
        try:
            loaded = T.read_history(hist_up.getvalue())
            st.session_state.hist = T.merge(st.session_state.get("hist"), loaded)
            st.session_state.hist_loaded = hist_up.name
            st.success(f"Merged {len(loaded)} rows.")
        except Exception as e:
            st.error(str(e))
    st.subheader("Import a brand")
    tr_sheet = st.file_uploader("Brand's portal sheet (.xlsx or .csv)", type=["xlsx", "csv"], key="tr_sheet")
    a, b, c = st.columns(3)
    with a:
        tr_brand = st.text_input("Brand name", key="tr_brand")
    with b:
        tr_vertical = st.selectbox("Vertical", VERTICAL_OPTIONS, key="tr_vertical")
    with c:
        tr_cohorts = st.text_area("Cohort rules (optional)", key="tr_cohorts", height=70,
                                  help="One per line: 'offline=offline' gives every template whose name contains 'offline' the cohort 'offline'. Use it when a brand sends to clearly different lists and you know which template went where. Leave it empty if you do not know.")
    if st.button("Import this brand", disabled=not (tr_sheet and tr_brand and tr_vertical)):
        try:
            rows, issues = T.import_sheet(tr_sheet.getvalue(), tr_sheet.name, tr_brand.strip(), tr_vertical, tr_cohorts)
        except M.PipelineError as e:
            st.error(str(e))
        else:
            show_issues(issues)
            if any(i["level"] == "error" for i in issues):
                st.error("Nothing was imported. Fix the errors above and try again.")
            else:
                st.session_state.hist = T.merge(st.session_state.get("hist"), rows)
                st.success(f"Imported {len(rows)} rows for {tr_brand}.")
    h = st.session_state.get("hist")
    if h is not None and len(h):
        st.subheader("What the history holds")
        rd = T.readiness(h)
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Rows", rd["rows"])
        m2.metric("Usable rows", rd["usable"])
        m3.metric(f"Distinct texts (need {T.GATE_GROUPS})", rd["groups"])
        m4.metric(f"Brands (need {T.GATE_BRANDS})", rd["brands"])
        (st.success if rd["gate_ok"] else st.info)(rd["message"])
        st.dataframe(pd.DataFrame(rd["per_brand"]), hide_index=True)
        st.caption("A row is usable when it has card text, at least 1,000 reads and at least 2 other texts from the same brand, cohort and year to compare with. "
                   "Templates carrying the same text are one distinct text.")
        if rd["gate_ok"]:
            mi = T.fit_model(h)
            st.write(f"**Trained component:** {mi['status']}. {mi['reason']}")
            if mi["status"] == "ok":
                st.write("Strongest features: " + ", ".join(f"{k} ({v:+.2f})" for k, v in mi["drivers"]))
        st.subheader("Do the text checks hold across brands?")
        ev = E.rule_evidence(h, rules)
        st.dataframe(ev, hide_index=True)
        st.caption("For each scoring check: how the rows it flags click compared with the brand's other texts (click index below 1 means below). "
                   "'OPPOSITE' means the check does not hold in this data and should be reweighted or dropped. Single sends and unknown audiences: read it as a lead, not a verdict.")
        with st.expander("All rows"):
            st.dataframe(h[["brand", "cohort", "template", "date", "send_number", "reads", "clicks", "click_rate", "loo_rate", "click_index", "reason"]], hide_index=True)
        st.download_button("Download training history (.csv)", T.to_csv(h), file_name="training_history.csv",
                           help="Keep this file yourself and merge it back next time.")
        if st.button("Reset to the bundled data"):
            st.session_state.hist = seed_history()[0]
            st.rerun()


# =========================================================================== TAB 3
def render_gold():
    st.header("Gold set")
    st.write("A gold set is a sheet of human content ratings. It measures how far the engines can be trusted. "
             "Rate each template from its card text and image **before** seeing any results. This sheet has no results in it on purpose.")
    h = st.session_state.get("hist")
    st.subheader("1. Make a blind rating sheet")
    if h is None or not len(h):
        st.info("Import at least one brand in the Training data tab first. The sheet is built from the templates in the history.")
    else:
        seed = st.number_input("Shuffle seed", min_value=0, value=0, step=1)
        if st.button("Make the rating sheet"):
            blind = G.blind_sheet(h, rules, seed=int(seed))
            st.session_state.blind_xlsx = G.template_workbook(blind, rules)
            st.session_state.blind_n = len(blind)
        if st.session_state.get("blind_xlsx"):
            st.success(f"{st.session_state.blind_n} templates, shuffled, results removed.")
            st.download_button("Download rating sheet (.xlsx)", st.session_state.blind_xlsx, file_name="gold_rating_sheet.xlsx")
    st.subheader("2. Score the engines")
    gold_up = st.file_uploader("Upload the completed rating sheet", type=["xlsx"], key="gold_up")
    use_llm = False
    if gold_up:
        if cfg.ready():
            use_llm = st.checkbox(f"Also rate every template with the language model ({cfg.model} at {cfg.host}); the text of the templates is sent there", key="gold_llm")
        try:
            gold = G.read_gold(gold_up.getvalue())
        except Exception as e:
            st.error(str(e))
            return
        llm_risk = None
        if use_llm:
            with st.spinner("Asking the language model..."):
                out = L.rate_content(cfg, [{"template": r.template_id, "brand": r.brand, "vertical": r.vertical, "title": r.title, "description": r.description,
                                            "button": getattr(r, "button_label", ""), "creative_note": getattr(r, "creative_note", "")} for r in gold.itertuples()], knowledge, STYLE)
            llm_risk = {k: v["content_risk"] for k, v in out.items()}
        res_g = G.score(gold, rules, hist=h, llm_risk=llm_risk)
        (st.success if "close enough" in res_g["verdict"] or "assist" in res_g["verdict"] else st.warning)(res_g["verdict"])
        st.write("**Counts**", res_g["counts"])
        st.write("**Internal ML model vs human, content risk** (weighted kappa: 1 is perfect, 0 is chance)")
        st.dataframe(pd.DataFrame(res_g["engine_vs_human"]), hide_index=True)
        if "llm_vs_human" in res_g:
            st.write("**Language model vs human**", res_g["llm_vs_human"])
        st.write("**Human vs human (the ceiling)**", res_g["human_vs_human"])
        if len(res_g["confusion"]):
            st.write("Rows: human rating. Columns: engine rating.")
            st.dataframe(res_g["confusion"])
        if len(res_g.get("flags", [])):
            st.write("**Text-check flags**")
            st.dataframe(res_g["flags"], hide_index=True)
        if "outcomes" in res_g:
            st.write("**Outcomes (secondary)**", res_g["outcomes"])
        for n_ in res_g["notes"]:
            st.caption(n_)
        st.download_button("Download scorecard (.md)", G.scorecard_markdown(res_g), file_name="gold_scorecard.md")


with tab_a:
    render_analyse()
with tab_t:
    render_training()
with tab_g:
    render_gold()
