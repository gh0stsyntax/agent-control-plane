"""HTTP-API und statische Web-UI (nur Python-Standardbibliothek)."""

import json
import mimetypes
import os
import re
import socket
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .assistant import Assistant
from .bridge import Bridge
from .contracts import ALL_STATES
from .pdp import DEFAULT_POLICY
from .governance import GOVERNED_KEYS, Forbidden
from .governance_ops import GovernanceError
from .llm import LLMClient, LLMError
from .platform import Platform, PlatformError
from .scenarios import SCENARIOS, random_mail, scenario_mail

# Windows-Registry liefert fuer .js teils text/plain
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


PROJECT_ROOT = os.path.dirname(WEB_DIR)


def make_handler(platform, assistant=None):
    routes = []
    assistant = assistant or Assistant(platform.store, LLMClient(PROJECT_ROOT))
    bridge = Bridge(platform)

    def route(method, pattern):
        def deco(fn):
            routes.append((method, re.compile("^" + pattern + "$"), fn))
            return fn
        return deco

    # ---- Lesen -----------------------------------------------------------
    @route("GET", "/api/meta")
    def meta(q, body):
        return {**platform.meta(), "states": ALL_STATES,
                "scenarios": [{k: s[k] for k in ("id", "title", "expect")} for s in SCENARIOS]}

    @route("GET", "/api/kpis")
    def kpis(q, body):
        return platform.kpis()

    @route("GET", "/api/cases")
    def cases(q, body):
        return platform.list_cases(q.get("state"))

    @route("GET", "/api/cases/(?P<cid>[\\w-]+)")
    def case(q, body, cid):
        return platform.case_view(cid)

    @route("GET", "/api/cases/(?P<cid>[\\w-]+)/auditpack")
    def auditpack(q, body, cid):
        return platform.audit_pack(cid)

    @route("GET", "/api/tasks")
    def tasks(q, body):
        return platform.list_tasks(q.get("status"))

    @route("GET", "/api/queues")
    def queues(q, body):
        return platform.queues()

    @route("GET", "/api/erp")
    def erp(q, body):
        return platform.erp_view()

    @route("GET", "/api/audit")
    def audit(q, body):
        return platform.store.events(case_id=q.get("caseId"), correlation_id=q.get("correlationId"),
                                     limit=int(q.get("limit", 300)))

    @route("GET", "/api/settings")
    def settings(q, body):
        return {k: platform.store.get_setting(k) for k in ("policy", "chaos", "guardrails", "automationOps", "robot", "killSwitch")}

    # ---- Schreiben -------------------------------------------------------
    @route("POST", "/api/ingest")
    def ingest(q, body):
        if body.get("scenarioId"):
            mail = scenario_mail(body["scenarioId"], fixed_message_id=bool(body.get("fixedMessageId")))
        else:
            mail = body.get("mail") or {}
        return platform.ingest(mail)

    @route("POST", "/api/simulate")
    def simulate(q, body):
        n = max(1, min(int(body.get("count", 20)), 500))
        out = {}
        for _ in range(n):
            r = platform.ingest(random_mail(platform.rng))
            key = r.get("state") or r["status"]
            out[key] = out.get(key, 0) + 1
        return {"count": n, "result": out}

    @route("POST", "/api/tasks/(?P<tid>[\\w-]+)/approve")
    def approve(q, body, tid):
        return platform.approve(tid, body.get("user"), bool(body.get("oobVerified")), body.get("comment", ""),
                                body.get("confirmHash"))

    @route("POST", "/api/tasks/(?P<tid>[\\w-]+)/deny")
    def deny(q, body, tid):
        return platform.deny(tid, body.get("user"), body.get("comment", ""))

    @route("POST", "/api/tasks/(?P<tid>[\\w-]+)/validate")
    def validate(q, body, tid):
        return platform.validate(tid, body.get("user"), body.get("documents", []))

    @route("POST", "/api/tasks/(?P<tid>[\\w-]+)/acknowledge")
    def acknowledge(q, body, tid):
        return platform.acknowledge(tid, body.get("user"))

    @route("POST", "/api/queues/(?P<iid>\\d+)/tamper")
    def tamper(q, body, iid):
        return platform.tamper(int(iid))

    @route("POST", "/api/robot/run")
    def robot(q, body):
        return platform.run_robot()

    @route("PUT", "/api/settings")
    def put_settings(q, body):
        # Demo-Steuerung (Chaos, Robot-Autostart) wirkt sofort. Governance-relevante Schluessel
        # (Regeln, Guardrails, Automation Ops) werden nie direkt gesetzt, sondern als Change Request (2.4).
        direct = [k for k in ("chaos", "robot") if k in body]
        for k in direct:
            platform.store.set_setting(k, body[k])
        if direct:
            platform.store.add_event(None, None, "-", "Demo-Steuerung", "config", "Geaendert: " + ", ".join(direct))
        out = settings(q, {})
        governed = {k: body[k] for k in GOVERNED_KEYS if k in body}
        if governed:
            out["changeRequest"] = platform.propose_change(body.get("user"), governed, body.get("comment", ""))
        return out

    @route("POST", "/api/settings/policy/reset")
    def reset_policy(q, body):
        return {"changeRequest": platform.propose_change(body.get("user"), {"policy": DEFAULT_POLICY},
                                                         "Zuruecksetzen auf Standard-Regeln")}

    # ---- Abschnitt 2: Governance ------------------------------------------
    @route("GET", "/api/governance")
    def governance(q, body):
        return platform.governance_view()

    @route("GET", "/api/alerts")
    def alerts(q, body):
        return platform.alerts()

    @route("POST", "/api/governance/changes")
    def propose(q, body):
        return platform.propose_change(body.get("user"), body.get("changes") or {}, body.get("comment", ""))

    @route("POST", "/api/governance/changes/(?P<cid>[\\w-]+)/(?P<action>approve|reject)")
    def decide(q, body, cid, action):
        return platform.decide_change(cid, body.get("user"), action == "approve", body.get("comment", ""))

    @route("POST", "/api/governance/killswitch")
    def killswitch(q, body):
        return platform.set_killswitch(body.get("user"), body.get("flags") or {})

    # ---- Abschnitt 3: External Agent Bridge ------------------------------------
    @route("GET", "/api/bridge")
    def bridge_info(q, body):
        return bridge.info()

    @route("POST", "/api/bridge/rotate")
    def bridge_rotate(q, body):
        return bridge.rotate_secret(body.get("user"))

    # ---- KI-Assistent (externer LLM-Provider ueber das Gateway) ----------------
    @route("GET", "/api/assistant/status")
    def assistant_status(q, body):
        return assistant.status()

    @route("POST", "/api/assistant")
    def assistant_ask(q, body):
        return assistant.ask(body.get("messages") or [], body.get("context") or "", body.get("user"))

    @route("POST", "/api/governance/evaluate")
    def evaluate(q, body):
        return platform.run_agent_evaluation(body.get("user"))

    @route("POST", "/api/reset")
    def reset(q, body):
        platform.reset()
        return {"ok": True}

    class Handler(BaseHTTPRequestHandler):
        server_version = "AEAP/1.0"

        def log_message(self, fmt, *args):
            pass

        def _send(self, status, payload, ctype="application/json; charset=utf-8", extra=None):
            data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, method):
            url = urlparse(self.path)
            if not url.path.startswith("/api/"):
                return self._static(url.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            body = {}
            length = int(self.headers.get("Content-Length") or 0)
            if method == "POST" and url.path == "/api/bridge/inbound":
                raw = self.rfile.read(length) if length else b""
                try:
                    code, payload, extra = bridge.handle(self.headers, raw)
                except Exception as e:  # noqa: BLE001
                    traceback.print_exc()
                    return self._send(500, {"error": f"{type(e).__name__}: {e}"})
                return self._send(code, payload, extra=extra)
            if length:
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except json.JSONDecodeError:
                    return self._send(400, {"error": "Ungueltiges JSON"})
            for m, rx, fn in routes:
                match = rx.match(url.path)
                if m == method and match:
                    try:
                        result = fn(q, body, **match.groupdict())
                        extra = None
                        if url.path.endswith("/auditpack"):
                            extra = {"Content-Disposition": f'attachment; filename="{match.group("cid")}-auditpack.json"'}
                        return self._send(200, result, extra=extra)
                    except Forbidden as e:
                        return self._send(403, {"error": str(e)})
                    except (PlatformError, GovernanceError) as e:
                        return self._send(409, {"error": str(e)})
                    except LLMError as e:
                        return self._send(502, {"error": str(e)})
                    except Exception as e:  # noqa: BLE001
                        traceback.print_exc()
                        return self._send(500, {"error": f"{type(e).__name__}: {e}"})
            return self._send(404, {"error": "Route nicht gefunden"})

        def _static(self, path):
            if path in ("", "/"):
                path = "/index.html"
            full = os.path.normpath(os.path.join(WEB_DIR, path.lstrip("/")))
            if not full.startswith(WEB_DIR) or not os.path.isfile(full):
                return self._send(404, {"error": "nicht gefunden"})
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript",):
                ctype += "; charset=utf-8"
            with open(full, "rb") as fh:
                return self._send(200, fh.read(), ctype)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

    return Handler


class ExclusiveHTTPServer(ThreadingHTTPServer):
    """Verhindert, dass zwei Server denselben Port teilen (Windows erlaubt das sonst mit SO_REUSEADDR)."""

    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def serve(db_path, host="127.0.0.1", port=8710, tries=20):
    platform = Platform(db_path)
    handler = make_handler(platform)
    last = None
    for p in range(port, port + tries):
        try:
            return ExclusiveHTTPServer((host, p), handler), platform
        except OSError as e:
            last = e
    raise OSError(f"Kein freier Port zwischen {port} und {port + tries - 1}: {last}")
