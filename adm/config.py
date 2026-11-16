"""Configuration kept in /data/config.json (0600): admin, telerouter link, Jellyfin, libraries, agent settings."""

from __future__ import annotations

import re
import secrets
import ssl
import time
from typing import Literal

from pydantic import BaseModel, Field, field_validator

SECRET_FIELDS = {"token", "api_key", "password_hash", "totp_secret"}
HTTPS_RE = re.compile(r"^https://[A-Za-z0-9.\-\[\]:]+(/[A-Za-z0-9._~/%-]*)?$")
NAME_RE = re.compile(r"^[^\x00-\x1f<>]{1,64}$")
MediaKind = Literal["movies", "shows", "books"]


def new_id() -> str:
    return secrets.token_hex(4)


def _https(v: str, allow_empty: bool = True) -> str:
    v = v.strip().rstrip("/")
    if not v and allow_empty:
        return ""
    if not HTTPS_RE.match(v):
        raise ValueError("must be an https:// URL")
    return v


def _ca(v: str) -> str:
    v = v.strip()
    if not v:
        return ""
    if len(v) > 8000 or v.count("-----BEGIN CERTIFICATE-----") != 1:
        raise ValueError("one PEM certificate")
    try:
        ssl.create_default_context(cadata=v)
    except (ValueError, ssl.SSLError) as e:
        raise ValueError("not a PEM certificate") from e
    return v + "\n"


class Admin(BaseModel):
    username: str
    password_hash: str
    totp_secret: str = ""
    totp_enabled: bool = False
    created: float = Field(default_factory=time.time)


class Telerouter(BaseModel):
    """Where agent sessions come from: telerouter's Sessions API (/v1) with a token that has the sessions scope."""
    url: str = ""                       # https://telerouter.lan:8443
    token: str = ""                     # trm_... (write-only)
    ca_pem: str = ""                    # telerouter's CA (its /ca.pem); "" = public CAs
    machine: str = ""                   # machine name; "" = the only one that allows API sessions
    folder: str = "~/adm-jobs"          # job folders are created below this on the agent machine

    _url = field_validator("url")(classmethod(lambda cls, v: _https(v)))
    _ca = field_validator("ca_pem")(classmethod(lambda cls, v: _ca(v)))

    @field_validator("token")
    @classmethod
    def _token(cls, v: str) -> str:
        v = v.strip()
        if v and not re.match(r"^trm_[A-Za-z0-9_-]{20,100}$", v):
            raise ValueError("a telerouter MCP token starts with trm_")
        return v

    @field_validator("machine")
    @classmethod
    def _machine(cls, v: str) -> str:
        v = " ".join(v.split())
        if v and not NAME_RE.match(v):
            raise ValueError("invalid machine name")
        return v

    @field_validator("folder")
    @classmethod
    def _folder(cls, v: str) -> str:
        v = " ".join(v.split()) or "~/adm-jobs"
        if not re.match(r"^~?[A-Za-z0-9_./ -]{0,150}$", v) or ".." in v.split("/"):
            raise ValueError("a plain path (letters, digits, _ . / - and spaces)")
        return v


class Jellyfin(BaseModel):
    url: str = ""
    api_key: str = ""                   # write-only
    ca_pem: str = ""

    _url = field_validator("url")(classmethod(lambda cls, v: _https(v)))
    _ca = field_validator("ca_pem")(classmethod(lambda cls, v: _ca(v)))

    @field_validator("api_key")
    @classmethod
    def _key(cls, v: str) -> str:
        v = v.strip()
        if v and not re.match(r"^[A-Za-z0-9]{16,128}$", v):
            raise ValueError("a Jellyfin API key is letters and digits")
        return v


class Library(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str
    kind: MediaKind
    path: str                           # inside the container, below one of ADM_LIBRARY_ROOTS
    refresh: Literal["none", "jellyfin"] = "none"
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = " ".join(v.split())
        if not NAME_RE.match(v):
            raise ValueError("1-64 characters, no control characters or < >")
        return v

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not v.startswith("/") or ".." in v.split("/") or not re.match(r"^/[^\x00-\x1f]{1,300}$", v):
            raise ValueError("an absolute path inside the container")
        return v


class AgentSettings(BaseModel):
    adm_url: str = ""                   # how the agent's machine reaches ADM, e.g. https://192.168.1.30:8450
    guidance: str = Field(default="", max_length=4000)   # owner's notes for every job (sources, quality, language)
    max_concurrent: int = Field(default=2, ge=1, le=10)
    job_timeout_hours: float = Field(default=12, ge=0.25, le=72)
    max_nudges: int = Field(default=2, ge=0, le=5)        # "you stopped without finishing" reminders
    keep_sessions: bool = False         # leave finished jobs' tmux sessions open (debugging)

    _url = field_validator("adm_url")(classmethod(lambda cls, v: _https(v)))


class SecuritySettings(BaseModel):
    session_idle_minutes: int = Field(default=60, ge=5, le=1440)
    session_absolute_hours: int = Field(default=12, ge=1, le=168)


class Config(BaseModel):
    version: int = 1
    admin: Admin | None = None
    telerouter: Telerouter = Field(default_factory=Telerouter)
    jellyfin: Jellyfin = Field(default_factory=Jellyfin)
    libraries: list[Library] = Field(default_factory=list, max_length=20)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)


def redact(obj):
    """A config object for the API: secrets replaced by `<name>_set: bool` (write-only)."""
    if isinstance(obj, BaseModel):
        obj = obj.model_dump()
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    if not isinstance(obj, dict):
        return obj
    out = {}
    for k, v in obj.items():
        if k in SECRET_FIELDS:
            out[f"{k}_set"] = bool(v)
        else:
            out[k] = redact(v)
    return out


def merge_secrets(new: dict, old: dict) -> dict:
    """An empty secret in an update keeps the stored one (the UI never sees secrets)."""
    out = dict(new)
    for k, v in old.items():
        if k in SECRET_FIELDS and out.get(k) in ("", None):
            out[k] = v
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_secrets(out[k], v)
    for k in list(out):
        if k.endswith("_set"):
            out.pop(k)
    return out
