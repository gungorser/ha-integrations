"""Budget Thuis (Budget Energie): electricity, gas and return delivery as sensors.

Configure > Servisler > Budget Thuis holds the login (e-mail and password of the
Budget Thuis app) and the scan interval (default hourly). Each run reads the daily
usage of the current month from the API the Budget Thuis app uses (unofficial):

- login: OAuth2 authorization code + PKCE through the accounts site's login form
  (client `mobile`), then refresh tokens (kept in `.storage/gungors.budget_thuis`,
  so a run does not log in again);
- contracts: `/home/v1/contactPerson` -> relation ids -> `/energy/v2/customer/productPicker`;
- usage: `/energy/v1/contract/<id>/usagecosts/details?PeriodDuration=Day`, summed
  over every contract: per day `usage*` blocks with `rawUsage` (electricity
  consumption, electricity production, gas).

Sensors (sensor.py): `sensor.budget_thuis_elektrik` (kWh), `sensor.budget_thuis_gaz`
(m³), `sensor.budget_thuis_teruglevering` (kWh): the month so far, `total_increasing`
(a new month starts again at 0), so the Energy dashboard takes them. Attributes: the
last day with data and its value. Readings lag a day or two.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import logging
import re
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import aiohttp

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

DATA_BUDGET_THUIS = "budget_thuis"
SIGNAL_UPDATE = f"{DOMAIN}_budget_thuis_update"
DEFAULT_SCAN_INTERVAL = 3600

ACCOUNTS_URL = "https://accounts.budgetthuis.nl"
API_URL = "https://app.api.nutsservices.nl"
CLIENT_ID = "mobile"
REDIRECT_URI = "budgetthuis://login_success"
SCOPE = "mobileApi webApi offline_access openid email idsServiceExternal"
LABEL = "BudgetEnergie"
USER_AGENT = "Budget Thuis/3.1.0 (nl.budgetthuis.app; build:2940; iOS 26.6.0) Alamofire/5.10.2"
_TOKEN_RE = re.compile(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"')
_REDIRECTS = (301, 302, 303, 307, 308)
_TIMEOUT = aiohttp.ClientTimeout(total=60)
_PERIOD_FMT = "%Y-%m-%dT%H:%M:%S.000"

KINDS = ("elektrik", "gaz", "teruglevering")


class BudgetThuisError(Exception):
    """A Budget Thuis call failed."""


class BudgetThuisAuthError(BudgetThuisError):
    """Wrong login, or a token the server refused."""


def kind_of(key: str) -> str | None:
    """`usageElectricityConsumption` -> elektrik, `...Production` -> teruglevering, gas -> gaz."""
    low = key.lower()
    if not low.startswith("usage"):
        return None
    if "gas" in low:
        return "gaz"
    if "electricity" in low and "production" in low:
        return "teruglevering"
    if "electricity" in low and "consumption" in low:
        return "elektrik"
    return None


def usage_days(payload: dict[str, Any]) -> dict[datetime.date, dict[str, float]]:
    """usagecosts/details -> {day: {kind: amount}} (days without a reading left out)."""
    days: dict[datetime.date, dict[str, float]] = {}
    for overview in payload.get("overviewPeriods") or []:
        for period in overview.get("usageCostsPerPeriod") or []:
            start = period.get("periodFrom")
            if not isinstance(start, str):
                continue
            try:
                day = datetime.datetime.fromisoformat(start).date()
            except ValueError:
                continue
            for key, block in period.items():
                kind = kind_of(key)
                if kind is None or not isinstance(block, dict):
                    continue
                try:
                    amount = float(block.get("rawUsage"))
                except (TypeError, ValueError):
                    continue
                days.setdefault(day, {})
                days[day][kind] = days[day].get(kind, 0.0) + amount
    return days


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge.rstrip(b"=").decode()


def _query(url: str, key: str) -> str | None:
    values = parse_qs(urlparse(url).query).get(key)
    return values[0] if values else None


def _location(resp: aiohttp.ClientResponse, step: str) -> str:
    if resp.status not in _REDIRECTS or not resp.headers.get("Location"):
        raise BudgetThuisError(f"{step}: no redirect ({resp.status})")
    return resp.headers["Location"]


class BudgetThuisApi:
    """Login, token refresh and the usage of one Budget Thuis account."""

    def __init__(self, session: aiohttp.ClientSession, accounts_url: str = ACCOUNTS_URL,
                 api_url: str = API_URL) -> None:
        self._session = session
        self._accounts = accounts_url.rstrip("/")
        self._api = api_url.rstrip("/")

    async def login(self, username: str, password: str) -> dict[str, Any]:
        """The app's login: authorize -> login form -> code -> tokens."""
        verifier, challenge = _pkce()
        try:
            async with aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=True), headers={"User-Agent": USER_AGENT},
                timeout=_TIMEOUT,
            ) as s:
                async with s.get(
                    f"{self._accounts}/connect/authorize",
                    params={
                        "client_id": CLIENT_ID, "response_type": "code",
                        "redirect_uri": REDIRECT_URI, "scope": SCOPE, "label": LABEL,
                        "code_challenge": challenge, "code_challenge_method": "S256",
                    },
                    allow_redirects=False,
                ) as resp:
                    login_url = _location(resp, "authorize")
                return_url = _query(login_url, "ReturnUrl")
                if not return_url:
                    raise BudgetThuisError("authorize: no ReturnUrl")
                async with s.get(urljoin(self._accounts, login_url), allow_redirects=False) as resp:
                    match = _TOKEN_RE.search(await resp.text())
                if not match:
                    raise BudgetThuisError("login page: no verification token")
                async with s.post(
                    f"{self._accounts}/inloggen",
                    data={
                        "Username": username, "Password": password, "ReturnUrl": return_url,
                        "Label": LABEL, "__RequestVerificationToken": match.group(1),
                        "button": "login",
                    },
                    allow_redirects=False,
                ) as resp:
                    if resp.status not in _REDIRECTS:
                        raise BudgetThuisAuthError("e-posta veya şifre yanlış")
                    callback_url = _location(resp, "login")
                async with s.get(urljoin(self._accounts, callback_url), allow_redirects=False) as resp:
                    code = _query(_location(resp, "callback"), "code")
                if not code:
                    raise BudgetThuisAuthError("login: no authorization code")
                return await self._token(s, {
                    "grant_type": "authorization_code", "code": code,
                    "code_verifier": verifier, "redirect_uri": REDIRECT_URI,
                    "client_id": CLIENT_ID,
                })
        except (TimeoutError, aiohttp.ClientError) as err:
            raise BudgetThuisError(f"login: {err}") from err

    async def refresh(self, refresh_token: str) -> dict[str, Any]:
        try:
            tokens = await self._token(self._session, {
                "grant_type": "refresh_token", "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
            })
        except (TimeoutError, aiohttp.ClientError) as err:
            raise BudgetThuisError(f"refresh: {err}") from err
        tokens.setdefault("refresh_token", refresh_token)
        return tokens

    async def _token(self, session: aiohttp.ClientSession, data: dict[str, str]) -> dict[str, Any]:
        async with session.post(
            f"{self._accounts}/connect/token", data=data,
            headers={"User-Agent": USER_AGENT}, allow_redirects=False,
        ) as resp:
            if resp.status in (400, 401, 403):
                raise BudgetThuisAuthError(f"token: {resp.status}")
            if resp.status != 200:
                raise BudgetThuisError(f"token: {resp.status}")
            payload = await resp.json(content_type=None)
        if not isinstance(payload, dict) or "access_token" not in payload:
            raise BudgetThuisError("token: no access_token")
        result = {"access_token": payload["access_token"],
                  "expires_at": time.time() + float(payload.get("expires_in", 3600))}
        if payload.get("refresh_token"):
            result["refresh_token"] = payload["refresh_token"]
        return result

    async def _request(self, token: str, method: str, path: str, **kwargs: Any) -> Any:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
                   "User-Agent": USER_AGENT}
        try:
            async with self._session.request(
                method, self._api + path, headers=headers, timeout=_TIMEOUT, **kwargs
            ) as resp:
                label = path.split("/")[3] if path.count("/") >= 3 else path
                if resp.status in (401, 403):
                    raise BudgetThuisAuthError(f"{label}: {resp.status}")
                if resp.status != 200:
                    raise BudgetThuisError(f"{label}: {resp.status}")
                return await resp.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError) as err:
            raise BudgetThuisError(f"{method} {path.split('/')[1]}: {err}") from err

    async def contracts(self, token: str) -> list[str]:
        """The active contract ids of the account."""
        person = await self._request(token, "GET", "/home/v1/contactPerson") or {}
        relation_ids = [
            pc["productCustomerId"]
            for customer in (person.get("contactPerson") or {}).get("customers") or []
            for pc in customer.get("productCustomers") or []
            if pc.get("productCustomerId") is not None
        ]
        if not relation_ids:
            return []
        picked = await self._request(
            token, "POST", "/energy/v2/customer/productPicker", json={"relationIds": relation_ids}
        ) or {}
        result = []
        for contract in picked.get("contractsInfo") or []:
            status = str(contract.get("contractStatus") or "").casefold()
            if contract.get("contractId") is not None and status in ("", "active"):
                result.append(str(contract["contractId"]))
        return result

    async def usage(self, token: str, contract_id: str, start: datetime.datetime,
                    end: datetime.datetime) -> dict[str, Any]:
        return await self._request(
            token, "GET", f"/energy/v1/contract/{contract_id}/usagecosts/details",
            params={
                "PeriodDuration": "Day",
                "PeriodFrom": start.strftime(_PERIOD_FMT),
                "PeriodTo": end.strftime(_PERIOD_FMT),
            },
        ) or {}


