"""Budget Thuis (Budget Energie): electricity, gas and return delivery as sensors.

Configure > Servisler > Budget Thuis logs in and sets the scan interval (default
hourly). Budget Thuis has no password login: it mails a link. With the mailbox's
password (Gmail: an app password) gungors reads that mail itself over IMAP (INBOX,
read-only, mails from "budget" since the request) and follows the link, in the background
(status `giriş yapılıyor`; the form has closed); without it the link is pasted (copied,
not clicked) on the next page. The login's refresh token is kept; a refused one logs in again by
mail on its own (at most every `RELOGIN_EVERY` seconds), or asks for a new login
(status `hata`, error "yeniden giriş gerekli"). Each run reads the daily usage of the
current month from the API the Budget Thuis app uses (unofficial):

- login: OAuth2 authorization code + PKCE (client `mobile`, no secret): authorize ->
  /inloglink-aanvragen (e-mail) -> the mailed link -> budgetthuis://login_success?code=
  -> /connect/token; then refresh tokens (kept in `.storage/gungors.budget_thuis`);
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

import asyncio
import base64
import datetime
import email
from email import policy as email_policy
from email.utils import parsedate_to_datetime
import hashlib
import imaplib
import json
import logging
import re
import secrets
import time
from typing import Any
from html import unescape
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

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
WEB_API_URL = "https://api.budgetthuis.nl"
CLIENT_ID = "mobile"
REDIRECT_URI = "budgetthuis://login_success"
SCOPE = "mobileApi webApi offline_access openid email idsServiceExternal"
LABEL = "BudgetEnergie"
USER_AGENT = "Budget Thuis/3.1.0 (nl.budgetthuis.app; build:2940; iOS 26.6.0) Alamofire/5.10.2"
_FORM_RE = re.compile(r'<form[^>]*action="([^"]*)"', re.IGNORECASE)
_INPUT_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')
_REDIRECTS = (301, 302, 303, 307, 308)
_TIMEOUT = aiohttp.ClientTimeout(total=60)
_PERIOD_FMT = "%Y-%m-%dT%H:%M:%S.000"

MAIL_WAIT = 150  # seconds to wait for the login mail
MAIL_POLL = 10
RELOGIN_EVERY = 6 * 3600
_URL_RE = re.compile(r"""https?://[^\s"'<>]+""", re.IGNORECASE)
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

KINDS = ("elektrik", "gaz", "teruglevering")
RELOGIN = "yeniden giriş gerekli: Yapılandır > Servisler > Budget Thuis"


class BudgetThuisError(Exception):
    """A Budget Thuis call failed."""


class BudgetThuisAuthError(BudgetThuisError):
    """Wrong login, or a token the server refused."""


class BudgetThuisMailError(BudgetThuisError):
    """The login mail could not be read (mailbox login, no mail in time)."""


def imap_server(address: str) -> str:
    """The IMAP server of a mail address (gmail.com -> imap.gmail.com)."""
    domain = address.rsplit("@", 1)[-1].strip().lower()
    if domain in ("gmail.com", "googlemail.com"):
        return "imap.gmail.com"
    return f"imap.{domain}"


def login_link(msg: email.message.Message) -> str | None:
    """The login link in a Budget Thuis mail (None when there is none)."""
    urls: list[str] = []
    for part in msg.walk():
        if part.get_content_maintype() != "text":
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        urls += [unescape(u).rstrip(").,;") for u in _URL_RE.findall(text)]
    for url in urls:  # "Direct inloggen" (mijn.*/autologin/...?token=) or an accounts link
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if "budget" in host and "token=" in parsed.query and (
            "autologin" in parsed.path or host.startswith("accounts.")
        ):
            return url
    for url in urls:  # a wrapped (click tracking) link that says what it is
        if any(word in url.lower() for word in ("inloglink", "inloggen", "login", "token")):
            return url
    if urls:
        _LOGGER.warning(
            "Budget Thuis mail: no login link among the hosts %s",
            sorted({urlparse(u).hostname or "" for u in urls}),
        )
    return None


