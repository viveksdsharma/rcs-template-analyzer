"""
Engine B: an open-source language model behind any OpenAI-compatible endpoint (Groq, Together, Hugging Face router,
or your own Ollama / vLLM server). Nothing here is tied to one vendor.

Two steps (see brief.py): per-template content reviews with no performance data, then findings and recommendations.
Each answer is validated; if the checks fail, the problems are sent back and the model is asked to correct them.
"""
import base64
import json
import os
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests

from .. import brief as B
from .. import check as C
from .. import metrics as M


class LLMError(Exception):
    pass


@dataclass
class LLMConfig:
    base_url: str
    api_key: str = ""
    model: str = ""
    vision: bool = False
    temperature: float = 0.2
    max_tokens: int = 6000
    timeout: int = 180
    retries: int = 2
    extra_headers: dict = field(default_factory=dict)

    @property
    def host(self):
        return urlparse(self.base_url).netloc or self.base_url

    def ready(self):
        return bool(self.base_url and self.model)


def config_from(secrets=None, env=None):
    """Read [llm] from Streamlit secrets (a mapping) or environment variables."""
    env = env if env is not None else os.environ
    s = dict(secrets or {})
    g = lambda k, e, d="": s.get(k) or env.get(e, d)  # noqa: E731
    v = g("vision", "LLM_VISION", "false")
    return LLMConfig(base_url=g("base_url", "LLM_BASE_URL"), api_key=g("api_key", "LLM_API_KEY"), model=g("model", "LLM_MODEL"),
                     vision=str(v).lower() in ("1", "true", "yes"))


def chat(cfg, messages, json_mode=True):
    url = cfg.base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json", **cfg.extra_headers}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    body = {"model": cfg.model, "messages": messages, "temperature": cfg.temperature, "max_tokens": cfg.max_tokens}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    last = None
    for attempt in range(cfg.retries + 1):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=cfg.timeout)
            if r.status_code == 400 and json_mode and "response_format" in r.text:
                body.pop("response_format", None)           # provider does not support JSON mode: retry without it
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}"
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise LLMError(f"The model endpoint returned HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()
            choice = data["choices"][0]
            return choice["message"]["content"], choice.get("finish_reason")
        except requests.RequestException as e:
            last = str(e)
            time.sleep(1.5 * (attempt + 1))
    raise LLMError(f"Could not reach the model endpoint after {cfg.retries + 1} tries: {last}")


def extract_json(text):
    """Pull one JSON object out of a model answer (handles code fences and chatter around it)."""
    if text is None:
        raise ValueError("empty answer")
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, re.S)
    if m:
        t = m.group(1)
    else:
        i, j = t.find("{"), t.rfind("}")
        if i == -1 or j == -1:
            raise ValueError("no JSON object found in the answer")
        t = t[i:j + 1]
    return json.loads(t)


def _image_part(path):
    ext = os.path.splitext(path)[1].lower().lstrip(".") or "jpeg"
    mime = {"jpg": "jpeg"}.get(ext, ext)
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{b64}"}}


def ask_json(cfg, user_content, validate, max_repairs=2, log=print, label=""):
    """Ask, parse, validate; on problems send them back. Returns (obj, problems_left, trace)."""
    messages = [{"role": "system", "content": B.SYSTEM}, {"role": "user", "content": user_content}]
    trace = []
    obj, problems = None, ["no answer"]
    for attempt in range(max_repairs + 1):
        text, finish = chat(cfg, messages)
        trace.append({"label": label, "attempt": attempt, "finish": finish, "chars": len(text or "")})
        try:
            obj = extract_json(text)
            problems = validate(obj)
        except (ValueError, json.JSONDecodeError) as e:
            obj, problems = None, [f"The answer was not valid JSON ({e})." + (" The answer was cut off: write shorter points." if finish == "length" else "")]
        if not problems:
            return obj, [], trace
        log(f"  {label}: attempt {attempt + 1} had {len(problems)} problem(s); asking for a correction")
        messages += [{"role": "assistant", "content": text or ""},
                     {"role": "user", "content": "The checks found these problems. Return the FULL corrected JSON object, changing only what is needed:\n- " + "\n- ".join(problems[:15])}]
    return obj, problems, trace


