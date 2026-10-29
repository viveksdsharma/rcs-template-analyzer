"""
One function, run_job(), does everything the analysis page needs. The Streamlit page is a thin layer on top of it,
so the same code can be tested without a browser and reused later (for example behind a scheduled trigger).

Engines:  "ml"   the Internal ML model (text scoring and, once the history is large enough, a trained component)
          "llm"  the open-source language model behind a hosted OpenAI-compatible endpoint
          "both" both, with the differences shown to the reviewer
"""
import hashlib
import io
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime

from . import ASSETS, CONFIG
from . import check as C
from . import inputs as I
from . import metrics as M
from . import training as T
from .engines import llm_engine as L
from .engines import ml_engine as E
from .engines import reconcile as R
from .knowledge import load_knowledge, load_rules, unvetted

OTHER_BRANDS = os.path.join(CONFIG, "other_brands.txt")
BUNDLED_TEMPLATE = os.path.join(ASSETS, "vi_template.pptx")


@dataclass
class JobInput:
    sheet_bytes: bytes
    sheet_name: str
    brand: str
    templates: list                    # [{template, label, short, creative, title, description, button, image}]
    brand_display: str = ""
    vertical: str = ""
    period: str = ""
    period_labels: dict = field(default_factory=dict)
    issue: str = ""
    requester: str = ""
    images: dict = field(default_factory=dict)        # file name -> bytes
    outputs: tuple = ("docx", "pdf", "pptx")
    engine: str = "ml"                 # ml | llm | both
    llm_cfg: object = None
    llm_consent: bool = False
    template_bytes: bytes = None       # a PowerPoint template uploaded for this run; None means the bundled one
    history: object = None             # training history (DataFrame), normally loaded from the bundled seed
    cohort_rules: str = ""
    extra_blocked_brands: list = field(default_factory=list)
    analyst: str = "Vi"


@dataclass
class JobResult:
    status: str = "new"
    files: dict = field(default_factory=dict)
    issues: list = field(default_factory=list)
    analysis: dict = None
    analysis_ml: dict = None
    analysis_llm: dict = None
    metrics: dict = None
    agreement: list = field(default_factory=list)
    engine_info: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    history_csv: bytes = None
    review_notes: str = ""
    log: list = field(default_factory=list)
    request: dict = None


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def preflight(inp):
    p = []
    if not inp.sheet_bytes:
        p.append("Upload the portal sheet.")
    if not inp.brand:
        p.append("Choose the brand.")
    if not inp.templates:
        p.append("Choose at least one template.")
    for t in inp.templates:
        miss = [k for k in ("title", "description") if not str(t.get(k, "")).strip()]
        if miss:
            p.append(f"Template '{t['template']}' has no card {' or '.join(miss)}. Paste the live card text.")
    if not str(inp.issue).strip():
        p.append("Say what the brand or partner asked for (the issue). The analysis is written around it.")
    if inp.engine in ("llm", "both"):
        if not (inp.llm_cfg and inp.llm_cfg.ready()):
            p.append("The language model is not configured (base URL and model are missing in the secrets).")
        elif not inp.llm_consent:
            p.append(f"Tick the box to confirm this data may be sent to {inp.llm_cfg.host}.")
    return p


def _brief_request(inp, metrics, tmp):
    cur = max(metrics["meta"]["years"])
    year_of = {t["template"]: t["year"] for t in metrics["templates"]}
    matched = I.match_images([t["template"] for t in inp.templates], inp.images)
    cur_t, ref_t, notes = [], [], []
    img_dir = os.path.join(tmp, "images")
    for t in inp.templates:
        name = t["template"]
        entry = {"template": name, "label": t.get("label") or name, "short": t.get("short") or t.get("label") or name,
                 "creative": t.get("creative") or "(see attached image)",
                 "card": {"title": t.get("title", ""), "description": t.get("description", ""), "button": t.get("button", "")}}
        fname = t.get("image") or matched.get(name)
        if fname and fname in inp.images:
            try:
                entry["image"] = I.save_image(inp.images[fname], img_dir, name)
            except Exception:
                notes.append(f"The image for '{name}' ({fname}) could not be read, so this template goes without a picture.")
        (cur_t if year_of.get(name) == cur else ref_t).append(entry)
    req = {"brand": inp.brand, "brand_display": inp.brand_display or inp.brand, "vertical": inp.vertical, "period": inp.period,
           "period_labels": inp.period_labels, "requester": inp.requester, "issue": inp.issue.strip(), "templates": cur_t,
           "reference_templates": ref_t, "images_dir": "images"}
    return req, img_dir, notes


def _merge_for_files(final, ml_a):
    """Deterministic parts always come from code; model parts override where present."""
    out = dict(final)
    for key in ("feature_matrix", "reference_templates"):
        if ml_a.get(key) and not out.get(key):
            out[key] = ml_a[key]
    deck = dict(ml_a.get("deck", {}))
    deck.update({k: v for k, v in (final.get("deck") or {}).items() if v})
    out["deck"] = deck
    return out


