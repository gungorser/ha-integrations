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
| `program` | all | - or `{<attr>: <default>}` | Follows the active entries of `calendar.program` (below); put it first. The value overrides the calendar's defaults for this entity |
| `link` | all | entity id, or a list (group) | The original the wrapper drives; the wrapper is unavailable while it is. With only `link` the wrapper is the original: it keeps no state of its own and changes only when the original reports; other features change the behaviour. Cover: a cover or an `input_number`/`number` 0-100 (a list: their average). Climate: a climate (mode and target are the thermostat's). Light: a light or a switch |
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
`physical_thermostat`, `control_output`, `heater_value`, `kp`, `ki`,
`kd`, `pid_i`, `pid_mode`; every entity with `hold`: `hold`; every entity with `program`:
`program` (its active programs: `uid`, `summary`, `start`, `end`, `rules`). A group wrapper has `entity_id`
(its members) so `expand()` works.

Services: `gungors.hold`, `gungors.release`, `gungors.set_pid_gain`, `gungors.set_pid_mode`,
`gungors.clear_integral`, `gungors.reload` (reloads the YAML of all platforms; Python changes
still need a restart).

Tests: `pip install pytest-homeassistant-custom-component`, then `pytest`.

## Programs

`calendar.program` (`calendar: - platform: gungors`, see `calendar.py`) holds the programs.
Each entry is a time window (it may repeat) whose description is the program, in YAML, the
rules grouped by domain:

```yaml
target: [Salon, light.melike_light]
light:
  on: true
  brightness: {min: 50, set: true}
climate:
  temperature: {max: 21}
```

- `target`: area ids or names and entity ids (entities with the `program` feature). Areas
  are resolved again at every start.
- `light`, `climate`, `cover`: one rule per attribute, a value (`eq`) or `{min, max}`, plus
  `set: true` for a set rule (`{eq: 21, set: true}`). Attributes: light `on`, `brightness`
  (%), `color` (a name or `[r, g, b]`), `color_temp` (K); climate `hvac_mode` (`heat`/`off`),
  `temperature`; cover `position`. `min`/`max` only for brightness, temperature and
  position. A rule only touches the targeted entities of its domain. (The older form with
  every attribute under one `rules:` key is still read.)
- `let` (the default) only limits: an off light or heating satisfies it. `set` also brings
  the device into the rule: a set brightness, colour or colour temperature keeps the light
  on, a set temperature keeps the heating on.
- An entry that two programs' rules can't both hold on an entity while they overlap is
  refused with an error (also from Home Assistant's calendar dialog), as is an unreadable one.
- While active: a user's request (dashboard, app, a script they run) that breaks a rule is
  refused and a browser_mod popup on their browsers says so, offering the nearest allowed
  value. Automations, wall switches and dials, and the state at the start are brought to
  the nearest allowed value without asking.
- When a set rule ends the device returns to its defaults (calendar `defaults`,
  overridable per entity: light `on: false`, `color: white`; climate `hvac_mode: "off"`;
  cover `position: sun`); let rules just lift. Covers take their `position` default at
  sunrise and sunset too. Defaults never lock. (Entries saved before the set/let flag
  were turned into set rules once.)

- `gungors.pause_program` / `gungors.resume_program` (target `calendar.program`, field
  `uid`): a paused running occurrence counts as ended (its set rules return the device to
  its defaults) until it is resumed, which applies it again as at a start, or until it ends. The
  calendar's `paused` attribute lists the paused entries' uids.

`rules.py` (format, conflicts), `program.py` (the manager) and `features/program.py`
(enforcement) are the full reference.

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
