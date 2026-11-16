"""ADM_* environment (deployment knobs). Everything else is configured in the web UI (config.json)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import __version__


def _list(v: str) -> list[str]:
    return [x.strip() for x in v.replace(";", ",").split(",") if x.strip()]


@dataclass
class Settings:
    data_dir: str = "/data"
    https_port: int = 8450
    internal_port: int = 8451        # plain HTTP on 127.0.0.1 only: MCP + uploads for agents behind SSH tunnels
    bind: str = "0.0.0.0"
    tls_names: list[str] = field(default_factory=list)       # extra names/IPs for the self-signed certificate
    tls_cert: str = ""                                        # own certificate + key instead (PEM paths)
    tls_key: str = ""
    setup_code: str = ""                                      # fixed first-run code (default: random, logged)
    trusted_proxies: list[str] = field(default_factory=list)  # reverse proxies whose X-Forwarded-For counts
    # library folders must live below one of these (the container's mounts); keeps /data (secrets) out of reach
    library_roots: list[str] = field(default_factory=lambda: ["/libraries"])
    staging_dir: str = ""                                     # uploads in progress ("" = <data>/staging)
    max_upload_gb: int = 200
    version: str = __version__

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        return cls(
            data_dir=e("ADM_DATA_DIR", "/data"),
            https_port=int(e("ADM_HTTPS_PORT", "8450")),
            internal_port=int(e("ADM_INTERNAL_PORT", "8451")),
            bind=e("ADM_BIND", "0.0.0.0"),
            tls_names=_list(e("ADM_TLS_NAMES", "")),
            tls_cert=e("ADM_TLS_CERT", ""),
            tls_key=e("ADM_TLS_KEY", ""),
            setup_code=e("ADM_SETUP_CODE", ""),
            trusted_proxies=_list(e("ADM_TRUSTED_PROXIES", "")),
            library_roots=_list(e("ADM_LIBRARY_ROOTS", "/libraries")) or ["/libraries"],
            staging_dir=e("ADM_STAGING_DIR", ""),
            max_upload_gb=int(e("ADM_MAX_UPLOAD_GB", "200")),
        )

    @property
    def staging(self) -> str:
        return self.staging_dir or os.path.join(self.data_dir, "staging")
