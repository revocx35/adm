"""Outbound HTTPS: telerouter's Sessions API and Jellyfin. Certificates are always verified (a private CA is
pinned per service, never `verify=False`), redirects are not followed, secrets go in headers only."""

from __future__ import annotations

import logging
import ssl

import httpx

from . import __version__
from .config import Jellyfin, Telerouter

log = logging.getLogger("adm.remote")


class RemoteError(Exception):
    pass


def _ssl(ca_pem: str) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if ca_pem:
        ctx.load_verify_locations(cadata=ca_pem)
    return ctx


def _detail(r: httpx.Response) -> str:
    try:
        d = r.json().get("detail")
        if isinstance(d, str):
            return d[:300]
    except ValueError:
        pass
    return f"HTTP {r.status_code}"


class TelerouterClient:
    def __init__(self, cfg: Telerouter, transport: httpx.AsyncBaseTransport | None = None):
        if not cfg.url or not cfg.token:
            raise RemoteError("telerouter is not set up (Settings -> telerouter)")
        self.cfg = cfg
        kw: dict = {"transport": transport} if transport else {"verify": _ssl(cfg.ca_pem)}
        self.http = httpx.AsyncClient(base_url=cfg.url, timeout=httpx.Timeout(30, connect=10),
                                      follow_redirects=False, headers={
                                          "Authorization": f"Bearer {cfg.token}",
                                          "User-Agent": f"adm/{__version__}"}, **kw)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        await self.http.aclose()

    async def _call(self, method: str, path: str, **kw) -> dict:
        try:
            r = await self.http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise RemoteError(f"telerouter unreachable ({e.__class__.__name__})") from e
        if r.status_code >= 400:
            raise RemoteError(f"telerouter: {_detail(r)}")
        try:
            return r.json()
        except ValueError as e:
            raise RemoteError("telerouter sent no JSON (is the URL right?)") from e

    async def machines(self) -> list[dict]:
        return (await self._call("GET", "/v1/machines")).get("machines", [])

    async def create(self, **body) -> dict:
        return await self._call("POST", "/v1/sessions", json=body)

    async def get(self, sid: str) -> dict:
        return await self._call("GET", f"/v1/sessions/{sid}")

    async def send(self, sid: str, text: str, timeout: int = 0) -> dict:
        return await self._call("POST", f"/v1/sessions/{sid}/messages", json={"text": text, "timeout": timeout})

    async def screen(self, sid: str, history: int = 0) -> str:
        return (await self._call("GET", f"/v1/sessions/{sid}/screen", params={"history": history})).get("screen", "")

    async def close(self, sid: str) -> dict:
        return await self._call("DELETE", f"/v1/sessions/{sid}")


async def jellyfin_refresh(cfg: Jellyfin, transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Ask Jellyfin to scan its libraries (POST /Library/Refresh)."""
    if not cfg.url or not cfg.api_key:
        raise RemoteError("Jellyfin is not set up (Settings -> Jellyfin)")
    kw: dict = {"transport": transport} if transport else {"verify": _ssl(cfg.ca_pem)}
    async with httpx.AsyncClient(timeout=20, follow_redirects=False, **kw) as http:
        try:
            r = await http.post(f"{cfg.url}/Library/Refresh",
                                headers={"Authorization": f'MediaBrowser Token="{cfg.api_key}"',
                                         "User-Agent": f"adm/{__version__}"})
        except httpx.HTTPError as e:
            raise RemoteError(f"Jellyfin unreachable ({e.__class__.__name__})") from e
    if r.status_code >= 400:
        raise RemoteError(f"Jellyfin: HTTP {r.status_code}")


async def jellyfin_check(cfg: Jellyfin, transport: httpx.AsyncBaseTransport | None = None) -> str:
    if not cfg.url or not cfg.api_key:
        raise RemoteError("Jellyfin is not set up")
    kw: dict = {"transport": transport} if transport else {"verify": _ssl(cfg.ca_pem)}
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, **kw) as http:
        try:
            r = await http.get(f"{cfg.url}/System/Info",
                               headers={"Authorization": f'MediaBrowser Token="{cfg.api_key}"'})
        except httpx.HTTPError as e:
            raise RemoteError(f"Jellyfin unreachable ({e.__class__.__name__})") from e
    if r.status_code >= 400:
        raise RemoteError(f"Jellyfin: HTTP {r.status_code}")
    try:
        info = r.json()
    except ValueError as e:
        raise RemoteError("Jellyfin sent no JSON") from e
    return f"{info.get('ServerName', 'Jellyfin')} {info.get('Version', '')}".strip()
