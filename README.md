# ha-integrations (gungors)

Home-grown Home Assistant integration (domain `gungors`): wrapper entities that drive hidden
original entities and add behaviour on top. Installed through HACS as a custom repository
(category **Integration**). Platforms are configured in YAML (`cover:`, `climate:`, `light:`,
`event:` with `- platform: gungors`); the YAML lives in the `ha-configs` repository
(`gungors/packages/<domain>.yaml`: `cover.yaml`, `climate.yaml`, `light.yaml`, `event.yaml`,
`calendar.yaml`, presets in `gungors.yaml`).

## Features

A gungors entity is a domain base class plus an ordered list of features:

```yaml
cover:
  - platform: gungors
    name: sercan
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
| `relay` | event | `{<key>: switch or light}` | A key's LED relay follows the key's function: on while one of its targets is on (below) |

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

Cover `toggle` is one tap, the same on every gungors cover: moving -> stop, closed -> open,
anything else -> close (HA's own toggle depends on the last move's direction). "Moving" comes
from `direction` / `travel_time`, or from the linked covers (a link-only group is moving while
one of its members is). Dashboards tap covers with `cover.toggle`. Open while opening and close
while closing stop the cover too (IKEA blinds do this themselves, Tuya motors ignore it).

Services: `gungors.set_pid_gain`, `gungors.set_pid_mode`,
`gungors.clear_integral`, `gungors.blink` (lights: `rgb_color`, `count`; flashes the original
and puts it back as it was; not a request, so no rule or `occupancy` sees it; a real request
ends it), `gungors.reload` (reloads the YAML of all platforms and the presets; Python changes
still need a restart).

## Configure (UI)

Since 1.20.0 gungors has one config entry (Settings > Devices & services > Gungor
Customizations, `config_flow.py`, logo in `brand/`). Configure (1.24.0) opens a menu Presets /
Services; Services is a menu Immich / Budget Thuis. Forms use closed sections that their header
opens and closes (Home Assistant does not nest sections, so the menus give the upper level).
Each form saves on submit:

- Presets: sections Light, Climate, Cover holding the five presets (light default/dim/night,
  climate default, cover default) as prefixed fields (`dim_brightness_pct`); an empty field
  is not applied. Light and climate take an optional `templates` mapping (attribute: template)
  over the typed fields; the cover position is a template field (a number works too).
- Services > Immich: sections general, users, people, trips, faceless photos, Partou. Users are a list (add /
  remove, name + API key; HA's list editor also edits it as YAML); a stored key shows empty and
  stays when left empty. People and the trip details are YAML fields. An empty address turns
  Immich off.
- Services > Budget Thuis: e-mail and scan interval (default 3600 s); Budget Thuis has no password, so a
  new login mails a link: copy it (do not click) into the page that follows. Empty e-mail: off.

Saving applies at once (no restart, no `gungors.reload`). The entry options keep the YAML
shape (`{"presets": {...}, "immich": {...}}`). Wrappers, features and the program calendar stay
in YAML.

Migration: on the first start without an entry, `gungors: presets:` and `gungors: immich:` from
YAML are imported into a new entry (secrets included). With an entry, these YAML keys are
ignored (still accepted, so a restart never fails) and a Repairs warning asks to remove them.

## Presets

Named attribute sets per domain, shared by the program defaults and the pushbuttons
(`presets.py`); set in the UI (Configure > Presetler), shown here in the stored shape:

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
    name: bebek
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
- A request a program rule limits is applied as the rule allows and the lights blink twice in red
  (not when a single press turns lights on: its preset may be limited, e.g. dimmer at night).
- `relay: {1: switch.yemek_1}` (wall switches whose key LEDs are wired to relays without a load):
  the relay is on while the key's function is on (`light` a target light on, `climate` not off,
  `cover` not closed, `program` running, `custom` a target `on`). It is written only when it
  differs (a press does not switch it; a relay switched by hand goes back), synced at startup and
  reload, left alone while the targets or the relay are unavailable, and waits while a target
  light blinks. The relay is claimed. Attribute `relays`: `{key: {entity_id, on}}`. A key the
  device lacks or a key without a function is a config error.

Tests: `pip install pytest-homeassistant-custom-component`, then `pytest`.

## Programs

`calendar.program` (`calendar: - platform: gungors`, see `calendar.py`) holds the programs.
Each entry is a time window (it may repeat) whose description is the program, in YAML, the
rules grouped by domain:

```yaml
target: [Salon, light.melike]
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

