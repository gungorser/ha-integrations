"""Config and options flow: the presets and the Immich settings in the UI.

One config entry ("Gungor Customizations"). Its options hold what used to be
`gungors: presets:` and `gungors: immich:` in YAML, in the YAML's own shape
(Home Assistant attribute names, plain JSON):

    {"presets": {"light": {"default": {"state": "off", ...}, ...}, ...},
     "immich": {"url": ..., "users": {...}, ...}}          # no "immich": Immich off

Configure opens a menu: Presetler (the five built-in presets, no add or delete)
and Immich (one form in sections). Wrappers, features and the program calendar
stay in YAML. The entry is created from the YAML on the first start (import step),
so an existing setup keeps working.
"""
from __future__ import annotations

import copy
import datetime
import time
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN
from .immich import IMMICH_SCHEMA
from .presets import BUILTIN
from .rules import NAMES, defaults_json, parse_defaults

TITLE = "Gungor Customizations"
DEFAULT_BT_INTERVAL = 3600  # Budget Thuis: hourly

# The presets the UI edits: (domain, name). Nothing else uses other names.
PRESETS: tuple[tuple[str, str], ...] = (
    ("light", "default"),
    ("light", "dim"),
    ("light", "night"),
    ("climate", "default"),
    ("cover", "default"),
)
TEMPLATES = "templates"
SECRET_FIELDS = ("password",)


def builtin_presets() -> dict[str, dict[str, dict[str, Any]]]:
    """The built-in presets in the stored (YAML) shape."""
    return {
        domain: defaults_json(presets) for domain, presets in BUILTIN.items()
    }


def presets_options(internal: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    """Validated presets (internal attribute names) -> the stored shape."""
    return {domain: defaults_json(presets) for domain, presets in internal.items()}


def immich_options(conf: dict[str, Any]) -> dict[str, Any]:
    """A validated `immich:` -> plain JSON (times as text)."""

    def plain(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(k): plain(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [plain(v) for v in value]
        if isinstance(value, datetime.time):
            return value.strftime("%H:%M")
        return value

    return plain(conf)


class GungorsConfigFlow(ConfigFlow, domain=DOMAIN):
    """One entry; created by hand (built-in presets) or from the YAML."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is None:
            return self.async_show_form(step_id="user")
        return self.async_create_entry(
            title=TITLE, data={}, options={"presets": {}}
        )

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """From `gungors: presets:` / `immich:` in YAML (already in the stored shape)."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        # Only what the YAML gives: a preset left out keeps following the built-in one
        # (and a `default` left out the calendar's older `defaults:`).
        options: dict[str, Any] = {"presets": dict(import_data.get("presets") or {})}
        if import_data.get("immich"):
            options["immich"] = import_data["immich"]
        return self.async_create_entry(title=TITLE, data={}, options=options)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return GungorsOptionsFlow()


# --- options ---------------------------------------------------------------------------------------


def _preset_schema(domain: str) -> vol.Schema:
    templates = {
        vol.Optional(TEMPLATES): selector.ObjectSelector(),
    }
    if domain == "light":
        return vol.Schema(
            {
                vol.Optional("state"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=["on", "off"], translation_key="light_state"
                    )
                ),
                vol.Optional("brightness_pct"): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=100, step=1, unit_of_measurement="%",
                        mode=selector.NumberSelectorMode.SLIDER,
                    )
                ),
                vol.Optional("rgb_color"): selector.ColorRGBSelector(),
                vol.Optional("color_temp_kelvin"): selector.ColorTempSelector(
                    selector.ColorTempSelectorConfig(unit="kelvin", min=2000, max=6500)
                ),
                **templates,
            }
        )
    if domain == "climate":
        return vol.Schema(
            {
                vol.Optional("state"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=["heat", "off"], translation_key="climate_state"
                    )
                ),
                vol.Optional("temperature"): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=5, max=35, step=0.5, unit_of_measurement="°C",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                **templates,
            }
        )
    return vol.Schema({vol.Optional("current_position"): selector.TemplateSelector()})


def _preset_form_values(domain: str, values: dict[str, Any]) -> dict[str, Any]:
    """Stored preset -> form values: templates of typed fields go to the templates box."""
    from .rules import is_template  # noqa: PLC0415

    if domain == "cover":
        return {k: str(v) for k, v in values.items()}
    form: dict[str, Any] = {}
    templates: dict[str, Any] = {}
    for key, value in values.items():
        if is_template(value):
            templates[key] = value
        else:
            form[key] = value
    if templates:
        form[TEMPLATES] = templates
    return form