def _trash(imap: imaplib.IMAP4, address: str, num: bytes) -> None:
    """Move a read login mail to the trash (Gmail: its Trash label; else delete + expunge)."""
    try:
        if imap_server(address) == "imap.gmail.com":
            imap.store(num, "+X-GM-LABELS", "\\Trash")
        else:
            imap.store(num, "+FLAGS", "\\Deleted")
            imap.expunge()
    except imaplib.IMAP4.error as err:
        _LOGGER.warning("Budget Thuis: login mail not deleted: %s", err)


def mail_login_link(address: str, password: str, since: float) -> str | None:
    """Blocking (executor): the login link of the newest Budget Thuis mail since `since`."""
    day = time.gmtime(since - 86400)
    since_day = f"{day.tm_mday:02d}-{_MONTHS[day.tm_mon - 1]}-{day.tm_year}"
    try:
        with imaplib.IMAP4_SSL(imap_server(address), 993, timeout=30) as imap:
            imap.login(address, password)
            imap.select("INBOX")
            _typ, data = imap.search(None, "SINCE", since_day, "FROM", '"budget"')
            ids = data[0].split() if data and data[0] else []
            for num in reversed(ids[-10:]):
                _typ, parts = imap.fetch(num, "(RFC822)")
                raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
                if not raw:
                    continue
                msg = email.message_from_bytes(raw, policy=email_policy.default)
                try:
                    sent = parsedate_to_datetime(str(msg["Date"])).timestamp()
                except (TypeError, ValueError):
                    continue
                if sent < since - 120:  # an older mail: its link is used up
                    continue
                link = login_link(msg)
                if link:
                    _trash(imap, address, num)  # used once: the login mail goes away
                    return link
    except imaplib.IMAP4.error as err:
        raise BudgetThuisMailError(f"mail girişi olmadı: {err}") from err
    except OSError as err:
        raise BudgetThuisMailError(f"mail sunucusu: {err}") from err
    return None


