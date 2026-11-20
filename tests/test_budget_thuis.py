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


def _accounts_app() -> web.Application:
    """Authorize -> /inloggen -> /inloglink-aanvragen (e-mail) -> mailed link -> code."""
    state: dict = {}

    async def authorize(request: web.Request) -> web.Response:
        state["challenge"] = request.query["code_challenge"]
        assert request.query["client_id"] == "mobile"
        if "token" in request.query:  # the mailed autologin token, sent with the app client
            assert request.query["strategy"] == "general_auto_login"
            assert request.query["email"] == "YUBiLmM="
            assert request.query["redirect_uri"] == "budgetthuis://login_success"
            if request.query["token"] != "good":
                raise web.HTTPFound("/automatisch-inloggen-mislukt")
            raise web.HTTPFound("/connect/authorize/callback?x=1&y=2", headers={
                "Set-Cookie": "auth=yes; Path=/"})
        raise web.HTTPFound("/inloggen?" + urlencode({"ReturnUrl": "/connect/authorize/callback?x=1"}))

    async def inloggen(request: web.Request) -> web.Response:
        raise web.HTTPFound("/inloglink-aanvragen?" + urlencode({"returnUrl": request.query["ReturnUrl"]}))

    async def link_page(request: web.Request) -> web.Response:
        resp = web.Response(
            text=(
                '<form method="post" action="/inloglink-aanvragen">'
                '<input type="email" name="EmailAddress" />'
                '<input value="/connect/authorize/callback?x=1&amp;y=2" type="hidden" name="ReturnUrl" />'
                '<input type="hidden" name="Label" value="budgetenergie" />'
                '<input name="__RequestVerificationToken" type="hidden" value="tok123" /></form>'
            ),
            content_type="text/html",
        )
        resp.set_cookie("csrf", "c1")
        return resp

    async def link_post(request: web.Request) -> web.Response:
        form = await request.post()
        assert form["__RequestVerificationToken"] == "tok123"
        assert request.cookies.get("csrf") == "c1"
        assert form["ReturnUrl"] == "/connect/authorize/callback?x=1&y=2"
        state["mailed"] = form["EmailAddress"]
        return web.Response(text="mail sent")

    async def mail_link(request: web.Request) -> web.Response:
        if request.query.get("t") != "good":
            return web.Response(text="verlopen", status=400)
        # a confirm page that posts itself
        return web.Response(
            text='<form method="post" action="/inloglink/bevestig">'
                 '<input type="hidden" name="t" value="good" /></form>',
            content_type="text/html",
        )

    async def confirm(request: web.Request) -> web.Response:
        assert (await request.post())["t"] == "good"
        resp = web.HTTPFound("/connect/authorize/callback?x=1&y=2")
        resp.set_cookie("auth", "yes")
        raise resp

    async def callback(request: web.Request) -> web.Response:
        assert request.cookies.get("auth") == "yes"
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

    async def failed(request: web.Request) -> web.Response:
        return web.Response(text="Inloggen mislukt")

    app = web.Application()
    app["state"] = state
    app.router.add_get("/connect/authorize", authorize)
    app.router.add_get("/inloggen", inloggen)
    app.router.add_get("/inloglink-aanvragen", link_page)
    app.router.add_post("/inloglink-aanvragen", link_post)
    app.router.add_get("/inloglink", mail_link)
    app.router.add_post("/inloglink/bevestig", confirm)
    app.router.add_get("/connect/authorize/callback", callback)
    app.router.add_get("/automatisch-inloggen-mislukt", failed)
    app.router.add_post("/connect/token", token)
    return app


