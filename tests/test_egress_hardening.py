"""Tenant egress hardening: reserved ranges and abuse ports are excluded from the internet allowance."""
import ipaddress

from app import k8s_manifests as k8s


def _covered(protocol):
    s = set()
    for p in k8s._internet_egress_ports():
        if p["protocol"] == protocol:
            s.update(range(p["port"], p.get("endPort", p["port"]) + 1))
    return s


def test_blocked_tcp_ports():
    tcp = _covered("TCP")
    for port in (25, 465, 587, 19, 23, 135, 136, 137, 138, 139, 445, 3389, 6660, 6665, 6669, 6697):
        assert port not in tcp, port


def test_blocked_udp_ports():
    udp = _covered("UDP")
    for port in (19, 111, 137, 161, 389, 445, 1900, 3389, 11211):
        assert port not in udp, port


def test_legitimate_ports_stay_open():
    tcp, udp = _covered("TCP"), _covered("UDP")
    for port in (22, 53, 80, 443, 3306, 5432, 25060, 8080, 6670, 9418):
        assert port in tcp, port
    for port in (53, 123, 443, 51820):  # DNS, NTP, QUIC, WireGuard
        assert port in udp, port


def test_no_overlapping_or_empty_ranges():
    for proto in ("TCP", "UDP"):
        rs = [p for p in k8s._internet_egress_ports() if p["protocol"] == proto]
        last = 0
        for p in rs:
            assert p["port"] > last and p["endPort"] >= p["port"]
            last = p["endPort"]
        assert last == 65535


def test_reserved_ranges_are_valid_and_cover_key_targets():
    nets = [ipaddress.ip_network(c) for c in k8s.EGRESS_EXCEPT_CIDRS]
    for ip in ("169.254.169.254", "100.64.0.1", "100.127.255.254", "10.244.1.5", "10.245.0.10",
               "172.16.0.1", "192.168.1.1", "127.0.0.1", "0.0.0.0", "224.0.0.251", "239.255.255.250",
               "255.255.255.255", "198.18.0.1"):
        assert any(ipaddress.ip_address(ip) in n for n in nets), ip
    for ip in ("8.8.8.8", "1.1.1.1", "100.63.255.255", "100.128.0.1", "198.20.0.1"):
        assert not any(ipaddress.ip_address(ip) in n for n in nets), ip


def test_private_cidrs_unchanged_for_storage_detection():
    # reconciler uses PRIVATE_CIDRS to spot VPC-private Spaces IPs; keep it to the RFC1918/link-local set.
    assert set(k8s.PRIVATE_CIDRS) == {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"}


def test_internet_policy_manifest_excludes_reserved_ranges():
    pols = k8s.network_policies(1, 8080, None)
    pol = next(p for p in pols if p["metadata"]["name"] == "allow-egress-internet")
    rule = pol["spec"]["egress"][0]
    exc = rule["to"][0]["ipBlock"]["except"]
    assert "169.254.0.0/16" in exc and "100.64.0.0/10" in exc and "224.0.0.0/4" in exc
    assert rule["ports"] == k8s._internet_egress_ports()
