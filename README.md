# RCS Template Analyzer (v2.1)

Upload the portal sheet, the card text and the screenshots. Pick an engine. Download a Word report, a PDF and a PowerPoint deck on the Vi template. The same app builds the training history for the in-house model and scores the engines against human ratings.

```
 browser (any laptop) -> password -> Streamlit app
   Tab 1  Analyse a brand   sheet + card text + screenshots -> metrics (code) -> engine -> check layer -> Word / PDF / PowerPoint
   Tab 2  Training data     5 bundled brands + any you import -> readiness -> trained component (only if it earns it)
   Tab 3  Gold set          blind rating sheet -> human ratings -> scorecard for the engines
```

## What is in this version
- **Password login.** The app refuses to start without `APP_PASSWORD`.
- **Check layer.** Before any file is built it blocks: a number that is not computed from the sheet, another brand's name (every brand in the training data is blocked automatically), internal classification text, a missing template review, a missing "directional" caveat, an unknown guardrail ID. Editing the analysis re-runs the checks.
- **Two engines, same task, same checks.**
  - **Internal ML model.** Scores each template's card text only (it never sees click data) and recommends changes. A *trained component* learns from the training history, but switches on only when the history holds 40+ distinct texts from 4+ other brands **and** it beats guessing on brands it has not seen. Until then the Engines tab says "Trained component: insufficient" and text scoring does the work. With today's bundled data (5 brands, 25 distinct texts) it is off.
  - **Open-source language model**, hosted, behind any OpenAI-compatible endpoint, so it runs on Streamlit Cloud. Step 1 reviews each template from its text and image with **no performance data**; step 2 writes findings and recommendations from the computed numbers. Failed checks go back to the model for correction, up to twice. A refused image falls back to text only for that template.
  - **Both, and compare**: the app shows where they differ on content risk and keeps the more cautious rating.
- **Vi PowerPoint template bundled** (`assets/vi_template.pptx`). The sidebar lets you upload a different template for one run; if it lacks the Vi layouts the app says so and uses the bundled one.
- **Training data bundled** (`data/seed`): Nykaa, bigbasket, Victoria's Secret, Myntra, Axis Bank. They load at every start, so a restart of the hosted app does not lose them.

## The language model: what I recommend (checked October 2026)

**Groq free tier, model `qwen/qwen3.8-27b`.** It is OpenAI-compatible, needs no card, and Groq's vision docs list it as accepting images (up to 3 per request, 20 MB each). Published free limits: 30 requests a minute, 250 a day. One analysis makes (number of templates + 1) calls, so a 5-template brand uses about 6, which allows roughly 40 analyses a day. Add `vision = true` to the secrets and the model reads each template's image.

| Option | Free? | Image input | Notes |
|---|---|---|---|
| **Groq + `qwen/qwen3.8-27b`** (recommended) | Yes, no card | Yes | Fast. Model names change: confirm at console.groq.com/docs/vision. |
| OpenRouter + `google/gemma-4-31b-it:free` or `qwen/qwen3.8-27b:free` | Yes | Yes | 20 requests a minute, **50 a day** (1,000 after a one-time $10 top-up). The free list changes often. Backup only. |
| Hugging Face Inference Providers | Only $0.10 of credit a month | Some models | Not really free any more. |
| Ollama on your Mac | Yes | Yes (a vision model) | Free and private, but Streamlit Cloud cannot reach your Mac. Fine for local runs: `base_url = "http://localhost:11434/v1"`. |

Things I could not verify, so check them yourself: that the Qwen 3.8 weights carry a licence you are happy to call "open source" (read its model card), and what Groq's free-tier terms say about how prompts are used. Brand text, numbers and images go to the provider, which is why the app asks for a tick-box every run. **Pin the model name**: if it changes, results change and any gold-set score you measured no longer applies. The first run with a real key is the first real test of this path (the tests use a mock server): use the sidebar's **Test the connection** button, which checks both text and image input.

