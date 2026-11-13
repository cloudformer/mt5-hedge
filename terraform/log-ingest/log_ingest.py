"""mt5-hedge log endpoint (Lambda Function URL, public). Two actions, both POST, form-encoded:

    POST /            write one log row           200 written | 4xx bad request | 5xx database
    POST /check       has this public IP run?      200 no | 409 yes | 4xx bad request | 5xx database
                      (both carry "now" = server UTC time, the page compares it with the local clock)

Every request carries header X-Sign = HMAC-SHA256(key, raw body) hex. The key ships inside the PyPI
package (mt5_hedge/sign_key.txt); Terraform injects the same file as SIGN_KEY, plus the previous one
as SIGN_KEY_PREV during a rotation. No timestamp in the signature: a wrong clock must never lose a log.

Columns and types come from hedge_log.sql (packaged next to this file): the SQL is the only list.
Only parameterised SQL is sent to the database; responses never echo SQL or database errors.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

TABLE = "hedge_log"
REQUIRED = ("key", "ts_utc", "pair_id", "action", "leg")
ACTIONS = {"open", "modify", "close"}
MAX_BODY = 16 * 1024
MAX_TEXT = 500
SERVER_FILLED = {"received_at"}

S3RDS_URL = os.environ["S3RDS_URL"]
S3RDS_AUTH = base64.b64encode(f"{os.environ['S3RDS_USER']}:{os.environ['S3RDS_PASSWORD']}".encode()).decode()
KEYS = [k.strip().encode() for k in (os.environ.get("SIGN_KEY", ""), os.environ.get("SIGN_KEY_PREV", "")) if k.strip()]


def _columns() -> dict:
    """{column: INTEGER|REAL|TEXT} parsed from CREATE TABLE hedge_log in hedge_log.sql."""
    sql = open(os.path.join(os.path.dirname(__file__), "hedge_log.sql"), encoding="utf-8").read()
    body = re.search(rf"CREATE TABLE IF NOT EXISTS {TABLE}\s*\((.*?)\n\);", sql, re.S).group(1)
    cols = {}
    for line in body.splitlines():
        line = line.split("--")[0].strip().rstrip(",")
        m = re.match(r"(\w+)\s+(INTEGER|REAL|TEXT)\b", line)
        if m:
            cols[m.group(1)] = m.group(2)
    return cols


COLUMNS = _columns()


# ---------- http ----------
def _resp(code: int, body: dict):
    return {"statusCode": code, "headers": {"content-type": "application/json"}, "body": json.dumps(body)}


def _raw_body(event) -> bytes:
    body = event.get("body") or ""
    return base64.b64decode(body) if event.get("isBase64Encoded") else body.encode("utf-8")


def _signed(event, raw: bytes) -> bool:
    given = (event.get("headers") or {}).get("x-sign", "")
    return any(hmac.compare_digest(given, hmac.new(k, raw, hashlib.sha256).hexdigest()) for k in KEYS)


def _form(raw: bytes) -> dict:
    return {k: v[-1] for k, v in urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True).items()}


# ---------- validation ----------
def _value(col: str, v: str):
    if v == "":
        return None
    t = COLUMNS[col]
    if t == "TEXT":
        if len(v) > MAX_TEXT:
            raise ValueError(f"{col} too long")
        return v
    try:
        return int(v) if t == "INTEGER" else float(v)
    except ValueError:
        raise ValueError(f"{col} must be a number")


def validate_row(form: dict) -> dict:
    if form.get("table") != TABLE:
        raise ValueError("table must be hedge_log")
    unknown = set(form) - set(COLUMNS) - {"table"}
    if unknown:
        raise ValueError("unknown field: " + ", ".join(sorted(unknown)))
    row = {c: _value(c, v) for c, v in form.items() if c in COLUMNS and c not in SERVER_FILLED}
    for c in REQUIRED:
        if row.get(c) in (None, ""):
            raise ValueError(f"missing {c}")
    if row["action"] not in ACTIONS:
        raise ValueError("action must be open, modify or close")
    if not re.fullmatch(r"[A-Za-z0-9]{1,8}", row["leg"]):
        raise ValueError("leg must be a short id")
    try:
        datetime.fromisoformat(row["ts_utc"])
    except ValueError:
        raise ValueError("ts_utc must be ISO 8601")
    row["received_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return row


# ---------- database ----------
def _query(sql: str, params: list) -> dict:
    req = urllib.request.Request(S3RDS_URL, data=json.dumps({"sql": sql, "params": params}).encode(), method="POST",
                                 headers={"content-type": "application/json", "authorization": f"Basic {S3RDS_AUTH}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def write_row(row: dict):
    cols = list(row)
    _query(f"INSERT INTO {TABLE} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
           f"ON CONFLICT(key) DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in cols if c != "key"),
           [row[c] for c in cols])


def ip_seen(ip: str) -> bool:
    out = _query(f"SELECT 1 FROM {TABLE} WHERE host_pub_ip = ? LIMIT 1", [ip])
    return bool(out.get("rows"))


# ---------- handler ----------
def handler(event, context):
    http = event.get("requestContext", {}).get("http", {})
    if http.get("method") != "POST":
        return _resp(405, {"error": "POST only"})
    raw = _raw_body(event)
    if len(raw) > MAX_BODY:
        return _resp(413, {"error": "body too large"})
    if not _signed(event, raw):
        return _resp(403, {"error": "rejected"})
    try:
        form = _form(raw)
    except UnicodeDecodeError:
        return _resp(400, {"error": "body must be utf-8 form data"})
    path = http.get("path", "/").rstrip("/") or "/"
    try:
        if path == "/":
            row = validate_row(form)
            write_row(row)
            return _resp(200, {"ok": 1, "key": row["key"]})
        if path == "/check":
            ip = form.get("host_pub_ip", "")
            if not re.fullmatch(r"[0-9a-fA-F.:]{3,45}", ip):
                return _resp(400, {"error": "host_pub_ip must be an IP"})
            seen = ip_seen(ip)
            return _resp(409 if seen else 200, {"seen": int(seen),
                                                "now": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        return _resp(404, {"error": "no such action"})
    except ValueError as e:
        return _resp(400, {"error": str(e)})
    except (urllib.error.URLError, OSError, TimeoutError):
        return _resp(503, {"error": "database unavailable"})