def async_get_manager(hass: HomeAssistant) -> BudgetThuisManager | None:
    return hass.data.get(DOMAIN, {}).get(DATA_BUDGET_THUIS)


class BudgetThuisManager:
    """Reads the month's usage every `scan_interval` seconds."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        self.hass = hass
        self.config = config
        self.status = "başlamadı"
        self.error: str | None = None
        self.last_run: datetime.datetime | None = None
        self.month: dict[str, float] = {}
        self.last_day: dict[str, tuple[datetime.date, float]] = {}
        self.month_start: datetime.date | None = None
        self.api = BudgetThuisApi(async_get_clientsession(hass))
        self._store: Store = Store(hass, 1, f"{DOMAIN}.budget_thuis")
        self._tokens: dict[str, Any] = {}
        self._unsub = None
        self._logged_keys = False

    async def async_start(self) -> None:
        stored = await self._store.async_load() or {}
        if stored.get("username") == self.config["username"]:
            self._tokens = stored.get("tokens") or {}
        self._schedule()
        self.hass.async_create_task(self.async_run(), "gungors budget thuis")

    async def async_reconfigure(self, config: dict[str, Any]) -> None:
        if (config["username"], config["password"]) != (
            self.config["username"], self.config["password"]
        ):
            self._tokens = {}
        self.config = config
        self._schedule()
        self.hass.async_create_task(self.async_run(), "gungors budget thuis")

    def _schedule(self) -> None:
        self.async_stop()
        seconds = int(self.config.get("scan_interval") or 0)
        if seconds:
            self._unsub = async_track_time_interval(
                self.hass, self._async_tick, datetime.timedelta(seconds=seconds),
                name="gungors budget thuis",
            )

    @callback
    def async_stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    async def _async_tick(self, _now: datetime.datetime) -> None:
        await self.async_run()

    async def _token(self) -> str:
        if self._tokens.get("access_token") and self._tokens.get("expires_at", 0) > time.time() + 60:
            return self._tokens["access_token"]
        tokens = None
        if self._tokens.get("refresh_token"):
            try:
                tokens = await self.api.refresh(self._tokens["refresh_token"])
            except BudgetThuisAuthError:
                tokens = None
        if tokens is None:
            tokens = await self.api.login(self.config["username"], self.config["password"])
        self._tokens = tokens
        await self._store.async_save({"username": self.config["username"], "tokens": tokens})
        return tokens["access_token"]

    async def async_run(self) -> None:
        now = dt_util.now()
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            try:
                days = await self._read(await self._token(), start, end)
            except BudgetThuisAuthError:
                self._tokens = {}  # a refused token: log in again once
                days = await self._read(await self._token(), start, end)
        except BudgetThuisError as err:
            self.status, self.error = "hata", str(err)
            _LOGGER.warning("Budget Thuis: %s", err)
        else:
            self.status, self.error = "tamam", None
            self.month_start = start.date()
            self.month = {kind: 0.0 for kind in KINDS}
            self.last_day = {}
            for day in sorted(days):
                for kind, amount in days[day].items():
                    self.month[kind] = round(self.month[kind] + amount, 3)
                    self.last_day[kind] = (day, round(amount, 3))
        self.last_run = now
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    async def _read(self, token: str, start: datetime.datetime,
                    end: datetime.datetime) -> dict[datetime.date, dict[str, float]]:
        days: dict[datetime.date, dict[str, float]] = {}
        for contract_id in await self.api.contracts(token):
            payload = await self.api.usage(token, contract_id, start, end)
            if not self._logged_keys:
                keys = sorted({
                    key
                    for overview in payload.get("overviewPeriods") or []
                    for period in overview.get("usageCostsPerPeriod") or []
                    for key in period
                })
                _LOGGER.info("Budget Thuis usage fields: %s", ", ".join(keys))
                self._logged_keys = True
            for day, values in usage_days(payload).items():
                for kind, amount in values.items():
                    days.setdefault(day, {})
                    days[day][kind] = days[day].get(kind, 0.0) + amount
        return days
