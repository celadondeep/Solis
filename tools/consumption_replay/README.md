# Offline consumption model research

This tool compares fixed, lightweight prototype settings. It never accesses Home Assistant or changes plant control. Run it in a separate research environment with Python, numpy and scikit-learn; these are not new AppDaemon dependencies.

From the Solis repository root:

```sh
python tools/consumption_replay/compare.py \
  --data-dir /path/to/private-inputs \
  --start 2026-08-08 --recent-start 2026-09-04 \
  --output /path/to/results.json
```

Each input JSON represents one plant. Required fields:

- `baseline_method`: `mean30` or `mean30_wd`.
- `history`: dated `{"date":"YYYY-MM-DD","kwh":12.3}` observations.
- `hourly_days`: mapping from local date to complete-day objects containing `kwh`, `hours` (24 local-clock buckets), and `counts` (physical hour counts; DST can produce 0 or 2).
- `invalid_days`: optional mapping of dates to known measurement-quality reasons. Document each exclusion independently of forecast error.

Only validated, complete target days should enter `hourly_days`. No dates are zero-filled. A forecast for D sees completed history only through D−2, matching the first next-day forecast produced on D−1. Training examples reconstruct that same availability boundary. All candidate metrics use paired target dates; prototypes without enough training data exclude that date for every candidate.

Candidates: the configured baseline, exponential level smoothing, damped Holt trend, level plus adaptive weekday effects, ridge regression with past consumption/calendar features, small boosted trees, and an equal combination of baseline/level/ridge. Settings are fixed in code; results do not establish which algorithm would win after a separate, well-designed tuning study.

The September 18 comparison reuses a period already inspected while developing V4. It is exploratory, not a new untouched test set. A deployment decision needs future frozen predictions and reliable actual measurements. Household histories and raw input exports are intentionally kept out of this repository.

Methods: [rolling-origin evaluation](https://otexts.com/fpp3/tscv.html), [damped Holt trend](https://otexts.com/fpp3/holt.html), [lagged features and time-aware evaluation](https://scikit-learn.org/stable/auto_examples/applications/plot_time_series_lagged_features.html), [forecast combinations](https://otexts.com/fpp3/combinations.html).
