# Changelog

## 1.2.0 — 2026-10-19

Marketplace readiness for Home Assistant 2026.11, where HACS ships built in as the **Marketplace**. No functional changes.

- Align the release version everywhere: the 1.1.1 release shipped with a `manifest.json` version and runtime `VERSION` of 1.1.0, so devices and diagnostics reported 1.1.0. All version references now agree, and 1.2.0 validates cleanly against the full test suite.
- Keep the repository Marketplace-compatible: the same `hacs.json` and catalog, `custom_components/nhl_api` structure, matching manifest and release tag versions, and hassfest plus HACS validation in CI, which now also runs nightly.
- Require Home Assistant 2026.11.0 or newer for this release through the Marketplace's minimum-version gate. Users on 2026.3.0–2026.10 are offered 1.1.1 instead; the code itself remains compatible with 2026.3.0 or newer (Python 3.14), and the validation workflow now tests 2026.3.0, 2026.9.1, and 2026.10.2.
- Add an MIT `LICENSE` file and license metadata. The integration is a full rewrite of the original `hass-nhlapi` project by JayBlackedOut, which the README credits.

### Upgrading from 1.1.1

Nothing to do. Update from HACS or the Marketplace and restart Home Assistant. Entity IDs, unique IDs, the `nhl_goal` event, and its payload keys are unchanged. Existing HACS installations migrate to the built-in Marketplace automatically when Home Assistant starts after updating to 2026.11.

## 1.1.0 — 2026-09-06

Season-readiness fixes for Home Assistant 2026.9.1, with the 2026.3.0 minimum retained.

- Stop timers and in-flight refreshes on unload, failed setup, and reload. Respect disabled polling and retry startup failures after one minute.
- Announce early goals for games tracked before puck drop; suppress historical scoring when joining midgame. Process catch-up goals in play order, retain all seen IDs for the game, and exclude shootout attempts.
- Keep finishing games through final play-by-play reconciliation and a configurable postgame window. Select split-squad games deterministically and exclude postponed/cancelled/suspended games.
- Validate NHL responses before changing tracking state. Preserve the scoreboard and last known goal attributes through play-by-play outages, with a separate scoring-feed health indicator.
- Correct goal strength, remaining time, and team records. Add elapsed `time_in_period` to goal events.
- Cache optional next-season lookups and share HTTP 429 cooldowns across team entries, including manual refreshes.
- Package English translations, including valid lowercase selector keys; continue storing/emitting uppercase team abbreviations.
- Add HACS manifest metadata, options for polling/name/postgame duration, downloadable diagnostics, bounded diagnostic updates, regression tests, and validation workflows.

### Upgrading from 1.0.1

Install the updated integration and restart Home Assistant. Keep existing config entries: entry IDs, entity unique IDs, and the `nhl_goal` event name/payload keys are preserved. Existing entries receive a 15-minute postgame default; use **Configure** to change it. Changing options reloads the entry.

Review goal automations: `time_remaining` now correctly means time remaining, and `goal_type` is populated from the NHL scoring summary or an unambiguous manpower fallback. When tracking multiple teams, use both `team_abbrev` and `goal_tracked_team` filters as shown in `automations.md`.

Goal deduplication is in memory. Restarting or reloading midgame suppresses existing goals rather than replaying events from the downtime. Corrections to already seen goals update attributes without re-announcing the goal. Scoring-feed outages retain cached goal attributes; check `goal_feed_available` before treating them as fresh.

Most optional diagnostic entities are disabled by default for new installations; existing registry choices are preserved. Final/official games now refresh every 30 seconds during postgame tracking.

See [patch verification](docs/reviews/2026.9/PATCH_NOTES.md) for the issue mapping, test evidence, and remaining live-release acceptance checks.
