# RCS Template Analyzer

Upload the portal sheet, the card text and the screenshots. Pick an engine. Download a Word report, a PDF and a PowerPoint deck in the same format as the Nykaa and Axis work.

```
 browser (any laptop)
      |  password
      v
  Streamlit app  ──►  metrics (code: every number)  ──►  checks (numbers, brands, internal marks, caveat)  ──►  Word / PDF / PowerPoint
                          ▲                                   ▲
                          │                                   │
        Engine A: rules + statistics (no model)     Engine B: open-source language model (two steps)
```

Nothing is stored on the server. Files are made in memory and handed back to you. The only thing you keep between runs is `history.csv`, which the app gives you to download.

## Read this before using real brand data

This app handles brand performance data, partner material and (optionally) the Vi PowerPoint template, which carries a Vodafone Idea sensitivity label. I could not find a Vi policy on external AI or cloud tools. **Get InfoSec's answer before deploying with real data.** In particular:

| Question | Why it matters |
|---|---|
| May this data sit on Streamlit Community Cloud, or must the app run inside Vi's tenant? | Use the `Dockerfile` to host it in your own cloud instead. |
| May brand text and numbers be sent to the language-model provider you configure? | The app asks for a tick-box confirmation every run and names the host. Choose a provider with no retention or training on API data, or run Ollama / vLLM on your own server. |
| Is the repository private? | It must be. It holds the guardrail wording and names a client in `config/rules.json` (`designed_with`). Never commit `secrets.toml`. |
| Is a password enough? | `APP_PASSWORD` is mandatory (the app refuses to start without it). Add the platform's own viewer restriction too, if your plan offers it. The wrong-password lockout is per browser session only. |

## What the two engines do (and do not do)

**Engine A, data-driven (no language model, no network).**
- Scores each template's **text only** with explicit rules in `config/rules.json` (no offer named, chance-based wording, urgency with nothing named, a floor or ceiling instead of an offer, long text, many variables, and so on).
- Then tests those rules against click rate inside the run: how often the text-only score orders templates the same way as click rate, within the same period.
- Tests each recommended rule against the run's own click data. If the data points the other way, the recommendation says so and is marked **Test**, not **Now**.
- A trained model (ridge regression on text features, target = click rate relative to the brand's own average) is used **only** when the history file holds at least 40 usable templates from at least 4 *other* brands, and only if it beats guessing when tested on brands it has not seen. With a handful of templates a model would learn noise, so below that threshold the app says so and does not use one.
- The rules are first guesses. They were written while analysing Nykaa, so for Nykaa the match is **not** an independent test, and the engine says that. Tune the weights from the evidence on other brands.
- It does not write rewritten card variations. Those come from the language-model engine.

**Engine B, open-source language model.** Any OpenAI-compatible endpoint (Groq, Together, Hugging Face, or your own Ollama / vLLM).
- Step 1, one call per template: the model sees the card text (and the image only if `vision = true`) and **no performance data**, then rates content risk and writes the review and two rewritten variations. This enforces "rate content before seeing results" in code.
- Step 2, one call: findings, hypotheses and recommendations, using the computed numbers and the step 1 reviews.
- Every answer goes through the same checks as Engine A. If the checks fail, the problems are sent back and the model is asked to correct them, up to twice.
- Models differ in how well they follow this. Test yours on a brand whose answer you know before trusting it.

**Both:** the two answers share one schema and the same checks, but they will not be word-for-word identical. The app shows where they differ on content risk, uses the language model's text and keeps the more cautious rating, and writes the disagreements into the review notes.

**Manual:** make the brief, paste it into Copilot chat with the screenshots, paste Copilot's JSON answer back. No external model is called.

## Checks that run before any file is made

1. The analysis covers every template, with valid ratings and priorities.
2. Every number in the text is computed from the sheet, quoted from the request or card text, or declared with its source. Anything else blocks the build.
3. No internal classification text, and no other brand's name, anywhere in the text or image names.
4. When every template is a single send date, the analysis must say findings are directional.
5. A recommendation may cite only guardrail IDs that exist in `knowledge/knowledge.json`.

**What the checks cannot do:** confirm that a real number is attached to the right thing (whole-number percentages are the weakest case), or that a claim about phone behaviour is true. A person must read the content review and recommendations before anything goes to a brand.

## Run it on your Mac

```bash
# 1. Python 3.11 or 3.12
python3 --version

# 2. In the project folder
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. PDF export needs LibreOffice (Word is not used on a Mac)
brew install --cask libreoffice

# 4. Secrets
mkdir -p .streamlit && cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# edit .streamlit/secrets.toml: set APP_PASSWORD, and the [llm] block if you want Engine B

# 5. Start
streamlit run app.py
```

## Put it on GitHub (private) and reach it from anywhere

```bash
git init
git add .
git status          # confirm .streamlit/secrets.toml and any .xlsx are NOT listed
git commit -m "RCS Template Analyzer"
# create an EMPTY PRIVATE repository on github.com, then:
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

**Streamlit Community Cloud:** share.streamlit.io, New app, pick the repo, branch `main`, main file `app.py`. Open Advanced settings and paste the contents of your `secrets.toml` into Secrets. `packages.txt` installs LibreOffice for the PDF step. Check the current plan's rules for private repositories and for restricting who can open the app.

**Inside your own tenant instead:** `docker build -t rcs-analyzer .` then run it on Azure Container Apps (or any container host) with `APP_PASSWORD`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` as environment variables.

## Secrets

```toml
APP_PASSWORD = "a long passphrase"
OTHER_BRANDS = "Brand A, Brand B, Brand C"   # names that must never appear in another brand's report

[llm]
base_url = "https://api.groq.com/openai/v1"  # any OpenAI-compatible endpoint
api_key  = "..."
model    = "..."                             # use the provider's current model name
vision   = false                             # true only if the model accepts images
```

## Files and settings you will edit

| File | What it controls |
|---|---|
| `config/rules.json` | The text rules, their weights and wording, thresholds, dos and don'ts. |
| `knowledge/knowledge.json` | Vi guardrails and verified platform facts, each with an ID and source. **The guardrails are marked `to_be_vetted`: have their owner confirm them.** |
| `config/sources.json` | Column names of the portal export. Add a profile for another source. |
| `config/style.json` | Fonts, colours, the list of blocked internal phrases. |
| `assets/vi_template.pptx` | Optional. Add it only to a private repository and only if InfoSec agrees. Otherwise upload it in the sidebar each session. Without it the deck uses a plain design. |

## Training history

Each run offers `history.csv`: one row per template (text features, click rate and click index). Keep it yourself, for example in OneDrive, and upload it next time. The app does not store it. The trained model will switch on by itself once the history is large enough and passes its own test.

## Tests

```bash
python -m unittest discover -s tests
```

58 tests, all on synthetic data (no real brand data is in this repository): metrics against the counts, the text features, the rules, each guard firing on a planted fault, the trained model on planted signal and on noise, the language-model flow against a local mock server (including a wrong-number answer that must be corrected), engine disagreement, the build, and the login and upload page driven headlessly.

## Not in this app

Picking up the request email, pulling the portal data automatically, sending files to internal reviewers, and replying to the brand once they approve. Those are the other branches of the plan and need Power Automate or a mail service with approved credentials.

## Known limits

- Comparisons are by calendar year. Several months inside one year are treated as one period.
- Each template is usually a single send date, so audience, day of week and frequency cannot be separated from the creative. Every output says findings are directional.
- The sheet holds no visits, orders, audience or send time, so the app cannot explain what happened after the click.
- More than about six templates makes a long report and deck.
