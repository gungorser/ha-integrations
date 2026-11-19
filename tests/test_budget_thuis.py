"""Budget Thuis: login flow, usage parsing, the manager and its sensors."""
from __future__ import annotations

import datetime
from urllib.parse import parse_qs, urlencode, urlparse

from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.gungors import budget_thuis
from custom_components.gungors.budget_thuis import (
    BudgetThuisApi,
    BudgetThuisAuthError,
    kind_of,
    usage_days,
)
from custom_components.gungors.const import DOMAIN


def _period(day: str, elec: float, prod: float, gas: float | None) -> dict:
    period = {
        "periodFrom": f"{day}T00:00:00+01:00",
        "usageElectricityConsumption": {"rawUsage": elec, "isFinal": True},
        "usageElectricityProduction": {"rawUsage": prod, "isFinal": True},
        "totalCosts": {"totalRawCosts": {"amountGross": 3.2}},
    }
    if gas is not None:
        period["usageGasConsumption"] = {"rawUsage": gas, "isFinal": False}
    return period


PAYLOAD = {"overviewPeriods": [{"usageCostsPerPeriod": [
    _period("2026-11-01", 8.5, 1.25, 3.1),
    _period("2026-11-02", 7.0, 0.0, None),
    {"periodFrom": "2026-11-03T00:00:00+01:00", "usageElectricityConsumption": {"rawUsage": None}},
]}]}


def test_kind_of() -> None:
    assert kind_of("usageElectricityConsumption") == "elektrik"
    assert kind_of("usageElectricityProduction") == "teruglevering"
    assert kind_of("usageGasConsumption") == "gaz"
    assert kind_of("usageGas") == "gaz"
    assert kind_of("totalCosts") is None


def test_usage_days() -> None:
    days = usage_days(PAYLOAD)
    assert days == {
        datetime.date(2026, 11, 1): {"elektrik": 8.5, "teruglevering": 1.25, "gaz": 3.1},
        datetime.date(2026, 11, 2): {"elektrik": 7.0, "teruglevering": 0.0},
    }


def _accounts_app(password: str) -> web.Application:
    state: dict = {}

    async def authorize(request: web.Request) -> web.Response:
        state["challenge"] = request.query["code_challenge"]
        assert request.query["client_id"] == "mobile"
        raise web.HTTPFound("/inloggen?" + urlencode({"ReturnUrl": "/connect/authorize/callback?x=1"}))

    async def login_page(request: web.Request) -> web.Response:
        resp = web.Response(
            text='<form><input name="__RequestVerificationToken" type="hidden" value="tok123" /></form>',
            content_type="text/html",
        )
        resp.set_cookie("csrf", "c1")
        return resp

    async def login_post(request: web.Request) -> web.Response:
        form = await request.post()
        assert form["__RequestVerificationToken"] == "tok123"
        assert request.cookies.get("csrf") == "c1"
        if form["Password"] != password:
            return web.Response(text="wrong", status=200)
        raise web.HTTPFound(form["ReturnUrl"])

    async def callback(request: web.Request) -> web.Response:
        raise web.HTTPFound("budgetthuis://login_success?code=abc")

    async def token(request: web.Request) -> web.Response:
        form = await request.post()
        if form["grant_type"] == "authorization_code":
            assert form["code"] == "abc" and form["code_verifier"]
            return web.json_response(
                {"access_token": "A1", "refresh_token": "R1", "expires_in": 3600}
            )
        if form["refresh_token"] == "R1":
            return web.json_response({"access_token": "A2", "expires_in": 3600})
        return web.json_response({"error": "invalid_grant"}, status=400)

    app = web.Application()
    app.router.add_get("/connect/authorize", authorize)
    app.router.add_get("/inloggen", login_page)
    app.router.add_post("/inloggen", login_post)
    app.router.add_get("/connect/authorize/callback", callback)
    app.router.add_post("/connect/token", token)
    return app


async def test_login_and_refresh(hass: HomeAssistant, socket_enabled) -> None:
    server = TestServer(_accounts_app("secret"))
    await server.start_server()
    try:
        api = BudgetThuisApi(async_get_clientsession(hass), str(server.make_url("")))
        tokens = await api.login("a@b.c", "secret")
        assert tokens["access_token"] == "A1" and tokens["refresh_token"] == "R1"
        refreshed = await api.refresh("R1")
        assert refreshed == {**refreshed, "access_token": "A2", "refresh_token": "R1"}
        with pytest.raises(BudgetThuisAuthError):
            await api.login("a@b.c", "wrong")
        with pytest.raises(BudgetThuisAuthError):
            await api.refresh("bad")
    finally:
        await server.close()


