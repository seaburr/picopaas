"""Pentest "attacker" toolbox for testing PicoPaaS's own network isolation (see docs/PENTEST.md).

All requests need `Authorization: Bearer $TOOLBOX_TOKEN`; if the env var is unset, everything is
refused. Only the fixed named checks below run - no arbitrary command execution. Secrets passed in
args are never logged or echoed back in full.
"""
import json
import os
import re
import socket
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

E = os.environ.get
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")
SA_TOKEN = "/var/run/secrets/kubernetes.io/serviceaccount/token"


class BadArgs(Exception):
    pass


def host_arg(a, key="host"):
    v = a.get(key)
    if not isinstance(v, str) or not HOST_RE.match(v):
        raise BadArgs("invalid %s" % key)
    return v


def port_arg(a, key="port", default=None):
    v = a.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, str)) or not str(v).isdigit() or not 1 <= int(v) <= 65535:
        raise BadArgs("invalid %s" % key)
    return int(v)


def timeout_arg(a):
    v = a.get("timeout", 3)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.1 <= v <= 15:
        raise BadArgs("invalid timeout (0.1-15)")
    return float(v)


def str_arg(a, key, required=True, maxlen=4096):
    v = a.get(key)
    if v is None and not required:
        return None
    if not isinstance(v, str) or not v or len(v) > maxlen:
        raise BadArgs("invalid %s" % key)
    return v


def scrub(msg, extra=()):
    msg = str(msg)
    for v in extra:
        if v and len(v) >= 4:
            msg = msg.replace(v, "***")
    return msg[:300]


