"""DB + Spaces connectivity probe. Never returns or logs credentials."""
import json
import os
import tempfile
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

E = os.environ.get


def scrub(msg):
    msg = str(msg)
    for k in ("DB_PASSWORD", "S3_SECRET_KEY", "S3_ACCESS_KEY", "DB_USER"):
        v = E(k)
        if v and len(v) >= 4:
            msg = msg.replace(v, "***")
    return msg[:300]


def err(e):
    return f"{type(e).__name__}: {scrub(e)}"


_ca_path = None


def ca_file():
    global _ca_path
    pem = E("DB_CA_CERT")
    if not pem:
        return None
    if _ca_path is None:
        fd, _ca_path = tempfile.mkstemp(suffix=".pem")
        with os.fdopen(fd, "w") as f:
            f.write(pem)
    return _ca_path


def db():
    import pymysql
    kw = dict(host=E("DB_HOST"), port=int(E("DB_PORT", "3306")), user=E("DB_USER"),
              password=E("DB_PASSWORD"), database=E("DB_NAME"), connect_timeout=8,
              autocommit=True)
    ca = ca_file()
    if ca:
        import ssl
        ctx = ssl.create_default_context(cafile=ca)  # verifies chain + hostname
        ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)  # DO's CA fails Python 3.13+ strict mode
        kw["ssl"] = ctx
    return pymysql.connect(**kw)


def cipher(conn):
    with conn.cursor() as c:
        c.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
        r = c.fetchone()
    return (r[1] or None) if r else None


def ensure(c):
    c.execute("CREATE TABLE IF NOT EXISTS probe (id BIGINT AUTO_INCREMENT PRIMARY KEY, "
              "payload LONGBLOB, created TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")


def h_db(_q):
    conn = db()
    try:
        with conn.cursor() as c:
            ensure(c)
            c.execute("INSERT INTO probe (payload) VALUES (%s)", (b"probe",))
            c.execute("SELECT COUNT(*) FROM probe")
            n = c.fetchone()[0]
        return 200, {"ok": True, "rows": n, "tls": cipher(conn)}
    finally:
        conn.close()


def mb_arg(q):
    mb = float(q.get("mb", ["1"])[0])
    if not 0 < mb <= 2048:
        raise ValueError("mb must be in (0, 2048]")
    return mb


def h_db_fill(q):
    mb = mb_arg(q)
    written = 0.0
    chunk = os.urandom(512 * 1024)
    try:
        conn = db()
    except Exception as e:
        return 500, {"ok": False, "written_mb": 0, "error": err(e)}
    try:
        with conn.cursor() as c:
            ensure(c)
            while written < mb:
                c.execute("INSERT INTO probe (payload) VALUES (%s)", (chunk,))
                written += 0.5
        return 200, {"ok": True, "written_mb": written}
    except Exception as e:
        return 500, {"ok": False, "written_mb": written, "error": err(e)}
    finally:
        conn.close()


def s3():
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3", endpoint_url=E("S3_ENDPOINT"), region_name=E("S3_REGION"),
        aws_access_key_id=E("S3_ACCESS_KEY"), aws_secret_access_key=E("S3_SECRET_KEY"),
        config=Config(connect_timeout=8, read_timeout=30, retries={"max_attempts": 1},
                      s3={"addressing_style": "path"}))


def h_s3(_q):
    cl, b = s3(), E("S3_BUCKET")
    key = f"probe/{uuid.uuid4()}"
    body = uuid.uuid4().bytes
    cl.put_object(Bucket=b, Key=key, Body=body)
    try:
        got = cl.get_object(Bucket=b, Key=key)["Body"].read()
        if got != body:
            return 500, {"ok": False, "error": "readback mismatch"}
        n = len(cl.list_objects_v2(Bucket=b, Prefix="probe/").get("Contents", []))
    finally:
        cl.delete_object(Bucket=b, Key=key)
    return 200, {"ok": True, "objects": n}


