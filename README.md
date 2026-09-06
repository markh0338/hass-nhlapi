[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg)](https://www.hacs.xyz/)

# Home Assistant NHL API

Track a single NHL team in Home Assistant, expose schedule and scoreboard data as sensor attributes, and fire `nhl_goal` events for automations when new goals are observed.

Version **1.1.0** targets Home Assistant **2026.9.1** and retains support for **2026.3.0 or newer** (Python 3.14). See [CHANGELOG.md](./CHANGELOG.md) for upgrade notes.

This repository builds on the original `hass-nhlapi` project by JayBlackedOut.

## Features

- Config-entry setup from the Home Assistant UI. YAML platform configuration is no longer supported.
- Dynamic polling that stays quiet when no game is relevant and speeds up automatically during pregame and live play.
- Goal event deduplication so automations only fire for newly observed goals.
- Diagnostic sensors plus a diagnostic `Refresh` button for troubleshooting.

## Installation

### HACS

1. Open HACS and search for `NHL API`. If it is not listed, add this repository as a custom repository with category **Integration**.
2. Install the integration.
3. Restart Home Assistant.
4. Go to **Settings > Devices & Services > Add Integration**.
5. Search for `NHL API`.
6. Choose a team from the dropdown, then set the optional name, live scan interval, and postgame tracking duration.

### Manual

1. Copy `custom_components/nhl_api` into your Home Assistant config directory.
2. Restart Home Assistant.
3. Go to **Settings > Devices & Services > Add Integration**.
4. Search for `NHL API`.
5. Choose a team from the dropdown, then set the optional name, live scan interval, and postgame tracking duration.

## Configuration

Each config entry tracks one team.

An entry tracks one game at a time. For concurrent preseason split-squad games, it prefers LIVE/CRIT over PRE over FUT, keeps its current game among equally ranked live games, and otherwise orders by start time and game ID. It cannot announce both concurrent games.

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| `team_abbrev` | Yes | String | NHL team selected from the config-flow dropdown. The integration stores and emits the uppercase abbreviation shown in [teams.md](./teams.md). |
| `name` | No | String | Friendly name for the primary sensor. Defaults to `NHL Sensor`. |
| `scan_interval` | No | Integer | Live polling interval in seconds. The integration enforces a minimum of `2` seconds. |

| `postgame_minutes` | No | Integer | Continue tracking a completed game for 0–60 minutes; default `15`. Final play-by-play reconciliation may extend this to one hour. |

Change the name, live interval, and postgame duration through **Settings > Devices & Services > NHL API > Configure**. Saving options reloads the entry and preserves existing entity IDs. Each team can be configured once.

Example values: `team_abbrev: MTL`, `name: Canadiens`, `scan_interval: 5`

## Polling Behavior

The coordinator adapts its cadence to the tracked game state:

| Situation | Sensor refresh cadence | Schedule lookup cadence |
| --- | --- | --- |
| No relevant game found | Every 60 minutes | Every 60 minutes |
| Future game later today | Every 10 minutes | Every 10 minutes |
| Future game on another day | Every 60 minutes | Every 60 minutes |
| Within 30 minutes of scheduled start (`FUT`) | Every 10 seconds | Every 10 minutes |
| Pregame (`PRE`) | Every 10 seconds | Every 5 minutes |
| Live / critical (`LIVE`, `CRIT`) | Configured live scan interval | Every 15 minutes |
| Final / official (`FINAL`, `OFF`) | Every 30 seconds during postgame tracking | Every 15 minutes, or when postgame tracking ends |
| Refresh failure | Retry after 1 minute | Retry after 1 minute |

A finishing game remains selected until the postgame duration ends and final play-by-play agrees with the scoreboard, with a one-hour maximum if the feed cannot catch up. Postponed, suspended, and cancelled games are excluded. Next-season schedules are requested only when needed and cached for six hours, including unsuccessful lookups.

HTTP 429 responses pause NHL requests across all configured teams until the server’s `Retry-After` deadline; the Refresh button also respects this deadline. Startup and schedule failures retry after one minute. A play-by-play failure preserves the scoreboard and last known goal attributes, marks `goal_feed_available` false, and retries that feed after one minute. Disabling polling in the entry’s system options stops automatic refreshes; manual refresh remains available.

The NHL API itself is not updated every second, so very low scan intervals mostly increase local churn rather than surfacing new data faster.

## Entities

### Primary Sensor

The main sensor state represents the tracked game's status:

| Sensor state | Meaning |
| --- | --- |
| `Today, 7:00 PM` / `Tomorrow, 7:00 PM` / `March 20, 2026 7:00 PM` | The game is in the future. The raw game state is still available as the `game_state` attribute and will be `FUT`. |
| `PRE` | The game is in pregame. |
| `LIVE` | The game is live. |
| `CRIT` | The game is in a critical late-game state reported by the NHL API. |
| `FINAL` | Final score is posted. |
| `OFF` | Game is official. |
| `No Game Scheduled` | No relevant game is currently available to track. |

Common sensor attributes:

| Attribute | Type | Description |
| --- | --- | --- |
| `game_id` | Integer | NHL game identifier for the tracked matchup. |
| `game_state` | String | Raw NHL API game state such as `FUT`, `PRE`, `LIVE`, `CRIT`, `FINAL`, or `OFF`. |
| `next_game_date` | String | Localized game date. |
| `next_game_time` | String | Localized game time. |
| `next_game_datetime` | Datetime | Localized game start datetime. |
| `away_id`, `home_id` | Integer | NHL team IDs. |
| `away_name`, `home_name` | String | Team common names. |
| `away_record`, `home_record` | String | Record supplied by the NHL (including preseason/playoff formats), or `W-L-OTL` from numeric fields when available. |
| `away_logo`, `home_logo` | String | Team logo URLs. |
| `away_logo_dark`, `home_logo_dark` | String | Dark-mode logo URLs when available. |
| `away_score`, `home_score` | Integer | Current score. |
| `away_sog`, `home_sog` | Integer | Shots on goal. |
| `current_period` | Integer | Current period number. |
| `current_period_type` | String | Period type such as `REG`, `OT`, or `SO`. |
| `is_intermission` | Boolean | `true` between periods. |
| `time_remaining` | String | Time remaining in the current period. |
| `national_broadcasts`, `away_broadcasts`, `home_broadcasts` | List | Broadcast networks grouped by market. |

Goal-related attributes are populated when play-by-play data is available for a live or recently completed game:

| Attribute | Type | Description |
| --- | --- | --- |
| `goal_type` | String | Strength such as `EVEN`, `PPG`, or `SHG`; empty when it cannot be determined reliably. |
| `goal_team_id` | Integer | Team ID for the most recent goal. |
| `goal_event_id` | Integer | NHL API event ID for the most recent goal. |
| `goal_team_abbrev`, `goal_team_name` | String | Team that scored the most recent goal. |
| `scoring_player_name`, `scoring_player_total`, `scoring_player_number` | Mixed | Scorer details for the most recent goal. |
| `assist1_player_name`, `assist1_player_total`, `assist1_player_number` | Mixed | Primary assist details. |
| `assist2_player_name`, `assist2_player_total`, `assist2_player_number` | Mixed | Secondary assist details. |
| `goal_period`, `goal_period_type`, `goal_time_remaining` | Mixed | Goal timing metadata. |
| `goal_feed_available` | Boolean/null | Whether the latest scoring feed is usable; `null` when goal tracking is inactive. Cached goal attributes may be stale when false. |
| `goal_tracked_team` | Boolean | `true` when the most recent goal was scored by the configured team. |

### Diagnostic Entities

The integration also exposes diagnostic sensors for refresh timing and API health:

- `Configured Live Scan Interval`
- `Effective Scan Interval`
- `Next Schedule Lookup`
- `Next Update`
- `Last Refresh Started`
- `Last Refresh Duration`
- `Observed Refresh Interval`
- `Refresh Count`
- `Goals Seen Count`
- `API Last Success`
- `API Last Error`
- `API Error Count`
- `API Timeout Count`
- `Last Attempt`
- `Last Good Game Refresh`
- `Goal Feed Available`
- `Last Good Goal Refresh`

Most timing and counter sensors are disabled by default for new entities. Existing enabled/disabled choices are preserved. Enabled diagnostics publish changed values at most once per minute during steady play, with immediate updates for main-state transitions, error changes, and manual refreshes. Download a runtime snapshot from the integration’s **Download diagnostics** menu for support.

A diagnostic `Refresh` button forces an immediate refresh. If the normal next update is sooner than the newly computed cadence, that earlier next update is preserved; if the refresh discovers a more urgent state such as `LIVE`, the coordinator keeps the sooner urgent cadence.

## Events

The integration fires an `nhl_goal` event each time a newly observed goal is seen in play-by-play data. A game observed before puck drop emits its first goal even when the first LIVE response already contains scoring. When attaching midgame after setup, restart, or reload, existing goals are baselined once the feed catches up, so historical scoring is not replayed. Deduplication is in memory for the tracked game; goal events are not a durable delivery queue. During an outage, newly observed goals are delivered in play order after recovery while the game remains tracked. Corrections to previously seen event IDs update attributes without another announcement. Shootout attempts do not emit `nhl_goal` events; regulation and overtime goals do.

Event payload fields include:

- `team_abbrev`
- `game_id`
- `event_id`
- `goal_type`
- `goal_team_id`
- `goal_team_abbrev`
- `goal_team_name`
- `goal_tracked_team`
- `scoring_player_name`
- `scoring_player_total`
- `scoring_player_number`
- `assist1_player_name`
- `assist1_player_total`
- `assist1_player_number`
- `assist2_player_name`
- `assist2_player_total`
- `assist2_player_number`
- `period_number`
- `period_type`
- `time_remaining`
- `time_in_period` (elapsed time)
- `home_score`
- `away_score`
- `home_team_abbrev`
- `away_team_abbrev`

`home_score` and `away_score` use the score recorded on the goal play when the NHL API provides it. If that per-play score is missing, the integration falls back to the latest gamecenter landing score snapshot.

If you track both teams in a matchup, each entry can emit the same scoring play. Filter events by both `team_abbrev` (the configured entry) and `goal_tracked_team: true` for one announcement when your team scores.

For automation examples, see [automations.md](./automations.md).

## Troubleshooting

To enable debug logging in Home Assistant:

```yaml
logger:
  logs:
    custom_components.nhl_api: debug
```

This integration now logs tracked game changes, state transitions, goal events, refresh cadence changes, and final API request failures with team and game context to make diagnosis easier.

## Development and release checks

```sh
uv sync --group dev
uv run pytest -q
uv run ruff check custom_components tests
uv run ruff format --check custom_components tests
```

The validation workflow tests Home Assistant 2026.3.0 and 2026.9.1 on Python 3.14 and runs hassfest and HACS validation. Fixture tests cover scoring, lifecycle, rate limits, options/reload, translations, and diagnostics. Before a season release, also observe a live game through pregame, scoring, final, and the next fixture; fixture tests cannot certify upstream API availability.

## Additional Docs

- [teams.md](./teams.md)
- [automations.md](./automations.md)
- [frontend.md](./frontend.md)
- [info.md](./info.md)

## Credits

- Original `hass-nhlapi` project by JayBlackedOut
- [The Undocumented NHL Stats API](https://statsapi.web.nhl.com/api/v1/schedule)
- [Drew Hynes' Unofficial Documentation](https://gitlab.com/dword4/nhlapi)
- Adam Pritchard's NHL Score API
- [The Reddit post that inspired this project](https://www.reddit.com/r/homeassistant/comments/b9vioe/got_home_assistant_to_grab_the_game_info_for_my/)
