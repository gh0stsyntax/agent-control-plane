"""Verdrahtung der Plattform: [4] Maestro, [6] Action Center, [13] Orchestrator-Queues, [14] Robot.

Ablauf S0-S10 aus 1.5, Zustandsmodell aus 1.6, Retry-Ownership aus 1.9.
"""

import copy
import json
import random
import uuid
from datetime import datetime, timedelta, timezone

from . import du
from . import governance as gov
from .agent import AGENTS, AgentFailure, SupplierTriageAgent, ToolNotPermitted
from .contracts import (TERMINAL_STATES, TOOL_CONTRACTS, TOOLS, TRANSITIONS, approval_hash, canonical_json, check_transition,
                        idempotency_key, mask_iban, sha256_hex)
from .pdp import DEFAULT_POLICY, PolicyDecisionPoint
from .store import Store, now_iso
from .systems import BusinessRuleException, SelectorNotFound, SystemException, Systems, seed
from .trust_layer import LLMGateway, MockLLM
from .governance_ops import GovernanceMixin

Q_INTAKE = "AEAP_Intake"
Q_EXEC = "AEAP_Execution"
Q_DLQ = "AEAP_DeadLetter"

ROBOT_MAX_SYS_RETRIES = 3

DEFAULT_SETTINGS = {
    "policy": DEFAULT_POLICY,
    "chaos": {
        "llmTimeoutRate": 0.0,
        "llm429Rate": 0.0,
        "llmSchemaErrorRate": 0.0,
        "erp5xxRate": 0.0,
        "erpTimeoutAfterCommitRate": 0.0,
        "selectorDriftRate": 0.0,
    },
    "guardrails": {"promptInjection": True, "piiMasking": True, "harmfulContent": True},
    "automationOps": {"storePromptIO": True},
    "robot": {"autoRun": True},
    # 2.9 Notbremse: K1 = Eingang anhalten, K2 = Agent anhalten, K3 = Ausfuehrung anhalten
    "killSwitch": {"intake": False, "agents": False, "execution": False},
    "alerting": gov.DEFAULT_ALERTING,
}

USERS = gov.USERS


class PlatformError(Exception):
    pass


