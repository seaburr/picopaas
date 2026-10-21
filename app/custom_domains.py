"""Custom-domain ownership + DNS verification.

Two independent checks gate a custom domain before the reconciler will serve it and ask cert-manager for a cert:

1. **Ownership** — a TXT record at ``_picopaas-challenge.<host>`` whose value is the per-app token we generated.
   This proves the user controls the zone and is OUR check (distinct from cert-manager's own ``_acme-challenge``
   record used by the ACME HTTP-01 solver). It stops a user attaching a domain they don't control / first-claim
   hijack, and is the hook for dangling-CNAME takeover hygiene (re-verify; tear down when it stops resolving to us).
2. **Pointing** — the host resolves to the same IP(s) as our LB (``CUSTOM_DOMAIN_CNAME_TARGET``). We compare resolved
   addresses rather than require a literal CNAME, so a CNAME, an ALIAS, or a flattened apex all pass.

The actual ACME challenge is still the real gate on issuance (HTTP-01 can only succeed once the host routes to us),
so these checks gate *provisioning*, belt-and-suspenders. See docs/ROADMAP.md "DNS verification".

DNS access is behind the ``Lookup`` interface so tests inject a fake and never touch the network.
"""
from __future__ import annotations

import re
import secrets
from typing import Iterable, List, Optional, Protocol, Set

from app import config


class CustomDomainError(ValueError):
    """Carries a user-facing, secret-free message."""


# A DNS hostname: 1–253 chars, dot-separated labels (1–63, alnum + internal hyphen), TLD of letters. Lowercased.
_HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(?:\.(?!-)[a-z0-9-]{1,63}(?<!-))*\.[a-z]{2,63}$"
)


def normalize_host(raw: str) -> str:
    """Lowercase, strip scheme/trailing dot, validate shape, and reject our own domains."""
    host = (raw or "").strip().lower()
    for scheme in ("https://", "http://"):
        if host.startswith(scheme):
            host = host[len(scheme):]
    host = host.split("/", 1)[0].rstrip(".")
    if not host or not _HOST_RE.match(host):
        raise CustomDomainError("Enter a valid domain, for example blog.example.com.")
    if host == config.APP_DOMAIN or host.endswith("." + config.APP_DOMAIN):
        raise CustomDomainError(f"That's a {config.APP_DOMAIN} address — a custom domain must be your own domain.")
    if host == config.CUSTOM_DOMAIN_CNAME_TARGET:
        raise CustomDomainError("That's the platform's own hostname.")
    return host


def generate_token() -> str:
    return secrets.token_hex(16)


def challenge_record(host: str) -> str:
    """The FQDN the user adds the ownership TXT at."""
    return f"{config.CUSTOM_DOMAIN_CHALLENGE_LABEL}.{host}"


class Lookup(Protocol):
    def txt(self, name: str) -> List[str]: ...
    def a(self, name: str) -> Set[str]: ...


class DnsLookup:
    """Real resolver (dnspython). Imported lazily so the dependency isn't needed in unit tests."""

    def __init__(self, timeout: float = 5.0) -> None:
        self._timeout = timeout

    def _resolver(self):
        import dns.resolver

        r = dns.resolver.Resolver()
        r.timeout = self._timeout
        r.lifetime = self._timeout
        return r

    def txt(self, name: str) -> List[str]:
        import dns.resolver

        try:
            answer = self._resolver().resolve(name, "TXT")
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return []
        except dns.exception.DNSException as exc:  # timeout, SERVFAIL, etc.
            raise CustomDomainError(f"Couldn't read DNS for {name} (try again in a minute).") from exc
        out: List[str] = []
        for rdata in answer:
            # TXT rdata is one or more quoted chunks; join them.
            out.append(b"".join(getattr(rdata, "strings", [])).decode("utf-8", "replace"))
        return out

    def a(self, name: str) -> Set[str]:
        import dns.resolver

        addrs: Set[str] = set()
        for rdtype in ("A", "AAAA"):
            try:
                for rdata in self._resolver().resolve(name, rdtype):
                    addrs.add(rdata.address)
            except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
                continue
            except dns.exception.DNSException as exc:
                raise CustomDomainError(f"Couldn't read DNS for {name} (try again in a minute).") from exc
        return addrs


def verify(host: str, token: str, *, lookup: Optional[Lookup] = None) -> None:
    """Raise CustomDomainError (with a user message) unless both checks pass. Returns None on success."""
    lk = lookup or DnsLookup()

    # 1. Ownership TXT. Fail closed: a missing/blank token must never match (e.g. an empty TXT value).
    if not token or not token.strip():
        raise CustomDomainError("This domain has no ownership token yet. Set the domain again to get one.")
    record = challenge_record(host)
    values = [v for v in lk.txt(record) if v and v.strip()]
    if token not in values:
        if not values:
            raise CustomDomainError(
                f"The ownership record isn't visible yet. Add a TXT record at {record} with the token shown, "
                "then try again (DNS can take a few minutes)."
            )
        raise CustomDomainError(f"The TXT record at {record} doesn't match the expected token.")

    # 2. Pointing: the host resolves to our LB's address(es).
    target = config.CUSTOM_DOMAIN_CNAME_TARGET
    ours = lk.a(target)
    theirs = lk.a(host)
    if not theirs:
        raise CustomDomainError(
            f"{host} doesn't resolve yet. Add a CNAME from {host} to {target} (or point it at the platform LB), "
            "then try again."
        )
    if not ours:
        # Fail closed: if we can't resolve our own LB we can't prove the host points at us.
        raise CustomDomainError(
            "We couldn't check the platform's address right now, so the domain can't be verified yet. "
            "Try again in a minute."
        )
    if not (ours & theirs):
        raise CustomDomainError(
            f"{host} resolves, but not to the platform. Point it at {target} with a CNAME and try again."
        )


def intersects(a: Iterable[str], b: Iterable[str]) -> bool:
    return bool(set(a) & set(b))
