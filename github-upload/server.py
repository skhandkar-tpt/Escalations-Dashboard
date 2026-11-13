#!/usr/bin/env python3
"""
Escalations Dashboard - live critical-ticket tracker for ManageEngine ServiceDesk Plus
(Zoho cloud edition or on-premise).

Zero dependencies: runs on the Python 3 standard library.

    python3 server.py            # then open http://localhost:8787

Configuration lives in a `.env` file next to this script (see .env.example).
Credentials never leave the server: the browser only talks to /api/tickets.
If no credentials are configured, the server runs in DEMO mode with synthetic data.
"""

import json
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------- config
def load_env(path):
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            val = val.strip().strip('"').strip("'")
            os.environ.setdefault(key.strip(), val)


load_env(os.path.join(ROOT, ".env"))


def env(name, default=""):
    return os.environ.get(name, default).strip()


DEFAULT_PRIORITIES = ",".join([
    "PSS-IN-P1 | CL 4 Hours", "SSD-IN-P1 | CL 8 Hours", "PSS-IN-P2 | CL 8 Hours",
    "SSD-IN-P2 | CL 24 Hours", "PSS-SR-P1 | CL 2 days", "SSD-SR-P1 | CL 2 Days",
])

FIELDS = [
    "subject", "display_id", "priority", "status", "technician", "group", "site", "requester",
    "created_time", "due_by_time", "first_response_due_by_time", "is_overdue",
    "is_first_response_overdue", "last_updated_time", "responded_time", "request_type", "level",
]


def env_list(name, default):
    return [v.strip() for v in env(name, default).split(",") if v.strip()]


CFG = {
    # "cloud" (Zoho OAuth) or "onprem" (technician API key)
    "mode": env("SDP_MODE", "cloud").lower(),
    # Cloud: https://sdpondemand.manageengine.com (.eu / .in / .com.au / .jp / .ca ...)
    #        add /app/<portal> for a non-default portal.
    # On-prem: https://servicedesk.yourcompany.com
    "base_url": env("SDP_BASE_URL").rstrip("/"),
    "accounts_url": env("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.com").rstrip("/"),
    "client_id": env("ZOHO_CLIENT_ID"),
    "client_secret": env("ZOHO_CLIENT_SECRET"),
    "refresh_token": env("ZOHO_REFRESH_TOKEN"),
    "onprem_key": env("SDP_AUTHTOKEN"),
    "priorities": env_list("CRITICAL_PRIORITIES", DEFAULT_PRIORITIES),
    "levels": env_list("LEVELS", ""),  # empty = all levels
    "closed_statuses": [s.lower() for s in env_list(
        "CLOSED_STATUSES", "Closed,Resolved,Cancelled,Canceled,Unresolved")],
    "at_risk_hours": float(env("AT_RISK_HOURS", "4")),
    "stale_hours": float(env("STALE_HOURS", "24")),
    "refresh_seconds": int(env("REFRESH_SECONDS", "60")),
    "max_tickets": int(env("MAX_TICKETS", "1000")),
    "ticket_url": env("TICKET_URL_TEMPLATE"),
    "port": int(env("PORT", "8787")),
    "host": env("HOST", "0.0.0.0"),
    "verify_tls": env("SDP_VERIFY_TLS", "true").lower() != "false",
}


def demo_mode():
    if not CFG["base_url"]:
        return True
    if CFG["mode"] == "onprem":
        return not CFG["onprem_key"]
    return not (CFG["client_id"] and CFG["client_secret"] and CFG["refresh_token"])


def ticket_link(tid):
    if CFG["ticket_url"]:
        return CFG["ticket_url"].replace("{id}", str(tid))
    if not CFG["base_url"]:
        return None
    if CFG["mode"] == "onprem":
        return f"{CFG['base_url']}/WorkOrder.do?woMode=viewWO&woID={tid}"
    base = CFG["base_url"]
    if "/app/" not in base:
        base += "/app/itdesk"
    return f"{base}/ui/requests/{tid}/details"


