"""
The open-source language model engine, behind any OpenAI-compatible endpoint: Groq, OpenRouter, Together, a Hugging Face
provider, or Ollama on your own machine. Nothing here is tied to one vendor, and it runs on Streamlit Cloud when the endpoint
is hosted.

Two steps (see brief.py): per-template content reviews with no performance data, then findings and recommendations.
Each answer is validated; if the checks fail, the problems are sent back and the model is asked to correct them.

Images: with vision switched on, each template's image is sent with its text in step 1. A model may refuse an image (for
example innerwear). That is not an error: the template is then reviewed from its text alone, and the run records that it was.
"""
import base64
import io
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
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


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


def chat(cfg, messages, json_mode=True, max_tokens=None):
    url = cfg.base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json", **cfg.extra_headers}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    body = {"model": cfg.model, "messages": messages, "temperature": cfg.temperature, "max_tokens": max_tokens or cfg.max_tokens}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    last = None
    for attempt in range(cfg.retries + 1):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=cfg.timeout)
            if r.status_code == 400 and "response_format" in body and "response_format" in r.text:
                body.pop("response_format", None)            # this provider has no JSON mode: ask without it
                continue
            if r.status_code == 400 and "max_tokens" in body and "max_tokens" in r.text and "max_completion_tokens" in r.text:
                body["max_completion_tokens"] = body.pop("max_tokens")   # some providers want the newer parameter name
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}"
                wait = 1.5 * (attempt + 1)
                ra = r.headers.get("Retry-After", "")
                if ra.isdigit() and int(ra) <= 30:
                    wait = int(ra) + 1
                time.sleep(wait)
                continue
            if r.status_code >= 400:
                raise LLMError(f"The model endpoint returned HTTP {r.status_code}: {r.text[:300]}", status=r.status_code)
            data = r.json()
            choice = data["choices"][0]
            return choice["message"].get("content") or "", choice.get("finish_reason")
        except requests.RequestException as e:
            last = str(e)
            time.sleep(1.5 * (attempt + 1))
    raise LLMError(f"Could not reach the model endpoint after {cfg.retries + 1} tries: {last}")


_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


def extract_json(text):
    """Pull one JSON object out of a model answer (handles reasoning tags, code fences and chatter around it)."""
    if text is None:
        raise ValueError("empty answer")
    t = _THINK.sub("", text).strip()
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


IMAGE_REFUSAL = re.compile(r"image|vision|multimodal|content[_ ]?(policy|filter)|safety", re.I)


def ping(cfg, with_image=False):
    """Tiny live test of the connection (and of image input). Returns (ok, message)."""
    if not cfg.ready():
        return False, "Base URL and model are not set in the secrets."
    try:
        text, _ = chat(cfg, [{"role": "user", "content": "Reply with the single word OK."}], json_mode=False, max_tokens=20)
    except LLMError as e:
        return False, str(e)
    msg = f"Connected to {cfg.host}, model {cfg.model} answered."
    if with_image:
        from PIL import Image
        bio = io.BytesIO()
        Image.new("RGB", (64, 64), (200, 30, 30)).save(bio, format="PNG")
        part = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(bio.getvalue()).decode()}}
        try:
            chat(cfg, [{"role": "user", "content": [{"type": "text", "text": "What colour is this image? One word."}, part]}], json_mode=False, max_tokens=20)
            msg += " Image input works."
        except LLMError as e:
            return False, msg + f" But image input failed: {e}"
    return True, msg


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


def ask_with_optional_image(cfg, prompt, image_path, validate, max_repairs, log, label):
    """Try with the image; if the endpoint refuses it, ask again with the text only. Returns (obj, left, trace, image_used)."""
    if cfg.vision and image_path and os.path.exists(image_path):
        try:
            content = [{"type": "text", "text": prompt}, _image_part(image_path)]
        except Exception:
            content = None
        if content is not None:
            try:
                obj, left, tr = ask_json(cfg, content, validate, max_repairs, log, label=label)
                return obj, left, tr, True
            except LLMError as e:
                if e.status in (400, 413, 415, 422) or IMAGE_REFUSAL.search(str(e)):
                    log(f"  {label}: the model could not use the image, so this template is reviewed from its text alone")
                else:
                    raise
    obj, left, tr = ask_json(cfg, prompt, validate, max_repairs, log, label=label)
    return obj, left, tr, False


