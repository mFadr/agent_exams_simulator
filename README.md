<<<<<<< HEAD
# Flight Price Predictor

A local web app (Dash + scikit-learn) that learns from scraped flight prices and forecasts the
**price level of every day in a month** for a chosen route. It shows the forecast as a colour-coded
calendar that changes with the **travel year** (e.g. 2026 / 2027 / 2028) and with **how many months
before departure the ticket is bought** (11 → 0 months in advance).

Each step of the ML pipeline (data preparation, train/test split and cross-validation, regression,
ensemble models) has its own tab with charts and tables, so you can see how the prediction is built.

---

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
python app.py
```

Open **http://127.0.0.1:8050**.

On startup the app trains all models on `PRG_rep.csv`, which takes a short while. Results are
cached in memory for each dataset, so switching tabs afterwards is fast.

### Requirements

- Python 3.10+
- `dash`, `pandas`, `numpy`, `scikit-learn`, `plotly` (see `requirements.txt`)

---

## Project structure

```
flight_price_predictor/
├── app.py            # Dash web app: layout, tabs, charts, callbacks
├── ml_pipeline.py    # ML pipeline: cleaning, features, CV, models, calendar forecast
├── assets/style.css  # App styling (loaded automatically by Dash)
├── PRG_rep.csv       # Default dataset: flights from Prague (PRG)
├── uploads/          # Datasets uploaded through the UI (e.g. VIE_rep.csv, flights from Vienna)
├── requirements.txt
└── prompt1.txt       # Original project brief
```

---

## Input data

The CSV files have **8 columns** and may or may not include a header row:

| # | Column       | Example      | Scale            |
|---|--------------|--------------|------------------|
| 1 | scrape date  | `2025-09-01` | date             |
| 2 | flight date  | `2026-01-01` | date             |
| 3 | origin       | `PRG`        | nominal          |
| 4 | destination  | `AMS`        | nominal          |
| 5 | departure    | `09:20 AM`   | ordinal (time)   |
| 6 | duration     | `1h 45min`   | ratio            |
| 7 | price        | `US$59.47`   | ratio (target)   |
| 8 | airline      | `Easyjet`    | nominal          |

Each row is the cheapest offer found on the *scrape date* for a flight on the *flight date*.
`PRG_rep.csv` has about 21,500 rows. Its routes are PRG → AMS, ATH, BCN, CPH, FCO and LON, the
flight dates are in January 2026, and the prices were scraped from September 2025 to January 2026.

You can choose another dataset from the **Dataset** dropdown, or upload a CSV in the same format.
Uploaded files are saved to `uploads/`.

---

## Pipeline

The app follows the steps in `prompt1.txt`. Steps 2–4 have their own visualisations, which is step 5
of the brief.

### 1 · Data preparation (`prepare_data`)

This step unifies the raw columns and repairs dirty rows. Every fix is counted in a **cleaning log**.

- **Merged cells:** values such as `US$36.92Wizz Air` are split back into price and airline.
- **Corrupted scrape dates:** values such as `20102-110-01` are repaired by forward fill. This works
  because the file is ordered by scrape date.
- **Prices:** both `US$59.47` and `$59.47` become `59.47`. The model predicts `log(1 + price)`.
- **Departure times:** 12-hour (`09:20 AM`) and 24-hour (`17:05`) times are both converted to minutes
  after midnight. They are also mapped to an ordinal part of day (night < morning < afternoon < evening).
- **Duration:** `1h 45min` becomes `105` minutes.
- **Airlines:** names are title-cased, and airlines with less than 0.5 % of rows are grouped as `Other`.
- **Removed rows:** missing values, duplicates, rows scraped after departure, and extreme price
  outliers per route (log-IQR rule, k = 3).
- **New features:**
  - `days_before`: lead time in days
  - day of month and day of week
  - weekend flag
  - holiday flag, and the distance in days to the nearest fixed CZ/European holiday

### 2 · Train/test split & cross-validation (`split_and_cv`)

- **Hold-out test set:** 20 % of *flight days per route*, made with `GroupShuffleSplit`. All
  observations of a test day stay out of training, so the test measures how well the model
  generalises to days it has never seen.
- **Three CV schemes are compared on a Ridge baseline:**
  - random **K-Fold**
  - **Group K-Fold** by flight day (used for model selection)
  - **Time-Series split** by scrape date

### 3 · Regression (`train_models`)

- **Ridge regression** (α = 1) on `log(1 + price)`.
- Nominal features are one-hot encoded and numeric features are standardised.
- A squared lead-time term models the booking curve.
- The tab shows the coefficients and predicted vs. actual prices.

### 4 · ML models

- **Random Forest:** a grid search over `max_depth` × `min_samples_leaf`, then a tuned model with
  250 trees.
- **Histogram Gradient Boosting.**
- **Naive baseline:** the mean price per route and lead week.
- The model with the lowest cross-validated MAE is used for the calendar.
- **Requirement checks** confirm that the selected model:
  - beats the Ridge and naive baselines
  - has a test MAPE below 25 %
  - actually uses lead time and day-level calendar features
  - is stable across CV folds
- **Feature importances** are summed back to the original columns.
- **NN export:** you can download the data ready for a neural network:
  - one-hot encoded and standardised features
  - the same train/test split and CV folds
  - unencoded features
  - a JSON file with the scaler and encoder parameters

  Only the training rows are used to fit the scaler and encoder.

### 6 · Price calendar (`predict_calendar`)

This tab is the final prediction chart.

**Controls:**

| Control | What it does |
|---|---|
| **Route** | Origin → destination |
| **Travel month** | Month of the flight |
| **Model** | Which trained model makes the forecast |
| **Travel year** | Slider for the base year and the next two years |
| **Months in advance** | Slider from 11 down to 0 |
| **Year-over-year growth** | 0–10 % price growth per year |
| **Far lead times** | How to handle lead times beyond the data: *flat* or *trend* |

**Output:**

- **Calendar heatmap:** one cell per day, with the predicted price and a Low / Medium / High label.
  The colour scale stays the same while you move the months-in-advance slider, so you can compare
  calendars directly.
- **Booking curve:** the average, minimum and maximum price for the month at each months-in-advance
  value.
- **Summary cards:** month average, cheapest day, most expensive day, and the best time to buy.

**How a day is predicted:** the model doesn't know which airline or departure time will be the
cheapest on a future day. So for each day it predicts the price for the route's most frequent flight
profiles (airline, departure time, duration) and averages them, weighted by how often each profile
occurs.

---

## Limitations

- **One year of data.** The dataset covers a single year (2026 flights). Later years reuse the same
  model with that year's weekday and holiday calendar, and apply the year-over-year growth you set.
- **Short lead times.** The data has a maximum lead time of about 4–5 months. Longer lead times are
  extrapolated: either held flat, or by continuing the log-linear trend at the far end of the booking
  curve (limited to ±30 %). In the app, extrapolated values are marked with `*` and open markers.
- **Fixed holidays only.** The holiday features use fixed dates. Movable holidays such as Easter are
  not included.
- **One currency.** Prices are treated as USD.
=======
<<<<<<< HEAD
# Agent-  Exam Simulator

A Windows desktop application (Python + PySide6) for realistic exam practice. It
combines **local AI models (Ollama)**, an optional **cloud AI provider (any
OpenAI-compatible API)**, and **deterministic grading**.

It works without any AI: you can create exams from the built-in question bank or
your own questions, take timed exams, and get scored results. AI adds question
generation, rubric grading of written answers, explanations and hints.

---

## 1. Installation (Windows 10/11)

Requires **Python 3.11+** (tested with 3.13).

```powershell
cd path\to\ag_exam
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