def tcp(host, port, timeout):
    """Returns (state, detail). state: open|closed|error."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "open", "connected"
    except socket.timeout:
        return "closed", "timeout"
    except ConnectionRefusedError:
        return "closed", "refused"
    except socket.gaierror as e:
        return "error", "dns: %s" % (e.strerror or e)
    except OSError as e:
        return "closed", "%s: %s" % (type(e).__name__, e.strerror or e)


def c_tcp_connect(a):
    return tcp(host_arg(a), port_arg(a), timeout_arg(a))


def c_port_scan(a):
    host, t = host_arg(a), timeout_arg(a)
    ports = a.get("ports")
    if not isinstance(ports, list) or not 1 <= len(ports) <= 100:
        raise BadArgs("ports must be a list of 1-100 ints")
    ports = [port_arg({"p": p}, "p") for p in ports]
    with ThreadPoolExecutor(max_workers=20) as ex:
        res = list(ex.map(lambda p: tcp(host, p, t)[0], ports))
    openp = [p for p, r in zip(ports, res) if r == "open"]
    if all(r == "error" for r in res):
        return "error", "host did not resolve"
    return ("open" if openp else "closed"), "open=%s scanned=%d" % (openp, len(ports))


def http_get(url, timeout):
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname or not HOST_RE.match(u.hostname):
        raise BadArgs("invalid url")
    req = urllib.request.Request(url, headers={"User-Agent": "picopaas-toolbox"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(2048)
            return "allowed", "status=%d bytes=%d" % (r.status, len(body))
    except urllib.error.HTTPError as e:
        return "allowed", "status=%d" % e.code
    except Exception as e:
        return "denied", "%s: %s" % (type(e).__name__, getattr(e, "reason", e))


def c_http_get(a):
    return http_get(str_arg(a, "url", maxlen=2048), timeout_arg(a))


def c_k8s_api(a):
    state, d = tcp("kubernetes.default.svc", 443, timeout_arg(a))
    tok = os.path.exists(SA_TOKEN)
    return state, "%s; sa_token=%s" % (d, "present" if tok else "absent")


def c_metadata(a):
    t = timeout_arg(a)
    state, d = tcp("169.254.169.254", 80, t)
    if state != "open":
        return "closed", d
    return http_get("http://169.254.169.254/", t)


def c_dns_lookup(a):
    name = host_arg(a, "name")
    try:
        _, _, ips = socket.gethostbyname_ex(name)
        return "open", "resolves=%s" % ips
    except socket.gaierror as e:
        return "closed", "nxdomain: %s" % (e.strerror or e)


def c_dns_srv_enum(a):
    patterns = ["mysql.picopaas-data.svc.cluster.local", "kubernetes.default.svc.cluster.local",
                "kube-dns.kube-system.svc.cluster.local"]
    found = {}
    for n in patterns:
        try:
            found[n] = socket.gethostbyname_ex(n)[2]
        except socket.gaierror:
            pass
    return ("open" if found else "closed"), "resolved=%s" % found


def c_mysql_login(a):
    import pymysql
    host, port = host_arg(a), port_arg(a, default=3306)
    user = str_arg(a, "user")
    password = str_arg(a, "password", required=False) or ""
    ca_pem = str_arg(a, "ca_pem", required=False)
    kw = dict(host=host, port=port, user=user, password=password, connect_timeout=timeout_arg(a))
    ca_path = None
    if ca_pem:
        import tempfile
        fd, ca_path = tempfile.mkstemp(suffix=".pem")
        with os.fdopen(fd, "w") as f:
            f.write(ca_pem)
        import ssl
        ctx = ssl.create_default_context(cafile=ca_path)
        ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)  # DO's CA fails Python 3.13+ strict mode
        kw["ssl"] = ctx
    try:
        conn = pymysql.connect(**kw)
        conn.close()
        return "allowed", "login ok"
    except pymysql.err.OperationalError as e:
        code = e.args[0] if e.args else None
        return "denied", "mysql error %s" % code
    except Exception as e:
        return "error", scrub(e, (password,))
    finally:
        if ca_path:
            os.unlink(ca_path)


def c_mysql_enum(a):
    import pymysql
    host, port = host_arg(a), port_arg(a, default=3306)
    user = str_arg(a, "user")
    password = str_arg(a, "password", required=False) or ""
    try:
        conn = pymysql.connect(host=host, port=port, user=user, password=password,
                                connect_timeout=timeout_arg(a))
        try:
            with conn.cursor() as c:
                c.execute("SHOW DATABASES")
                dbs = [r[0] for r in c.fetchall()]
            return "allowed", "databases=%s" % dbs
        finally:
            conn.close()
    except Exception as e:
        return "denied", scrub(e, (password,))


def c_s3_access(a):
    import boto3
    from botocore.config import Config
    endpoint = str_arg(a, "endpoint")
    region = str_arg(a, "region")
    bucket = str_arg(a, "bucket")
    access_key = str_arg(a, "access_key")
    secret_key = str_arg(a, "secret_key")
    op = a.get("op", "list")
    if op not in ("list", "get", "put"):
        raise BadArgs("op must be list|get|put")
    cl = boto3.client("s3", endpoint_url=endpoint, region_name=region,
                       aws_access_key_id=access_key, aws_secret_access_key=secret_key,
                       config=Config(connect_timeout=timeout_arg(a), read_timeout=10,
                                     retries={"max_attempts": 1}, s3={"addressing_style": "path"}))
    try:
        if op == "list":
            cl.list_objects_v2(Bucket=bucket, MaxKeys=1)
        elif op == "get":
            cl.list_objects_v2(Bucket=bucket, MaxKeys=1)
        elif op == "put":
            cl.put_object(Bucket=bucket, Key="toolbox-probe/check", Body=b"x")
            cl.delete_object(Bucket=bucket, Key="toolbox-probe/check")
        return "allowed", "op=%s ok" % op
    except Exception as e:
        return "denied", scrub(e, (secret_key, access_key))


def c_smtp_egress(a):
    return tcp(host_arg(a), port_arg(a, default=25), timeout_arg(a))


CHECKS = {
    "tcp_connect": c_tcp_connect,
    "port_scan": c_port_scan,
    "http_get": c_http_get,
    "k8s_api": c_k8s_api,
    "metadata": c_metadata,
    "dns_lookup": c_dns_lookup,
    "dns_srv_enum": c_dns_srv_enum,
    "mysql_login": c_mysql_login,
    "mysql_enum": c_mysql_enum,
    "s3_access": c_s3_access,
    "smtp_egress": c_smtp_egress,
}


class H(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        token = E("TOOLBOX_TOKEN")
        if not token:
            return False
        hdr = self.headers.get("Authorization", "")
        return hdr == "Bearer %s" % token

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            data = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/checks":
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            return self._send(200, {"checks": sorted(CHECKS)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/run":
            return self._send(404, {"error": "not found"})
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._send(400, {"error": "invalid json"})
        check = body.get("check")
        args = body.get("args") or {}
        fn = CHECKS.get(check)
        if not fn or not isinstance(args, dict):
            return self._send(400, {"check": check, "result": "error", "detail": "unknown check or bad args"})
        try:
            result, detail = fn(args)
        except BadArgs as e:
            result, detail = "error", str(e)
        except Exception as e:
            result, detail = "error", scrub(e)
        self._send(200, {"check": check, "result": result, "detail": detail})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(E("PORT", "8080"))), H).serve_forever()
