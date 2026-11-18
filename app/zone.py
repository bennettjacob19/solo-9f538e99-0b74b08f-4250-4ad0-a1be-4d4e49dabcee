"""Zone text parsing, whole-version validation, immutable snapshots, and
single-version resolution for the svc.test. authoritative zone.

Subdomain delegation model: a name holding an NS record is a delegation
point (zone cut). Exactly one NS record is allowed there, its target must
lie strictly inside the delegated subdomain and must carry glue A record(s)
in the same version. The delegation point itself may not hold A/CNAME, and
nothing below the cut may exist except the glue A records. Queries at or
below a cut get a non-authoritative referral (NS in Authority, glue only in
Additional); a CNAME chain that runs into a cut stops at the CNAME — glue
is never served as a business A answer."""

import ipaddress
import re
import threading

ZONE = "svc.test."
DEFAULT_TTL = 60
MAX_TTL = 2_147_483_647

_LABEL_RE = re.compile(r"^[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?$")


class ZoneError(Exception):
    """Raised when a candidate zone version fails validation.

    Carries the full list of human-readable problems; the whole version
    is rejected, nothing is published.
    """

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def fqdn(name):
    """Normalize an operator-supplied name to an in-zone FQDN.

    Accepts relative names ("web"), zone FQDNs ("web.svc.test."), and the
    zone apex. Rejects anything outside the zone.
    """
    n = (name or "").strip().lower().rstrip(".")
    if not n:
        raise ValueError("empty name")
    if n == ZONE[:-1]:
        return ZONE
    if n.endswith("." + ZONE[:-1]):
        candidate = n + "."
    elif "." in n:
        raise ValueError(f"name '{name}' is outside zone {ZONE}")
    else:
        candidate = n + "." + ZONE
    _check_name(candidate)
    return candidate


def _check_name(name):
    if len(name) > 253:
        raise ValueError(f"name too long: {name}")
    for label in name.rstrip(".").split("."):
        if not _LABEL_RE.match(label):
            raise ValueError(f"bad label '{label}' in {name}")


def _new_rec(ttl):
    return {"A": [], "CNAME": None, "NS": [], "ttl": ttl}


def parse_zone_text(text):
    """Parse and validate a full zone version.

    Line format:  <name> <A|CNAME|NS> <value> [ttl]   (# starts a comment)

    Returns {name: {"A": [...], "CNAME": target|None, "NS": [...], "ttl": int}}.
    Raises ZoneError listing every problem found; nothing is half-accepted.
    """
    records = {}
    errors = []

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3:
            errors.append(f"line {lineno}: expected '<name> <A|CNAME|NS> <value> [ttl]'")
            continue
        if len(parts) > 4:
            errors.append(f"line {lineno}: too many fields")
            continue
        name_s, rtype, value = parts[0], parts[1].upper(), parts[2]
        ttl = DEFAULT_TTL
        if len(parts) == 4:
            try:
                ttl = int(parts[3])
                if not (0 < ttl <= MAX_TTL):
                    raise ValueError
            except ValueError:
                errors.append(f"line {lineno}: bad ttl '{parts[3]}'")
                continue
        try:
            name = fqdn(name_s)
        except ValueError as exc:
            errors.append(f"line {lineno}: {exc}")
            continue
        rec = records.setdefault(name, _new_rec(ttl))

        if rtype == "A":
            try:
                ip = str(ipaddress.IPv4Address(value))
            except ValueError:
                errors.append(f"line {lineno}: invalid IPv4 address '{value}'")
                continue
            if ip not in rec["A"]:
                rec["A"].append(ip)
        elif rtype == "CNAME":
            try:
                target = fqdn(value)
            except ValueError:
                errors.append(
                    f"line {lineno}: CNAME target '{value}' is not a name inside {ZONE}"
                )
                continue
            if rec["CNAME"] is not None:
                errors.append(f"line {lineno}: duplicate CNAME for {name}")
                continue
            rec["CNAME"] = target
        elif rtype == "NS":
            try:
                target = fqdn(value)
            except ValueError:
                errors.append(
                    f"line {lineno}: NS target '{value}' is not a name inside {ZONE}"
                )
                continue
            if target not in rec["NS"]:
                rec["NS"].append(target)
        else:
            errors.append(
                f"line {lineno}: unsupported record type '{parts[1]}' (A/CNAME/NS only)"
            )

    errors.extend(validate_records(records))
    if errors:
        raise ZoneError(errors)
    return records


