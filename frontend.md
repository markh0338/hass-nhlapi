# Frontend Example

This repository does not include a dedicated Lovelace card, but the sensor exposes enough data to build a lightweight scoreboard with template sensors and a standard entities card.

Example screenshots:

- No game scheduled: ![No games scheduled](./no_game.png)
- Game scheduled or live: ![With a game scheduled](./with_game.png)

## Template Sensors

Replace `sensor.canadiens` with your NHL sensor entity ID.

```yaml
template:
  - sensor:
      - name: "{{ state_attr('sensor.canadiens', 'away_name') or 'Away Team' }}"
        unique_id: nhl_away_team
        state: "{{ state_attr('sensor.canadiens', 'away_score') }}"
        picture: "{{ state_attr('sensor.canadiens', 'away_logo') }}"
      - name: "{{ state_attr('sensor.canadiens', 'home_name') or 'Home Team' }}"
        unique_id: nhl_home_team
        state: "{{ state_attr('sensor.canadiens', 'home_score') }}"
        picture: "{{ state_attr('sensor.canadiens', 'home_logo') }}"
```

## Lovelace Card

```yaml
type: entities
title: NHL Scoreboard
show_header_toggle: false
entities:
  - entity: sensor.canadiens
  - entity: sensor.nhl_away_team
  - entity: sensor.nhl_home_team
```

## Optional Card Mod Styling

If you use Lovelace Card Mod, you can center the team logos:

```yaml
type: entities
title: NHL Scoreboard
show_header_toggle: false
entities:
  - entity: sensor.canadiens
  - entity: sensor.nhl_away_team
    card_mod:
      style:
        hui-generic-entity-row$: |
          state-badge {
            background-position: center;
            background-size: contain;
            background-repeat: no-repeat;
          }
  - entity: sensor.nhl_home_team
    card_mod:
      style:
        hui-generic-entity-row$: |
          state-badge {
            background-position: center;
            background-size: contain;
            background-repeat: no-repeat;
          }
```

Related docs:

- [README.md](./README.md)
- [automations.md](./automations.md)