## Run it on a Mac
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
brew install --cask libreoffice          # PDF export (Word is not used on a Mac)
mkdir -p .streamlit && cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# edit .streamlit/secrets.toml: APP_PASSWORD and the [llm] block
streamlit run app.py
```

## Put it on GitHub (private) and reach it from any laptop
```bash
git init && git add . && git status     # confirm .streamlit/secrets.toml is NOT listed
git commit -m "RCS Template Analyzer v2.1"
# create an EMPTY PRIVATE repository on github.com, then:
git branch -M main && git remote add origin https://github.com/<you>/<repo>.git && git push -u origin main
```
**The repository must be private.** `data/seed` holds real brand performance data and card text, and `assets/vi_template.pptx` carries a Vodafone Idea sensitivity label.

**Streamlit Community Cloud:** share.streamlit.io, New app, pick the repo, branch `main`, main file `app.py`. In Advanced settings > Secrets paste your `secrets.toml`. `packages.txt` installs LibreOffice for the PDF step. Check that your plan lets you deploy from a private repository; if not, use the Docker route below. **Inside your own tenant:** `docker build -t rcs-analyzer .` and run it on any container host with `APP_PASSWORD`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `LLM_VISION` as environment variables.

## Adding brands to the training data
Same export as for an analysis, with `template title`, `template body`, `CTA` (and optionally `exact text`, `exclude`, `exclude reason`). Include **every** template in the period, not only the good ones. Either import in the Training tab (lasts for the session) or, to keep it, put the workbook in `data/seed/` and add a line to `data/seed/seed.json`. Cohort rules (`offline=offline`) are for lists you actually know about; brands do not share targeting, so the app never guesses one.

How the training rows are built: counts (not rates), so the click-rate base is known; each row is compared with the brand's *other* texts in the same cohort and year (leave-one-out), so one huge template cannot be its own baseline; templates with the same text are one text group and are never split between training and test; rows are weighted by their clicks. Thresholds (40 texts, 4 brands) are my proposals, not evidence-based.

## Files you will edit
| File | What it controls |
|---|---|
| `config/rules.json` | The scoring checks, weights, wording, thresholds, dos and don'ts. Retune from the Training tab's "do the checks hold across brands" table and the Gold set scorecard. |
| `knowledge/knowledge.json` | Vi guardrails and verified platform facts. **The eight guardrails are marked `to_be_vetted`: have their owner confirm them.** |
| `config/sources.json` | Column names of the portal export (aliases for each). |
| `config/style.json` | Fonts, colours, blocked internal phrases. |
| `data/seed/seed.json` | Which workbooks load at start-up, with brand, vertical and cohort rules. |

## Tests
```bash
python -m unittest discover -s tests        # about 90 seconds; needs LibreOffice for the PDF tests
```
131 tests. They cover the metrics, the importer and the real bundled workbooks, the baseline maths, the checks firing on planted faults, the language-model flow against a local mock server (wrong-number correction, refused image, rate limit, reasoning tags, connection test), the bundled and uploaded templates, and the login and all three tabs driven headlessly.

## Not in this app
Email pickup, automatic portal pulls, sending files to reviewers and replying to the brand (Branches 1 and 3: Power Automate). Image analysis is only what the language model does when it reads an image; the trained component reads text only.

## Known limits
- Each send has its own audience and timing, which the sheet does not show, so every output says findings are directional.
- The sheet holds no visits, orders, audience or send time.
- The Internal ML model reads text only. On the first BFSI brand it rated all four templates Low although click rates differed about 5x: it cannot see an ill-fitting CTA or an offer that lives only in the image. The language model, with vision on, can look at the image.
- The check layer confirms that a real number exists in the computed results, not that it is attached to the right thing. Whole-number percentages are its weakest case.
- The eight Vi guardrails are unvetted; the scoring checks were first written while analysing Nykaa, so for Nykaa the app says its match is not an independent test.
- More than about six templates makes a long report and deck.
