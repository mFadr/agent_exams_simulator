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
