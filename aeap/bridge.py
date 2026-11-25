"""Abschnitt 3.4/3.6: External Agent Bridge [C] bzw. Partner-Events [3] als signierter Eingang.

Vertrag für Aufrufer:
  POST /api/bridge/inbound
  Header  X-AEAP-Timestamp: <Unix-Sekunden>
          X-AEAP-Signature: sha256=<hex(HMAC-SHA256(secret, "<timestamp>.<rohbody>"))>
          Idempotency-Key:  <eindeutig je fachlicher Nachricht>
          traceparent:      00-<32 hex trace-id>-<16 hex span-id>-<2 hex flags>   (optional, W3C Trace Context)
  Body    {"type": "supplier.message", "data": {"from": …, "subject": …, "body": …, "attachments": [...]}}

Prüfreihenfolge: Zeitfenster → Signatur → Idempotency-Key → Schema → Verarbeitung.
Gleicher Key + gleicher Inhalt = gespeicherte Antwort (sicher wiederholbar). Gleicher Key + anderer Inhalt = 409.
"""

import hashlib
import hmac
import json
import re
import secrets
import time

from .store import now_iso

TRACEPARENT_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
EVENT_TYPES = {"supplier.message"}
DEFAULT_WINDOW_S = 300


def sign(secret, timestamp, raw_body):
    if isinstance(raw_body, str):
        raw_body = raw_body.encode("utf-8")
    mac = hmac.new(secret.encode("utf-8"), str(timestamp).encode("utf-8") + b"." + raw_body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


class Bridge:
    def __init__(self, platform):
        self.platform = platform
        self.store = platform.store

    def config(self):
        cfg = self.store.get_setting("bridge")
        if not cfg or not cfg.get("secret"):
            cfg = {"secret": secrets.token_hex(24), "replayWindowS": DEFAULT_WINDOW_S, "enabled": True}
            self.store.set_setting("bridge", cfg)
        return cfg

    def rotate_secret(self, user):
        from . import governance as gov
        gov.require(user, "killswitch")  # Betriebsrecht (CoE/Teamlead/Compliance)
        cfg = self.config()
        cfg["secret"] = secrets.token_hex(24)
        self.store.set_setting("bridge", cfg)
        self.store.add_event(None, None, "-", "[C] External Bridge", "bridge_config", f"Bridge-Secret rotiert durch {user}")
        return self.info()

    def info(self):
        cfg = self.config()
        rows = self.store.query("SELECT idem_key, ts, status_code, response FROM bridge_requests ORDER BY ts DESC LIMIT 30")
        for r in rows:
            r["response"] = json.loads(r["response"])
        rejects = self.store.query("SELECT ts, message FROM events WHERE kind='bridge_reject' ORDER BY id DESC LIMIT 30")
        return {"endpoint": "/api/bridge/inbound", "secret": cfg["secret"], "replayWindowS": cfg["replayWindowS"],
                "enabled": cfg["enabled"], "eventTypes": sorted(EVENT_TYPES), "accepted": rows, "rejected": rejects}

    def _reject(self, code, msg, trace_id=None):
        self.store.add_event(None, None, "S0", "[C] External Bridge", "bridge_reject", f"HTTP {code}: {msg}",
                             {"traceId": trace_id} if trace_id else None)
        return code, {"error": msg}, {}

    def handle(self, headers, raw_body):
        """Gibt (HTTP-Status, Antwort-JSON, Zusatz-Header) zurück."""
        cfg = self.config()
        get = lambda k: (headers.get(k) or "").strip()  # noqa: E731
        tp = TRACEPARENT_RE.match(get("traceparent").lower())
        trace_id = tp.group(1) if tp else secrets.token_hex(16)
        if not cfg.get("enabled", True):
            return self._reject(503, "Bridge deaktiviert", trace_id)

        # 1. Zeitfenster (Replay-Schutz)
        try:
            ts = int(get("X-AEAP-Timestamp"))
        except ValueError:
            return self._reject(401, "X-AEAP-Timestamp fehlt oder ist ungueltig", trace_id)
        if abs(time.time() - ts) > cfg.get("replayWindowS", DEFAULT_WINDOW_S):
            return self._reject(401, f"Zeitstempel ausserhalb des Replay-Fensters ({cfg.get('replayWindowS')} s)", trace_id)

        # 2. Signatur (konstante Laufzeit beim Vergleich)
        expected = sign(cfg["secret"], ts, raw_body)
        if not hmac.compare_digest(expected, get("X-AEAP-Signature")):
            return self._reject(401, "Signatur ungueltig", trace_id)

        # 3. Idempotenz
        idem = get("Idempotency-Key")
        if not idem or len(idem) > 200:
            return self._reject(400, "Idempotency-Key fehlt", trace_id)
        body_hash = hashlib.sha256(raw_body).hexdigest()
        prev = self.store.one("SELECT * FROM bridge_requests WHERE idem_key=?", (idem,))
        if prev:
            if prev["body_hash"] != body_hash:
                return self._reject(409, "Idempotency-Key wurde mit anderem Inhalt wiederverwendet", trace_id)
            self.store.add_event(None, None, "S0", "[C] External Bridge", "bridge_replay",
                                 f"Wiederholung mit Idempotency-Key {idem[:24]} → gespeicherte Antwort, keine Neuverarbeitung")
            return prev["status_code"], json.loads(prev["response"]), {"Idempotent-Replay": "true"}

        # 4. Schema
        try:
            msg = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._reject(400, "Body ist kein gueltiges JSON", trace_id)
        if not isinstance(msg, dict) or msg.get("type") not in EVENT_TYPES or not isinstance(msg.get("data"), dict):
            return self._reject(422, f"Unbekannter Nachrichtentyp. Erlaubt: {', '.join(sorted(EVENT_TYPES))}", trace_id)
        data = msg["data"]
        atts = data.get("attachments") or []
        if not isinstance(atts, list) or any(not isinstance(a, dict) for a in atts):
            return self._reject(422, "attachments muss eine Liste von Objekten sein", trace_id)

        # 5. Verarbeitung über den normalen Ingress (gleiche Kontrollen wie E-Mail)
        mail = {
            "channel": "partner-api",
            "messageId": f"<bridge-{hashlib.sha256(idem.encode()).hexdigest()[:20]}@partner>",
            "from": str(data.get("from", "")),
            "subject": str(data.get("subject", "")),
            "body": str(data.get("body", "")),
            "attachments": [{"filename": str(a.get("filename", "anhang.txt")), "content": str(a.get("content", "")),
                             "quality": float(a.get("quality", 1.0))} for a in atts],
        }
        result = self.platform.ingest(mail)
        if result.get("caseId"):
            case = self.store.get_case(result["caseId"])
            env = case["envelope"]
            env["source"]["traceparent"] = f"00-{trace_id}-{secrets.token_hex(8)}-01"
            env["source"]["bridgeIdempotencyKey"] = idem
            self.store.save_case(case["case_id"], env, case["state"])
        status = 202 if result["status"] in ("ACCEPTED", "HELD") else 200
        response = {**result, "traceId": trace_id}
        self.store.execute("INSERT INTO bridge_requests(idem_key, ts, status_code, response, body_hash) VALUES(?,?,?,?,?)",
                           (idem, now_iso(), status, json.dumps(response, ensure_ascii=False), body_hash))
        self.store.add_event(result.get("caseId"), None, "S0", "[C] External Bridge", "bridge_accept",
                             f"Signierte Partner-Nachricht angenommen ({result['status']}), traceId {trace_id}",
                             {"traceId": trace_id, "idempotencyKey": idem})
        return status, response, {}