# --------------------------------------------------------------------------- SDP client
class SDPClient:
    def __init__(self):
        self._token = None
        self._token_exp = 0
        self._lock = threading.Lock()
        import ssl
        self._ctx = None if CFG["verify_tls"] else ssl._create_unverified_context()

    def _access_token(self):
        with self._lock:
            if self._token and time.time() < self._token_exp - 60:
                return self._token
            data = urllib.parse.urlencode({
                "refresh_token": CFG["refresh_token"],
                "client_id": CFG["client_id"],
                "client_secret": CFG["client_secret"],
                "grant_type": "refresh_token",
            }).encode()
            req = urllib.request.Request(f"{CFG['accounts_url']}/oauth/v2/token", data=data)
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read())
            if "access_token" not in body:
                raise RuntimeError(f"Zoho OAuth error: {body.get('error', body)}")
            self._token = body["access_token"]
            self._token_exp = time.time() + int(body.get("expires_in", 3600))
            return self._token

    def _headers(self):
        h = {"Accept": "application/vnd.manageengine.sdp.v3+json"}
        if CFG["mode"] == "onprem":
            h["authtoken"] = CFG["onprem_key"]
        else:
            h["Authorization"] = f"Zoho-oauthtoken {self._access_token()}"
        return h

    def _get(self, path, input_data):
        qs = urllib.parse.urlencode({"input_data": json.dumps(input_data)})
        url = f"{CFG['base_url']}/api/v3/{path}?{qs}"
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=45, context=self._ctx) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            if e.code == 401:
                self._token = None
            raise RuntimeError(f"SDP API HTTP {e.code}: {detail}") from None

    def open_critical_requests(self):
        """Pages through open requests at the configured priorities."""
        criteria = [
            {"field": "priority.name", "condition": "is", "values": CFG["priorities"]},
            {"field": "status.in_progress", "condition": "is", "values": [True],
             "logical_operator": "AND"},
        ]
        if CFG["levels"]:
            criteria.append({"field": "level.name", "condition": "is", "values": CFG["levels"],
                             "logical_operator": "AND"})
        out, start, row_count = [], 1, 100
        use_criteria = True
        while len(out) < CFG["max_tickets"]:
            list_info = {
                "row_count": row_count,
                "start_index": start,
                "sort_field": "created_time",
                "sort_order": "desc",
                "get_total_count": True,
                "fields_required": FIELDS,
            }
            if use_criteria:
                list_info["search_criteria"] = criteria
            else:
                # Fallback for instances that reject search_criteria: built-in open filter.
                list_info["filter_by"] = {"name": "Open_System"}
            try:
                body = self._get("requests", {"list_info": list_info})
            except RuntimeError as e:
                if use_criteria and start == 1 and "HTTP 4" in str(e) and "401" not in str(e):
                    use_criteria = False
                    continue
                raise
            out.extend(body.get("requests", []))
            if not body.get("list_info", {}).get("has_more_rows"):
                break
            start += row_count
        return out


# --------------------------------------------------------------------------- normalise
def _ms(field):
    """SDP time fields look like {"display_value": "...", "value": "1699999999999"}."""
    if isinstance(field, dict) and field.get("value"):
        try:
            return int(field["value"])
        except (TypeError, ValueError):
            return None
    return None


def _name(field):
    return field.get("name") if isinstance(field, dict) else None


