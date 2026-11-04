# ha-integrations (gungors)

Home-grown Home Assistant integration (domain `gungors`): wrapper entities that drive hidden
original entities and add behaviour on top. Installed through HACS as a custom repository
(category **Integration**). Platforms are configured in YAML (`cover:`, `climate:`, `light:`,
`event:` with `- platform: gungors`); the YAML lives in the `ha-configs` repository
(`gungors/covers.yaml`, `gungors/heating.yaml`, `gungors/lights.yaml`,
`gungors/pushbuttons.yaml`, `gungors/presets.yaml`).

## Features

A gungors entity is a domain base class plus an ordered list of features:

```yaml
cover:
  - platform: gungors
    name: sercan_cover
    device_class: blind
    features:
      - program
      - window: binary_sensor.sercan_window_contact
      - direction
      - link: cover.sercan_blind
```

The features are combined by inheritance in list order (`core.py` has the rules): put features
that intercept requests (`program`, `window`) first; `link` is always last. Exactly one feature may
write to an entity (the link writes the original, `valve` the TRV numbers, `pwm` the switch); an
entry that breaks a rule is not created and the log says why.

| Feature | Domains | Value | What it does |
|---|---|---|---|
| `program` | all | - or `{<attr>: <default>, preheat: {rate, max}}` | Follows the active entries of `calendar.program` (below); put it first. The value overrides the calendar's defaults for this entity; `preheat` (climate) reaches a coming program's set temperature by its start (below) |
| `link` | all | entity id, or a list (group) | The original the wrapper drives; the wrapper is unavailable while it is. With only `link` the wrapper is the original: it keeps no state of its own and changes only when the original reports; other features change the behaviour. Cover: a cover or an `input_number`/`number` 0-100 (a list: their average). Climate: a climate (mode and target are the thermostat's). Light: a light or a switch |
| `direction` | cover | - or `{start_timeout, stop_silence}` | Movement tracking for blinds that only report positions (IKEA via Z2M) |
| `travel_time` | cover | - or `{z2m_base_topic}` | Position estimation for curtain motors that report only at the end (Tuya via Z2M) |
| `invert` | cover | - | The device's open is the room's close |
| `window` | cover | binary_sensor | Holds remote commands while the window is open; wall buttons (pushbutton blueprint, `gungors_physical_cover`) are physical and always go through |
| `occupancy` | light | binary_sensor, or `{sensor, brightness_pct, transition, after_sunset_only}` | A motion sensor lights the light (`brightness_pct` 50, `transition` 2 s, only after sunset unless `after_sunset_only: false`) and darkens it when the motion ends, but only if the sensor lit it. A light that is on wins over motion and belongs to whoever turned it on; every request that is not the sensor's (dashboard, pushbutton, automation, program, the device itself) is the user's and takes the light over. Put it after `program`, before `link` |
| `temperature` | climate | sensor | Room temperature |
| `pid` | climate | `{kp, ki, kd, keep_alive, force_off, ...}` | Heating demand 0-100 (ScratMan's PID, vendored) |
| `valve` | climate | `{opening, closing, min}` | Demand onto the TRV valve opening/closing degree numbers |
| `pwm` | climate | `{entity, period, min_cycle_duration}` | Demand onto an on/off heater by pulse-width modulation |
| `buttons` | event | list of functions | Pushbutton ready functions `light`, `cover`, `climate`, `program`, `custom`, placed on keys by the location rule (below) |
| `custom` | event | `{<slot>: {action, target, data}}` | Any action on a free slot of a pushbutton (`2_hold`) |

Each feature module's docstring (`features/*.py`) is its full reference: behaviour, options,
attributes. Base classes: `cover.py`, `climate.py`, `light.py`.

Attributes other repositories read: cover `actual_position`, `pending_position`,
`physical_buttons` (true with `window`; the pushbutton blueprint sends those covers the
`gungors_physical_cover` event, the others the normal cover services),
`calibrated`, `position_source`, `open_time`, `close_time`; climate `hvac_action`,
`physical_thermostat`, `control_output`, `heater_value`, `kp`, `ki`,
`kd`, `pid_i`, `pid_mode`, `preheat`, `preheat_last` (with `program: {preheat: ...}`); every entity with `program`:
`program` (its active programs: `uid`, `summary`, `start`, `end`, `rules`). A group wrapper has `entity_id`
(its members) so `expand()` works.

Services: `gungors.set_pid_gain`, `gungors.set_pid_mode`,
`gungors.clear_integral`, `gungors.blink` (lights: `rgb_color`, `count`; flashes the original
and puts it back as it was; not a request, so no rule or `occupancy` sees it; a real request
ends it), `gungors.reload` (reloads the YAML of all platforms and the presets; Python changes
still need a restart).

## Presets

Named attribute sets per domain, shared by the program defaults and the pushbuttons
(`presets.py`):

```yaml
gungors:
  presets:
    light:
      default: {state: "off", rgb_color: [255, 255, 255], brightness_pct: 100}
      dim: {brightness_pct: 10}
      night: {rgb_color: [255, 0, 0]}
    climate:
      default: {state: "off"}
    cover:
      default: {current_position: "{{ 100 if is_state('sun.sun', 'above_horizon') else 0 }}"}
```

Attribute names as in the program rules; a value may be a template. A preset sets only the
attributes it lists. `default` is also the domain's program default (over the calendar's older
`defaults:`). Presets missing from the YAML have the built-in values above.

## Pushbuttons

`event: - platform: gungors` mirrors a button device's own event entity (Zigbee2MQTT
`event.<device>_action`, enabled with Z2M's Home Assistant option for event entities) and acts
on its presses (`event.py`, `features/button.py` are the full reference):

```yaml
event:
  - platform: gungors
    name: bebek_pushbutton
    target: {area_id: bebek}        # optional: the device's area otherwise
    keys: 3                         # keys on the device
    door: right                     # side of the door it is on; leave out where there is none
    features:
      - buttons:
          - light                   # near the door: 3_*
          - cover                   # far: 1_*
          - climate                 # middle single: 2_single
          - program: "Bebek uyku"   # middle double: 2_double
          - custom: {action: switch.toggle, target: {entity_id: switch.x}}  # middle hold
          - light: {button: 2, target: {area_id: araba}}   # own key and target
      - link: event.bebek_pushbutton_action
```

- Location rule: near the door = `light` (all three gestures), farthest = `cover` (all three),
  middle key of a three-key device = `climate` single, `program` double, `custom` hold. Moes key 1
  is the leftmost: `door: left` makes 1 near, `right` makes the last key near. A one-key device
  has only near; a two-key device has no middle, so climate / program / custom need a `button`;
  without `door` every function needs one. Each function takes optional `button` (its key) and
  `target` (else the wrapper's, else the device's area). A function the rule cannot place, a key
  the device lacks or a slot taken twice is a config error. The old `{<button>: <function>}`
  form still reads.
- Slots are `<button>_<gesture>` (`single`, `double`, `hold`; a one-button model's are button
  1's). The `custom` feature (`{<slot>: {action, target, data}}`) takes any slot left free.
- `light`: single off / on at `default`; double toggles the brightness between `dim` and
  `default`; hold toggles the colour between `night` and `default` (args `single:`, `double:`,
  `hold:` name other presets).
- `climate`: toggles (one blink: green on, blue off). `program`: starts or stops the timeless
  program (two blinks).
- `cover`: single open, double close (physical: through an open window), a press while it moves stops it; hold closes the whole
  floor with a backup scene, or restores it.
- A request a program rule limits is applied as the rule allows and the lights blink twice in red.

Tests: `pip install pytest-homeassistant-custom-component`, then `pytest`.

## Programs

`calendar.program` (`calendar: - platform: gungors`, see `calendar.py`) holds the programs.
Each entry is a time window (it may repeat) whose description is the program, in YAML, the
rules grouped by domain:

```yaml
target: [Salon, light.melike_light]
light:
  state: "on"
  brightness_pct: {min: 50, set: true}
climate:
  temperature: {max: 21}
```

- `target`: area ids or names and entity ids (entities with the `program` feature). Areas
  are resolved again at every start.
- `start`, `end` (optional): the start or end relative to the sun instead of the entry's own
  time: `sunrise` or `sunset` with an optional offset (`sunset-30m`, `sunrise+2h`,
  `sunrise+1h30m`). Every occurrence of a repeating entry takes its own day's sun. A sun end
  that falls before the start is the next day's (`22:00` -> `end: sunrise` runs through the
  night); an occurrence whose sun start comes at or after its fixed end is left out. The
  entry's stored time then only marks the day; the calendar, conflicts and the card all use
  the real times.
- `light`, `climate`, `cover`: one rule per attribute, a value (`eq`) or `{min, max}`, plus
  `set: true` for a set rule (`{eq: 21, set: true}`). Attributes, named as in Home Assistant:
  light `state` (`"on"`/`"off"`), `brightness_pct`, `rgb_color` (`[r, g, b]` or a name),
  `color_temp_kelvin`; climate `state` (the HVAC mode, `heat`/`off`), `temperature`; cover
  `current_position`. `min`/`max` only for brightness_pct, temperature and current_position.
  A rule only touches the targeted entities of its domain. (The older names `on`,
  `brightness`, `color`, `color_temp`, `hvac_mode`, `position` and the older form with every
  attribute under one `rules:` key are still read; saved entries were rewritten once with
  the new names.)
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
  overridable per entity with `- program: {<n>: <value>}`: light `state: "off"`,
  `rgb_color: [255, 255, 255]`; climate `state: "off"`; cover `current_position:
  "{{ 100 if is_state('sun.sun', 'above_horizon') else 0 }}"`, merged per domain with the
  calendar's YAML `defaults:`, which are the only place to change them: edit the YAML, then
  `gungors.reload`; the program card's defaults tab only shows them); let rules just lift.
  A default may be a template: it is rendered when
  applied and applied again whenever its result changes (the cover one at sunrise and
  sunset), unless a set rule holds the attribute. Defaults never lock. (The older cover
  `position: sun` reads as the sun template; entries saved before the set/let flag were
  turned into set rules once.)

- `gungors.pause_program` / `gungors.resume_program` (target `calendar.program`, field
  `uid`): a paused running occurrence counts as ended (its set rules return the device to
  its defaults) until it is resumed, which applies it again as at a start, or until it ends. The
  calendar's `paused` attribute lists the paused entries' uids.
- Timeless programs (`baby sleep`: no time of their own, started when needed) are kept by
  gungors, not in the calendar: `gungors.save_timeless_program` (`summary`, `description` as
  above but without `start`/`end`, optional `uid` to change one) and
  `gungors.delete_timeless_program` (`uid`). `gungors.start_program` (`uid` or the name,
  optional `duration`, 24 h when not given) adds a run: a one-off calendar entry from now
  whose description carries `timeless: <uid>`; `gungors.stop_program` ends it now. A stop,
  the run's end or deleting the program removes that entry from the calendar, so a timeless
  program has at most one entry, while it runs; starting a running one does nothing. A change to the program applies to its running run at once.
  Runs are not checked for conflicts on saving: while a run is active it pauses every
  running entry it conflicts with (a shared entity and an attribute both can't hold), also
  one that starts later, and resumes them when it ends or is paused; the newest run wins
  over an older one the same way. An entry the user pauses meanwhile stays paused; one the
  user resumes is not paused again (it then keeps the entity as the earlier one).
- Preheat: a thermostat with `- program: {preheat: {rate: 1.5}}` (°C per hour; `max`
  how early at most, 3 h when not given) is at a program's set temperature (`eq`, or the
  `min` of a range) by the program's start. (target - room temperature) / rate before the
  start the program's temperature rule applies as if it had started: requests are brought
  to it like any rule, and it goes before the temperature rules of programs running then.
  Once open the window stays open until the start (also over a restart); pausing the
  program (`gungors.pause_program` works on it then, the pause lasts until its end) or
  deleting it closes it and the heating returns as at a set rule's end. Programs with
  `climate: preheat: false` and timeless runs are not preheated for. The thermostat's
  `preheat` attribute is the plan (`target`, `at`, `start`, `rate`, `active`),
  `preheat_last` the latest outcome (`reached`, `late_min`); the calendar's `preheating`
  attribute lists the programs being preheated for, and in `gungors/programs` such a
  program has state `preheating` and `preheat: {start, active}`.
- `gungors.pin_program` (`uid`, `pinned: false` to unpin) pins an entry or a timeless
  program for everyone; the program card lists the pinned ones first. The calendar's
  `pinned` and `timeless` attributes list their uids (so a change updates its state).
- Websocket `gungors/programs`: every entry (a series once) and timeless program with
  `state` (`running`, `paused`, `scheduled`, `preheating`, `done`, `idle`, `invalid`), the current or next
  `start`/`end`, `all_day`, `pinned`, `preheat`, `paused_by` (the timeless program that paused it) and, for a
  timeless one, `run` (its running entry's uid, for pause/resume).

`rules.py` (format, conflicts), `program.py` (the manager) and `features/program.py`
(enforcement) are the full reference.

## Wrappers drive hidden originals

Every gungors entity drives an original device entity, which is hidden in Home Assistant:
dashboards and automations use the wrapper (`cover.sercan_cover`, `climate.<room>_thermostat`),
never the original (`cover.sercan_blind`, `climate.<room>_climate`). The wrapper-to-original
pairs are the `link` values in ha-configs `gungors/covers.yaml`, `gungors/heating.yaml` and
`gungors/lights.yaml`.

## Related repositories

- **ha-configs**: the YAML that configures these platforms (`gungors/covers.yaml`,
  `gungors/heating.yaml`) and the pushbutton blueprint that fires `gungors_physical_cover`.
  Naming rule for the physical devices behind the wrappers:
  https://github.com/gungorser/ha-configs#naming-physical-devices
- **ha-floorplan / ha-dashboards**: the 3D floor view shows the wrapper entities.

## Releasing

Bump `version` in `custom_components/gungors/manifest.json`, commit, publish a GitHub release
(`vX.Y.Z`), update it in HACS and restart Home Assistant.

Claude: no agent; the main session works here following [CLAUDE.md](CLAUDE.md) (rules, release checklist).