async def test_login_link_and_refresh(hass: HomeAssistant, socket_enabled) -> None:
    app = _accounts_app()
    server = TestServer(app)
    await server.start_server()
    try:
        base = str(server.make_url("")).rstrip("/")
        api = BudgetThuisApi(async_get_clientsession(hass), base)
        verifier = await api.request_link("a@b.c")
        assert app["state"]["mailed"] == "a@b.c" and verifier
        tokens = await api.login_with_link(f"{base}/inloglink?t=good", verifier)
        assert tokens["access_token"] == "A1" and tokens["refresh_token"] == "R1"
        # the end address works too
        tokens = await api.login_with_link("budgetthuis://login_success?code=abc", verifier)
        assert tokens["refresh_token"] == "R1"
        # the mail's "Direct inloggen" link: its token goes to the app's own authorize
        auto = "https://mijn.budgetthuis.nl/autologin/general_auto_login/?token={}&email=YUBiLmM="
        tokens = await api.login_with_link(auto.format("good"), verifier)
        assert tokens["refresh_token"] == "R1"
        with pytest.raises(BudgetThuisAuthError, match="automatisch-inloggen-mislukt"):
            await api.login_with_link(auto.format("old"), verifier)
        refreshed = await api.refresh("R1")
        assert refreshed["access_token"] == "A2" and refreshed["refresh_token"] == "R1"
        with pytest.raises(BudgetThuisAuthError):
            await api.login_with_link(f"{base}/inloglink?t=old", verifier)
        with pytest.raises(BudgetThuisAuthError):
            await api.refresh("bad")
    finally:
        await server.close()


class FakeApi:
    def __init__(self) -> None:
        self.refreshes = 0
        self.refused = False
        self.dead = False
        self.links: list[str] = []

    async def request_link(self, email: str) -> str:
        self.links.append(email)
        return "verifier"

    async def login_with_link(self, link: str, verifier: str) -> dict:
        if link.strip() != "https://mail/link":
            raise BudgetThuisAuthError("giriş linki geçersiz veya süresi dolmuş (400)")
        return {"access_token": "A", "refresh_token": "R0", "expires_at": 9e12}

    async def refresh(self, refresh_token: str) -> dict:
        if self.dead:
            raise BudgetThuisAuthError("token: 400")
        self.refreshes += 1
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
    monkeypatch.setattr(
        "custom_components.gungors.budget_thuis.BudgetThuisApi", lambda *_a, **_k: api
    )
    return api


async def _setup(hass: HomeAssistant, options: dict) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=options)
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    return entry


BT = {"username": "a@b.c", "scan_interval": 3600, "refresh_token": "R0", "login_at": 1.0}


async def test_sensors_month_so_far(hass: HomeAssistant, fake_api: FakeApi) -> None:
    await _setup(hass, {"presets": {}, "budget_thuis": dict(BT)})
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


async def test_refused_token_refreshes_then_asks_for_login(
    hass: HomeAssistant, fake_api: FakeApi
) -> None:
    await _setup(hass, {"presets": {}, "budget_thuis": dict(BT)})
    assert fake_api.refreshes == 1
    fake_api.refused = True
    await budget_thuis.async_get_manager(hass).async_run()
    assert fake_api.refreshes == 2
    assert hass.states.get("sensor.budget_thuis_elektrik").attributes["status"] == "tamam"

    fake_api.dead = True
    fake_api.refused = True
    await budget_thuis.async_get_manager(hass).async_run()
    attrs = hass.states.get("sensor.budget_thuis_elektrik").attributes
    assert attrs["status"] == "hata" and "yeniden giriş" in attrs["error"]


