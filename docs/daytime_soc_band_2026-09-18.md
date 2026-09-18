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

## Deployment and validation

109 offline tests passed, including eight new cases covering the observed
failure, both capacity scales, hysteresis, stable cutoff, no-production,
grid/export-floor safeguards, planner mapping and night sleep priority.

Deployed two files at 18:07:08 Europe/Vilnius. Live SHA-256 values were checked
before replacement and after writing; both originals were backed up under
`/homeassistant/.codex-backups/eimo-daytime-band-20260918T150707Z`.
Only AppDaemon was stopped and started. Home Assistant and the Cloud command
queue continued running.

At 18:07:13 the live Eimo plan became `feed_in`, slot on, cutoff 94%, with
planner health `ok`. The native executor submitted the new intent. The
queue sent its mode command at 18:07:26, then waited for confirmation.
No direct inverter service calls, shortened command intervals or write
pings were used. The existing minimum of 360 seconds after each write and
subsequent register readback still applies; slot parameters precede enable.

Hardware confirmation is still being observed; the plan state alone does
not establish that all inverter settings have been applied.

## Rollback

Stop AppDaemon, restore the two files listed in the backup's manifest,
and start AppDaemon. Do not restore the entire configuration or reset the
Cloud queue. The normal executor will reconcile the currently valid plan.