def run_llm_engine(cfg, request, metrics, knowledge, style, other_brands_path=None, extra_brands=None, image_dir=None, log=print, max_repairs=2):
    """Returns (analysis dict or None, problems list, trace list)."""
    trace, problems_all = [], []
    reviews = {}
    cur = max(metrics["meta"]["years"])
    cur_names = [t["template"] for t in metrics["templates"] if t["year"] == cur]
    req = {t["template"]: t for t in request["templates"]}

    for name in cur_names:
        t = req[name]
        card = t.get("card", {})
        card_text = " ".join([str(card.get("title", "")), str(card.get("description", ""))])
        prompt = B.step1_prompt(request, t, knowledge)
        content = prompt
        if cfg.vision and image_dir and t.get("image") and os.path.exists(os.path.join(image_dir, t["image"])):
            content = [{"type": "text", "text": prompt}, _image_part(os.path.join(image_dir, t["image"]))]
        validate = lambda o, n=name, ct=card_text: C.check_template_fragment(o, n, request, style, other_brands_path, extra_brands, card_text=ct) if isinstance(o, dict) else ["not an object"]  # noqa: E731
        log(f"Step 1: reviewing {name}")
        obj, left, tr = ask_json(cfg, content, validate, max_repairs, log, label=f"step1:{name}")
        trace += tr
        if left or obj is None:
            problems_all += [f"{name}: {p}" for p in left]
            return None, problems_all, trace
        reviews[name] = obj

    log("Step 2: findings, hypotheses and recommendations")
    known = {g["id"] for g in knowledge.get("guardrails", [])}

    def assemble(g):
        a = {k: v for k, v in g.items() if k not in ("templates",)}
        a.setdefault("meta", {"title": f"{request.get('brand_display') or request.get('brand')}: Template Analysis and Input to the RCA",
                              "subtitle": f"Analysis of {len(cur_names)} templates", "deck_title": "RCS Template Analysis", "deck_subtitle": request.get("period", "")})
        a["templates"] = []
        for name in cur_names:
            rv = reviews[name]
            t = next(x for x in metrics["templates"] if x["template"] == name)
            sr = M.fmt_pct(M.shown_rate(t, "P2AResponseRate"))
            a["templates"].append({"template": name, "content_risk": rv["content_risk"], "content_risk_basis": rv.get("content_risk_basis", ""),
                                   "works": rv.get("works", []), "watch_outs": rv.get("watch_outs", []), "variations": rv.get("variations", []),
                                   "strip": f"Click rate {sr}; ReadRate {M.fmt_pct(M.shown_rate(t, 'ReadRate'))}",
                                   "speaker_note": f"Content risk {rv['content_risk']} was rated from the text and image alone. Click rate {sr} is shown for comparison."})
        for r in a.get("recommendations", []) or []:
            r.setdefault("guardrails", [])
        a.setdefault("yoy_commentary", [])
        a.setdefault("numbers_commentary", [])
        a.setdefault("external_numbers", [])
        return a

    def validate2(g):
        if not isinstance(g, dict):
            return ["not an object"]
        a = assemble(g)
        return [i["message"] for i in C.run_all(a, request, metrics, style, other_brands_path, knowledge, extra_brands)]

    prompt2 = B.step2_prompt(request, metrics, reviews, knowledge)
    obj, left, tr = ask_json(cfg, prompt2, validate2, max_repairs, log, label="step2")
    trace += tr
    if obj is None:
        return None, left, trace
    return assemble(obj), left, trace


def parse_manual(text, request, metrics, knowledge, style, other_brands_path=None, extra_brands=None):
    """For the manual route: the user pasted Copilot's single JSON answer (from brief.full_brief)."""
    obj = extract_json(text)
    a = {k: v for k, v in obj.items()}
    a.setdefault("meta", {"title": f"{request.get('brand_display') or request.get('brand')}: Template Analysis and Input to the RCA",
                          "subtitle": "", "deck_title": "RCS Template Analysis", "deck_subtitle": request.get("period", "")})
    for t in a.get("templates", []):
        tm = next((x for x in metrics["templates"] if x["template"] == t.get("template")), None)
        if tm:
            t.setdefault("strip", f"Click rate {M.fmt_pct(M.shown_rate(tm, 'P2AResponseRate'))}")
            t.setdefault("variations", [])
    a.setdefault("yoy_commentary", [])
    a.setdefault("numbers_commentary", [])
    a.setdefault("external_numbers", [])
    return a, [i["message"] for i in C.run_all(a, request, metrics, style, other_brands_path, knowledge, extra_brands)]
