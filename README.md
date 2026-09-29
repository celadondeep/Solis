# Solis AppDaemon — unified controls and predictive headroom

This branch preserves the Python application source running in Home Assistant
on 2026-09-12. The live checkout was based on `2597cbd9` and contained the new,
previously untracked `energy_system` package. This checkpoint makes the Eimo
migration reproducible without replacing the repository's main branch.

The migration changes `energy_system/profiles.py` and
`energy_system/supervisor.py`: Eimo reads the unified `solis` control entities
and its confirmation queue status. The rest of this snapshot is existing live
application code; it was not newly deployed as part of this migration.
Namai control behavior is unchanged by these two edits.

Home Assistant's package and custom integration are maintained in
[ha-config](https://github.com/celadondeep/ha-config). See
`docs/eimo_unified_solis_2026-09-12.md` there for the command timing,
verification results and remaining cloud issues.

The Eimo automatic power capability is enabled after the CID 5162 physical
OFF/ON cycle was verified on 2026-09-12. Manual and automatic writes require
a later real readback. The supervisor treats `cloud_backoff` as degraded.
The source snapshot passed Python compilation. The integration's independent
43 regression tests live in ha-config. Integration version 4.2.1 adds startup
silence to request monitoring and 300/600/1200-second recovery pauses.
The live Eimo executor was switched off at 09:03 LT on 2026-09-13 and back on
at 09:31:52. The timing review did not issue either of those changes and
preserves the latest live setting.

## 2026-09-13 predictive headroom

Model 4.3 adds 8 percentage points of planning headroom for **both** sites
(77% preferred upper SOC when hardware max is 100%). It anticipates capacity
pressure four hours ahead, budgets command setup time, protects P10 demand
until the next recharge opportunity and holds an outstanding cutoff stable.
Fresh measured PV has a bounded one-hour influence on the forecast.
No BMS protection or grid charging settings are modified.

Run `python -m unittest discover -s tests -p test_predictive_headroom.py`.
The 14 tests cover early action, cloudy-day restraint, grid/export guards,
night reserve, immutable outstanding targets, dawn budget and manual/storm
overrides. See ha-config `docs/energy_headroom_dynamic_2026-09-13.md` for
evidence, deployment hashes and rollback. Solis telemetry version 4.3.0
has a separate bounded dynamic read schedule; its 59 tests live in ha-config.

## 2026-09-16 execution recovery

Model 4.4 fixes night discharge continuation, grid-loss sleep protection,
stale plan explanations and leases, lingering daytime buffer requests and
sleeping-inverter execution diagnostics. The Eimo night setup allowance is
30 minutes (Namai 5 minutes), including the follow-up deployed September 13.
The planning headroom remains 8 additional SOC points.

Run `python -m unittest discover -s tests`: 21 planner regression tests.
The companion Solis integration 4.4.0 has 73 independent queue/API tests in
ha-config. See `docs/eimo_command_recovery_2026-09-16.md` in ha-config for
failure handling, deployment and actual live verification.

## 2026-09-17 reusable plant architecture and rolling consumption

Model 4.5 moves plant data into `energy_system/sites/*.json`, with shared
`site_defaults.json` and optional `portfolios.json`. `plant_apps.py` builds
the same manager, consumption and shadow classes for every registered plant.
The planner is shared; HA executes it with a Modbus or SolisCloud blueprint
from ha-config. Both adapters enforce the same renewable plan lease.

Both live dashboards use the same four views and actual power feedback.
There are no inverter serial numbers or site-specific entity maps in the
shared dashboard code. Physical capabilities, limits, state files, outputs,
finance membership and transport bindings belong to the site profile.
Legacy per-site wrapper files remain for compatibility, but apps.yaml does
not use them. Existing auxiliary home automations remain separately managed.

Consumption uses the last 30 completed local calendar days. Daily and weekday
graphs display observed means after isolated outliers are excluded. Hourly
means come from complete days of HA recorder hourly changes, with DST-aware
coverage checks. Forecasts apply a conservative weekday correction. A daily
00:20 refresh and at most one delayed retry use HA history, not SolisCloud.
An unavailable history query preserves the last usable forecast shape and
publishes data-quality status. Learned files are written atomically.

Run `python -m unittest discover -s tests`: 52 tests cover planning,
site isolation, profile reuse, rolling windows, DST and recorder recovery.
See [architecture and adding a plant](docs/plant_architecture.md).