def run_job(inp, log=print, analysis_override=None):
    """analysis_override: a reviewer-edited analysis. The engines are skipped; checks and files are rerun on it."""
    res = JobResult()
    say = lambda m: (res.log.append(m), log(m))  # noqa: E731
    pre = preflight(inp)
    if pre:
        res.status = "stopped_inputs"
        res.issues = [{"level": "error", "code": "preflight", "message": m} for m in pre]
        return res

    style = _load_json(os.path.join(CONFIG, "style.json"))
    knowledge, rules = load_knowledge(), load_rules()
    try:
        metrics = M.run(io.BytesIO(inp.sheet_bytes), brand=inp.brand, templates=[t["template"] for t in inp.templates], filename=inp.sheet_name)
    except M.PipelineError as e:
        res.status = "stopped_metrics"
        res.issues = [{"level": "error", "code": "metrics", "message": str(e)}]
        return res
    res.metrics = metrics
    say(f"Metrics: {metrics['meta']['n_templates']} templates, periods {metrics['meta']['years']}")
    if not metrics["can_proceed"]:
        res.status = "stopped_data"
        res.issues = metrics["issues"]
        return res

    with tempfile.TemporaryDirectory() as tmp:
        request, img_dir, img_notes = _brief_request(inp, metrics, tmp)
        res.request = request
        res.issues += [{"level": "info", "code": "image_unreadable", "message": n} for n in img_notes]
        extra = [b.strip() for b in inp.extra_blocked_brands if b and b.strip()]
        history = inp.history if inp.history is not None and len(inp.history) else None

        say("Internal ML model")
        ml_a, info = E.build_analysis(metrics, request, knowledge, rules, history=history, analyst=inp.analyst)
        res.analysis_ml, res.engine_info["ml"] = ml_a, info

        llm_a = None
        if analysis_override is not None:
            pass
        elif inp.engine in ("llm", "both"):
            say(f"Open-source LLM ({inp.llm_cfg.model} at {inp.llm_cfg.host})")
            try:
                llm_a, problems, trace = L.run_llm_engine(inp.llm_cfg, request, metrics, knowledge, style, OTHER_BRANDS, extra, img_dir, log=say)
                res.engine_info["llm"] = {"model": inp.llm_cfg.model, "host": inp.llm_cfg.host, "vision": bool(inp.llm_cfg.vision),
                                          "calls": len([t for t in trace if "attempt" in t]), "trace": trace}
                res.issues += [{"level": "info", "code": "image_not_used", "message": f"{t['label']}: {t['note']}"} for t in trace if t.get("note")]
                if problems:
                    res.issues += [{"level": "error", "code": "llm_checks", "message": m} for m in problems]
                    llm_a = None if inp.engine == "llm" else llm_a
            except L.LLMError as e:
                res.issues.append({"level": "error" if inp.engine == "llm" else "warn", "code": "llm_error", "message": str(e)})
                llm_a = None
        res.analysis_llm = llm_a

        final, notes = None, []
        if analysis_override is not None:
            final = analysis_override
            notes.append("The analysis was edited by a reviewer before the files were built.")
        elif inp.engine == "ml":
            final = ml_a
        elif inp.engine == "llm":
            final = llm_a
        else:
            if llm_a is not None and not any(i["code"] == "llm_checks" for i in res.issues):
                res.agreement, summ = R.agreement(ml_a, llm_a)
                res.engine_info["agreement"] = summ
                final, notes = R.reconcile(ml_a, llm_a)
            else:
                final = ml_a
                notes.append("The LLM's answer was not usable, so the files are built from the Internal ML model alone.")
                res.issues.append({"level": "warn", "code": "fallback", "message": notes[-1]})
        res.notes = notes
        if final is None:
            res.status = "blocked_by_checks" if res.issues else "no_analysis"
            return res

        final = _merge_for_files(final, ml_a)
        res.issues += C.run_all(final, request, metrics, style, OTHER_BRANDS, knowledge, extra)
        res.analysis = final
        errors = [i for i in res.issues if i["level"] == "error"]

        new_rows = T.rows_from_run(metrics, request, vertical=inp.vertical, cohort_rules_text=inp.cohort_rules)
        res.history_csv = T.to_csv(T.merge(history, new_rows))
        res.review_notes = review_notes(inp, res, knowledge)
        if errors:
            res.status = "blocked_by_checks"
            say(f"Checks found {len(errors)} error(s); files were not built.")
            return res

        from .pdf import docx_to_pdf
        from .render_docx import build_docx
        from .render_pptx import build_pptx
        base = (request["brand_display"] or request["brand"]).replace(" ", "_")
        base = "".join(ch for ch in base if ch.isalnum() or ch in "_-")
        want = set(inp.outputs)
        docx_path = os.path.join(tmp, f"{base}_RCS_Template_Analysis.docx")
        if want & {"docx", "pdf"}:
            build_docx(metrics, final, request, docx_path, style, img_dir)
            if "docx" in want:
                res.files[os.path.basename(docx_path)] = _read(docx_path)
            if "pdf" in want:
                try:
                    pdf_path = docx_path[:-5] + ".pdf"
                    how = docx_to_pdf(docx_path, pdf_path)
                    res.files[os.path.basename(pdf_path)] = _read(pdf_path)
                    say(f"PDF made with {how}")
                except Exception as e:
                    res.issues.append({"level": "warn", "code": "pdf", "message": str(e)})
        if "pptx" in want:
            tpl_path, tpl_name = None, ""
            if inp.template_bytes:
                tpl_path, tpl_name = os.path.join(tmp, "template.pptx"), "the template you uploaded"
                with open(tpl_path, "wb") as f:
                    f.write(inp.template_bytes)
            elif os.path.exists(BUNDLED_TEMPLATE):
                tpl_path, tpl_name = BUNDLED_TEMPLATE, "the bundled Vi template"
            pp = os.path.join(tmp, f"{base}_RCS_Template_Deck.pptx")
            try:
                build_pptx(metrics, final, request, tpl_path, pp, style, img_dir)
            except ValueError as e:                     # an uploaded file that is not the Vi template
                res.issues.append({"level": "warn", "code": "template_rejected", "message": f"{e} The deck was built on the bundled template instead."})
                build_pptx(metrics, final, request, BUNDLED_TEMPLATE if os.path.exists(BUNDLED_TEMPLATE) else None, pp, style, img_dir)
                tpl_name = "the bundled Vi template"
            res.files[os.path.basename(pp)] = _read(pp)
            lbl = _label(res.files[os.path.basename(pp)])
            if lbl:
                res.issues.append({"level": "warn", "code": "sensitivity_label_inherited",
                                   "message": f"The deck carries {tpl_name}'s sensitivity label '{lbl}'. It is left as it is. Relabel it in PowerPoint as your policy requires before it goes outside the company."})
            if not tpl_path:
                res.issues.append({"level": "info", "code": "plain_deck", "message": "No Vi template was found, so the deck uses a plain design."})
        res.status = "built"
    return res