def h_s3_fill(q):
    mb = mb_arg(q)
    written = 0.0
    chunk = os.urandom(1024 * 1024)
    try:
        cl, b = s3(), E("S3_BUCKET")
        run = uuid.uuid4().hex[:8]
        i = 0
        while written < mb:
            cl.put_object(Bucket=b, Key=f"probe-fill/{run}/{i}", Body=chunk)
            written += 1
            i += 1
        return 200, {"ok": True, "written_mb": written}
    except Exception as e:
        return 500, {"ok": False, "written_mb": written, "error": err(e)}


def h_db_read(_q):
    conn = db()
    try:
        with conn.cursor() as c:
            c.execute("SELECT COUNT(*) FROM probe")
            return 200, {"ok": True, "rows": c.fetchone()[0]}
    except Exception as e:
        return 500, {"ok": False, "error": err(e)}
    finally:
        conn.close()


def h_db_size(_q):
    conn = db()
    try:
        with conn.cursor() as c:
            c.execute("SELECT COALESCE(SUM(data_length + index_length), 0) FROM information_schema.tables "
                      "WHERE table_schema = DATABASE()")
            return 200, {"ok": True, "mb": round(int(c.fetchone()[0]) / 1048576, 1)}
    except Exception as e:
        return 500, {"ok": False, "error": err(e)}
    finally:
        conn.close()


def h_db_clear(q):
    """mode=delete (DELETE FROM), truncate (TRUNCATE TABLE) or drop (DROP TABLE): the ways an app might try to
    get back under its cap."""
    mode = q.get("mode", ["delete"])[0]
    sql = {"delete": "DELETE FROM probe", "truncate": "TRUNCATE TABLE probe", "drop": "DROP TABLE probe"}.get(mode)
    if not sql:
        return 400, {"ok": False, "error": "mode must be delete, truncate or drop"}
    conn = db()
    try:
        with conn.cursor() as c:
            c.execute(sql)
        conn.commit()
        return 200, {"ok": True, "mode": mode}
    except Exception as e:
        return 500, {"ok": False, "mode": mode, "error": err(e)}
    finally:
        conn.close()


def h_s3_read(_q):
    try:
        cl, b = s3(), E("S3_BUCKET")
        n = len(cl.list_objects_v2(Bucket=b).get("Contents", []))
        return 200, {"ok": True, "objects": n}
    except Exception as e:
        return 500, {"ok": False, "error": err(e)}


def h_s3_clear(_q):
    """Delete every object (what an over-cap app would do to get back under its cap)."""
    deleted = 0
    try:
        cl, b = s3(), E("S3_BUCKET")
        while True:
            objs = cl.list_objects_v2(Bucket=b).get("Contents", [])
            if not objs:
                return 200, {"ok": True, "deleted": deleted}
            for o in objs:
                cl.delete_object(Bucket=b, Key=o["Key"])
                deleted += 1
    except Exception as e:
        return 500, {"ok": False, "deleted": deleted, "error": err(e)}


def h_disk_fill(q):
    """Write `mb` of random data to local scratch disk (/tmp) to exercise the per-app ephemeral-storage cap."""
    mb = mb_arg(q)
    written = 0
    chunk = os.urandom(1024 * 1024)
    try:
        with open(f"/tmp/fill-{uuid.uuid4().hex[:8]}", "wb") as f:
            while written < mb:
                f.write(chunk)
                f.flush()
                written += 1
        return 200, {"ok": True, "written_mb": written}
    except Exception as e:
        return 500, {"ok": False, "written_mb": written, "error": err(e)}


ROUTES = {"/db": h_db, "/s3": h_s3, "/db/fill": h_db_fill, "/s3/fill": h_s3_fill, "/db/read": h_db_read,
          "/db/size": h_db_size, "/db/clear": h_db_clear, "/s3/read": h_s3_read, "/s3/clear": h_s3_clear,
          "/disk/fill": h_disk_fill}


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/healthz":
            return self._send(200, b"ok", "text/plain")
        if u.path == "/":
            return self._send(200, {"app": "picopaas-probe", "endpoints": sorted(ROUTES)})
        fn = ROUTES.get(u.path)
        if not fn:
            return self._send(404, {"ok": False, "error": "not found"})
        try:
            code, body = fn(parse_qs(u.query))
        except Exception as e:
            code, body = 500, {"ok": False, "error": err(e)}
        self._send(code, body)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(E("PORT", "8080"))), H).serve_forever()
