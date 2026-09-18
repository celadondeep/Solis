# Eimo daytime discharge incident — 2026-09-18

## Cause and evidence

At 17:19 Europe/Vilnius, Eimo's plan requested Self-Use with the discharge
slot disabled, although battery SOC was 100% and the configured preferred
upper band was 77%. The existing discharge time read back as 00:00–23:59.
Cloud telemetry was fresh and the command queue was following that plan.
This incident was a planner decision error, not evidence of a lost slot-time
write or a consumption-model failure.

`daytime_buffer` incorrectly required at least 0.3 kWh of predicted PV
surplus in the next four hours for both proactive headroom and restoration
of the preferred SOC band. Near sunset the load forecast exceeded PV, so
this check suppressed the above-band discharge while production continued.

The frozen 17:58:19 snapshot reproduced the problem with SOC 99%, PV energy
0.144 kWh and load energy 2.993 kWh in the next four hours. Before the change
the result was `export_now=false`. After the change it was `export_now=true`,
`solar_export_priority=true`, `soc_buffer_active=true`, cutoff 94%.

## Change

- Pass the adapter's existing `production` flag into the shared buffer policy.
- While production is active, SOC at least two points above the preferred
  upper band can request a bounded discharge without forecast surplus.
- Preserve continuation hysteresis down to upper band + 0.5 points.
- Preserve the 0.75 kWh maximum new target step, stable outstanding cutoff,
  grid check, export floor, manual/storm priorities and native TOU cutoff.
- Preserve the separate night planner's authority over OFF and night export.
- Publish `buffer_preferred_band_due` and `buffer_near_pv_surplus_kwh` to
  distinguish above-band discharge from forecast-driven early headroom.

The same policy runs for both plants. Site profiles still supply capacity,
minimum SOC, preferred band and communication-specific execution margin.
The hybrid consumption model is unchanged.

## Second defect found during live sunset verification

At 18:21 PV fell to 32 W. The horizon correctly requested night sleep,
`inverter_on=false`, but the final atomic plan still published `inverter_on=on`.
The planner ORed the horizon's explicit power decision with a separate,
broader legacy production-hours flag. The latter still considered it daytime.

The valid night guidance now owns the final power decision. It already checks
both measured PV and the current forecast; the older daylight window cannot
reverse an explicit sleep decision. Invalid forecast fallback, storm/manual
priority, grid-loss protection and the existing hard-floor guard remain.

The new regression failed before the change and passed afterward. It checks
both plant floors, explicit wake, grid loss and storm override; another test
checks that invalid night guidance does not shut down production.

## Deployment and validation

111 offline tests passed, including ten new cases covering the observed
failure, both capacity scales, hysteresis, stable cutoff, no-production,
grid/export-floor safeguards, planner mapping and night sleep priority.

Deployed two files at 18:07:08 Europe/Vilnius. Live SHA-256 values were checked
before replacement and after writing; both originals were backed up under
`/homeassistant/.codex-backups/eimo-daytime-band-20260918T150707Z`.
The second fix updated `planner.py` at 18:24:07, backed up under
`/homeassistant/.codex-backups/eimo-night-priority-20260918T152407Z`.
Only AppDaemon was stopped and started for each deployment. Home Assistant
and the Cloud command queue continued running.

At 18:07:13 the live Eimo plan became `feed_in`, slot on, cutoff 94%, with
planner health `ok`. The native executor submitted the new intent. The
queue sent its mode command at 18:07:26. The inverter confirmed the mode at
18:13:35, and the queue sent cutoff 94% at 18:13:38. Cutoff was confirmed at
18:19:41, followed by slot enable at 18:19:44. Actual write gaps were
372.209 and 365.983 seconds.
No direct inverter service calls, shortened command intervals or write
pings were used. The existing minimum of 360 seconds after each write and
subsequent register readback still applies; slot parameters precede enable.

The night transition occurred after slot enable had already been sent.
At 18:24:21 both atomic plans correctly requested power off and slot off;
the Eimo queue replaced its pending targets accordingly. The already sent
command stayed tracked until readback, then the current off target was
reconciled with the same write interval.

Final native register confirmation (all times Europe/Vilnius):

| Command | Write completed | Register confirmation |
| --- | --- | --- |
| Feed-In Priority / TOU | 18:07:26 | 18:13:35 |
| Discharge cutoff 94% | 18:13:38 | 18:19:41 |
| Discharge slot on | 18:19:44 | 18:25:48 |
| Discharge slot off, after night transition | 18:25:54 | 18:32:09 |
| Inverter off | 18:32:28 | 18:38:32 |

Write-completion gaps were 372.209, 365.983, 369.674 and 394.460 seconds.
Discharge time was already 00:00–23:59, so no redundant time write was needed.
Telemetry recorded about 1.285 kW of battery discharge, then 0 W after sleep.
At 18:39 the Eimo queue was idle, slot off, inverter off and executor health
`ok`; Cloud health remained healthy. Home's local executor also confirmed
slot off, inverter off and health `ok`. No direct override or queue reset
was used to obtain these results.

Solis code commits: `dada4ff7a5942e7abf91cd2c419c8074734861cf` and
`53b85195b3ce30b1066f13e1b242951518a3a9b8`.
HA configuration commits: `4bc73242310e32556abbb8e554df1d283b2d019d` and
`6dd50137a94e1ec20a7c364f53932c80b5d14aa0`.
The shared planner's GitHub Actions run
[35362333046](https://github.com/celadondeep/Solis/actions/runs/35362333046)
passed. HA's separate control workflow does not trigger on AppDaemon paths;
it was not reported as a new passing run for this patch.

This verifies the incident and the sunset transition. It is not a guarantee
against future upstream Cloud outages; the existing monitoring, backoff and
local discharge cutoff remain responsible for those conditions.

## Rollback

Stop AppDaemon, restore files from the corresponding backup manifests,
and start AppDaemon. Do not restore the entire configuration or reset the
Cloud queue. The normal executor will reconcile the currently valid plan.