On first start the app creates `%APPDATA%\AIExamSimulator\` (database, config,
logs, exports, backups) and installs 5 exam presets and 30 sample questions
covering all 12 question formats. To use a different folder, set
`AIES_DATA_DIR` (see `.env.example`).

## 2. Local AI with Ollama (optional, private, free)

1. Install Ollama from <https://ollama.com/download> and start it (it runs in the system tray).
2. Download a model in a terminal, e.g.:

   | Model | Download | Notes |
   |---|---|---|
   | `ollama pull llama3.2:3b` | ~2 GB | runs on most PCs (8 GB RAM), basic quality |
   | `ollama pull qwen2.5:7b` | ~4.7 GB | 16 GB RAM recommended, good structured output |
   | `ollama pull llama3.1:8b` | ~4.9 GB | 16 GB RAM recommended |

3. In the app, open **AI Settings**, click **Check Ollama**, choose the model and click **Save settings**.

You don't need a GPU, but it is much faster with one. Without a GPU, a batch of
4 questions on an 8B model can take several minutes. Larger models give better
questions.

## 3. Cloud AI (optional)

In **AI Settings → Cloud AI**:

1. Choose a provider preset (OpenAI, Anthropic's OpenAI-compatible endpoint, OpenRouter, Groq,
   Mistral) or **Custom** and enter any OpenAI-compatible `/v1` endpoint (HTTPS required except for localhost).
2. Paste your API key and click **Save key**. It is stored in the **Windows Credential Manager**.
   It is never written to the config file, database, backups, exports or logs, and log output is
   passed through a redaction filter.
3. Enter a model name or click **Fetch models**, then **Test connection**.
4. Under **Cloud permissions**, tick which operations may use the cloud. Sending study-material
   excerpts to the cloud is a separate setting and is off by default.

Before each operation that could send data to the cloud, the app shows a dialog that says what
will be sent. You can choose *Allow*, *Use local model only* or *Cancel*. The **AI usage log**
records metadata only (time, operation, provider, success, number of characters sent to the cloud).

## 4. AI modes

| Mode | Behaviour |
|---|---|
| **Local only** | Every AI operation runs through Ollama. Nothing leaves the PC. |
| **Cloud only** | Every AI operation uses the cloud provider (with consent). |
| **Hybrid (automatic)** | Routine work (generation, explanations, hints) uses the local model first. Grading of essays/code and hard-difficulty generation use the cloud first. If the first provider fails, the other one is tried. Every fallback is shown in the UI; the app never switches to the cloud without consent. |
| **Manual** | You pick one provider. Some screens also allow a per-operation override. |

**Objective questions are always scored by deterministic code, never by AI.**

## 5. Features

- **Exam wizard**: start from a preset (Quick Practice, Standard, Difficult, Mock Final, Custom) or a
  saved template. Configure subject (any subject), topics, number of questions, difficulty, duration,
  pass mark, negative marking, partial credit, question types, language, attempts, shuffling,
  revisiting, unanswered questions, permitted tools (calculator / notes / formula sheet) and when
  feedback is shown.
- **Modes**: Fixed, Adaptive, Weakness practice, Revision (questions you got wrong before),
  Mixed mock, and Practice (immediate feedback, hints, retry, similar questions; not counted in statistics).
- **12 question formats**: single choice, multiple select, true/false, short answer, fill-in-the-blank,
  numerical (tolerance + units), math problem (SymPy symbolic equivalence), programming (test cases),
  essay, case study, matching, ordering.
- **Question quality control**: every AI question is checked against a schema and rules before use
  (answer key consistency, a single correct option, a rubric where needed, blank count, missing
  figures/code, near-duplicates). An optional second check has the model solve objective
  questions independently, and SymPy recomputes numerical answers. Questions that disagree are
  **flagged**, and you can edit or regenerate them before the exam starts.
- **Exam interface**: countdown based on a stored absolute deadline (restarting the app does not
  add time), question navigator with answered/flagged states, flags, confidence ratings, progress bar,
  submit confirmation, automatic submission when time runs out, autosave on every change,
  recovery after a crash or restart, and an optional full-screen mode.
- **Grading**: deterministic objective scoring. Numerical answers use tolerance, unit
  conversion and error diagnosis (sign, order of magnitude, rounding, missing unit). Math answers use
  symbolic equivalence. Code is run against test cases in a restricted subprocess (opt-in). Essays and
  case studies get rubric-based AI evaluation: the AI scores each criterion and the app adds up the
  points. Low-confidence AI grading is marked for review, and every grade can be overridden manually.
- **Results**: score, percentage, pass/fail, and counts of correct/partial/incorrect/unanswered answers,
  broken down by topic, question type and difficulty. For each question: your answer, the correct
  answer, how it was graded, rubric criteria, error types, explanation, recommended action and source
  reference. Filters let you show all, incorrect, unanswered, flagged or needs-review questions. You can
  ask for an AI explanation of a mistake (labelled as AI-generated). Export to PDF/CSV, and start a
  follow-up exam on weak areas.
- **Study materials**: import PDF, DOCX, TXT, Markdown or pasted text. Page/section references are kept,
  and you can include or exclude individual sections. Image-only pages are detected and reported (OCR
  is optional). The app detects when the original file is missing or has changed. When generating
  questions from your materials, only the relevant excerpts are sent to the model (TF-IDF ranking,
  6,000-character limit), never the whole document.
- **Question bank**: search and filter (subject, type, difficulty, origin, review status, collection),
  pagination, editor for all formats, version history (edits never change completed exams, because each
  exam keeps its own copy of its questions), collections, AI regenerate/verify, and JSON import/export
  plus CSV export. Imported files are validated, and any "verified" status in them is reset.
- **Statistics & learning profile**: score history, accuracy by topic and difficulty, correct vs
  incorrect, time per question type, and date/subject filters. The weakness analysis distinguishes
  *not enough data*, *one-time mistake*, *recurring weakness*, *developing* and *mastered*, using
  Wilson confidence intervals. It also detects patterns: recognising answers vs explaining them,
  concepts vs calculations, drop-off at higher difficulty, recurring unit/sign errors, and
  overconfidence. Each pattern shows its evidence and is marked as tentative or strong.
- **Your data**: backup and restore (zip), full JSON export, delete history, light/dark theme.

## 6. Running the tests

```powershell
.venv\Scripts\activate
$env:QT_QPA_PLATFORM="offscreen"
python -m pytest tests -q
```

The test suite uses mock AI providers and mocked HTTP. It does not need an API key, Ollama or a
display. It covers: scoring and partial credit, numerical tolerance, units and symbolic equivalence,
question validation and duplicate detection, AI output conversion, routing, fallback, consent and
retries, Ollama/cloud error mapping (401/404/429/5xx, network errors), rubric parsing and deterministic
aggregation, timer, expiry, autosave and recovery, migrations and corrupt databases, document import
errors and OCR detection, retrieval, adaptive selection and weakness detection, CSV/PDF/JSON export,
backup/restore, secret redaction, restricted code execution, the eight required end-to-end workflows,
and a UI smoke test that takes and submits an exam.

**Tested but not automated**: question generation against a real Ollama model (`llama3.1:8b`) was
run manually during development. It produced 4 validated questions in about 150 s on CPU, and the
verification pass correctly flagged one question whose answer key was wrong. Cloud providers were
tested only against mocked HTTP; no real API key was available.

## 7. Packaging (PyInstaller)

```powershell
pip install pyinstaller
pyinstaller ai_exam_simulator.spec
# → dist\AIExamSimulator\AIExamSimulator.exe
```

Use the one-folder build because it starts faster. In a frozen build, code execution for
programming questions needs a separate Python interpreter on `PATH`. If none is found, the feature
explains that and falls back to rubric grading. To make an installer, wrap `dist\AIExamSimulator`
with Inno Setup or WiX. Code-sign the exe to avoid SmartScreen warnings.

## 8. Project structure

```
main.py                       entry point
ai_exam_simulator/
  config/    settings.py (pydantic settings, data paths, atomic writes), defaults.py
  app/       application.py (dependency-injection container), navigation.py
  ai/        provider_interface, ollama_provider, cloud_provider, hybrid_router, prompt_templates,
             question_generator, answer_evaluator, json_utils, mock_provider (tests)
  exams/     question_models, validation, exam_engine (session state machine), exam_builder, scoring
  grading/   objective_grader, numerical_grader (SymPy), code_grader, rubric_grader, grading_service
  learning/  analytics, weakness_detection, adaptive_engine
  documents/ text_extraction, importer, source_retrieval, question_bank_io
  storage/   database (SQLite/WAL), migrations (PRAGMA user_version), repositories
  reports/   report_generator, export_service (CSV/PDF/JSON/backup)
  security/  secrets (Credential Manager), redaction, validation
  ui/        main_window, dashboard, exam_setup, exam_session, answer_widgets, results_view,
             question_bank, question_editor, materials_view, statistics_view, settings_view, widgets, theme, workers
  data/      sample_questions.json