async def _bt_form(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    """Configure > Services > Budget Thuis."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    for step in ("services", "budget_thuis"):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": step}
        )
    assert result["step_id"] == "budget_thuis"
    return result


def _bt(**values) -> dict:
    """The Budget Thuis form."""
    return values


async def test_login_by_link_in_configure(hass: HomeAssistant, fake_api: FakeApi) -> None:
    entry = await _setup(hass, {"presets": {}})
    assert hass.states.get("sensor.budget_thuis_elektrik").state == "unavailable"

    result = await _bt_form(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _bt(username="a@b.c", scan_interval={"hours": 0, "minutes": 30, "seconds": 0})
    )
    assert result["step_id"] == "budget_thuis_link"
    assert fake_api.links == ["a@b.c"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"link": "https://mail/old"}
    )
    assert result["errors"] == {"base": "budget_thuis"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"link": " https://mail/link "}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    bt = entry.options["budget_thuis"]
    assert bt["username"] == "a@b.c" and bt["scan_interval"] == 1800
    assert bt["refresh_token"] == "R0" and bt["login_at"]
    assert float(hass.states.get("sensor.budget_thuis_elektrik").state) == 16.5

    # only the interval: no new mail
    result = await _bt_form(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _bt(username="a@b.c", scan_interval={"hours": 1, "minutes": 0, "seconds": 0})
    )
    assert result["type"] == "create_entry"
    assert fake_api.links == ["a@b.c"]
    assert entry.options["budget_thuis"]["scan_interval"] == 3600
    await hass.async_block_till_done()

    # empty e-mail: off
    result = await _bt_form(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], _bt())
    await hass.async_block_till_done()
    assert "budget_thuis" not in entry.options
    assert budget_thuis.async_get_manager(hass) is None
    assert hass.states.get("sensor.budget_thuis_elektrik").state == "unavailable"


MAIL = (
    "From: Budget Thuis <noreply@budgetthuis.nl>\r\n"
    "To: a@gmail.com\r\nSubject: Je inloglink\r\n"
    "Date: {date}\r\nMIME-Version: 1.0\r\n"
    "Content-Type: text/html; charset=utf-8\r\n\r\n"
    '<a href="https://www.budgetthuis.nl/privacy">privacy</a>'
    '<a href="https://accounts.budgetthuis.nl/inloglink?token=T1&amp;returnUrl=%2Fx">Inloggen</a>'
)

REAL_MAIL = (  # the shape of the real "Uw persoonlijke inloglink" mail (2026-11-20)
    "From: Budget Thuis <noreply@budgetthuis.nl>\r\nSubject: Uw persoonlijke inloglink\r\n"
    "Content-Type: text/html; charset=utf-8\r\n\r\n"
    '<a href="https://www.budgetthuis.nl/app">Download app</a>'
    '<a href="https://mijn.budgetthuis.nl/autologin/general_auto_login/?token=Q2Z=&amp;'
    'email=YUBiLmM=">Direct inloggen</a>'
    '<a href="https://www.budgetthuis.nl/combikorting?utm_term=mail-klik">x</a>'
    '<a href="https://mijn.budgetthuis.nl">MijnBudget</a>'
    '<img src="https://links.budgetthuis.nl/wf/open?upn=u001.abc">'
)


def test_login_link_from_mail() -> None:
    import email  # noqa: PLC0415

    msg = email.message_from_string(MAIL.format(date="Fri, 20 Nov 2026 08:00:00 +0100"))
    assert budget_thuis.login_link(msg) == (
        "https://accounts.budgetthuis.nl/inloglink?token=T1&returnUrl=%2Fx"
    )
    tracked = email.message_from_string(
        "Content-Type: text/plain\r\n\r\nKlik: https://click.mail.example/ls/login?u=1 "
        "of https://www.budgetthuis.nl/contact"
    )
    assert budget_thuis.login_link(tracked) == "https://click.mail.example/ls/login?u=1"
    real = email.message_from_string(REAL_MAIL)
    link = budget_thuis.login_link(real)
    assert link == (
        "https://mijn.budgetthuis.nl/autologin/general_auto_login/?token=Q2Z=&email=YUBiLmM="
    )
    api = BudgetThuisApi(None, "https://acc")
    url = api.app_login_url(link, "verifier")
    query = parse_qs(urlparse(url).query)
    assert url.startswith("https://acc/connect/authorize?")
    assert query["client_id"] == ["mobile"] and query["token"] == ["Q2Z="]
    assert query["email"] == ["YUBiLmM="] and query["strategy"] == ["general_auto_login"]
    assert query["code_challenge_method"] == ["S256"]
    assert api.app_login_url("budgetthuis://login_success?code=a", "v").startswith("budgetthuis:")
    assert budget_thuis.imap_server("x@gmail.com") == "imap.gmail.com"
    assert budget_thuis.imap_server("x@ziggo.nl") == "imap.ziggo.nl"


def test_mail_login_link_imap(monkeypatch) -> None:
    import email.utils  # noqa: PLC0415
    import time  # noqa: PLC0415

    now = time.time()
    mails = {
        b"1": MAIL.format(date=email.utils.formatdate(now - 3600)).replace("T1", "OLD"),
        b"2": MAIL.format(date=email.utils.formatdate(now + 5)),
    }
    calls: dict = {}

    class FakeImap:
        def __init__(self, host, port, timeout=None) -> None:
            calls["host"] = host

        def __enter__(self):
            return self

        def __exit__(self, *_a) -> None:
            pass

        def login(self, user, password) -> None:
            if password != "app":
                raise budget_thuis.imaplib.IMAP4.error("AUTHENTICATIONFAILED")
            calls["user"] = user

        def select(self, box, readonly=False) -> None:
            calls["readonly"] = readonly

        def search(self, _charset, *criteria):
            calls["criteria"] = criteria
            return "OK", [b" ".join(sorted(mails))]

        def fetch(self, num, _what):
            return "OK", [(b"x", mails[num].encode())]

    monkeypatch.setattr(budget_thuis.imaplib, "IMAP4_SSL", FakeImap)
    link = budget_thuis.mail_login_link("a@gmail.com", "app", now)
    assert link.endswith("token=T1&returnUrl=%2Fx")
    assert calls["host"] == "imap.gmail.com" and calls["readonly"] is True
    assert calls["criteria"][-2:] == ("FROM", '"budget"')
    # only the old mail: nothing yet
    del mails[b"2"]
    assert budget_thuis.mail_login_link("a@gmail.com", "app", now) is None
    with pytest.raises(budget_thuis.BudgetThuisMailError):
        budget_thuis.mail_login_link("a@gmail.com", "wrong", now)


@pytest.fixture
def fake_mail(monkeypatch) -> dict:
    """mail_login_link: returns `box["link"]`; no waiting."""
    box: dict = {"link": "https://mail/link", "reads": 0}

    def read(address, password, since):
        box["reads"] += 1
        box["password"] = password
        return box["link"]

    monkeypatch.setattr(budget_thuis, "mail_login_link", read)
    monkeypatch.setattr(budget_thuis, "MAIL_POLL", 0)
    return box


async def test_login_by_mail_in_configure(
    hass: HomeAssistant, fake_api: FakeApi, fake_mail: dict
) -> None:
    entry = await _setup(hass, {"presets": {}})
    hour = {"hours": 1, "minutes": 0, "seconds": 0}
    result = await _bt_form(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _bt(username="a@gmail.com", mail_password="app", scan_interval=hour)
    )
    # the form saves at once; the mail login runs in the background
    assert result["type"] == "create_entry", result
    bt = entry.options["budget_thuis"]
    assert bt["mail_password"] == "app" and bt["login_at"] and "refresh_token" not in bt
    await hass.async_block_till_done()
    assert fake_api.links == ["a@gmail.com"] and fake_mail["password"] == "app"
    elec = hass.states.get("sensor.budget_thuis_elektrik")
    assert float(elec.state) == 16.5 and elec.attributes["status"] == "tamam"

    # the stored password is prefilled (a password field hides it); kept as is: no new login
    result = await _bt_form(hass, entry)
    for marker in result["data_schema"].schema:
        if marker.schema == "mail_password":
            assert marker.description["suggested_value"] == "app"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        _bt(username="a@gmail.com", mail_password="app",
            scan_interval={"hours": 0, "minutes": 30, "seconds": 0}),
    )
    assert result["type"] == "create_entry"
    assert entry.options["budget_thuis"]["mail_password"] == "app"
    await hass.async_block_till_done()
    assert fake_api.links == ["a@gmail.com"]

    # no mail in time: status hata with the reason; "new login" tries again at once
    fake_mail["link"] = None
    budget_thuis.MAIL_WAIT = -1
    try:
        result = await _bt_form(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            _bt(username="a@gmail.com", mail_password="app", new_login=True, scan_interval=hour),
        )
        assert result["type"] == "create_entry"
        await hass.async_block_till_done()
        attrs = hass.states.get("sensor.budget_thuis_elektrik").attributes
        assert attrs["status"] == "hata" and "gelmedi" in attrs["error"]
        assert fake_api.links == ["a@gmail.com", "a@gmail.com"]
    finally:
        budget_thuis.MAIL_WAIT = 150
    await hass.async_block_till_done()


async def test_refused_token_logs_in_again_by_mail(
    hass: HomeAssistant, fake_api: FakeApi, fake_mail: dict
) -> None:
    await _setup(hass, {"presets": {}, "budget_thuis": {**BT, "mail_password": "app"}})
    fake_api.dead = True
    fake_api.refused = True
    manager = budget_thuis.async_get_manager(hass)
    await manager.async_run()
    assert fake_mail["reads"] == 1 and fake_api.links == ["a@b.c"]
    assert hass.states.get("sensor.budget_thuis_elektrik").attributes["status"] == "tamam"

    # not again within RELOGIN_EVERY
    manager._tokens = {}
    await manager.async_run()
    attrs = hass.states.get("sensor.budget_thuis_elektrik").attributes
    assert attrs["status"] == "hata" and "yeniden giriş" in attrs["error"]
    assert fake_mail["reads"] == 1
