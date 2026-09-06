# Automation Examples

Each newly observed goal produces an `nhl_goal` event. Games observed before puck drop emit the first goal. Starting or reloading midgame baselines existing goals without replaying them. The cache is in memory; corrections to a previously seen goal do not reannounce it, and shootout attempts are excluded. Filter both the configured team and whether that team scored, especially when tracking both opponents.

Useful event fields:

| Field | Description |
| --- | --- |
| `team_abbrev` | Configured team abbreviation for the sensor that raised the event. |
| `goal_tracked_team` | `true` if the configured team scored the goal. |
| `goal_team_abbrev` | Team that actually scored the goal. |
| `goal_team_name` | Full team name for the scoring team. |
| `scoring_player_name` | Goal scorer. |
| `goal_type` | Strength such as `EVEN`, `PPG`, or `SHG`. |
| `period_number` | Period number. |
| `time_remaining` | Time remaining in the period when the goal was scored. |
| `home_score`, `away_score` | Score recorded on the goal play when the NHL API provides it, otherwise the latest landing-score snapshot. |

## Announce Goals For The Tracked Team

```yaml
alias: Montreal Goal Announcement
trigger:
  - platform: event
    event_type: nhl_goal
    event_data:
      team_abbrev: MTL
      goal_tracked_team: true
action:
  - service: tts.google_translate_say
    target:
      entity_id: media_player.living_room_speaker
    data:
      message: The Habs scored!
mode: queued
```

## Filter By Team Abbreviation

`team_abbrev` is always uppercase in the event payload.

```yaml
alias: Montreal Goal Announcement
trigger:
  - platform: event
    event_type: nhl_goal
    event_data:
      team_abbrev: MTL
      goal_tracked_team: true
action:
  - service: notify.mobile_app_iphone
    data:
      message: >-
        Goal for {{ trigger.event.data.goal_team_name }}.
        {{ trigger.event.data.scoring_player_name }} scored a
        {{ trigger.event.data.goal_type | lower }} goal.
mode: queued
```

## Only Alert On Power-Play Goals

```yaml
alias: Montreal Power Play Goal
trigger:
  - platform: event
    event_type: nhl_goal
    event_data:
      team_abbrev: MTL
      goal_tracked_team: true
condition:
  - condition: template
    value_template: "{{ trigger.event.data.goal_type == 'PPG' }}"
action:
  - service: persistent_notification.create
    data:
      title: Power-play goal
      message: >-
        {{ trigger.event.data.scoring_player_name }} scored with
        {{ trigger.event.data.time_remaining }} left in period
        {{ trigger.event.data.period_number }}.
mode: queued
```

Related docs:

- [README.md](./README.md)
- [teams.md](./teams.md)