def _preset_from_form(domain: str, user_input: dict[str, Any]) -> dict[str, Any]:
    """Form values -> stored preset (templates over typed fields)."""
    values = {k: v for k, v in user_input.items() if k != TEMPLATES and v not in (None, "")}
    if "brightness_pct" in values:
        values["brightness_pct"] = int(values["brightness_pct"])
    if "rgb_color" in values:
        values["rgb_color"] = [int(c) for c in values["rgb_color"]]
    if "color_temp_kelvin" in values:
        values["color_temp_kelvin"] = int(values["color_temp_kelvin"])
    if domain == "cover" and "current_position" in values:
        text = str(values["current_position"]).strip()
        try:
            values["current_position"] = int(float(text))
        except ValueError:
            values["current_position"] = text
    templates = user_input.get(TEMPLATES) or {}
    if not isinstance(templates, dict):
        raise vol.Invalid("templates: a mapping of attribute: template")
    values.update({str(k): v for k, v in templates.items()})
    parse_defaults(domain, values)  # raises vol.Invalid
    return values


IMMICH_TRIPS_ADVANCED = (
    "max_gap_days", "name_min_share", "name_min_photos", "min_assets",
    "home_country", "detail", "names", "min_days",
)


def _immich_schema(conf: dict[str, Any]) -> vol.Schema:
    users = list((conf.get("users") or {}).keys())
    password = selector.TextSelector(
        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
    )
    faceless_action = selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=["delete", "trash", "keep", "album"],
            translation_key="faceless_action",
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )
    user_fields: dict[Any, Any] = {vol.Optional(user): password for user in users}
    user_fields[vol.Optional("new_user")] = selector.TextSelector()
    user_fields[vol.Optional("new_user_key")] = password
    return vol.Schema(
        {
            vol.Required("general"): section(
                vol.Schema(
                    {
                        vol.Optional("url"): selector.TextSelector(
                            selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
                        ),
                        vol.Optional("scan_interval", default=600): selector.NumberSelector(
                            selector.NumberSelectorConfig(
                                min=0, max=86400, step=1, unit_of_measurement="s",
                                mode=selector.NumberSelectorMode.BOX,
                            )
                        ),
                        vol.Optional("owner"): selector.SelectSelector(
                            selector.SelectSelectorConfig(options=users or ["-"])
                        ),
                        vol.Optional("share_albums", default=False): selector.BooleanSelector(),
                    }
                ),
                {"collapsed": True},
            ),
            vol.Required("users"): section(vol.Schema(user_fields), {"collapsed": True}),
            vol.Required("people"): section(
                vol.Schema({vol.Optional("people"): selector.ObjectSelector()}),
                {"collapsed": True},
            ),
            vol.Required("trips"): section(
                vol.Schema(
                    {
                        vol.Optional("distance_km", default=50): selector.NumberSelector(
                            selector.NumberSelectorConfig(
                                min=0, max=10000, step=1, unit_of_measurement="km",
                                mode=selector.NumberSelectorMode.BOX,
                            )
                        ),
                        vol.Optional("advanced"): selector.ObjectSelector(),
                    }
                ),
                {"collapsed": True},
            ),
            vol.Required("faceless"): section(
                vol.Schema(
                    {
                        vol.Optional("home", default="album"): faceless_action,
                        vol.Optional("trip", default="album"): faceless_action,
                        vol.Optional("album", default="check_before_delete"): selector.TextSelector(),
                        vol.Optional("wait_hours", default=24): selector.NumberSelector(
                            selector.NumberSelectorConfig(
                                min=0, max=720, step=1, unit_of_measurement="h",
                                mode=selector.NumberSelectorMode.BOX,
                            )
                        ),
                    }
                ),
                {"collapsed": True},
            ),
            vol.Required("partou"): section(
                vol.Schema(
                    {
                        vol.Optional("username"): selector.TextSelector(
                            selector.TextSelectorConfig(type=selector.TextSelectorType.EMAIL)
                        ),
                        vol.Optional("password"): password,
                        vol.Optional("after", default="18:00"): selector.TimeSelector(),
                    }
                ),
                {"collapsed": True},
            ),
        }
    )


