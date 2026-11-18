#!/usr/bin/env python3
"""End-to-end verification for the svc.test. zone publisher.

Covers:
  0. store-level transaction atomicity (failed publish leaves no half version)
     and old-schema DB migration (old versions stay readable, NS becomes storable)
  1. cold start with empty DB (SERVFAIL), publish v1, real UDP DNS answers
  2. CNAME chain resolution, NXDOMAIN / REFUSED / NODATA behaviour
  3. whole-version rejection: dangling / cycle / external target / conflicts
  4. publish v2: new queries use the new version
  5. delegation: referral (aa=0, NS in Authority, only glue in Additional) for
     the delegation point and everything below it; glue never a business answer
  6. delegation validation: missing glue / A|CNAME at the cut / extra records
     below the cut / out-of-subdomain or multiple NS targets -> whole version
     rejected, old version keeps serving
  7. consistency: answers and referrals during concurrent publishes never mix
     versions (NS target and glue IP always pair from one committed version)
  8. write failure: old version keeps serving, DB untouched, recovery works
  9. restart: committed version (incl. delegation) is reloaded, no partial data
 10. legacy DB: app starts on a pre-NS schema, serves old versions, migrates

Run with the project venv python:  /tmp/testvenv/bin/python tests/e2e_test.py
"""

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

import dnslib
import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.store import Store  # noqa: E402
from app.zone import Snapshot, ZoneError, parse_zone_text, resolve  # noqa: E402

WEB = "http://127.0.0.1:18000"
DNS_PORT = 15353
LOG = "/tmp/zonedns-e2e-server.log"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    line = f"[{mark}] {name}"
    if detail and not cond:
        line += f"  -- {detail}"
    print(line, flush=True)


def dns_query(name, qtype="A"):
    pkt = dnslib.DNSRecord.question(name, qtype).send("127.0.0.1", DNS_PORT, timeout=3)
    return dnslib.DNSRecord.parse(pkt)


def section(rrset):
    return sorted(
        (str(rr.rname), dnslib.QTYPE[rr.rtype], str(rr.rdata)) for rr in rrset
    )


def answers_of(resp):
    return section(resp.rr)


def publish(text, note=""):
    return httpx.post(WEB + "/publish", data={"zone_text": text, "note": note}, timeout=10)


def wait_ready(proc, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"app exited early with {proc.returncode}; see {LOG}")
        try:
            if httpx.get(WEB + "/api/current", timeout=2).status_code == 200:
                dns_query("svc.test.", "A")  # UDP socket answers (any rcode)
                return
        except Exception:
            pass
        time.sleep(0.3)
    raise RuntimeError("app did not become ready; see " + LOG)


def start_app(dbpath):
    env = dict(
        os.environ,
        ZONE_DB=dbpath,
        DNS_BIND="127.0.0.1",
        DNS_PORT=str(DNS_PORT),
        WEB_HOST="127.0.0.1",
        WEB_PORT="18000",
        REFRESH_INTERVAL="0.5",
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.main"],
        cwd=ROOT,
        env=env,
        stdout=open(LOG, "ab"),
        stderr=subprocess.STDOUT,
    )
    wait_ready(proc)
    return proc


def stop_app(proc):
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