def classify(raw, now_ms):
    pri = _name(raw.get("priority")) or "None"
    status = _name(raw.get("status")) or "Unknown"
    due = _ms(raw.get("due_by_time"))
    fr_due = _ms(raw.get("first_response_due_by_time"))
    created = _ms(raw.get("created_time"))
    updated = _ms(raw.get("last_updated_time"))
    responded = _ms(raw.get("responded_time"))
    tech = _name(raw.get("technician"))
    st = raw.get("status") or {}
    # stop_timer statuses (Awaiting Client Response, On Hold, RCA Pending ...) pause the SLA clock
    paused = bool(st.get("stop_timer"))

    # SDP's own flags are authoritative for breaches (they account for paused time)
    reasons, risks = [], []
    if raw.get("is_overdue"):
        reasons.append("Resolution SLA breached")
    if raw.get("is_first_response_overdue"):
        reasons.append("First response SLA breached")

    risk_ms = CFG["at_risk_hours"] * 3600_000
    if not reasons and not paused:
        if due and due - now_ms <= risk_ms:
            risks.append(f"Resolution due in < {CFG['at_risk_hours']:g}h")
        if fr_due and not responded and fr_due - now_ms <= risk_ms:
            risks.append(f"First response due in < {CFG['at_risk_hours']:g}h")
    if not tech:
        risks.append("Unassigned")
    if not due and not paused:
        risks.append("No SLA due date")
    last_touch = updated or created
    if not paused and last_touch and now_ms - last_touch > CFG["stale_hours"] * 3600_000:
        risks.append(f"No update in {CFG['stale_hours']:g}h+")

    health = "breached" if reasons else ("at_risk" if risks else "healthy")
    tid = raw.get("id")
    return {
        "id": tid,
        "display_id": raw.get("display_id") or tid,
        "subject": raw.get("subject") or "(no subject)",
        "priority": pri,
        "priority_color": (raw.get("priority") or {}).get("color"),
        "status": status,
        "sla_paused": paused,
        "level": _name(raw.get("level")),
        "request_type": _name(raw.get("request_type")),
        "technician": tech,
        "group": _name(raw.get("group")),
        "requester": _name(raw.get("requester")),
        "site": _name(raw.get("site")),
        "created": created,
        "due": due,
        "first_response_due": fr_due,
        "last_updated": updated,
        "health": health,
        "issues": reasons + risks,
        "url": ticket_link(tid),
    }


def build_payload(raw_requests, source):
    now_ms = int(time.time() * 1000)
    pri_set = {p.lower() for p in CFG["priorities"]}
    lvl_set = {lv.lower() for lv in CFG["levels"]}
    tickets = []
    for r in raw_requests:
        t = classify(r, now_ms)
        if t["priority"].lower() not in pri_set:
            continue
        if lvl_set and (t["level"] or "").lower() not in lvl_set:
            continue
        st = r.get("status") or {}
        if st.get("in_progress") is False or t["status"].lower() in CFG["closed_statuses"]:
            continue
        tickets.append(t)
    order = {"breached": 0, "at_risk": 1, "healthy": 2}
    pri_rank = {p.lower(): i for i, p in enumerate(CFG["priorities"])}
    tickets.sort(key=lambda t: (order[t["health"]], pri_rank.get(t["priority"].lower(), 99),
                                t["due"] or 1 << 62))
    return {
        "source": source,
        "fetched_at": now_ms,
        "config": {
            "priorities": CFG["priorities"],
            "levels": CFG["levels"],
            "at_risk_hours": CFG["at_risk_hours"],
            "stale_hours": CFG["stale_hours"],
            "refresh_seconds": CFG["refresh_seconds"],
        },
        "tickets": tickets,
    }