class FakeApi:
    def __init__(self) -> None:
        self.logins = 0
        self.refused = False

    async def login(self, username: str, password: str) -> dict:
        self.logins += 1
        if password != "pw":
            raise BudgetThuisAuthError("e-posta veya şifre yanlış")
        return {"access_token": "A", "refresh_token": "R", "expires_at": 9e12}

    async def refresh(self, refresh_token: str) -> dict:
        return {"access_token": "A", "refresh_token": "R", "expires_at": 9e12}

    async def contracts(self, token: str) -> list[str]:
        if self.refused:
            self.refused = False
            raise BudgetThuisAuthError("contactPerson: 401")
        return ["c1", "c2"]

    async def usage(self, token, contract_id, start, end) -> dict:
        assert start.day == 1 and start.hour == 0
        return PAYLOAD if contract_id == "c1" else {"overviewPeriods": [{"usageCostsPerPeriod": [
            _period("2026-11-02", 1.0, 0.0, 2.0)]}]}


@pytest.fixture
def fake_api(monkeypatch) -> FakeApi:
    api = FakeApi()
    monkeypatch.setattr(budget_thuis, "BudgetThuisApi", lambda *_a, **_k: api)
    return api


async def _setup(hass: HomeAssistant, options: dict) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=options)
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    return entry


async def test_sensors_month_so_far(hass: HomeAssistant, fake_api: FakeApi) -> None:
    await _setup(hass, {"presets": {}, "budget_thuis": {
        "username": "a@b.c", "password": "pw", "scan_interval": 3600}})
    elec = hass.states.get("sensor.budget_thuis_elektrik")
    assert float(elec.state) == 16.5
    assert elec.attributes["unit_of_measurement"] == "kWh"
    assert elec.attributes["state_class"] == "total_increasing"
    assert elec.attributes["last_day"] == "2026-11-02"
    assert elec.attributes["last_day_value"] == 8.0
    gas = hass.states.get("sensor.budget_thuis_gaz")
    assert float(gas.state) == 5.1
    assert gas.attributes["unit_of_measurement"] == "m³"
    assert gas.attributes["device_class"] == "gas"
    assert float(hass.states.get("sensor.budget_thuis_teruglevering").state) == 1.25


async def test_refused_token_logs_in_again(hass: HomeAssistant, fake_api: FakeApi) -> None:
    await _setup(hass, {"presets": {}, "budget_thuis": {
        "username": "a@b.c", "password": "pw", "scan_interval": 3600}})
    assert fake_api.logins == 1
    fake_api.refused = True
    await budget_thuis.async_get_manager(hass).async_run()
    assert fake_api.logins == 2
    assert hass.states.get("sensor.budget_thuis_elektrik").attributes["status"] == "tamam"


async def test_wrong_password_and_not_configured(hass: HomeAssistant, fake_api: FakeApi) -> None:
    entry = await _setup(hass, {"presets": {}})
    assert hass.states.get("sensor.budget_thuis_elektrik").state == "unavailable"

    result = await hass.config_entries.options.async_init(entry.entry_id)
    for step in ("services", "budget_thuis"):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": step}
        )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"username": "a@b.c", "password": "nope", "scan_interval": 1800}
    )
    await hass.async_block_till_done()
    assert entry.options["budget_thuis"] == {
        "username": "a@b.c", "password": "nope", "scan_interval": 1800}
    manager = budget_thuis.async_get_manager(hass)
    assert manager.status == "hata" and "şifre" in manager.error

    # password left empty keeps it; a new one replaces it and runs at once
    result = await hass.config_entries.options.async_init(entry.entry_id)
    for step in ("services", "budget_thuis"):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": step}
        )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"username": "a@b.c", "password": "pw"}
    )
    await hass.async_block_till_done()
    assert float(hass.states.get("sensor.budget_thuis_elektrik").state) == 16.5

    result = await hass.config_entries.options.async_init(entry.entry_id)
    for step in ("services", "budget_thuis"):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": step}
        )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert "budget_thuis" not in entry.options
    assert budget_thuis.async_get_manager(hass) is None
    assert hass.states.get("sensor.budget_thuis_elektrik").state == "unavailable"