def _is_below(name, ancestor):
    """True if name is strictly below ancestor (a proper subname)."""
    return name.endswith("." + ancestor)


def validate_records(records):
    """Cross-record rules for one candidate version.

    - a name cannot hold both A and CNAME
    - delegation: a name with NS is a zone cut. It must hold exactly one NS
      record whose target lies strictly inside the delegated subdomain and
      has glue A record(s) in this same version; the cut name itself may not
      hold A/CNAME; below the cut only the glue name's A records may exist
    - every CNAME target must exist in this same version (no dangling);
      a CNAME may point at a glue name, but never at a delegation point
    - CNAME chains must be acyclic and terminate at a name with A record(s)
    """
    errors = []
    for name in sorted(records):
        rec = records[name]
        if rec["A"] and rec["CNAME"]:
            errors.append(f"{name}: cannot have both A and CNAME records")

    delegations = sorted(n for n, r in records.items() if r["NS"])
    for cut in delegations:
        rec = records[cut]
        if cut == ZONE:
            errors.append(f"{ZONE}: NS at the zone apex is not supported")
            continue
        if rec["A"] or rec["CNAME"]:
            errors.append(f"{cut}: delegation point cannot also hold A/CNAME records")
        if len(rec["NS"]) != 1:
            errors.append(
                f"{cut}: delegation must point to exactly one in-subdomain "
                f"name server (got {len(rec['NS'])} NS records)"
            )
        for target in rec["NS"]:
            if not _is_below(target, cut):
                errors.append(
                    f"{cut}: NS target {target} is not inside the delegated subdomain"
                )
                continue
            glue = records.get(target)
            if glue is None or not glue["A"]:
                errors.append(
                    f"{cut}: NS target {target} has no glue A record in this version"
                )
        # below the cut, only the glue name's A records may exist
        glue_names = set(rec["NS"])
        for name in sorted(records):
            if name == cut or not _is_below(name, cut):
                continue
            other = records[name]
            if name not in glue_names:
                errors.append(
                    f"{name}: only glue A records for the NS target may exist "
                    f"below delegation {cut}"
                )
            elif other["CNAME"] or other["NS"]:
                errors.append(f"{name}: glue name may only hold A records")

    def below_some_cut(name):
        return any(name == cut or _is_below(name, cut) for cut in delegations)

    for name in sorted(records):
        rec = records[name]
        if not rec["CNAME"]:
            continue
        if below_some_cut(name):
            continue  # already rejected by the delegation rules above
        seen = {name}
        node = rec["CNAME"]
        while True:
            target = records.get(node)
            if target is None:
                errors.append(
                    f"{name}: CNAME target {node} is not defined in this version (dangling)"
                )
                break
            if node in seen:
                errors.append(f"{name}: CNAME chain loops back to {node} (cycle)")
                break
            seen.add(node)
            if target["NS"]:
                errors.append(
                    f"{name}: CNAME target {node} is a delegation point; "
                    "chains must end at A records"
                )
                break
            if target["CNAME"]:
                node = target["CNAME"]
                continue
            if target["A"]:
                break  # chain ends at address records: OK (a glue name is
                # allowed; the resolver stops at the cut and never serves
                # glue as a business A answer)
            break  # pragma: no cover - a stored record always has A/CNAME/NS
    return errors


def flatten_records(records):
    """parsed-dict -> list of (name, rtype, value, ttl) rows for storage."""
    rows = []
    for name in sorted(records):
        rec = records[name]
        for ip in rec["A"]:
            rows.append((name, "A", ip, rec["ttl"]))
        if rec["CNAME"]:
            rows.append((name, "CNAME", rec["CNAME"], rec["ttl"]))
        for ns in rec["NS"]:
            rows.append((name, "NS", ns, rec["ttl"]))
    return rows


class Snapshot:
    """Immutable view of one published version. A query resolves its whole
    alias chain against the single Snapshot instance captured at query start,
    so the chain, the final A address, and any referral (NS + glue) always
    come from the same committed version."""

    __slots__ = ("version_id", "created_at", "records")

    def __init__(self, version_id, created_at, records):
        self.version_id = version_id
        self.created_at = created_at
        # freeze: lists become tuples, structure is never mutated afterwards
        self.records = {
            name: {
                "A": tuple(rec["A"]),
                "CNAME": rec["CNAME"],
                "NS": tuple(rec["NS"]),
                "ttl": rec["ttl"],
            }
            for name, rec in records.items()
        }

    def zone_text(self):
        lines = []
        for name in sorted(self.records):
            rec = self.records[name]
            for ip in rec["A"]:
                lines.append(f"{name} A {ip} {rec['ttl']}")
            if rec["CNAME"]:
                lines.append(f"{name} CNAME {rec['CNAME']} {rec['ttl']}")
            for ns in rec["NS"]:
                lines.append(f"{name} NS {ns} {rec['ttl']}")
        return "\n".join(lines)