def _immich_form_values(conf: dict[str, Any]) -> dict[str, Any]:
    """Stored immich -> suggested form values (secrets left out: empty keeps them)."""
    trips = conf.get("trips") or {}
    partou = conf.get("partou") or {}
    return {
        "general": {
            k: conf[k] for k in ("url", "scan_interval", "owner", "share_albums") if k in conf
        },
        "users": {},
        "people": {"people": conf.get("people") or {}},
        "trips": {
            **({"distance_km": trips["distance_km"]} if "distance_km" in trips else {}),
            "advanced": {k: trips[k] for k in IMMICH_TRIPS_ADVANCED if k in trips},
        },
        "faceless": dict(conf.get("faceless") or {}),
        "partou": {k: partou[k] for k in ("username", "after") if k in partou},
    }


def _immich_from_form(old: dict[str, Any], user_input: dict[str, Any]) -> dict[str, Any] | None:
    """Form -> stored immich (validated); None when the address is empty (Immich off)."""
    general = user_input.get("general") or {}
    url = (general.get("url") or "").strip()
    if not url:
        return None
    users_in = dict(user_input.get("users") or {})
    new_user = (users_in.pop("new_user", "") or "").strip()
    new_key = users_in.pop("new_user_key", "") or ""
    users: dict[str, str] = {}
    for user, key in (old.get("users") or {}).items():
        typed = (users_in.get(user) or "").strip()
        if not typed:
            users[user] = key  # left empty: keep the stored key
        elif typed != "-":
            users[user] = typed
        # "-": the user is removed
    if new_user:
        if not new_key.strip():
            raise vol.Invalid("new_user_key")
        users[new_user] = new_key.strip()
    conf: dict[str, Any] = {
        "url": url,
        "users": users,
        "scan_interval": int(general.get("scan_interval", 600)),
        "share_albums": bool(general.get("share_albums", False)),
        "people": (user_input.get("people") or {}).get("people") or {},
    }
    owner = general.get("owner")
    if owner and owner in users:
        conf["owner"] = owner
    trips_in = user_input.get("trips") or {}
    advanced = trips_in.get("advanced") or {}
    if not isinstance(advanced, dict):
        raise vol.Invalid("trips")
    conf["trips"] = {**advanced, "distance_km": float(trips_in.get("distance_km", 50))}
    faceless = user_input.get("faceless") or {}
    conf["faceless"] = {
        "home": faceless.get("home") or "album",
        "trip": faceless.get("trip") or "album",
        "album": faceless.get("album") or "check_before_delete",
        "wait_hours": float(faceless.get("wait_hours", 24)),
    }
    partou_in = user_input.get("partou") or {}
    username = (partou_in.get("username") or "").strip()
    if username:
        old_partou = old.get("partou") or {}
        partou = {
            k: v for k, v in old_partou.items() if k not in ("username", "password", "after")
        }
        partou["username"] = username
        partou["password"] = partou_in.get("password") or old_partou.get("password") or ""
        if not partou["password"]:
            raise vol.Invalid("partou_password")
        partou["after"] = str(partou_in.get("after") or "18:00")[:5]
        conf["partou"] = partou
    IMMICH_SCHEMA(copy.deepcopy(conf))  # raises vol.Invalid
    return conf


