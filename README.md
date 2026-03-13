[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg)](https://www.hacs.xyz/)

# Home Assistant NHL API

Track a single NHL team in Home Assistant, expose schedule and scoreboard data as sensor attributes, and fire `nhl_goal` events for automations when new goals are observed.

This repository builds on the original `hass-nhlapi` project by JayBlackedOut.

## Features

- Config-entry setup from the Home Assistant UI. YAML platform configuration is no longer supported.
- Dynamic polling that stays quiet when no game is relevant and speeds up automatically during pregame and live play.
- Goal event deduplication so automations only fire for newly observed goals.
- Diagnostic sensors plus a diagnostic `Refresh` button for troubleshooting.

## Installation

### HACS

1. Open HACS and search for `NHL API`.
2. Install the integration.
3. Restart Home Assistant.
4. Go to **Settings > Devices & Services > Add Integration**.
5. Search for `NHL API`.
6. Choose a team from the dropdown, then enter an optional name and the live scan interval.

### Manual

1. Copy `custom_components/nhl_api` into your Home Assistant config directory.
2. Restart Home Assistant.
3. Go to **Settings > Devices & Services > Add Integration**.
4. Search for `NHL API`.
5. Choose a team from the dropdown, then enter an optional name and the live scan interval.

## Configuration

Each config entry tracks one team.

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| `team_abbrev` | Yes | String | NHL team selected from the config-flow dropdown. The integration stores and emits the uppercase abbreviation shown in [teams.md](./teams.md). |
| `name` | No | String | Friendly name for the primary sensor. Defaults to `NHL Sensor`. |
| `scan_interval` | No | Integer | Live polling interval in seconds. The integration enforces a minimum of `2` seconds. |

Example values: `team_abbrev: MTL`, `name: Canadiens`, `scan_interval: 5`

## Polling Behavior

The coordinator adapts its cadence to the tracked game state:

| Situation | Sensor refresh cadence | Schedule lookup cadence |
| --- | --- | --- |
| No relevant game found | Every 60 minutes | Every 60 minutes |
| Future game later today | Every 10 minutes | Every 10 minutes |
| Future game on another day | Every 60 minutes | Every 60 minutes |
| Pregame (`PRE`) | Every 10 seconds | Every 5 minutes |
| Live / critical (`LIVE`, `CRIT`) | Configured live scan interval | Every 15 minutes |
| Final / official (`FINAL`, `OFF`) | Every 10 minutes | Every 15 minutes |
| Refresh failure | Retry after 1 minute | Retry after 1 minute |

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
| `away_record`, `home_record` | String | Team records in `W-L-OTL` format when available. |
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
| `goal_type` | String | Strength such as `EVEN`, `PPG`, or `SHG`. |
| `goal_team_id` | Integer | Team ID for the most recent goal. |
| `goal_event_id` | Integer | NHL API event ID for the most recent goal. |
| `goal_team_abbrev`, `goal_team_name` | String | Team that scored the most recent goal. |
| `scoring_player_name`, `scoring_player_total`, `scoring_player_number` | Mixed | Scorer details for the most recent goal. |
| `assist1_player_name`, `assist1_player_total`, `assist1_player_number` | Mixed | Primary assist details. |
| `assist2_player_name`, `assist2_player_total`, `assist2_player_number` | Mixed | Secondary assist details. |
| `goal_period`, `goal_period_type`, `goal_time_remaining` | Mixed | Goal timing metadata. |
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

A diagnostic `Refresh` button forces an immediate refresh. If the normal next update is sooner than the newly computed cadence, that earlier next update is preserved; if the refresh discovers a more urgent state such as `LIVE`, the coordinator keeps the sooner urgent cadence.

## Events

The integration fires an `nhl_goal` event each time a newly observed goal is seen in play-by-play data. Existing goals are baselined on first load, so startup does not replay old scoring events.

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
- `home_score`
- `away_score`
- `home_team_abbrev`
- `away_team_abbrev`

`home_score` and `away_score` use the score recorded on the goal play when the NHL API provides it. If that per-play score is missing, the integration falls back to the latest gamecenter landing score snapshot.

For automation examples, see [automations.md](./automations.md).

## Troubleshooting

To enable debug logging in Home Assistant:

```yaml
logger:
  logs:
    custom_components.nhl_api: debug
```

This integration now logs tracked game changes, state transitions, goal events, refresh cadence changes, and final API request failures with team and game context to make diagnosis easier.

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