def snapshot_from_rows(version_id, created_at, rows):
    """Rebuild a Snapshot from stored rows (name, rtype, value, ttl).

    Rows of type A/CNAME written by older versions load unchanged; NS rows
    appear only in versions written after delegation support."""
    records = {}
    for name, rtype, value, ttl in rows:
        rec = records.setdefault(name, _new_rec(ttl))
        if rtype == "A":
            rec["A"].append(value)
        elif rtype == "CNAME":
            rec["CNAME"] = value
        else:  # NS
            rec["NS"].append(value)
    return Snapshot(version_id, created_at, records)


class SnapshotHolder:
    """Atomically-swapped reference to the current Snapshot.

    Publishing swaps the reference only after the SQLite transaction has
    committed; in-flight queries keep using the snapshot they captured, so
    service never pauses and never mixes versions."""

    def __init__(self):
        self._lock = threading.Lock()
        self._snap = None

    def get(self):
        with self._lock:
            return self._snap

    def set(self, snap):
        with self._lock:
            self._snap = snap


def _find_cut(snapshot, qname):
    """Deepest delegation point (name with NS records) at or above qname,
    strictly below the zone apex. Returns (cut_name, record) or (None, None).
    Callers must have already confirmed qname is inside the zone."""
    node = qname
    while node != ZONE:
        rec = snapshot.records.get(node)
        if rec is not None and rec["NS"]:
            return node, rec
        node = node.split(".", 1)[1]
    return None, None


def resolve(snapshot, qname, qtype):
    """Resolve qname/qtype against one snapshot.

    Returns (rcode, aa, answers, authority, additional) where each section
    is a list of (name, rtype, value, ttl). qtype is a mnemonic: A, CNAME,
    NS, ANY, ...

    A qname at or below a delegation point gets a non-authoritative
    referral: aa=0, empty answer, the NS record in authority, and only the
    corresponding glue A records in additional — all taken from this one
    snapshot. A CNAME chain that reaches a cut stops at the CNAME: glue is
    authoritative data for the child side, never a business A answer here.
    """
    qname = qname.lower()
    if qname != ZONE and not qname.endswith("." + ZONE):
        return "REFUSED", 0, [], [], []

    cut_name, cut_rec = _find_cut(snapshot, qname)
    if cut_name is not None:
        authority = [(cut_name, "NS", ns, cut_rec["ttl"]) for ns in cut_rec["NS"]]
        additional = []
        for ns in cut_rec["NS"]:
            glue = snapshot.records.get(ns)
            if glue:  # a validated version always has the glue; be safe
                additional.extend((ns, "A", ip, glue["ttl"]) for ip in glue["A"])
        return "NOERROR", 0, [], authority, additional

    answers = []
    node = qname
    seen = set()
    while True:
        rec = snapshot.records.get(node)
        if rec is None:
            if answers:
                # A validated version never dangles; degrade gracefully.
                return "NOERROR", 1, answers, [], []
            if node == ZONE:
                return "NOERROR", 1, [], [], []  # apex exists but holds no such data
            return "NXDOMAIN", 1, [], [], []
        if rec["CNAME"]:
            if node in seen:  # corrupt data safeguard; validated zones never loop
                return "NOERROR", 1, answers, [], []
            seen.add(node)
            answers.append((node, "CNAME", rec["CNAME"], rec["ttl"]))
            if qtype == "CNAME":
                return "NOERROR", 1, answers, [], []
            node = rec["CNAME"]
            if _find_cut(snapshot, node)[0] is not None:
                # The chain runs into delegated space: stop at the CNAME.
                # Glue must not be served as the business A answer.
                return "NOERROR", 1, answers, [], []
            continue
        if rec["A"]:
            if qtype in ("A", "ANY"):
                answers.extend((node, "A", ip, rec["ttl"]) for ip in rec["A"])
            return "NOERROR", 1, answers, [], []
        return "NOERROR", 1, answers, [], []  # pragma: no cover