## Immich albums

Immich settings (Configure > Servisler > Immich; before 1.20.0 `gungors: immich:` in YAML,
`immich.py`, shown here in the stored shape) fill Immich albums
through the Immich API, with one API key per Immich user (faces and photos are per user):

- People: every photo or video with a named person (`people: {Kaan: Kaan}`, the name as given in
  Immich, in each user's library) goes to that person's album, owned by `owner` and shared with
  the other users as editors.
- Trips: per user, a trip starts at the first photo farther than `distance_km` from `zone.home`
  and ends at the next photo taken at home; days without photos in between do not matter, up
  to `max_gap_days` (10 calendar days; few photos at home carry GPS).
  Camera photos without GPS taken during it belong to it. Trips less than `max_gap_days` apart
  are one trip (also of different users), unless a user of the later one came home in between.
  A trip with fewer than `min_assets` (3) photos is no trip. Countries match by name
  (`The Netherlands` = `netherlands`, `Türkiye` = `turkey`). Album name: every place in visit
  order + the start year, ascii lowercase: the `detail` field (state or city) for the countries
  listed there (Turkey: il, Netherlands: city), else the country's Turkish name (built in,
  `names:` over it): `ankara_samsun_2024`, `ispanya_2025`. A trip in more than one country
  (`home_country`, default `netherlands`, not counted) is named by its countries
  (`ispanya_fransa_almanya_2023`); a place or country with less than `name_min_share` (0.1) of
  the photos or a country with fewer than `name_min_photos` (2) photos (a stopover) is left out of the name; `_2` on a clash. `min_days: {netherlands: 2}`: a trip mostly
  in that country that spans fewer calendar days is not a trip. A trip's album opens with its
  first photo and is renamed once at its end if the name changed and nobody renamed it.
- Faceless: images uploaded after the first run, `wait_hours` after upload, with Immich's face
  detection queue empty and no face found, not a favourite, not archived and in no album of their
  own: `faceless.home` (outside trips: `keep` by default, `trash`, `delete`) or `faceless.trip`
  (trip photos: `album` by default, `keep`, `trash`, `delete`) decides. `album` -> into the trip
  album like any trip photo, `keep` -> left as it is in no album (a trip photo stays out of the
  trip album), `trash` -> the Immich trash (restorable for 30 days), `delete` -> deleted for
  good. Unless `faceless.trip` is `album`, a new image joins its trip album only after the
  faceless check (a face found, or skipped). Configure > Immich has a dropdown for each. Videos
  are never touched. Before 1.25.0 faceless photos went to a `check_before_delete` album (and
  `home: album` reads as `keep` now); that album is left as it is.
- `select.immich` is the mode (restored): `Kapalı`, `Deneme` (plans only; nothing written),
  `Albüm` (writes; the old `Albüm + çöp` restores as `Albüm`). `sensor.immich` (`tamam` / `hata`) carries the last run: new assets
  per person album, the trips (name, dates, assets, new ones), the faceless counts, the error.
  A run every `scan_interval` (600 s; `0`: no timer), on a mode change, on
  `button.immich_sync` and on `gungors.immich_run`.
- `share_albums: true`: every album a user owns (also ones made by hand or by the phone app) is
  shared with the other users as editors; the old faceless albums stay private.
- Each asset is added to an album once (`.storage/gungors.immich`): one removed by hand stays out;
  a faceless decision is final.
