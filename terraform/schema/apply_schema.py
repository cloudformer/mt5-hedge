"""Create the database (if missing) and run the .sql in it, one transaction. Idempotent.
    python3 apply_schema.py <db_url ending with /> <db_name> <file.sql>      env DB_USER / DB_PASSWORD
Terraform runs it after the database Lambda is up and whenever the .sql changes."""
import base64
import json
import os
import sys
import urllib.error
import urllib.request


def call(url, body):
    auth = base64.b64encode(f"{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}".encode()).decode()
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json", "authorization": f"Basic {auth}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main():
    base, db, path = sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3]
    status, body = call(f"{base}/api/databases", {"name": db})
    if status not in (201, 409):
        sys.exit(f"create database {db}: {status} {body}")
    print(f"database {db}: {'created' if status == 201 else 'exists'}")
    stmts = [s.strip() for s in open(path, encoding="utf-8").read().split(";") if s.strip() and not all(
        line.strip().startswith("--") for line in s.strip().splitlines())]
    status, body = call(f"{base}/api/db/{db}/tx", {"statements": [{"sql": s} for s in stmts]})
    if status != 200:
        sys.exit(f"schema: {status} {body}")
    print(f"schema: {len(stmts)} statements ok")


if __name__ == "__main__":
    main()
