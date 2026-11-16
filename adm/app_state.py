"""What the web app, the MCP server and the job loop share."""

from __future__ import annotations

from dataclasses import dataclass, field

from .settings import Settings
from .store import ConfigStore, Db


@dataclass
class State:
    settings: Settings
    store: ConfigStore
    db: Db
    tls_ca_pem: str = ""          # ADM's own CA (agents trust it for the MCP server); "" with an own certificate
    own_cert: bool = False
    spki_pin: str = ""            # base64 SHA-256 of the server key (curl --pinnedpubkey for uploads)
    jobs: object = field(default=None)