tests/       automated test suite
```

## 9. Known limitations

- **AI quality depends on the model.** Small local models sometimes write weak or wrong questions.
  Validation and the verification pass catch many of these, but not all. *Unverified* only means the
  question passed the rule checks; *tool-verified* means the numerical key matches its own solution
  expression, not that the question itself makes sense. Review generated questions before important
  practice.
- **AI rubric grading can be wrong.** It is labelled everywhere, uncertain grades are marked for
  review, and you can override any score.
- **Code execution is not a security sandbox.** It runs in a separate isolated Python process with a
  time limit, no environment variables, an empty working folder, restricted builtins and an import
  whitelist. Python-level restrictions can be bypassed, and Windows has no simple unprivileged OS
  sandbox. For this reason execution is **off by default**, and the app shows a warning before you
  turn it on.
- **OCR** needs Tesseract OCR and `pip install pytesseract`. Without them, scanned pages are reported
  as not processed.
- **No anti-cheating.** Full-screen mode only reduces distractions; it does no monitoring.
- Images and diagrams inside documents are not interpreted. Questions cannot contain images.
- If Windows Credential Manager is unavailable, the API key is kept in memory for the current session
  only, and the app tells you so.
- Cloud model names are not hard-coded. Enter one or use **Fetch models**.

*Built by Matěj Fadrhons  [github.com/mFadr](https://github.com/mFadr)*
=======
# agent_exams_simulator
>>>>>>> f823e4b85469d66e08c4773286fdd2d17f991692
>>>>>>> 9c2bb0da89ad0830eb431d3d13662c1ab2d8ea26