def _strip_private(obj):
    if isinstance(obj, dict):
        return {k: _strip_private(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_strip_private(v) for v in obj]
    return obj


class Platform(GovernanceMixin):
    Q_INTAKE, Q_EXEC, Q_DLQ = Q_INTAKE, Q_EXEC, Q_DLQ

    def __init__(self, db_path, seed_value=None):
        self.store = Store(db_path)
        self.rng = random.Random(seed_value)
        self.systems = Systems(self.store, self.rng)
        self.gateway = LLMGateway(self.store, MockLLM(self.store, self.rng))
        self.agent = SupplierTriageAgent(self.store, self.systems, self.gateway, self.rng)
        self.pdp = PolicyDecisionPoint(self.store, self.systems)
        self._init_settings()
        seed(self.store)

    def _init_settings(self):
        for k, v in DEFAULT_SETTINGS.items():
            if self.store.get_setting(k) is None:
                self.store.set_setting(k, v)

    def reset(self):
        with self.store.lock:
            for t in ("cases", "events", "tasks", "queue_items", "erp_vendors", "erp_purchase_orders",
                      "erp_documents", "erp_change_log", "outbox", "tickets", "settings", "counters",
                      "change_requests", "settings_history", "bridge_requests"):
                self.store.execute(f"DELETE FROM {t}")
            self._init_settings()
            seed(self.store)

    # =====================================================================
    # Helpers
    # =====================================================================
    def _log(self, env, stage, component, kind, message, data=None):
        self.store.add_event(env["caseId"] if env else None, env["correlationId"] if env else None,
                             stage, component, kind, message, data)

    def _transition(self, case, target, stage, component, message=None):
        current = case["state"]
        check_transition(current, target)
        case["state"] = target
        env = case["envelope"]
        env.setdefault("history", []).append({"ts": now_iso(), "from": current, "to": target, "stage": stage})
        self.store.save_case(case["case_id"], env, target)
        self._log(env, stage, component, "state", message or f"{current} -> {target}", {"from": current, "to": target})

    def _escalate(self, case, stage, component, reason):
        if "ESCALATED" in TRANSITIONS.get(case["state"], set()):
            case["envelope"]["escalation"] = {"reason": reason, "stage": stage, "ts": now_iso()}
            self._transition(case, "ESCALATED", stage, component, f"ESCALATED: {reason}")
            self._create_task(case, "ESCALATION", f"Eskalation: {reason}", {
                "reason": reason, "stage": stage,
                "summary": (case["envelope"].get("agent") or {}).get("proposal", {}).get("summary"),
            })

    def _queue_add(self, queue, reference, case_id, payload):
        ts = now_iso()
        try:
            cur = self.store.execute(
                "INSERT INTO queue_items(queue, reference, case_id, status, payload, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (queue, reference, case_id, "New", json.dumps(payload, ensure_ascii=False), ts, ts))
            return cur.lastrowid
        except Exception as e:  # sqlite3.IntegrityError: Unique Reference
            if "UNIQUE" in str(e):
                return None
            raise

    def _queue_set(self, item_id, **fields):
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.store.execute(f"UPDATE queue_items SET {cols} WHERE id=?", (*fields.values(), item_id))

    def _get_case(self, case_id):
        case = self.store.get_case(case_id)
        if not case:
            raise PlatformError(f"Case {case_id} nicht gefunden")
        return case

    # =====================================================================
    # S0/S1 Ingress + Intake (Dispatcher)
    # =====================================================================
    def ingest(self, mail):
        with self.store.lock:
            mail = dict(mail)
            mail.setdefault("messageId", f"<{uuid.uuid4().hex[:16]}@mail.example>")
            mail.setdefault("receivedAt", now_iso())
            mail.setdefault("attachments", [])
            mail.setdefault("channel", "email")

            # Poison-Message-Erkennung -> Dead-Letter-Queue
            poison = None
            if not (mail.get("subject") or mail.get("body") or mail["attachments"]):
                poison = "leere Nachricht ohne Betreff, Text und Anhang"
            elif any(not isinstance(a.get("content"), str) or not a.get("content") for a in mail["attachments"]):
                poison = "Anhang nicht lesbar (korrupt oder leer)"
            if poison:
                ref = "dlq-" + sha256_hex(mail["messageId"])[:16]
                self._queue_add(Q_DLQ, ref, None, {"messageId": mail["messageId"], "reason": poison,
                                                   "subject": mail.get("subject")})
                self.store.add_event(None, None, "S0", "[2] Integration Service", "dead_letter",
                                     f"Poison Message {mail['messageId']} -> {Q_DLQ}: {poison}")
                return {"status": "DEAD_LETTER", "reason": poison}

            idem = idempotency_key(mail["messageId"], mail["attachments"])
            if self.store.get_setting("killSwitch", {}).get("intake"):
                held = self._queue_add(Q_INTAKE, "held-" + idem, None, {
                    "held": True, "messageId": mail["messageId"], "from": mail.get("from"),
                    "subject": mail.get("subject"), "mail": mail})
                if held is None:
                    return {"status": "DUPLICATE", "idempotencyKey": idem, "caseId": None}
                self.store.add_event(None, None, "S0", "[2] Integration Service", "killswitch_hold",
                                     f"Notbremse K1 aktiv: {mail['messageId']} wird zurueckgehalten (nicht verloren)")
                return {"status": "HELD", "reason": "Notbremse K1 (Eingang) aktiv"}
            self.store.add_event(None, None, "S0", "[2] Integration Service", "event",
                                 f"Event-Trigger 'Email Received' {mail['messageId']} ({mail.get('from')})")
            item_id = self._queue_add(Q_INTAKE, idem, None, {
                "messageId": mail["messageId"], "from": mail.get("from"), "subject": mail.get("subject"),
            })
            if item_id is None:
                existing = self.store.one("SELECT case_id FROM queue_items WHERE queue=? AND reference=?", (Q_INTAKE, idem))
                self.store.add_event(existing and existing["case_id"], None, "S1", "[14] Dispatcher", "duplicate",
                                     f"Duplikat verworfen (Unique Reference {idem[:12]}...) fuer {mail['messageId']}")
                return {"status": "DUPLICATE", "idempotencyKey": idem,
                        "caseId": existing["case_id"] if existing else None}

            # S2 Case-Start (Maestro)
            year = datetime.now(timezone.utc).year
            case_id = f"AEAP-{year}-{self.store.next_counter('case'):07d}"
            env = {
                "caseId": case_id,
                "correlationId": str(uuid.uuid4()),
                "idempotencyKey": idem,
                "source": {
                    "channel": mail["channel"],
                    "messageId": mail["messageId"],
                    "from": mail.get("from", ""),
                    "subject": mail.get("subject", ""),
                    "body": mail.get("body", ""),
                    "receivedAt": mail["receivedAt"],
                    "attachments": [{"filename": a.get("filename"), "sha256": sha256_hex(a["content"]),
                                     "quality": a.get("quality", 1.0)} for a in mail["attachments"]],
                },
                "_raw": {"attachments": mail["attachments"]},
                "documents": [],
                "agent": None,
                "policy": None,
                "approval": None,
                "execution": None,
                "history": [{"ts": now_iso(), "from": None, "to": "RECEIVED", "stage": "S2"}],
            }
            self.store.insert_case(env, "RECEIVED")
            self._queue_set(item_id, case_id=case_id, status="Successful")
            self._log(env, "S1", "[14] Dispatcher", "enqueue",
                      f"Queue-Item {Q_INTAKE}#{item_id} angelegt, Reference = Idempotency-Key")
            self._log(env, "S2", "[4] Maestro", "case_start", f"Prozessinstanz {case_id} gestartet",
                      {"correlationId": env["correlationId"]})
            self.advance(case_id)
            return {"status": "ACCEPTED", "caseId": case_id, "state": self._get_case(case_id)["state"]}

    # =====================================================================
    # Maestro: Pipeline bis zum naechsten Wait- oder Endzustand
    # =====================================================================
    def advance(self, case_id):
        with self.store.lock:
            case = self._get_case(case_id)
            pol = self.pdp.policy()
            th = pol["thresholds"]
            env = case["envelope"]

            if case["state"] == "RECEIVED":
                docs = du.process(env["_raw"]["attachments"], env["source"]["body"])
                self._transition(case, "DIGITIZED", "S3", "[15] DU",
                                 f"Digitize: {len(docs)} Dokument(e), Qualitaet {[d['scanQuality'] for d in docs]}")
                self._transition(case, "CLASSIFIED", "S3", "[15] DU",
                                 "Classify: " + ", ".join(f"{d['type']} ({d['classConfidence']})" for d in docs))
                env["documents"] = docs
                self._transition(case, "EXTRACTED", "S3", "[15] DU",
                                 "Extract: " + "; ".join(
                                     ", ".join(f"{k}={mask_iban(v) if k == 'iban' and v else v}" for k, v in d["fields"].items())
                                     for d in docs))
                reasons = du.needs_validation(docs, th["duClassConfidence"], th["duFieldConfidence"])
                if reasons:
                    self._transition(case, "VALIDATION_PENDING", "S3", "[15] DU",
                                     "Konfidenz unter Schwelle -> Validation Station")
                    self._create_task(case, "VALIDATION", "Validation Station: Extraktion pruefen", {
                        "reasons": reasons,
                        "documents": _strip_private(docs),
                        "thresholds": {"class": th["duClassConfidence"], "field": th["duFieldConfidence"]},
                    })
                    return

            if case["state"] in ("EXTRACTED", "VALIDATED"):
                if self.store.get_setting("killSwitch", {}).get("agents"):
                    self._log(env, "S4", "Automation Ops", "killswitch_hold",
                              "Notbremse K2 aktiv: Agent angehalten, Case wartet vor S4")
                    return
                self._reason(case)
                if case["state"] != "REASONED":
                    return

            if case["state"] == "REASONED":
                self._decide(case)

            if case["state"] in ("AUTO_APPROVED", "APPROVED"):
                self._enqueue_execution(case)
                if self.store.get_setting("robot", {}).get("autoRun", True):
                    self.run_robot()

    # ---- S4 ----------------------------------------------------------------
    def _reason(self, case):
        env = case["envelope"]
        pol = self.pdp.policy()
        key = f"{self.agent.agent_id}@{self.agent.version}"
        try:
            proposal, meta = self.agent.run(env, pol["allowlist"].get(key, []))
        except (AgentFailure, ToolNotPermitted) as e:
            self._log(env, "S4", "[A] Agent-Wrapper", "error", str(e))
            env["agent"] = {"agentId": self.agent.agent_id, "version": self.agent.version, "error": str(e)}
            self._escalate(case, "S4", "[A] Agent-Wrapper", str(e))
            return
        env["agent"] = {**meta, "proposal": proposal}
        self._transition(case, "REASONED", "S4", "[A] Agent",
                         f"Vorschlag {proposal['intent']} (Konfidenz {proposal['confidence']}, "
                         f"{meta['attempts']} LLM-Call(s)): {proposal['summary']}")

    # ---- S5 ----------------------------------------------------------------
    def _decide(self, case):
        env = case["envelope"]
        decision = self.pdp.decide(env)
        env["policy"] = decision
        self._transition(case, "DECIDED", "S5", "[5] PDP",
                         f"{decision['decision']} / {decision['riskTier']} (Regeln {decision['rulesVersion']}): "
                         + "; ".join(decision["reasons"]))
        actions = env["agent"]["proposal"]["actions"]
        d = decision["decision"]
        if d == "REJECT":
            self._transition(case, "REJECTED_POLICY", "S5", "[5] PDP", "REJECTED_POLICY: " + "; ".join(decision["reasons"]))
            self._log(env, "S5", "[5] PDP", "audit", "Policy-Verstoss auditiert", {"rulesFired": decision["rulesFired"]})
            return
        if d == "ESCALATE":
            self._escalate(case, "S5", "[5] PDP", "; ".join(decision["reasons"]))
            return
        h = approval_hash(env["caseId"], env["idempotencyKey"], actions)
        if d == "AUTO_EXECUTE":
            env["approval"] = {"mode": "AUTO", "decision": "APPROVED", "evidenceHash": h,
                               "approvedBy": [f"PDP {decision['rulesVersion']}"], "ts": now_iso()}
            self._transition(case, "AUTO_APPROVED", "S5", "[5] PDP", f"Automatisch freigegeben, Hash {h[:23]}...")
            return
        required = 2 if d == "REQUIRE_4EYES" else 1
        tier = decision["riskTier"]
        title = {
            "REQUIRE_4EYES": "4-Augen-Freigabe + Out-of-Band",
            "REQUIRE_APPROVAL": "Stichprobe: Freigabe" if decision.get("sampled") else "Freigabe",
        }[d]
        task_id = self._create_task(case, "APPROVAL", f"{title}: {env['agent']['proposal']['intent']}", {
            "summary": env["agent"]["proposal"]["summary"],
            "intent": env["agent"]["proposal"]["intent"],
            "riskTier": tier,
            "reasons": decision["reasons"],
            "rulesFired": decision["rulesFired"],
            "signals": decision["signals"],
            "documents": _strip_private(env["documents"]),
            "plannedActions": actions,
            "canonicalActions": canonical_json({"caseId": env["caseId"], "idempotencyKey": env["idempotencyKey"],
                                                "actions": actions}),
            "citations": env["agent"]["proposal"].get("citations", []),
            "agentRisks": env["agent"]["proposal"].get("risks", []),
            "replyDraft": env["agent"]["proposal"].get("replyDraft"),
        }, required=required, require_oob=bool(decision.get("requireOob")), evidence_hash=h, tier=tier)
        env["approval"] = {"mode": "HUMAN", "taskId": task_id, "decision": "PENDING", "evidenceHash": h,
                           "requiredApprovals": required}
        self._transition(case, "APPROVAL_PENDING", "S6", "[6] Action Center",
                         f"Task {task_id} angelegt ({required} Freigabe(n){', Out-of-Band' if decision.get('requireOob') else ''})")

    # =====================================================================
    # [6] Action Center
    # =====================================================================
    def _create_task(self, case, ttype, title, payload, required=1, require_oob=False, evidence_hash=None, tier=None):
        n = self.store.next_counter("task")
        task_id = f"t-{n}"
        created = datetime.now(timezone.utc)
        sla_hours = self.pdp.policy()["slaHours"].get(tier or "R2", 8) if ttype == "APPROVAL" else (4 if ttype == "VALIDATION" else 24)
        sla_due = (created + timedelta(hours=sla_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.store.execute(
            "INSERT INTO tasks(task_id, case_id, type, status, title, risk_tier, required_approvals, require_oob, "
            "approvals, payload, evidence_hash, created_at, sla_due) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, case["case_id"], ttype, "PENDING", title,
             tier or (case["envelope"].get("policy") or {}).get("riskTier"), required, int(require_oob), "[]",
             json.dumps(payload, ensure_ascii=False), evidence_hash, now_iso(), sla_due))
        self._log(case["envelope"], "S6" if ttype != "VALIDATION" else "S3", "[6] Action Center", "task_created",
                  f"{ttype}-Task {task_id}: {title} (SLA {sla_hours} h)")
        return task_id

    def get_task(self, task_id):
        t = self.store.one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        if not t:
            raise PlatformError(f"Task {task_id} nicht gefunden")
        t["approvals"] = json.loads(t["approvals"])
        t["payload"] = json.loads(t["payload"]) if t["payload"] else None
        due = datetime.strptime(t["sla_due"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        overdue = t["status"] == "PENDING" and datetime.now(timezone.utc) > due
        t["slaOverdue"] = overdue
        t["escalationLevel"] = "Teamlead" if overdue else None
        return t

    def list_tasks(self, status=None):
        rows = self.store.query(
            "SELECT task_id FROM tasks" + (" WHERE status=?" if status else "") + " ORDER BY created_at DESC",
            (status,) if status else ())
        return [self.get_task(r["task_id"]) for r in rows]

    def approve(self, task_id, user, oob_verified=False, comment="", confirm_hash=None):
        with self.store.lock:
            t = self.get_task(task_id)
            if t["type"] != "APPROVAL" or t["status"] != "PENDING":
                raise PlatformError("Task ist kein offener Approval-Task")
            gov.require(user, "task.approve")
            if any(a["user"] == user for a in t["approvals"]):
                raise PlatformError(f"4-Augen-Prinzip: {user} hat bereits freigegeben")
            if confirm_hash and confirm_hash != t["evidence_hash"]:
                raise PlatformError("Bestaetigter Hash passt nicht zum Evidence Pack")
            if t["require_oob"] and len(t["approvals"]) + 1 >= t["required_approvals"] \
                    and not oob_verified and not any(a.get("oobVerified") for a in t["approvals"]):
                raise PlatformError("Out-of-Band-Verifikation (Rueckruf unter Stammdaten-Telefonnummer) fehlt")
            if t["risk_tier"] == "R3" and len(t["approvals"]) + 1 >= t["required_approvals"]                     and not any(gov.can(u, "task.approve.r3_lead") for u in [user] + [a["user"] for a in t["approvals"]]):
                raise PlatformError("R3-Regel: mindestens eine Freigabe muss durch Teamlead erfolgen")
            t["approvals"].append({"user": user, "ts": now_iso(), "oobVerified": bool(oob_verified), "comment": comment})
            case = self._get_case(t["case_id"])
            env = case["envelope"]
            self._log(env, "S6", "[6] Action Center", "approval",
                      f"Freigabe {len(t['approvals'])}/{t['required_approvals']} durch {user}"
                      + (" inkl. Out-of-Band-Verifikation" if oob_verified else ""))
            done = len(t["approvals"]) >= t["required_approvals"]
            self.store.execute("UPDATE tasks SET approvals=?, status=?, decision=?, completed_at=? WHERE task_id=?",
                               (json.dumps(t["approvals"]), "COMPLETED" if done else "PENDING",
                                "APPROVED" if done else None, now_iso() if done else None, task_id))
            if done:
                env["approval"].update({"decision": "APPROVED", "approvedBy": [a["user"] for a in t["approvals"]],
                                        "oobVerified": any(a["oobVerified"] for a in t["approvals"]), "ts": now_iso()})
                self._transition(case, "APPROVED", "S6", "[6] Action Center", "Freigegeben durch " +
                                 ", ".join(a["user"] for a in t["approvals"]))
                self.advance(case["case_id"])
            else:
                env["approval"]["approvals"] = t["approvals"]
                self.store.save_case(case["case_id"], env, case["state"])
            return self.get_task(task_id)

    def deny(self, task_id, user, comment=""):
        with self.store.lock:
            t = self.get_task(task_id)
            if t["type"] != "APPROVAL" or t["status"] != "PENDING":
                raise PlatformError("Task ist kein offener Approval-Task")
            gov.require(user, "task.approve")
            self.store.execute("UPDATE tasks SET status='COMPLETED', decision='DENIED', completed_at=?, approvals=? WHERE task_id=?",
                               (now_iso(), json.dumps(t["approvals"] + [{"user": user, "ts": now_iso(), "denied": True,
                                                                         "comment": comment}]), task_id))
            case = self._get_case(t["case_id"])
            case["envelope"]["approval"].update({"decision": "DENIED", "deniedBy": user, "comment": comment})
            self._transition(case, "DENIED", "S6", "[6] Action Center", f"Abgelehnt durch {user}: {comment or '-'}")
            self._transition(case, "CLOSED_DENIED", "S10", "[4] Maestro", "Case ohne Ausfuehrung geschlossen")
            return self.get_task(task_id)

    def validate(self, task_id, user, documents):
        """Validation Station: Mensch korrigiert Klasse/Felder, Konfidenz wird 1.0."""
        with self.store.lock:
            t = self.get_task(task_id)
            if t["type"] != "VALIDATION" or t["status"] != "PENDING":
                raise PlatformError("Task ist kein offener Validation-Task")
            gov.require(user, "task.validate")
            case = self._get_case(t["case_id"])
            env = case["envelope"]
            corrections = []
            by_id = {d["docId"]: d for d in documents}
            for d in env["documents"]:
                upd = by_id.get(d["docId"])
                if not upd:
                    continue
                if upd.get("type") and upd["type"] != d["type"]:
                    corrections.append(f"{d['docId']}.type: {d['type']} -> {upd['type']}")
                    d["type"] = upd["type"]
                d["classConfidence"] = 1.0
                for k, v in (upd.get("fields") or {}).items():
                    if k == "amount" and v not in (None, ""):
                        v = round(float(str(v).replace(",", ".")), 2)
                    if v in ("", None):
                        v = None
                    if d["fields"].get(k) != v:
                        corrections.append(f"{d['docId']}.{k}: {d['fields'].get(k)!r} -> {v!r}")
                    d["fields"][k] = v
                    d["fieldConfidence"][k] = 1.0
                d["validatedBy"] = user
            self.store.execute("UPDATE tasks SET status='COMPLETED', decision='VALIDATED', completed_at=?, approvals=? WHERE task_id=?",
                               (now_iso(), json.dumps([{"user": user, "ts": now_iso(), "corrections": corrections}]), task_id))
            self._transition(case, "VALIDATED", "S3", "[6] Validation Station",
                             f"Validiert durch {user}: " + ("; ".join(corrections) if corrections else "keine Korrekturen"))
            self.advance(case["case_id"])
            return self.get_task(task_id)

    def acknowledge(self, task_id, user):
        with self.store.lock:
            t = self.get_task(task_id)
            if t["type"] != "ESCALATION" or t["status"] != "PENDING":
                raise PlatformError("Task ist keine offene Eskalation")
            gov.require(user, "task.escalation")
            self.store.execute("UPDATE tasks SET status='COMPLETED', decision='ACKNOWLEDGED', completed_at=?, approvals=? WHERE task_id=?",
                               (now_iso(), json.dumps([{"user": user, "ts": now_iso()}]), task_id))
            case = self._get_case(t["case_id"])
            self._log(case["envelope"], "S10", "[6] Action Center", "escalation_ack", f"Eskalation uebernommen durch {user}")
            return self.get_task(task_id)

    # =====================================================================
    # S7 Enqueue + [14] Robot (S8/S9) + S10 Close
    # =====================================================================
    def _enqueue_execution(self, case):
        env = case["envelope"]
        payload = {
            "caseId": env["caseId"],
            "correlationId": env["correlationId"],
            "idempotencyKey": env["idempotencyKey"],
            "actions": env["agent"]["proposal"]["actions"],
            "approvalHash": env["approval"]["evidenceHash"],
        }
        item_id = self._queue_add(Q_EXEC, f"{env['caseId']}:exec", env["caseId"], payload)
        if item_id is None:
            return
        env["execution"] = {"queue": Q_EXEC, "queueItemId": f"q-{item_id}", "attempt": 0, "result": "QUEUED"}
        self.store.save_case(case["case_id"], env, case["state"])
        self._log(env, "S7", "[13] Orchestrator", "enqueue",
                  f"{Q_EXEC}#{item_id} angelegt mit Approval-Hash {env['approval']['evidenceHash'][:23]}...")

    def tamper(self, item_id):
        """Demo TOCTOU: Payload nach der Freigabe manipulieren."""
        with self.store.lock:
            item = self.store.one("SELECT * FROM queue_items WHERE id=?", (item_id,))
            if not item or item["queue"] != Q_EXEC or item["status"] != "New":
                raise PlatformError("Nur neue Items in AEAP_Execution koennen manipuliert werden")
            payload = json.loads(item["payload"])
            a = payload["actions"][0]["args"]
            if "iban" in a:
                a["iban"] = "DE12700202700015873005"
                what = "IBAN auf Angreiferkonto geaendert"
            elif "amount" in a and a["amount"] is not None:
                a["amount"] = round(float(a["amount"]) * 10, 2)
                what = "Betrag verzehnfacht"
            else:
                a["tampered"] = True
                what = "Argument hinzugefuegt"
            self._queue_set(item_id, payload=json.dumps(payload, ensure_ascii=False))
            self.store.add_event(item["case_id"], payload["correlationId"], "S7", "[13] Orchestrator", "tamper",
                                 f"MANIPULATION (Demo): Queue-Item #{item_id} veraendert: {what}")
            return {"itemId": item_id, "change": what}

    def run_robot(self):
        processed = []
        if self.store.get_setting("killSwitch", {}).get("execution"):
            return processed
        with self.store.lock:
            items = self.store.query("SELECT * FROM queue_items WHERE queue=? AND status='New' ORDER BY id", (Q_EXEC,))
            for item in items:
                processed.append(self._process_item(item))
        return processed

    def _execute_action(self, env, item_id, idx, action, attempt_log):
        tool, args = action["tool"], dict(action["args"])
        idem = sha256_hex(f"{env['idempotencyKey']}|{idx}|{tool}")
        meta = TOOLS[tool]
        channel = "UI (SAP GUI, Healing Agent aktiv)" if meta.get("channel") == "UI" else "API"
        fn = {
            "erp.post_invoice": lambda: self.systems.post_invoice(idem, env["caseId"], args["supplierId"], args["invoiceNo"],
                                                                  args["amount"], args.get("currency", "EUR"), args.get("poNumber")),
            "erp.create_credit_note": lambda: self.systems.create_credit_note(idem, env["caseId"], args["supplierId"],
                                                                              args["reference"], args["amount"], args.get("currency", "EUR")),
            "erp.update_vendor_bank": lambda: self.systems.update_vendor_bank(idem, env["caseId"], args["supplierId"],
                                                                              args["iban"], args.get("accountHolder")),
            "crm.create_ticket": lambda: self.systems.create_ticket(idem, env["caseId"], args.get("tags", []), args.get("summary", "")),
        }.get(tool)
        if fn is None:
            raise BusinessRuleException(f"Kein Executor fuer {tool}")
        retries = 0
        while True:
            attempt_log.append(1)
            try:
                res = fn()
                msg = f"{tool} via {channel}: " + ", ".join(f"{k}={v}" for k, v in res.items())
                if res.get("replayed"):
                    msg += " (Idempotenz: bereits ausgefuehrt, keine Doppelbuchung)"
                self._log(env, "S8", "[14] Robot", "execute", msg, {"idempotencyKey": idem[:16]})
                return res
            except SystemException as e:
                if retries >= ROBOT_MAX_SYS_RETRIES:
                    raise
                retries += 1
                if isinstance(e, SelectorNotFound):
                    self._log(env, "S8", "[14] Healing Agent", "heal",
                              f"{e} -> Healing Agent: Selektor per Fuzzy-Match/CV neu ermittelt, Retry {retries}/{ROBOT_MAX_SYS_RETRIES}")
                else:
                    self._log(env, "S8", "[14] Robot", "retry",
                              f"System Exception: {e} -> Retry {retries}/{ROBOT_MAX_SYS_RETRIES} mit Idempotency-Key")

    def _process_item(self, item):
        payload = json.loads(item["payload"])
        case = self._get_case(payload["caseId"])
        env = case["envelope"]
        self._queue_set(item["id"], status="InProgress")
        self._transition(case, "EXECUTING", "S8", "[14] Robot", f"Robot uebernimmt {Q_EXEC}#{item['id']} (REFramework)")

        # TOCTOU-Pruefung: Hash ueber den Queue-Payload gegen den freigegebenen Hash aus Maestro/Action Center
        recomputed = approval_hash(payload["caseId"], payload["idempotencyKey"], payload["actions"])
        approved = env["approval"]["evidenceHash"]
        if recomputed != approved:
            reason = f"APPROVAL_HASH_MISMATCH: freigegeben {approved[:23]}..., Payload {recomputed[:23]}..."
            self._queue_set(item["id"], status="Failed", exception_type="Business", exception_reason=reason)
            env["execution"].update({"result": "BLOCKED_HASH_MISMATCH", "attempt": 0})
            self._transition(case, "FAILED_BUSINESS", "S8", "[14] Robot", "Nicht ausgefuehrt: " + reason)
            self._escalate(case, "S8", "[14] Robot", "Approval-Hash weicht ab (moegliche Manipulation)")
            return {"itemId": item["id"], "result": "BLOCKED_HASH_MISMATCH"}
        self._log(env, "S8", "[14] Robot", "hash_ok", "Approval-Hash verifiziert")

        attempt_log, results = [], []
        try:
            for idx, action in enumerate(payload["actions"]):
                results.append(self._execute_action(env, item["id"], idx, action, attempt_log))
        except BusinessRuleException as e:
            self._queue_set(item["id"], status="Failed", exception_type="Business", exception_reason=str(e),
                            attempt=len(attempt_log))
            env["execution"].update({"result": "FAILED_BUSINESS", "attempt": len(attempt_log), "error": str(e)})
            self._transition(case, "FAILED_BUSINESS", "S8", "[14] Robot", f"Business Rule Exception (kein Retry): {e}")
            self._escalate(case, "S8", "[14] Robot", f"Business Rule Exception: {e}")
            return {"itemId": item["id"], "result": "FAILED_BUSINESS"}
        except SystemException as e:
            self._queue_set(item["id"], status="Failed", exception_type="System", exception_reason=str(e),
                            attempt=len(attempt_log))
            env["execution"].update({"result": "FAILED_SYSTEM", "attempt": len(attempt_log), "error": str(e)})
            self._transition(case, "FAILED_SYSTEM", "S8", "[14] Robot", f"System Exception, Retries erschoepft: {e}")
            self._escalate(case, "S8", "[14] Robot", f"System Exception nach {ROBOT_MAX_SYS_RETRIES} Retries: {e}")
            return {"itemId": item["id"], "result": "FAILED_SYSTEM"}

        # S9 Verify (Read-back)
        checks = []
        for action, res in zip(payload["actions"], results):
            ok, msg = self.systems.read_back(action["tool"], action["args"], res)
            checks.append({"tool": action["tool"], "ok": ok, "detail": msg})
            self._log(env, "S9", "[14] Robot", "verify", ("OK " if ok else "MISMATCH ") + msg)
        env["execution"].update({"attempt": len(attempt_log), "results": results, "verification": checks})
        if not all(c["ok"] for c in checks):
            self._queue_set(item["id"], status="Failed", exception_type="Business", exception_reason="Read-back Mismatch",
                            attempt=len(attempt_log))
            env["execution"]["result"] = "VERIFY_MISMATCH"
            self._escalate(case, "S9", "[14] Robot", "Read-back weicht vom Soll ab, Kompensation pruefen")
            return {"itemId": item["id"], "result": "VERIFY_MISMATCH"}
        env["execution"]["result"] = "VERIFIED"
        self._queue_set(item["id"], status="Successful", attempt=len(attempt_log))
        self._transition(case, "VERIFIED", "S9", "[14] Robot", "Read-back: Ist = Soll")
        self._close(case)
        return {"itemId": item["id"], "result": "VERIFIED"}

    def _close(self, case):
        env = case["envelope"]
        proposal = env["agent"]["proposal"]
        recipient = env["source"]["from"]
        if proposal["intent"] == "UPDATE_BANK_DETAILS":
            # SOP-KRED-07: niemals Kontaktdaten aus der Anfrage-Mail verwenden
            v = self.systems.get_vendor(proposal["actions"][0]["args"]["supplierId"])
            recipient = v["email"] if v else recipient
        self.systems.send_mail(sha256_hex(env["idempotencyKey"] + "|reply"), env["caseId"], recipient,
                               f"Re: {env['source']['subject']} [{env['caseId']}]",
                               proposal.get("replyDraft") or "Ihr Anliegen wurde bearbeitet.")
        self._log(env, "S10", "[2] Integration Service", "reply", f"Antwort-Mail an {recipient} versendet")
        env["closedAt"] = now_iso()
        self._transition(case, "CLOSED", "S10", "[4] Maestro", "Case geschlossen, Audit-Pack verfuegbar")

    # =====================================================================
    # Lesesichten
    # =====================================================================
    def case_view(self, case_id):
        case = self._get_case(case_id)
        tasks = [self.get_task(r["task_id"]) for r in
                 self.store.query("SELECT task_id FROM tasks WHERE case_id=? ORDER BY created_at", (case_id,))]
        items = self.store.query("SELECT * FROM queue_items WHERE case_id=? ORDER BY id", (case_id,))
        for i in items:
            i["payload"] = json.loads(i["payload"])
        events = list(reversed(self.store.events(case_id=case_id, limit=1000)))
        env = _strip_private(case["envelope"])
        return {"case": {**case, "envelope": env}, "tasks": tasks, "queueItems": items, "events": events}

    def audit_pack(self, case_id):
        view = self.case_view(case_id)
        pack = {
            "generatedAt": now_iso(),
            "caseId": case_id,
            "correlationId": view["case"]["correlation_id"],
            "finalState": view["case"]["state"],
            "envelope": view["case"]["envelope"],
            "tasks": view["tasks"],
            "queueItems": view["queueItems"],
            "events": view["events"],
        }
        pack["packHash"] = "sha256:" + sha256_hex(canonical_json(pack))
        return pack

    def list_cases(self, state=None):
        out = []
        for c in self.store.list_cases(state):
            env = c["envelope"]
            docs = env.get("documents") or []
            prop = (env.get("agent") or {}).get("proposal") or {}
            out.append({
                "caseId": c["case_id"], "state": c["state"], "riskTier": c["risk_tier"],
                "createdAt": c["created_at"], "updatedAt": c["updated_at"],
                "from": env["source"].get("from"), "subject": env["source"].get("subject"),
                "docType": docs[0]["type"] if docs else None,
                "intent": prop.get("intent"),
                "decision": (env.get("policy") or {}).get("decision"),
                "amount": (docs[0]["fields"].get("amount") if docs else None),
                "terminal": c["state"] in TERMINAL_STATES,
            })
        return out

    def queues(self):
        rows = self.store.query("SELECT * FROM queue_items ORDER BY id DESC LIMIT 300")
        for r in rows:
            r["payload"] = json.loads(r["payload"])
        stats = self.store.query("SELECT queue, status, COUNT(*) n FROM queue_items GROUP BY queue, status")
        return {"items": rows, "stats": stats}

    def erp_view(self):
        return {
            "vendors": self.store.query("SELECT * FROM erp_vendors ORDER BY supplier_id"),
            "purchaseOrders": self.store.query("SELECT * FROM erp_purchase_orders ORDER BY po_number"),
            "documents": self.store.query("SELECT * FROM erp_documents ORDER BY created_at DESC LIMIT 200"),
            "changeLog": self.store.query("SELECT * FROM erp_change_log ORDER BY id DESC LIMIT 100"),
            "outbox": self.store.query("SELECT * FROM outbox ORDER BY id DESC LIMIT 100"),
            "tickets": self.store.query("SELECT * FROM tickets ORDER BY id DESC LIMIT 100"),
        }

    def kpis(self):
        cases = self.store.query("SELECT case_id, state, risk_tier FROM cases")
        by_state, by_tier = {}, {}
        for c in cases:
            by_state[c["state"]] = by_state.get(c["state"], 0) + 1
            if c["risk_tier"]:
                by_tier[c["risk_tier"]] = by_tier.get(c["risk_tier"], 0) + 1
        closed = by_state.get("CLOSED", 0)
        human_cases = {r["case_id"] for r in self.store.query("SELECT DISTINCT case_id FROM tasks WHERE type IN ('APPROVAL','VALIDATION')")}
        closed_ids = {c["case_id"] for c in cases if c["state"] == "CLOSED"}
        stp = len(closed_ids - human_cases)
        llm = self.store.query("SELECT data FROM events WHERE kind='llm_call'")
        tokens = sum((json.loads(r["data"]) or {}).get("promptTokens", 0) + (json.loads(r["data"]) or {}).get("completionTokens", 0)
                     for r in llm)
        return {
            "total": len(cases),
            "byState": by_state,
            "byTier": by_tier,
            "closed": closed,
            "straightThrough": stp,
            "stpRate": round(stp / closed, 3) if closed else None,
            "openTasks": self.store.one("SELECT COUNT(*) n FROM tasks WHERE status='PENDING'")["n"],
            "openApprovals": self.store.one("SELECT COUNT(*) n FROM tasks WHERE status='PENDING' AND type='APPROVAL'")["n"],
            "execQueueDepth": self.store.one("SELECT COUNT(*) n FROM queue_items WHERE queue=? AND status='New'", (Q_EXEC,))["n"],
            "deadLetters": self.store.one("SELECT COUNT(*) n FROM queue_items WHERE queue=?", (Q_DLQ,))["n"],
            "duplicatesDropped": self.store.one("SELECT COUNT(*) n FROM events WHERE kind='duplicate'")["n"],
            "llmCalls": len(llm),
            "llmTokens": tokens,
            "policyRejects": by_state.get("REJECTED_POLICY", 0),
            "escalated": by_state.get("ESCALATED", 0),
            "hashBlocks": self.store.one("SELECT COUNT(*) n FROM queue_items WHERE exception_reason LIKE 'APPROVAL_HASH_MISMATCH%'")["n"],
            "killSwitch": self.store.get_setting("killSwitch", {}),
        }

    def meta(self):
        return {
            "agents": AGENTS,
            "tools": TOOLS,
            "toolContracts": TOOL_CONTRACTS,
            "users": USERS,
            "roles": gov.ROLES,
            "permissions": {k: {"label": v["label"], "roles": sorted(v["roles"])} for k, v in gov.PERMISSIONS.items()},
            "queues": [Q_INTAKE, Q_EXEC, Q_DLQ],
        }
