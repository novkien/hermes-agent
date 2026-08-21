"""IP allowlist: CIDR parsing/matching and client-IP resolution.

Trust model (owner decision D-004, 2026-08-08): the LAN is the trust
boundary. The BFF accepts clients whose *effective* IP address falls inside
one of the configured CIDRs (default 192.168.0.0/24 LAN + 100.64.0.0/10
Tailscale CGNAT). Everything else is rejected 403 *before* any session,
CSRF, or upstream work happens (fail-closed).

Proxy handling: ``X-Forwarded-For`` is honored ONLY when the operator
explicitly sets TRUST_PROXY_HEADERS=1. Otherwise the direct peer IP
(``request.client.host``) is used and XFF is ignored entirely — trusting
an unconfigured client-supplied header would let any caller spoof an
allowed IP (acceptance criterion 3).

Fail-closed: an unset/empty/invalid ALLOWED_CIDRS config means NO network
is allowed; ``is_allowed()`` returns False for every peer and the startup
guard refuses a public bind.
"""

from __future__ import annotations

import ipaddress
from typing import Optional


class CidrList:
    """A parsed allowlist of CIDR networks.

    ``parse("192.168.0.0/24,100.64.0.0/10")``. Empty/invalid input yields
    an empty network set, which matches NOTHING (fail-closed) — never
    allow-all.
    """

    def __init__(self, cidrs: list[str] | None = None):
        networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for raw in cidrs or []:
            text = (raw or "").strip()
            if not text:
                continue
            try:
                networks.append(ipaddress.ip_network(text, strict=False))
            except ValueError:
                # Invalid entry: skip it. The list stays as-is; an
                # entirely-invalid/unset list remains empty => reject all.
                continue
        self._networks = networks

    @classmethod
    def parse(cls, csv: str | None) -> "CidrList":
        if not csv:
            return cls([])
        return cls([part.strip() for part in csv.split(",") if part.strip()])

    def __bool__(self) -> bool:
        return bool(self._networks)

    def __len__(self) -> int:
        return len(self._networks)

    def contains(self, ip: str | None) -> bool:
        if not ip:
            return False
        try:
            addr = ipaddress.ip_address(ip.strip())
        except ValueError:
            return False
        for net in self._networks:
            if addr in net:
                return True
        return False

    def describe(self) -> str:
        return ",".join(str(n) for n in self._networks) or "<empty: reject-all>"


def resolve_client_ip(
    peer_ip: str | None,
    xff: str | None,
    trust_proxy_headers: bool,
) -> Optional[str]:
    """Effective client IP.

    - peer_ip: direct TCP peer (``request.client.host``). Always the
      fallback and the ONLY source when TRUST_PROXY_HEADERS is off.
    - xff: ``X-Forwarded-For`` header value (left-most hop is the original
      client when set by a trusted proxy). Ignored unless
      ``trust_proxy_headers`` is True.
    """
    if trust_proxy_headers and xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return peer_ip