# schema as written by the pre-delegation release (rtype CHECK without 'NS')
LEGACY_SCHEMA = """
CREATE TABLE versions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT ''
);
CREATE TABLE records (
    version_id INTEGER NOT NULL REFERENCES versions(id),
    name       TEXT NOT NULL,
    rtype      TEXT NOT NULL CHECK (rtype IN ('A', 'CNAME')),
    value      TEXT NOT NULL,
    ttl        INTEGER NOT NULL,
    UNIQUE (version_id, name, rtype, value)
);
CREATE INDEX idx_records_version ON records(version_id);
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def make_legacy_db(path):
    """Build a DB exactly as the old release would leave it."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(LEGACY_SCHEMA)
        conn.execute(
            "INSERT INTO versions (id, created_at, note) VALUES (1, ?, 'legacy')",
            ("2024-01-01T00:00:00+00:00",),
        )
        conn.executemany(
            "INSERT INTO records (version_id, name, rtype, value, ttl)"
            " VALUES (1, ?, ?, ?, ?)",
            [
                ("web.svc.test.", "A", "10.9.9.9", 60),
                ("app.svc.test.", "CNAME", "web.svc.test.", 60),
            ],
        )
        conn.execute("INSERT INTO meta (key, value) VALUES ('current_version', '1')")
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- unit: store
def test_store_atomicity(tmp):
    db = os.path.join(tmp, "unit.db")
    store = Store(db)
    store.init()
    vid, _ = store.publish([("web.svc.test.", "A", "10.0.0.1", 60)], note="v1")
    check("unit: first publish becomes current", store.current_version_id() == vid)
    try:
        store.publish(
            [("x.svc.test.", "A", "10.0.0.2", 60), ("x.svc.test.", "A", "10.0.0.2", 60)]
        )
        check("unit: duplicate rows violate UNIQUE -> publish raises", False)
    except Exception:
        check("unit: duplicate rows violate UNIQUE -> publish raises", True)
    check("unit: pointer unchanged after failed txn", store.current_version_id() == vid)
    check("unit: no orphan version row", store.load_version(vid + 1) is None)
    cur = store.load_current()
    check("unit: old records intact", len(cur["rows"]) == 1)

    # NS rows round-trip through storage
    vid2, _ = store.publish(
        [("dev.svc.test.", "NS", "ns.dev.svc.test.", 60),
         ("ns.dev.svc.test.", "A", "10.0.0.53", 60)],
        note="delegation",
    )
    rows = store.load_version(vid2)["rows"]
    check("unit: NS rows round-trip",
          ("dev.svc.test.", "NS", "ns.dev.svc.test.", 60) in rows
          and ("ns.dev.svc.test.", "A", "10.0.0.53", 60) in rows, str(rows))


def test_store_migration(tmp):
    db = os.path.join(tmp, "legacy.db")
    make_legacy_db(db)
    store = Store(db)
    store.init()  # must migrate the rtype CHECK without losing anything
    cur = store.load_current()
    check("unit: legacy DB current version readable after migration",
          cur is not None and cur["id"] == 1 and len(cur["rows"]) == 2,
          str(cur))
    vid, _ = store.publish(
        [("dev.svc.test.", "NS", "ns.dev.svc.test.", 60),
         ("ns.dev.svc.test.", "A", "10.0.0.53", 60)],
        note="post-migration",
    )
    check("unit: NS publish works on migrated DB", store.current_version_id() == vid)
    old = store.load_version(1)
    check("unit: legacy version still intact after NS publish",
          old is not None and ("web.svc.test.", "A", "10.9.9.9", 60) in old["rows"])
    store.init()  # idempotent: second init must not fail or duplicate
    check("unit: migration is idempotent", store.current_version_id() == vid)


def test_validation():
    bad = {
        "dangling": "app CNAME missing\n",
        "cycle": "a CNAME b\nb CNAME a\n",
        "self cycle": "loop CNAME loop\n",
        "external target": "app CNAME www.example.com.\n",
        "A+CNAME conflict": "web A 10.0.0.1\napp A 10.0.0.2\napp CNAME web\n",
        "bad ipv4": "web A 999.1.1.1\n",
        "out-of-zone name": "evil.other.com. A 10.0.0.1\n",
        "dangling chain end": "a CNAME b\nb CNAME c\n",
        # ---- delegation rules ----
        "delegation missing glue":
            "dev.svc.test. NS ns.dev.svc.test.\nweb A 10.0.0.1\n",
        "delegation point with A":
            "dev.svc.test. NS ns.dev.svc.test.\n"
            "dev.svc.test. A 10.0.0.9\nns.dev.svc.test. A 10.0.0.53\n",
        "delegation point with CNAME":
            "web A 10.0.0.1\ndev.svc.test. NS ns.dev.svc.test.\n"
            "dev.svc.test. CNAME web\nns.dev.svc.test. A 10.0.0.53\n",
        "extra A below cut":
            "dev.svc.test. NS ns.dev.svc.test.\nns.dev.svc.test. A 10.0.0.53\n"
            "www.dev.svc.test. A 10.0.0.8\n",
        "CNAME below cut":
            "web A 10.0.0.1\ndev.svc.test. NS ns.dev.svc.test.\n"
            "ns.dev.svc.test. A 10.0.0.53\nx.dev.svc.test. CNAME web\n",
        "NS target outside subdomain":
            "dev.svc.test. NS ns.svc.test.\nns.svc.test. A 10.0.0.53\n",
        "NS target outside zone":
            "dev.svc.test. NS ns.example.com.\n",
        "two NS targets":
            "dev.svc.test. NS ns.dev.svc.test.\ndev.svc.test. NS ns2.dev.svc.test.\n"
            "ns.dev.svc.test. A 10.0.0.53\nns2.dev.svc.test. A 10.0.0.54\n",
        "NS at apex":
            "svc.test. NS ns.svc.test.\nns.svc.test. A 10.0.0.1\n",
        "CNAME to delegation point":
            "app CNAME dev.svc.test.\ndev.svc.test. NS ns.dev.svc.test.\n"
            "ns.dev.svc.test. A 10.0.0.53\n",
        "glue name with CNAME":
            "web A 10.0.0.1\ndev.svc.test. NS ns.dev.svc.test.\n"
            "ns.dev.svc.test. CNAME web\n",
    }
    for name, text in bad.items():
        try:
            parse_zone_text(text)
            check(f"unit: reject {name}", False, "parsed without error")
        except ZoneError:
            check(f"unit: reject {name}", True)
    ok = parse_zone_text("web A 10.0.0.1\napp CNAME web\n# comment\n\n")
    check("unit: valid zone parses", set(ok) == {"web.svc.test.", "app.svc.test."})
    ok = parse_zone_text(
        "web A 10.0.0.1\n"
        "dev.svc.test. NS ns.dev.svc.test.\n"
        "ns.dev.svc.test. A 10.0.0.53\n"
        "alias2glue CNAME ns.dev.svc.test.\n"  # CNAME -> glue name is allowed
    )
    check("unit: valid delegation parses",
          ok["dev.svc.test."]["NS"] == ["ns.dev.svc.test."]
          and ok["ns.dev.svc.test."]["A"] == ["10.0.0.53"])