async def async_mail_login(
    hass: HomeAssistant, api: BudgetThuisApi, address: str, password: str,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ask for the login mail, read its link over IMAP and log in -> tokens.

    `state["verifier"]` gets the code verifier as soon as the mail is asked for, so a
    link pasted by hand still works when the mail is not found.
    """
    since = time.time()
    verifier = await api.request_link(address)
    if state is not None:
        state["verifier"] = verifier
    while True:
        await asyncio.sleep(MAIL_POLL)
        link = await hass.async_add_executor_job(mail_login_link, address, password, since)
        if link:
            return await api.login_with_link(link, verifier)
        if time.time() - since > MAIL_WAIT:
            raise BudgetThuisMailError(f"giriş maili {MAIL_WAIT} saniyede gelmedi")


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


def token_claims(token: str) -> dict[str, Any]:
    """The non-secret claims of an access token (JWT) for the log: client, scope, login."""
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}
    keep = ("client_id", "scope", "amr", "idp", "aud", "label", "role", "auth_time")
    return {k: data[k] for k in keep if k in data}


def _key_paths(data: Any, prefix: str = "") -> set[str]:
    """The key paths of a JSON value (lists as []), no values: for the log."""
    paths: set[str] = set()
    if isinstance(data, dict):
        for k, v in data.items():
            paths.add(prefix + k)
            paths |= _key_paths(v, prefix + k + ".")
    elif isinstance(data, list):
        for item in data:
            paths |= _key_paths(item, prefix.rstrip(".") + "[].")
    return paths


def _numbers(data: Any) -> list[str]:
    """Every distinct whole number (or digit string) in a JSON value, customers first."""
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
        elif isinstance(value, bool):
            return
        elif isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
            text = str(value)
            if len(text) >= 4 and text not in found:
                found.append(text)

    walk((data.get("contactPerson") or {}).get("customers") if isinstance(data, dict) else data)
    walk(data)
    return found[:12]


def product_summary(person: dict[str, Any]) -> list[str]:
    """contactPerson -> the product kinds per relation (no ids or names) for the log."""
    kinds = []
    for customer in (person.get("contactPerson") or {}).get("customers") or []:
        for pc in customer.get("productCustomers") or []:
            kinds.append(",".join(
                f"{k}={v}" for k, v in pc.items()
                if isinstance(v, str) and k.lower().endswith(("type", "label", "status", "brand"))
            ) or "?")
    return kinds


def _inputs(html: str) -> list[dict[str, str]]:
    """Every <input> tag's attributes (any attribute order)."""
    return [
        {k.lower(): unescape(v) for k, v in _ATTR_RE.findall(tag)}
        for tag in _INPUT_RE.findall(html)
    ]


def _input_value(html: str, name: str) -> str | None:
    for attrs in _inputs(html):
        if attrs.get("name") == name:
            return attrs.get("value", "")
    return None


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
                 api_url: str = API_URL, web_api_url: str = WEB_API_URL) -> None:
        self._session = session
        self._accounts = accounts_url.rstrip("/")
        self._api = api_url.rstrip("/")
        self._web_api = web_api_url.rstrip("/")

    def _login_session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=True), headers={"User-Agent": USER_AGENT},
            timeout=_TIMEOUT,
        )

    async def request_link(self, email: str) -> str:
        """Ask Budget Thuis to mail a login link (there is no password login).

        Starts the app's authorize request (PKCE); returns the code verifier that
        `login_with_link` needs once the link from the mail is pasted.
        """
        verifier, challenge = _pkce()
        try:
            async with self._login_session() as s:
                url = f"{self._accounts}/connect/authorize?" + urlencode({
                    "client_id": CLIENT_ID, "response_type": "code",
                    "redirect_uri": REDIRECT_URI, "scope": SCOPE, "label": LABEL,
                    "code_challenge": challenge, "code_challenge_method": "S256",
                })
                html = ""
                for _hop in range(6):  # authorize -> /inloggen -> /inloglink-aanvragen
                    async with s.get(url, allow_redirects=False) as resp:
                        if resp.status in _REDIRECTS:
                            url = urljoin(url, _location(resp, "authorize"))
                            continue
                        html = await resp.text()
                        break
                token = _input_value(html, "__RequestVerificationToken")
                return_url = _input_value(html, "ReturnUrl")
                if not token or return_url is None:
                    raise BudgetThuisError("login page: no form")
                async with s.post(
                    f"{self._accounts}/inloglink-aanvragen",
                    data={
                        "EmailAddress": email, "ReturnUrl": return_url,
                        "Label": _input_value(html, "Label") or LABEL,
                        "__RequestVerificationToken": token,
                    },
                    allow_redirects=False,
                ) as resp:
                    if resp.status >= 400:
                        raise BudgetThuisError(f"inloglink-aanvragen: {resp.status}")
        except (TimeoutError, aiohttp.ClientError) as err:
            raise BudgetThuisError(f"login link: {err}") from err
        return verifier

    def app_login_url(self, link: str, verifier: str) -> str:
        """The mailed link -> the app's own authorize request carrying its login token.

        The mail's "Direct inloggen" link (mijn.budgetthuis.nl/autologin/<strategy>/?token=
        &email=) starts the website's login (client onlineReact), which never ends at the
        app's redirect. Its token and e-mail go to /connect/authorize as extra parameters,
        so they are sent with the app's client and PKCE challenge instead. Other links
        (an accounts link, the budgetthuis:// end address) stay as they are.
        """
        parsed = urlparse(link)
        query = parse_qs(parsed.query)
        if "autologin" not in parsed.path or not query.get("token"):
            return link
        strategy = [p for p in parsed.path.split("/") if p][-1]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        params = {
            "client_id": CLIENT_ID, "response_type": "code", "redirect_uri": REDIRECT_URI,
            "scope": SCOPE, "label": LABEL,
            "code_challenge": challenge.rstrip(b"=").decode(), "code_challenge_method": "S256",
            "token": query["token"][0], "strategy": query.get("strategy", [strategy])[0],
        }
        if query.get("email"):
            params["email"] = query["email"][0]
        return f"{self._accounts}/connect/authorize?" + urlencode(params)

    async def login_with_link(self, link: str, verifier: str) -> dict[str, Any]:
        """The link from the mail (or the budgetthuis://login_success?code=... it ends at) -> tokens."""
        link = link.strip()
        try:
            async with self._login_session() as s:
                url = self.app_login_url(link, verifier)
                for _hop in range(12):
                    if url.startswith(REDIRECT_URI):
                        break
                    async with s.get(url, allow_redirects=False) as resp:
                        if resp.status in _REDIRECTS:
                            url = urljoin(url, _location(resp, "login link"))
                            continue
                        html = await resp.text()
                    # A page that posts itself (a confirm button or an auto-submit form).
                    form = _FORM_RE.search(html)
                    if resp.status != 200 or form is None:
                        raise BudgetThuisAuthError(
                            f"giriş linki geçersiz veya süresi dolmuş ({resp.status}, "
                            f"{urlparse(url).hostname}{urlparse(url).path})"
                        )
                    fields = {
                        a["name"]: a.get("value", "")
                        for a in _inputs(html)
                        if a.get("name") and a.get("type", "text").lower() in ("hidden", "submit")
                    }
                    async with s.post(
                        urljoin(url, unescape(form.group(1)) or url), data=fields,
                        allow_redirects=False,
                    ) as post:
                        if post.status not in _REDIRECTS:
                            raise BudgetThuisAuthError(
                                f"giriş linki geçersiz veya süresi dolmuş ({post.status}, "
                                f"{urlparse(url).hostname}{urlparse(url).path})"
                            )
                        url = urljoin(url, _location(post, "login link"))
                else:
                    raise BudgetThuisError("login link: too many redirects")
                code = _query(url, "code")
                if not code:
                    raise BudgetThuisAuthError("login link: no authorization code")
                return await self._token(s, {
                    "grant_type": "authorization_code", "code": code,
                    "code_verifier": verifier, "redirect_uri": REDIRECT_URI,
                    "client_id": CLIENT_ID,
                })
        except (TimeoutError, aiohttp.ClientError) as err:
            raise BudgetThuisError(f"login link: {err}") from err

    async def refresh(self, refresh_token: str) -> dict[str, Any]:
        data = {"grant_type": "refresh_token", "refresh_token": refresh_token,
                "client_id": CLIENT_ID}
        try:
            try:
                tokens = await self._token(self._session, data)
            except aiohttp.ServerDisconnectedError:  # a stale pooled connection: once more
                tokens = await self._token(self._session, data)
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
        try:
            return await self._request_once(token, method, path, **kwargs)
        except BudgetThuisError as err:
            if not isinstance(err.__cause__, aiohttp.ServerDisconnectedError):
                raise
            return await self._request_once(token, method, path, **kwargs)  # stale connection

    async def _request_once(self, token: str, method: str, path: str, base: str | None = None,
                            **kwargs: Any) -> Any:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
                   "User-Agent": USER_AGENT}
        try:
            async with self._session.request(
                method, (base or self._api) + path, headers=headers, timeout=_TIMEOUT, **kwargs
            ) as resp:
                label = path.split("/")[3] if path.count("/") >= 3 else path
                if resp.status in (401, 403):
                    body = (await resp.text())[:200].strip()
                    raise BudgetThuisAuthError(
                        f"{label}: {resp.status}" + (f" ({body})" if body else "")
                    )
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
        try:
            picked = [await self._request(
                token, "POST", "/energy/v2/customer/productPicker",
                json={"relationIds": relation_ids},
            ) or {}]
        except BudgetThuisAuthError as err:
            # 403 for the whole list (seen live): maybe one relation is not an energy
            # product. Ask per relation and keep the ones that answer.
            _LOGGER.warning(
                "Budget Thuis productPicker refused (%s); token %s; products %s; "
                "trying each of %d relations", err, token_claims(token),
                product_summary(person), len(relation_ids),
            )
            picked = []
            for relation_id in relation_ids:
                try:
                    picked.append(await self._request(
                        token, "POST", "/energy/v2/customer/productPicker",
                        json={"relationIds": [relation_id]},
                    ) or {})
                except BudgetThuisAuthError as one_err:
                    _LOGGER.warning("Budget Thuis productPicker, one relation: %s", one_err)
            if not picked:
                result = await self._contractees(token, person)
                if result:
                    return result
                raise
        result = []
        for answer in picked:
            for contract in answer.get("contractsInfo") or []:
                status = str(contract.get("contractStatus") or "").casefold()
                if contract.get("contractId") is not None and status in ("", "active"):
                    result.append(str(contract["contractId"]))
        return result

    async def _contractees(self, token: str, person: dict[str, Any]) -> list[str]:
        """Contracts via api.budgetthuis.nl contractees/<Klantnummer>/contracts.

        That API wants the customer number (Klantnummer); contactPerson does not say
        which of its numbers that is, so every number in it is tried once.
        """
        _LOGGER.warning("Budget Thuis contactPerson fields: %s", sorted(_key_paths(person)))
        result: list[str] = []
        for number in _numbers(person):
            try:
                answer = await self._request(
                    token, "GET", f"/energy/v1/contractees/{number}/contracts",
                    base=self._web_api,
                ) or {}
            except BudgetThuisError as err:
                _LOGGER.info("Budget Thuis contractees: %s", err)
                continue
            for contract in answer.get("contractDetails") or []:
                status = str(contract.get("contractStatus") or "").casefold()
                if contract.get("contractId") is not None and status in ("", "active"):
                    result.append(str(contract["contractId"]))
            if result:
                _LOGGER.info("Budget Thuis: contracts found via contractees")
                break
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
        self._mail_login_at = 0.0
        self._running = False

    async def async_start(self) -> None:
        stored = await self._store.async_load() or {}
        if stored.get("login_at") == self.config.get("login_at"):
            self._tokens = stored.get("tokens") or {}
        else:  # a new login from Configure: its refresh token
            self._tokens = {"refresh_token": self.config.get("refresh_token")}
        self._schedule()
        self.hass.async_create_task(self.async_run(), "gungors budget thuis")

    async def async_reconfigure(self, config: dict[str, Any]) -> None:
        if config.get("login_at") != self.config.get("login_at"):
            # a new login from Configure: its refresh token, or a mail login right away
            self._tokens = {"refresh_token": config.get("refresh_token")}
            self._mail_login_at = 0.0
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
        try:
            if not self._tokens.get("refresh_token"):
                raise BudgetThuisAuthError("no refresh token")
            tokens = await self.api.refresh(self._tokens["refresh_token"])
        except BudgetThuisAuthError as err:
            tokens = await self._mail_login(err)
        self._tokens = tokens
        await self._store.async_save({"login_at": self.config.get("login_at"), "tokens": tokens})
        return tokens["access_token"]

    async def _mail_login(self, err: BudgetThuisAuthError) -> dict[str, Any]:
        """A refused refresh token: log in again by mail when the mailbox is known."""
        password = self.config.get("mail_password")
        if not password or time.time() - self._mail_login_at < RELOGIN_EVERY:
            raise BudgetThuisAuthError(RELOGIN) from err
        self._mail_login_at = time.time()
        _LOGGER.info("Budget Thuis: logging in by mail (%s)", err)
        self.status, self.error = "giriş yapılıyor", None
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)
        try:
            tokens = await async_mail_login(
                self.hass, self.api, self.config["username"], password
            )
        except BudgetThuisError as mail_err:
            raise BudgetThuisAuthError(f"{RELOGIN} ({mail_err})") from mail_err
        if not tokens.get("refresh_token"):
            raise BudgetThuisAuthError(RELOGIN)
        return tokens

    async def async_run(self) -> None:
        if self._running:  # a mail login can take minutes: no second run meanwhile
            return
        self._running = True
        try:
            await self._async_run()
        finally:
            self._running = False

    async def _async_run(self) -> None:
        now = dt_util.now()
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            try:
                days = await self._read(await self._token(), start, end)
            except BudgetThuisAuthError as err:
                if str(err).startswith(RELOGIN):
                    raise
                self._tokens.pop("access_token", None)  # refused: refresh once
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
