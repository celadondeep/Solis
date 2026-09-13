# Solis AppDaemon — Eimo unified controls checkpoint

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