def test_resolve_delegation():
    records = parse_zone_text(
        "web A 10.0.0.10\n"
        "app CNAME web\n"
        "dev.svc.test. NS ns.dev.svc.test. 60\n"
        "ns.dev.svc.test. A 10.0.0.53 60\n"
        "alias2glue CNAME ns.dev.svc.test.\n"
    )
    snap = Snapshot(1, "now", records)

    referral = ("NOERROR", 0, [],
                [("dev.svc.test.", "NS", "ns.dev.svc.test.", 60)],
                [("ns.dev.svc.test.", "A", "10.0.0.53", 60)])
    for qname, qtype in [("dev.svc.test.", "A"), ("dev.svc.test.", "NS"),
                         ("dev.svc.test.", "ANY"), ("www.dev.svc.test.", "A"),
                         ("ns.dev.svc.test.", "A"), ("a.b.dev.svc.test.", "A")]:
        got = resolve(snap, qname, qtype)
        check(f"unit: referral for {qname} {qtype}", got == referral, str(got))

    rcode, aa, ans, auth, add = resolve(snap, "alias2glue.svc.test.", "A")
    check("unit: CNAME to glue stops at the CNAME",
          rcode == "NOERROR" and aa == 1
          and ans == [("alias2glue.svc.test.", "CNAME", "ns.dev.svc.test.", 60)]
          and auth == [] and add == [], str((rcode, aa, ans, auth, add)))

    rcode, aa, ans, _, _ = resolve(snap, "app.svc.test.", "A")
    check("unit: normal chain unaffected by delegation",
          rcode == "NOERROR" and aa == 1 and ans[-1] == ("web.svc.test.", "A", "10.0.0.10", 60),
          str(ans))