def run_llm_engine(cfg, request, metrics, knowledge, style, other_brands_path=None, extra_brands=None, image_dir=None, log=print, max_repairs=2):
    """Returns (analysis dict or None, problems list, trace list). trace entries with 'note' record images that were not used."""
    trace, problems_all = [], []
    reviews = {}
    cur = max(metrics["meta"]["years"])
    cur_names = list(M.distinct(metrics, cur))
    req = {t["template"]: t for t in request["templates"]}

    for name in cur_names:
        t = req[name]
        card = t.get("card", {})
        card_text = " ".join([str(card.get("title", "")), str(card.get("description", ""))])
        prompt = B.step1_prompt(request, t, knowledge)
        img = os.path.join(image_dir, t["image"]) if (image_dir and t.get("image")) else None
        validate = lambda o, n=name, ct=card_text: C.check_template_fragment(o, n, request, style, other_brands_path, extra_brands, card_text=ct) if isinstance(o, dict) else ["not an object"]  # noqa: E731
        log(f"Step 1: reviewing {name}")
        obj, left, tr, used = ask_with_optional_image(cfg, prompt, img, validate, max_repairs, log, f"step1:{name}")
        trace += tr
        if cfg.vision and img and not used:
            trace.append({"label": f"step1:{name}", "note": "image not used (refused or unreadable); reviewed from text alone"})
        if left or obj is None:
            problems_all += [f"{name}: {p}" for p in left]
            return None, problems_all, trace
        reviews[name] = obj

    log("Step 2: findings, hypotheses and recommendations")

    def assemble(g):
        a = {k: v for k, v in g.items() if k not in ("templates",)}
        a.setdefault("meta", {"title": f"{request.get('brand_display') or request.get('brand')}: Template Analysis and Input to the RCA",
                              "subtitle": f"Analysis of {len(cur_names)} templates", "deck_title": "RCS Template Analysis", "deck_subtitle": request.get("period", "")})
        a["templates"] = []
        for name in cur_names:
            rv = reviews[name]
            st = M.card_stats(M.distinct(metrics, cur)[name])
            sr = M.fmt_pct(st["cr"])
            a["templates"].append({"template": name, "content_risk": rv["content_risk"], "content_risk_basis": rv.get("content_risk_basis", ""),
                                   "works": rv.get("works", []), "watch_outs": rv.get("watch_outs", []), "variations": rv.get("variations", []),
                                   "strip": f"Click rate {sr}; ReadRate {M.fmt_pct(st['rr'])}",
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


def rate_content(cfg, rows, knowledge, style, log=print, max_repairs=1):
    """Content-only rating of free-standing templates (used by the Gold set tab). rows: dicts with
    template, title, description, button, brand, vertical. Returns {template: {'content_risk':..., 'basis':...}}."""
    out = {}
    for r in rows:
        req = {"brand": r.get("brand", ""), "brand_display": r.get("brand", ""), "vertical": r.get("vertical", ""), "period": "", "issue": "Rate this template's content."}
        t = {"template": r["template"], "creative": r.get("creative_note") or "(no image description)",
             "card": {"title": r.get("title", ""), "description": r.get("description", ""), "button": r.get("button", "")}}
        card_text = f"{r.get('title', '')} {r.get('description', '')}"
        validate = lambda o, n=r["template"], ct=card_text: C.check_template_fragment(o, n, req, style, None, None, card_text=ct) if isinstance(o, dict) else ["not an object"]  # noqa: E731
        try:
            obj, left, _ = ask_json(cfg, B.step1_prompt(req, t, knowledge), validate, max_repairs, log, label=f"gold:{r['template']}")
        except LLMError as e:
            out[r["template"]] = {"content_risk": None, "basis": f"error: {e}"}
            continue
        out[r["template"]] = {"content_risk": (obj or {}).get("content_risk") if not left else None, "basis": (obj or {}).get("content_risk_basis", "")}
    return out
