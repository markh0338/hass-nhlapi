# Changelog

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