# ------------------------------------------------------------------ e2e flow
def main():
    tmp = tempfile.mkdtemp(prefix="zonedns-e2e-")
    db = os.path.join(tmp, "zone.db")
    open(LOG, "w").close()

    test_store_atomicity(tmp)
    test_store_migration(tmp)
    test_validation()
    test_resolve_delegation()

    proc = start_app(db)
    try:
        # 1. empty DB: nothing published yet
        r = httpx.get(WEB + "/api/current", timeout=5)
        check("e2e: empty DB -> no current version", r.json()["version"] is None)
        resp = dns_query("web.svc.test.", "A")
        check("e2e: empty DB -> SERVFAIL", dnslib.RCODE[resp.header.rcode] == "SERVFAIL")

        # 2. publish v1
        v1 = "web A 10.0.0.10 60\nweb A 10.0.0.11 60\napi CNAME web\napp CNAME api\ndb A 10.0.1.5 120\n"
        r = publish(v1, note="v1")
        check("e2e: publish v1 -> 303", r.status_code == 303, f"got {r.status_code}: {r.text[:200]}")
        cur = httpx.get(WEB + "/api/current", timeout=5).json()
        check("e2e: current is v1 with 5 records",
              cur["version"] == 1 and len(cur["records"]) == 5, str(cur)[:200])

        # 3. real DNS answers from v1
        resp = dns_query("app.svc.test.", "A")
        want = sorted([
            ("app.svc.test.", "CNAME", "api.svc.test."),
            ("api.svc.test.", "CNAME", "web.svc.test."),
            ("web.svc.test.", "A", "10.0.0.10"),
            ("web.svc.test.", "A", "10.0.0.11"),
        ])
        check("e2e: chain app->api->web + A addresses",
              dnslib.RCODE[resp.header.rcode] == "NOERROR" and answers_of(resp) == want,
              str(answers_of(resp)))
        check("e2e: authoritative bit set", resp.header.aa == 1)

        resp = dns_query("api.svc.test.", "CNAME")
        check("e2e: CNAME qtype returns the alias only",
              answers_of(resp) == [("api.svc.test.", "CNAME", "web.svc.test.")],
              str(answers_of(resp)))

        resp = dns_query("db.svc.test.", "CNAME")
        check("e2e: CNAME qtype on A-only name -> NODATA",
              dnslib.RCODE[resp.header.rcode] == "NOERROR" and not resp.rr)

        resp = dns_query("nope.svc.test.", "A")
        check("e2e: unknown name -> NXDOMAIN",
              dnslib.RCODE[resp.header.rcode] == "NXDOMAIN")

        resp = dns_query("other.com.", "A")
        check("e2e: out-of-zone -> REFUSED",
              dnslib.RCODE[resp.header.rcode] == "REFUSED")

        resp = dns_query("svc.test.", "A")
        check("e2e: zone apex -> NOERROR/NODATA",
              dnslib.RCODE[resp.header.rcode] == "NOERROR" and not resp.rr)

        r = httpx.get(WEB + "/query", params={"name": "app", "qtype": "A"}, timeout=5)
        check("e2e: web query page issues real query",
              r.status_code == 200 and "10.0.0.10" in r.text and "NOERROR" in r.text)

        # 4. invalid versions rejected wholesale; v1 keeps serving
        bad_zones = {
            "dangling": "app CNAME missing\nweb A 10.0.0.1\n",
            "cycle": "a CNAME b\nb CNAME a\n",
            "external": "app CNAME www.example.com.\n",
            "conflict": "web A 10.0.0.1\napp A 10.0.0.2\napp CNAME web\n",
        }
        for name, text in bad_zones.items():
            r = publish(text)
            check(f"e2e: invalid zone ({name}) -> 400", r.status_code == 400,
                  f"got {r.status_code}")
        cur = httpx.get(WEB + "/api/current", timeout=5).json()
        check("e2e: current still v1 after rejections", cur["version"] == 1)
        resp = dns_query("app.svc.test.", "A")
        check("e2e: DNS still serves v1 after rejections", answers_of(resp) == want)

        # 5. publish v2: new queries use the new version
        v2 = "web2 A 10.0.0.2 60\napp CNAME web2\n"
        r = publish(v2, note="v2")
        check("e2e: publish v2 -> 303", r.status_code == 303)
        resp = dns_query("app.svc.test.", "A")
        want2 = sorted([
            ("app.svc.test.", "CNAME", "web2.svc.test."),
            ("web2.svc.test.", "A", "10.0.0.2"),
        ])
        check("e2e: new queries use v2", answers_of(resp) == want2, str(answers_of(resp)))
        resp = dns_query("web.svc.test.", "A")
        check("e2e: names dropped in v2 -> NXDOMAIN",
              dnslib.RCODE[resp.header.rcode] == "NXDOMAIN")

        # 6. delegation: publish v3 with dev.svc.test. delegated
        v3 = (
            "web A 10.0.0.10 60\n"
            "app CNAME web\n"
            "dev.svc.test. NS ns.dev.svc.test. 60\n"
            "ns.dev.svc.test. A 10.0.0.53 60\n"
            "alias2glue CNAME ns.dev.svc.test.\n"
        )
        r = publish(v3, note="delegate dev")
        check("e2e: publish delegation v3 -> 303", r.status_code == 303,
              f"got {r.status_code}: {r.text[:200]}")
        cur = httpx.get(WEB + "/api/current", timeout=5).json()
        check("e2e: API exposes NS record",
              {"name": "dev.svc.test.", "type": "NS", "value": "ns.dev.svc.test.", "ttl": 60}
              in cur["records"], str(cur)[:300])

        want_auth = [("dev.svc.test.", "NS", "ns.dev.svc.test.")]
        want_add = [("ns.dev.svc.test.", "A", "10.0.0.53")]
        for qname, qtype in [("dev.svc.test.", "A"), ("dev.svc.test.", "NS"),
                             ("www.dev.svc.test.", "A"), ("ns.dev.svc.test.", "A")]:
            resp = dns_query(qname, qtype)
            ok = (dnslib.RCODE[resp.header.rcode] == "NOERROR"
                  and resp.header.aa == 0
                  and not resp.rr
                  and section(resp.auth) == want_auth
                  and section(resp.ar) == want_add)
            check(f"e2e: referral for {qname} {qtype} (aa=0, NS+glue)", ok,
                  f"aa={resp.header.aa} ans={answers_of(resp)} "
                  f"auth={section(resp.auth)} add={section(resp.ar)}")

        resp = dns_query("app.svc.test.", "A")
        check("e2e: names outside delegation still authoritative",
              resp.header.aa == 1 and answers_of(resp) == sorted([
                  ("app.svc.test.", "CNAME", "web.svc.test."),
                  ("web.svc.test.", "A", "10.0.0.10"),
              ]), str(answers_of(resp)))

        resp = dns_query("alias2glue.svc.test.", "A")
        check("e2e: CNAME to glue does not leak glue as answer",
              resp.header.aa == 1
              and answers_of(resp) == [("alias2glue.svc.test.", "CNAME", "ns.dev.svc.test.")]
              and not section(resp.ar), str(answers_of(resp)))

        r = httpx.get(WEB + "/query", params={"name": "dev.svc.test.", "qtype": "NS"}, timeout=5)
        check("e2e: web query supports NS and shows all sections",
              r.status_code == 200 and "AUTHORITY SECTION" in r.text
              and "ADDITIONAL SECTION" in r.text
              and "ns.dev.svc.test." in r.text and "10.0.0.53" in r.text
              and "aa: 0" in r.text, r.text[:300])

        # 7. invalid delegations rejected wholesale; v3 keeps serving
        bad_delegations = {
            "missing glue": "dev.svc.test. NS ns.dev.svc.test.\nweb A 10.0.0.1\n",
            "A at cut": "dev.svc.test. NS ns.dev.svc.test.\ndev.svc.test. A 10.0.0.9\n"
                        "ns.dev.svc.test. A 10.0.0.53\n",
            "CNAME at cut": "web A 10.0.0.1\ndev.svc.test. NS ns.dev.svc.test.\n"
                            "dev.svc.test. CNAME web\nns.dev.svc.test. A 10.0.0.53\n",
            "extra A below cut": "dev.svc.test. NS ns.dev.svc.test.\n"
                                 "ns.dev.svc.test. A 10.0.0.53\nwww.dev.svc.test. A 10.0.0.8\n",
            "CNAME below cut": "web A 10.0.0.1\ndev.svc.test. NS ns.dev.svc.test.\n"
                               "ns.dev.svc.test. A 10.0.0.53\nx.dev.svc.test. CNAME web\n",
            "NS outside subdomain": "dev.svc.test. NS ns.svc.test.\nns.svc.test. A 10.0.0.53\n",
            "two NS": "dev.svc.test. NS ns.dev.svc.test.\ndev.svc.test. NS ns2.dev.svc.test.\n"
                      "ns.dev.svc.test. A 10.0.0.53\nns2.dev.svc.test. A 10.0.0.54\n",
        }
        for name, text in bad_delegations.items():
            r = publish(text)
            check(f"e2e: invalid delegation ({name}) -> 400", r.status_code == 400,
                  f"got {r.status_code}")
        cur = httpx.get(WEB + "/api/current", timeout=5).json()
        check("e2e: current still v3 after delegation rejections", cur["version"] == 3)
        resp = dns_query("www.dev.svc.test.", "A")
        check("e2e: referral still served after rejections",
              resp.header.aa == 0 and section(resp.auth) == want_auth
              and section(resp.ar) == want_add)

        # 8. consistency: hammer queries while flipping between two versions
        #    whose delegations differ in BOTH NS target and glue IP
        zone_a = ("app CNAME web1\nweb1 A 10.0.0.1 30\n"
                  "dev.svc.test. NS ns.dev.svc.test.\nns.dev.svc.test. A 10.0.0.53 30\n")
        zone_b = ("app CNAME web2\nweb2 A 10.0.0.2 30\n"
                  "dev.svc.test. NS ns2.dev.svc.test.\nns2.dev.svc.test. A 10.0.0.54 30\n")
        consistent = {("web1.svc.test.", "10.0.0.1"), ("web2.svc.test.", "10.0.0.2")}
        referrals = {("ns.dev.svc.test.", "10.0.0.53"), ("ns2.dev.svc.test.", "10.0.0.54")}
        errors, samples = [], [0]
        stop = threading.Event()

        # start from a known flip variant so hammers never see the previous step's zone
        r = publish(zone_a, note="flip-base")
        check("e2e: flip base publish -> 303", r.status_code == 303)

        def hammer():
            while not stop.is_set():
                try:
                    r2 = dns_query("app.svc.test.", "A")
                    rc = dnslib.RCODE[r2.header.rcode]
                    cn = [str(rr.rdata) for rr in r2.rr if dnslib.QTYPE[rr.rtype] == "CNAME"]
                    ip = [str(rr.rdata) for rr in r2.rr if dnslib.QTYPE[rr.rtype] == "A"]
                    samples[0] += 1
                    if rc != "NOERROR" or len(cn) != 1 or len(ip) != 1 \
                            or (cn[0], ip[0]) not in consistent:
                        errors.append(("chain", rc, cn, ip))
                except Exception as exc:  # noqa: BLE001
                    errors.append(("chain", "EXC", repr(exc)))

        def hammer_referral():
            while not stop.is_set():
                try:
                    r2 = dns_query("www.dev.svc.test.", "A")
                    rc = dnslib.RCODE[r2.header.rcode]
                    auth = section(r2.auth)
                    add = section(r2.ar)
                    samples[0] += 1
                    # NS and glue must pair from one single committed version
                    ok = (rc == "NOERROR" and r2.header.aa == 0 and not r2.rr
                          and len(auth) == 1 and auth[0][0] == "dev.svc.test."
                          and auth[0][1] == "NS"
                          and len(add) == 1 and add[0][1] == "A"
                          and add[0][0] == auth[0][2]
                          and (auth[0][2], add[0][2]) in referrals)
                    if not ok:
                        errors.append(("referral", rc, auth, add))
                except Exception as exc:  # noqa: BLE001
                    errors.append(("referral", "EXC", repr(exc)))

        threads = [threading.Thread(target=hammer) for _ in range(2)]
        threads += [threading.Thread(target=hammer_referral) for _ in range(2)]
        for t in threads:
            t.start()
        for i in range(8):
            r = publish(zone_a if i % 2 == 0 else zone_b, note=f"flip{i}")
            if r.status_code != 303:
                errors.append(("publish", r.status_code))
            time.sleep(0.1)
        stop.set()
        for t in threads:
            t.join()
        check("e2e: answers and referrals during publishes never mix versions",
              not errors and samples[0] > 50,
              f"errors={errors[:3]} samples={samples[0]}")

        # 9. write failure: DB path replaced by a directory -> publish must fail
        versions_before = httpx.get(WEB + "/api/versions", timeout=5).json()
        current_before = httpx.get(WEB + "/api/current", timeout=5).json()
        os.rename(db, db + ".real")
        os.mkdir(db)  # sqlite can no longer open the database file
        try:
            r = publish(zone_a, note="must fail")
            check("e2e: write failure -> 500", r.status_code == 500,
                  f"got {r.status_code}: {r.text[:120]}")
            check("e2e: write failure page says old version stays",
                  "写入失败" in r.text)
            resp = dns_query("app.svc.test.", "A")
            cn = [str(rr.rdata) for rr in resp.rr if dnslib.QTYPE[rr.rtype] == "CNAME"]
            ip = [str(rr.rdata) for rr in resp.rr if dnslib.QTYPE[rr.rtype] == "A"]
            check("e2e: DNS keeps serving during write failure",
                  (cn[0], ip[0]) in consistent, f"{cn} {ip}")
            resp = dns_query("www.dev.svc.test.", "A")
            auth = section(resp.auth)
            add = section(resp.ar)
            check("e2e: referral keeps serving during write failure",
                  resp.header.aa == 0 and len(auth) == 1 and len(add) == 1
                  and (auth[0][2], add[0][2]) in referrals, f"{auth} {add}")
        finally:
            os.rmdir(db)
            os.rename(db + ".real", db)
        time.sleep(1.2)  # let the refresh loop re-read the restored DB
        check("e2e: current version unchanged by failed write",
              httpx.get(WEB + "/api/current", timeout=5).json()["version"]
              == current_before["version"])
        check("e2e: no half-written version in DB",
              httpx.get(WEB + "/api/versions", timeout=5).json() == versions_before)
        r = publish(zone_b, note="recovered")
        check("e2e: publish works again after recovery", r.status_code == 303,
              f"got {r.status_code}")

        # 10. restart: committed version is reloaded; nothing partial appears
        last = httpx.get(WEB + "/api/current", timeout=5).json()
        n_versions = len(httpx.get(WEB + "/api/versions", timeout=5).json())
    finally:
        stop_app(proc)

    proc = start_app(db)  # same DB file
    try:
        cur = httpx.get(WEB + "/api/current", timeout=5).json()
        check("e2e: restart reloads last committed version",
              cur["version"] == last["version"], f"{cur['version']} != {last['version']}")
        resp = dns_query("app.svc.test.", "A")
        ip = [str(rr.rdata) for rr in resp.rr if dnslib.QTYPE[rr.rtype] == "A"]
        check("e2e: restart serves reloaded version", ip == ["10.0.0.2"], str(ip))
        resp = dns_query("www.dev.svc.test.", "A")
        check("e2e: restart reloads the delegation (referral intact)",
              resp.header.aa == 0
              and section(resp.auth) == [("dev.svc.test.", "NS", "ns2.dev.svc.test.")]
              and section(resp.ar) == [("ns2.dev.svc.test.", "A", "10.0.0.54")],
              f"auth={section(resp.auth)} add={section(resp.ar)}")
        check("e2e: restart leaves no partial versions",
              len(httpx.get(WEB + "/api/versions", timeout=5).json()) == n_versions)
        r = httpx.get(WEB + "/", timeout=5)
        check("e2e: UI renders after restart", r.status_code == 200 and "当前版本" in r.text)
    finally:
        stop_app(proc)

    # 11. legacy DB from the pre-delegation release: old versions readable,
    #     schema migrated on startup, delegation publishable afterwards
    legacy_db = os.path.join(tmp, "legacy-e2e.db")
    make_legacy_db(legacy_db)
    proc = start_app(legacy_db)
    try:
        cur = httpx.get(WEB + "/api/current", timeout=5).json()
        check("e2e: legacy DB current version readable",
              cur["version"] == 1
              and {"name": "web.svc.test.", "type": "A", "value": "10.9.9.9", "ttl": 60}
              in cur["records"], str(cur)[:300])
        resp = dns_query("app.svc.test.", "A")
        check("e2e: legacy version serves over DNS",
              answers_of(resp) == sorted([
                  ("app.svc.test.", "CNAME", "web.svc.test."),
                  ("web.svc.test.", "A", "10.9.9.9"),
              ]), str(answers_of(resp)))
        r = publish("dev.svc.test. NS ns.dev.svc.test.\nns.dev.svc.test. A 10.0.0.53\n",
                    note="delegation on migrated db")
        check("e2e: delegation publishable on migrated legacy DB", r.status_code == 303,
              f"got {r.status_code}: {r.text[:200]}")
        resp = dns_query("dev.svc.test.", "A")
        check("e2e: referral works on migrated legacy DB",
              resp.header.aa == 0
              and section(resp.auth) == [("dev.svc.test.", "NS", "ns.dev.svc.test.")]
              and section(resp.ar) == [("ns.dev.svc.test.", "A", "10.0.0.53")],
              f"auth={section(resp.auth)} add={section(resp.ar)}")
        versions = httpx.get(WEB + "/api/versions", timeout=5).json()
        check("e2e: legacy version retained after migration",
              any(v["id"] == 1 and v["records"] == 2 for v in versions)
              and len(versions) == 2, str(versions))
    finally:
        stop_app(proc)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed checks:")
        for name in FAIL:
            print("  -", name)
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