- Partou (`partou:` with `username`, `password`, optional `albums: {Kaan: kaan_partou}`, `after:
  "18:00"`, `partou.py`): once a day after `after` (local) and on `gungors.immich_run` /
  `button.immich_sync`, the photos and videos of each child's album in the Partou parent portal
  (full size, 2400 px) are uploaded to the `owner`'s library, each Partou item once
  (`.storage`), with the EXIF capture time (videos: the portal's day at noon) and into monthly
  albums `kaan_partou_mart_2026`. This writes in every mode but `Kapalı`, also in `Deneme`.
  The uploads stay out of the person, trip and faceless rules. `sensor.immich` `partou`: per
  child `toplam`, `yeni`, `albümler`, or `hata`.

## Budget Thuis

`budget_thuis.py` reads the daily usage of the current month from the (unofficial) API of the
Budget Thuis app: login with the app's OAuth2 PKCE flow (client `mobile`, no secret): authorize
-> `/inloglink-aanvragen` mails a link -> the pasted link is followed (also a self-posting
confirm page) to `budgetthuis://login_success?code=` -> `/connect/token`. Only the refresh token
is kept (entry options at login, then `.storage/gungors.budget_thuis` as it rotates); a refused
one sets `status: hata`, `error: yeniden giriş gerekli`, contracts via
`/home/v1/contactPerson` and `/energy/v2/customer/productPicker`, usage via
`/energy/v1/contract/<id>/usagecosts/details?PeriodDuration=Day`, summed over all contracts.
Every `usage*` block of a day with a `rawUsage` counts: electricity consumption, electricity
production, gas. The field names of the first run are logged at info level.

Sensors (from the config entry, unavailable while not configured), the month so far,
`total_increasing` (0 again at a new month), so the Energy dashboard takes them:

| Entity | Unit |
| --- | --- |
| `sensor.budget_thuis_elektrik` | kWh |
| `sensor.budget_thuis_gaz` | m³ |
| `sensor.budget_thuis_teruglevering` | kWh |

Attributes: `last_day` and `last_day_value` (the newest day with a reading; readings lag a day
or two), `month_start`, `last_run`, `status` (`tamam` / `hata`), `error`.

## Wrappers drive hidden originals

Every gungors entity drives an original device entity, which is hidden in Home Assistant:
dashboards and automations use the wrapper (`cover.sercan`, `climate.<room>`),
never the original (`cover.sercan_blind`, `climate.<room>_climate`). The wrapper-to-original
pairs are the `link` values in ha-configs `gungors/packages/cover.yaml`, `gungors/packages/climate.yaml` and
`gungors/packages/light.yaml`.

## Naming

Wrappers and the physical devices they drive follow one convention, in every repository
(ha-configs packages, dashboards, cards, Blender page ids map to these entity ids):

| Kind | Entity id | YAML `name` | Example |
|---|---|---|---|
| Wrapper | `<domain>.<room>[_<role>]` | `<room>[_<role>]` | `light.salon`, `climate.salon`, `cover.yatak_window`, `event.yemek` |
| Physical | `<domain>.phys_<room>[<role>]_<domain>` | (named in Zigbee2MQTT) | `light.phys_salon_light`, `cover.phys_yatakwindow_cover` |

- A wrapper's id never repeats its domain: `light.araba`, not `light.araba_light`. Wrappers have
  no device, so `light.sercan`, `cover.sercan`, `climate.sercan` and `event.sercan` do not clash.
  Pushbuttons are `event.<room>` whatever the hardware (Moes, ESPHome switchbutton); the device
  type shows on the physical side.
- A physical id keeps the domain suffix: a room's lamp and TRV are two Zigbee devices, and
  device names must differ. Rename physical devices in Zigbee2MQTT, never only in Home Assistant
  (ha-configs README, "Naming physical devices").
- A wrapper's friendly name is set in the Home Assistant entity registry, readable Turkish
  (`Salon ışığı`, `Yatak kapı perdesi`, `Salon termostatı`, `Yemek butonu`), not in YAML.
- `unique_id` is permanent: keep it when a wrapper is renamed (older ones still carry the old
  name, e.g. `unique_id: araba_light`). Rename the entity id in the registry
  (`ha_set_entity(new_entity_id=..., name=...)`), then change the YAML `name` and every
  reference in ha-configs in the same change. Since 1.16.2 a registry rename keeps the
  wrapper's state and claims; claims are keyed by `<domain>.<name>`.

## Related repositories

- **ha-configs**: the YAML that configures these platforms (`gungors/packages/<domain>.yaml`).
  Naming rule for the physical devices behind the wrappers:
  https://github.com/gungorser/ha-configs#naming-physical-devices
- **ha-floorplan / ha-dashboards**: the 3D floor view shows the wrapper entities.

## Releasing

Bump `version` in `custom_components/gungors/manifest.json`, commit, publish a GitHub release
(`vX.Y.Z`), update it in HACS and restart Home Assistant.

Claude: no agent; the main session works here following [CLAUDE.md](CLAUDE.md) (rules, release checklist).