def _label(pptx_bytes):
    import re
    import zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(pptx_bytes)) as z:
            if "docProps/custom.xml" not in z.namelist():
                return None
            m = re.search(r'name="MSIP_Label_[^"]*_Name"[^>]*>\s*<vt:lpwstr>([^<]+)<', z.read("docProps/custom.xml").decode("utf8", "ignore"))
            return m.group(1).strip() if m else None
    except Exception:
        return None


def review_notes(inp, res, knowledge):
    """Internal file for the reviewers. Never goes to the brand."""
    L_ = ["# Review notes (internal, do not send to the brand)", f"- Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
          f"- Brand: {inp.brand}   Engine: {ENGINE_LABELS.get(inp.engine, inp.engine)}", f"- Input sheet: {inp.sheet_name} (sha256 {hashlib.sha256(inp.sheet_bytes).hexdigest()[:12]})"]
    if "llm" in res.engine_info:
        e = res.engine_info["llm"]
        L_.append(f"- Open-source LLM: {e['model']} at {e['host']}; images {'sent' if e.get('vision') else 'not sent'}; {e['calls']} call(s) including corrections")
    mi = res.engine_info.get("ml", {}).get("model", {})
    L_.append(f"- Internal ML model, text scoring: always on. Trained component: {mi.get('status', 'none')}. {mi.get('reason', '')}")
    c = res.engine_info.get("ml", {}).get("concordance", {})
    if c:
        L_.append(f"- Text-only score vs click rate: {c['concordant']} concordant, {c['discordant']} discordant, {c['tied']} tied pairs (within the same period). Single sends: directional.")
    un = unvetted(knowledge)
    if un:
        L_.append(f"- Knowledge pack: guardrails {', '.join(un)} are marked to_be_vetted. Confirm with their owner before relying on them.")
    if res.agreement:
        L_.append("\n## Where the two engines differ on content risk")
        for r in res.agreement:
            keys = [k for k in r if k not in ("template", "agree", "gap")]
            L_.append(f"- {r['template']}: " + " vs ".join(f"{k} {r[k]}" for k in keys) + ("" if r["agree"] else "  <-- differ"))
    if res.notes:
        L_.append("\n## Reconciliation notes")
        L_ += [f"- {n}" for n in res.notes]
    L_.append("\n## Checks")
    if not res.issues:
        L_.append("- No issues.")
    for i in res.issues:
        L_.append(f"- [{i['level']}] {i.get('where', '') or i.get('template', '')} {i['message']}")
    L_.append("\n## What the reviewer should read line by line\n- The content review (works and watch-outs) and the recommendations. The number check confirms numbers exist in the computed results; it cannot confirm they are attached to the right thing.\n- Any statement about how RCS renders on a phone: it must come from the knowledge pack.\n- Audience is unknown: brands do not share targeting.")
    return "\n".join(L_)


ENGINE_LABELS = {"ml": "Internal ML model", "llm": "Open-source LLM", "both": "Both"}