class GungorsOptionsFlow(OptionsFlow):
    """Configure: Presetler / Immich."""

    def _options(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self.config_entry.options))

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["presets", "services"])

    async def async_step_services(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(step_id="services", menu_options=["immich", "budget_thuis"])

    async def async_step_budget_thuis(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """E-mail and interval; a new login mails a link (Budget Thuis has no password)."""
        from .budget_thuis import BudgetThuisApi, BudgetThuisError  # noqa: PLC0415

        options = self._options()
        old = options.get("budget_thuis") or {}
        errors: dict[str, str] = {}
        placeholders = {"error": "", "status": "bağlı" if old.get("refresh_token") else "bağlı değil"}
        if user_input is not None:
            email = (user_input.get("username") or "").strip()
            interval = int(user_input.get("scan_interval", DEFAULT_BT_INTERVAL))
            if not email:
                options.pop("budget_thuis", None)
                return self.async_create_entry(data=options)
            new_login = (
                user_input.get("new_login")
                or not old.get("refresh_token")
                or email != old.get("username")
            )
            if not new_login:
                options["budget_thuis"] = {**old, "scan_interval": interval}
                return self.async_create_entry(data=options)
            api = BudgetThuisApi(async_get_clientsession(self.hass))
            try:
                self._bt_verifier = await api.request_link(email)
            except BudgetThuisError as err:
                errors["base"] = "budget_thuis"
                placeholders["error"] = str(err)
            else:
                self._bt_login = {"username": email, "scan_interval": interval}
                return await self.async_step_budget_thuis_link()
        schema = vol.Schema(
            {
                vol.Optional("username"): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.EMAIL)
                ),
                vol.Optional("new_login", default=False): selector.BooleanSelector(),
                vol.Optional("scan_interval", default=DEFAULT_BT_INTERVAL): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=300, max=86400, step=60, unit_of_measurement="s",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        suggested = {k: old[k] for k in ("username", "scan_interval") if k in old}
        return self.async_show_form(
            step_id="budget_thuis",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or suggested),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_budget_thuis_link(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Paste the link from the mail (copied, not clicked)."""
        from .budget_thuis import BudgetThuisApi, BudgetThuisError  # noqa: PLC0415

        errors: dict[str, str] = {}
        placeholders = {"error": "", "email": self._bt_login["username"]}
        if user_input is not None and (user_input.get("link") or "").strip():
            api = BudgetThuisApi(async_get_clientsession(self.hass))
            try:
                tokens = await api.login_with_link(user_input["link"], self._bt_verifier)
            except BudgetThuisError as err:
                errors["base"] = "budget_thuis"
                placeholders["error"] = str(err)
            else:
                if not tokens.get("refresh_token"):
                    errors["base"] = "budget_thuis"
                    placeholders["error"] = "no refresh token"
                else:
                    options = self._options()
                    options["budget_thuis"] = {
                        **self._bt_login,
                        "refresh_token": tokens["refresh_token"],
                        "login_at": time.time(),
                    }
                    return self.async_create_entry(data=options)
        return self.async_show_form(
            step_id="budget_thuis_link",
            data_schema=vol.Schema({vol.Optional("link"): selector.TextSelector()}),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_presets(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """One form: a closed section per preset, opened by its header."""
        options = self._options()
        errors: dict[str, str] = {}
        placeholders = {"error": ""}
        stored_all = options.get("presets") or {}
        if user_input is not None:
            new: dict[str, dict[str, Any]] = {}
            try:
                for domain, name in PRESETS:
                    key = f"{domain}_{name}"
                    try:
                        new.setdefault(domain, {})[name] = _preset_from_form(
                            domain, user_input.get(key) or {}
                        )
                    except vol.Invalid as err:
                        raise vol.Invalid(f"{key}: {err.msg if hasattr(err, 'msg') else err}") from err
            except vol.Invalid as err:
                errors["base"] = "invalid_preset"
                placeholders["error"] = str(err.msg)
            else:
                presets = {d: dict(v) for d, v in stored_all.items()}
                for domain, values in new.items():
                    presets.setdefault(domain, {}).update(values)
                options["presets"] = presets
                return self.async_create_entry(data=options)
        builtin = builtin_presets()
        fields: dict[Any, Any] = {}
        suggested: dict[str, Any] = {}
        for domain, name in PRESETS:
            key = f"{domain}_{name}"
            fields[vol.Required(key)] = section(_preset_schema(domain), {"collapsed": True})
            stored = (stored_all.get(domain) or {}).get(name)
            if stored is None:
                stored = builtin.get(domain, {}).get(name, {})
            suggested[key] = _preset_form_values(domain, stored)
        schema = self.add_suggested_values_to_schema(
            vol.Schema(fields), user_input if user_input is not None else suggested
        )
        return self.async_show_form(
            step_id="presets", data_schema=schema, errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_immich(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = self._options()
        old = options.get("immich") or {}
        errors: dict[str, str] = {}
        placeholders = {"error": ""}
        if user_input is not None:
            try:
                conf = _immich_from_form(old, user_input)
            except vol.Invalid as err:
                errors["base"] = "invalid_immich"
                placeholders["error"] = str(err.msg if hasattr(err, "msg") else err)
            else:
                if conf is None:
                    options.pop("immich", None)
                else:
                    options["immich"] = conf
                return self.async_create_entry(data=options)
        schema = self.add_suggested_values_to_schema(
            _immich_schema(old),
            user_input if user_input is not None else _immich_form_values(old),
        )
        return self.async_show_form(
            step_id="immich", data_schema=schema, errors=errors, description_placeholders=placeholders
        )


__all__ = ["GungorsConfigFlow", "GungorsOptionsFlow", "NAMES"]
