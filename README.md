# ha-integrations (gungors)

Home-grown Home Assistant integration (domain `gungors`): wrapper entities that drive hidden
original entities and add behaviour on top. Installed through HACS as a custom repository
(category **Integration**). Platforms are configured in YAML (`cover:`, `climate:`, `light:` with
`- platform: gungors`); the YAML lives in the `ha-configs` repository (`packages/covers.yaml`,
`packages/heating.yaml`, `packages/lights.yaml`).

## Features

A gungors entity is a domain base class plus an ordered list of features:

```yaml
cover:
  - platform: gungors
    name: sercan_cover
    device_class: blind
    features:
      - hold
      - buttons
      - window: binary_sensor.sercan_window_contact
      - direction
      - link: cover.sercan_blind
```

The features are combined by inheritance in list order (`core.py` has the rules): put features
that intercept requests (`hold`, `window`) first; `link` is always last. Exactly one feature may
write to an entity (the link writes the original, `valve` the TRV numbers, `pwm` the switch); an
entry that breaks a rule is not created and the log says why.

| Feature | Domains | Value | What it does |
|---|---|---|---|
| `link` | all | entity id, or a list (group) | Two-way sync with the original; the wrapper is unavailable while it is. Cover: a cover or an `input_number`/`number` 0-100. Climate: a climate, or `{entity, cooldown_time}`. Light: a light or a switch |
| `hold` | all | - | `gungors.hold` / `gungors.release`: set a state for a while, `strict` (enforced) or `manual` |
| `direction` | cover | - or `{start_timeout, stop_silence}` | Movement tracking for blinds that only report positions (IKEA via Z2M) |
| `travel_time` | cover | - or `{z2m_base_topic}` | Position estimation for curtain motors that report only at the end (Tuya via Z2M) |
| `invert` | cover | - | The device's open is the room's close |
| `window` | cover | binary_sensor | Holds remote commands while the window is open |
| `buttons` | cover | - | Wall buttons through the pushbutton blueprint (`gungors_physical_cover`) |
| `temperature` | climate | sensor | Room temperature |
| `pid` | climate | `{kp, ki, kd, keep_alive, force_off, ...}` | Heating demand 0-100 (ScratMan's PID, vendored) |
| `valve` | climate | `{opening, closing, min}` | Demand onto the TRV valve opening/closing degree numbers |
| `pwm` | climate | `{entity, period, min_cycle_duration}` | Demand onto an on/off heater by pulse-width modulation |

Each feature module's docstring (`features/*.py`) is its full reference: behaviour, options,
attributes. Base classes: `cover.py`, `climate.py`, `light.py`.

Attributes other repositories read: cover `actual_position`, `pending_position`,
`physical_buttons` (true with `buttons`; the pushbutton blueprint selects covers by it),
`calibrated`, `position_source`, `open_time`, `close_time`; climate `hvac_action`,
`physical_thermostat`, `pre_off_target_temp`, `control_output`, `heater_value`, `kp`, `ki`,
`kd`, `pid_i`, `pid_mode`; every entity with `hold`: `hold`. A group wrapper has `entity_id`
(its members) so `expand()` works.

Services: `gungors.hold`, `gungors.release`, `gungors.set_pid_gain`, `gungors.set_pid_mode`,
`gungors.clear_integral`, `gungors.reload` (reloads the YAML of all platforms; Python changes
still need a restart).

Tests: `pip install pytest-homeassistant-custom-component`, then `pytest`.

## Wrappers drive hidden originals

Every gungors entity drives an original device entity, which is hidden in Home Assistant:
dashboards and automations use the wrapper (`cover.sercan_cover`, `climate.<room>_thermostat`),
never the original (`cover.sercan_blind`, `climate.<room>_climate`). The wrapper-to-original
pairs are the `link` values in ha-configs `packages/covers.yaml`, `packages/heating.yaml` and
`packages/lights.yaml`.

## Related repositories

- **ha-configs**: the YAML that configures these platforms (`packages/covers.yaml`,
  `packages/heating.yaml`) and the pushbutton blueprint that fires `gungors_physical_cover`.
- **ha-floorplan / ha-dashboards**: the 3D floor view shows the wrapper entities.

## Releasing

Bump `version` in `custom_components/gungors/manifest.json`, commit, publish a GitHub release
(`vX.Y.Z`), update it in HACS and restart Home Assistant.

Claude: no agent; the main session works here following [CLAUDE.md](CLAUDE.md) (rules, release checklist).