# --------------------------------------------------------------------------- demo data
def demo_requests():
    rnd = random.Random(int(time.time() // 300))  # stable for 5 min, then drifts
    now = int(time.time() * 1000)
    h = 3600_000
    subjects = [
        "Production DB failover not completing", "VPN down for EMEA office",
        "Payment gateway timeouts on checkout", "Email delivery delayed > 30 min",
        "SSO login loop for all users", "Translation memory server unreachable",
        "Client portal returning 502", "File transfer (SFTP) job failing nightly",
        "CRM sync stuck since morning", "Laptop fleet BitLocker recovery prompts",
        "Teams calls dropping in APAC", "Backup job failed on NAS-02",
        "Disk 95% on build server", "Printer queue down - Legal floor",
        "Phishing email reported by finance", "API rate limit hit on vendor integration",
        "Data warehouse load delayed", "Website SSL certificate expiring",
    ]
    techs = ["Priya Nair | Service Desk", "Marco Silva | Service Desk", "Hannah Weiss | SE",
             "Kenji Tanaka | Service Desk", "Live Monitor", None]
    groups = ["MTI Clients", "EU MTI Clients", "Premium Support", "Service Desk L2", "SE Team"]
    # (name, stop_timer) - mirrors the real status list
    statuses = [("Open", False), ("In Progress", False), ("In Progress", False),
                ("Awaiting Client Response", True), ("Awaiting Client Response", True),
                ("Awaiting Internal Response", False), ("Jira Ticket in Progress", False),
                ("On Hold", True), ("On Chase", False)]
    sla_hours = {"P1": 8, "P2": 24}
    out = []
    for i in range(rnd.randint(28, 40)):
        pname = rnd.choice(CFG["priorities"])
        hours = 48 if "-SR-" in pname else sla_hours.get(pname.split("|")[0].strip()[-2:], 8)
        if "| CL 4 Hours" in pname:
            hours = 4
        created = now - rnd.randint(1, int(hours * 1.6) + 2) * h
        due = created + hours * h
        fr_due = created + max(1, hours // 8) * h
        responded = fr_due - rnd.randint(1, 50) * 60_000 if rnd.random() < 0.93 else None
        updated = now - rnd.randint(0, 40) * h // (2 if rnd.random() < 0.7 else 1)
        st_name, stop = rnd.choice(statuses)
        overdue = (not stop) and due < now
        out.append({
            "id": str(75076000104600000 + i * 7),
            "display_id": str(199000 + i),
            "subject": rnd.choice(subjects),
            "priority": {"name": pname, "color": "#ed2626" if "-IN-" in pname else "#0eba0b"},
            "status": {"name": st_name, "stop_timer": stop, "in_progress": True},
            "technician": {"name": t} if (t := rnd.choice(techs)) else None,
            "group": {"name": rnd.choice(groups)},
            "level": {"name": rnd.choice(["Tier 2 - Service Desk", "Tier 3 - Solutions Engineering", "Tier 4 - Dev Team"])},
            "requester": {"name": rnd.choice(["A. Brooks", "L. Chen", "R. Patel", "S. Moreau"])},
            "created_time": {"value": str(created)},
            "due_by_time": {"value": str(due)},
            "first_response_due_by_time": {"value": str(fr_due)},
            "responded_time": {"value": str(responded)} if responded else None,
            "last_updated_time": {"value": str(max(updated, created))},
            "is_overdue": overdue,
            "is_first_response_overdue": (not responded) and fr_due < now,
        })
    return out


# --------------------------------------------------------------------------- cache
class Cache:
    def __init__(self):
        self.payload = None
        self.error = None
        self.fetched = 0
        self.lock = threading.Lock()
        self.client = None if demo_mode() else SDPClient()

    def get(self, force=False):
        with self.lock:
            fresh = time.time() - self.fetched < max(15, CFG["refresh_seconds"] - 5)
            if self.payload and fresh and not force:
                return self.payload, self.error
            try:
                if self.client is None:
                    self.payload = build_payload(demo_requests(), "demo")
                else:
                    self.payload = build_payload(self.client.open_critical_requests(), "live")
                self.error = None
            except Exception as e:  # keep serving last good data, surface the error
                self.error = str(e)
                print(f"[{datetime.now():%H:%M:%S}] fetch failed: {e}")
            self.fetched = time.time()
            return self.payload, self.error


CACHE = Cache()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=os.path.join(ROOT, "public"), **kw)

    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else ""):
            return
        super().log_message(fmt, *args)

    def do_GET(self):
        if self.path.startswith("/api/tickets"):
            force = "force=1" in self.path
            payload, err = CACHE.get(force=force)
            body = dict(payload or {"tickets": [], "source": "none", "fetched_at": None,
                                    "config": {"priorities": CFG["priorities"],
                                               "refresh_seconds": CFG["refresh_seconds"]}})
            body["error"] = err
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        return super().do_GET()


if __name__ == "__main__":
    mode = "DEMO (no credentials in .env)" if demo_mode() else f"LIVE ({CFG['mode']}: {CFG['base_url']})"
    print(f"Escalations Dashboard - {mode}")
    print(f"Tracking priorities: {', '.join(CFG['priorities'])}")
    print(f"Open http://localhost:{CFG['port']}")
    ThreadingHTTPServer((CFG["host"], CFG["port"]), Handler).serve_forever()
