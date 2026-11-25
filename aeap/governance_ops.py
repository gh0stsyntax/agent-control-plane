"""Abschnitt 2 als Plattform-Operationen: Change Requests, Notbremse, Evaluation, Guardrail-Status, Alarme."""

import copy
import json

from . import governance as gov
from .agent import AGENTS
from .contracts import canonical_json, sha256_hex
from .store import now_iso


class GovernanceError(Exception):
    pass


class GovernanceMixin:
    # Erwartet: self.store, self.agent, self.ingest, self.advance, self.run_robot, self.kpis, self.list_tasks,
    # self.Q_INTAKE / Q_EXEC / Q_DLQ

    # ---- 2.4 Change Requests ---------------------------------------------
    def settings_snapshot(self):
        return {k: self.store.get_setting(k) for k in gov.GOVERNED_KEYS}

    @staticmethod
    def _cr_row(r):
        for k in ("changes", "diff", "evaluation"):
            r[k] = json.loads(r[k]) if r.get(k) else None
        return r

    def list_changes(self):
        return [self._cr_row(r) for r in self.store.query("SELECT * FROM change_requests ORDER BY ts DESC")]

    def get_change(self, cr_id):
        r = self.store.one("SELECT * FROM change_requests WHERE id=?", (cr_id,))
        if not r:
            raise GovernanceError(f"Change Request {cr_id} nicht gefunden")
        return self._cr_row(r)

    def propose_change(self, user, changes, comment=""):
        """Regel-/Guardrail-Aenderungen werden nie direkt wirksam, sondern als Change Request mit Regressionstest."""
        with self.store.lock:
            gov.require(user, "policy.propose")
            current = self.settings_snapshot()
            proposed = copy.deepcopy(current)
            for k in gov.GOVERNED_KEYS:
                if changes.get(k) is not None:
                    proposed[k] = copy.deepcopy(changes[k])
            diff = []
            for k in gov.GOVERNED_KEYS:
                diff += gov.flat_diff(current[k], proposed[k], k)
            if not diff:
                raise GovernanceError("Keine Aenderung gegenueber der aktiven Konfiguration")
            policy_changed = any(d["path"].startswith("policy.") and d["path"] != "policy.rulesVersion" for d in diff)
            if policy_changed and proposed["policy"]["rulesVersion"] == current["policy"]["rulesVersion"]:
                proposed["policy"]["rulesVersion"] = gov.bump_version(current["policy"]["rulesVersion"])
                diff.append({"path": "policy.rulesVersion", "old": current["policy"]["rulesVersion"],
                             "new": proposed["policy"]["rulesVersion"], "auto": True})
            touched = {d["path"].split(".")[0] for d in diff}
            evaluation = gov.evaluate(proposed)
            cr_id = f"CR-{self.store.next_counter('cr'):04d}"
            self.store.execute(
                "INSERT INTO change_requests(id, ts, proposed_by, status, comment, changes, diff, evaluation, base_hash) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (cr_id, now_iso(), user, "PENDING", comment,
                 json.dumps({k: proposed[k] for k in touched}, ensure_ascii=False),
                 json.dumps(diff, ensure_ascii=False, default=str),
                 json.dumps(evaluation, ensure_ascii=False),
                 sha256_hex(canonical_json(current))))
            self.store.add_event(None, None, "-", "Governance", "change_request",
                                 f"{cr_id} beantragt von {user}: {len(diff)} Aenderung(en), Release-Gate {evaluation['gate']} "
                                 f"({evaluation['passed']}/{evaluation['total']} Kontrollfaelle)", {"diff": diff})
            return self.get_change(cr_id)

    def decide_change(self, cr_id, user, approve, comment=""):
        with self.store.lock:
            cr = self.get_change(cr_id)
            if cr["status"] != "PENDING":
                raise GovernanceError(f"{cr_id} ist nicht mehr offen ({cr['status']})")
            if approve:
                gov.require(user, "policy.approve")
                if user == cr["proposed_by"]:
                    raise GovernanceError("Funktionstrennung: Antragsteller darf die eigene Aenderung nicht genehmigen")
                if cr["evaluation"]["gate"] == "BLOCKED":
                    raise GovernanceError("Release-Gate blockiert: kritische Kontrollfaelle verletzt ("
                                          + ", ".join(cr["evaluation"]["criticalFailed"]) + ")")
                if sha256_hex(canonical_json(self.settings_snapshot())) != cr["base_hash"]:
                    raise GovernanceError("Die aktive Konfiguration hat sich seit dem Antrag geaendert. Bitte neu beantragen")
                for k, v in cr["changes"].items():
                    self.store.set_setting(k, v)
                    self.store.execute(
                        "INSERT INTO settings_history(ts, key, value, change_id, applied_by) VALUES(?,?,?,?,?)",
                        (now_iso(), k, json.dumps(v, ensure_ascii=False), cr_id, user))
                status = "APPLIED"
            else:
                if user != cr["proposed_by"]:
                    gov.require(user, "policy.approve")
                status = "REJECTED"
            self.store.execute(
                "UPDATE change_requests SET status=?, decided_by=?, decided_at=?, decision_comment=? WHERE id=?",
                (status, user, now_iso(), comment, cr_id))
            version = (cr["changes"].get("policy") or {}).get("rulesVersion")
            self.store.add_event(None, None, "-", "Governance", "change_request",
                                 f"{cr_id} {'genehmigt und aktiviert' if approve else 'abgelehnt/zurueckgezogen'} durch {user}"
                                 + (f" (Regeln {version})" if approve and version else ""))
            return self.get_change(cr_id)

    def settings_history(self):
        return self.store.query("SELECT id, ts, key, change_id, applied_by FROM settings_history ORDER BY id DESC LIMIT 50")

    # ---- 2.9 Notbremse ---------------------------------------------------
    def set_killswitch(self, user, flags):
        """Nichts geht verloren: beim Loesen wird zurueckgehaltene Arbeit automatisch fortgesetzt."""
        with self.store.lock:
            gov.require(user, "killswitch")
            before = self.store.get_setting("killSwitch", {})
            after = {k: bool(flags.get(k, before.get(k, False))) for k in gov.KILL_LABELS}
            self.store.set_setting("killSwitch", after)
            for k, label in gov.KILL_LABELS.items():
                if bool(before.get(k)) != after[k]:
                    self.store.add_event(None, None, "-", "Automation Ops", "killswitch",
                                         f"Notbremse {label} {'GEZOGEN' if after[k] else 'geloest'} durch {user}")
            resumed = {"intake": 0, "agents": 0, "execution": 0}
            if before.get("intake") and not after["intake"]:
                held = self.store.query("SELECT * FROM queue_items WHERE queue=? AND status='New' AND reference LIKE 'held-%' ORDER BY id",
                                        (self.Q_INTAKE,))
                for item in held:
                    self.store.execute("DELETE FROM queue_items WHERE id=?", (item["id"],))
                    self.ingest(json.loads(item["payload"])["mail"])
                    resumed["intake"] += 1
            if before.get("agents") and not after["agents"]:
                for c in self.store.query("SELECT case_id FROM cases WHERE state IN ('EXTRACTED','VALIDATED') ORDER BY created_at"):
                    self.advance(c["case_id"])
                    resumed["agents"] += 1
            if before.get("execution") and not after["execution"] and self.store.get_setting("robot", {}).get("autoRun", True):
                resumed["execution"] = len(self.run_robot())
            return {"killSwitch": after, "resumed": resumed}

    # ---- 2.5 Agent-Evaluation --------------------------------------------
    def run_agent_evaluation(self, user):
        gov.require(user, "agent.evaluate")
        result = gov.evaluate(self.settings_snapshot())
        record = {"ts": now_iso(), "user": user, "agent": f"{self.agent.agent_id}@{self.agent.version}", "result": result}
        self.store.set_setting("agentEval", record)
        self.store.add_event(None, None, "-", "Governance", "evaluation",
                             f"Evaluation {record['agent']} durch {user}: {result['passed']}/{result['total']} bestanden, "
                             f"Gate {result['gate']}")
        return record

    # ---- 2.3 Guardrail-Status --------------------------------------------
    def _rule_counts(self):
        counts = {}
        for c in self.store.query("SELECT envelope FROM cases"):
            for r in (json.loads(c["envelope"]).get("policy") or {}).get("rulesFired", []):
                rid = r.split(" ")[0]
                counts[rid] = counts.get(rid, 0) + 1
        return counts

    def _tokens_per_case(self):
        per = {}
        for r in self.store.query("SELECT case_id, data FROM events WHERE kind='llm_call'"):
            d = json.loads(r["data"]) if r["data"] else {}
            per[r["case_id"]] = per.get(r["case_id"], 0) + d.get("promptTokens", 0) + d.get("completionTokens", 0)
        return per

    def _count(self, sql, params=()):
        return self.store.one(sql, params)["n"]

    def guardrail_status(self):
        cfg = {**self.store.get_setting("guardrails", {}), **self.store.get_setting("automationOps", {})}
        rules = self._rule_counts()
        llm = [json.loads(r["data"]) for r in self.store.query("SELECT data FROM events WHERE kind='llm_call'") if r["data"]]
        budget = self.store.get_setting("alerting", gov.DEFAULT_ALERTING)["maxTokensPerCase"]
        c = self._count
        hits = {
            "G-01": c("SELECT COUNT(*) n FROM events WHERE kind='duplicate'"),
            "G-02": c("SELECT COUNT(*) n FROM queue_items WHERE queue=?", (self.Q_DLQ,)),
            "G-03": sum(sum((d.get("maskedEntities") or {}).values()) for d in llm),
            "G-04": sum(1 for d in llm if (d.get("guardrails") or {}).get("promptInjection")),
            "G-05": sum(1 for d in llm if (d.get("guardrails") or {}).get("harmful")),
            "G-06": c("SELECT COUNT(*) n FROM events WHERE kind='schema_error'"),
            "G-07": rules.get("R-005", 0),
            "G-08": rules.get("R-001", 0),
            "G-09": rules.get("R-004", 0),
            "G-10": c("SELECT COUNT(*) n FROM cases WHERE risk_tier IS NOT NULL"),
            "G-11": c("SELECT COUNT(*) n FROM tasks WHERE type='APPROVAL' AND required_approvals=2"),
            "G-12": c("SELECT COUNT(*) n FROM queue_items WHERE exception_reason LIKE 'APPROVAL_HASH_MISMATCH%'"),
            "G-13": c("SELECT COUNT(*) n FROM events WHERE kind='verify' AND message LIKE 'MISMATCH%'"),
            "G-14": c("SELECT COUNT(*) n FROM events WHERE kind='killswitch'"),
            "G-15": c("SELECT COUNT(*) n FROM change_requests"),
            "G-16": sum(1 for d in llm if d.get("input") == "[Inhalt durch Policy blockiert]"),
            "G-17": sum(1 for v in self._tokens_per_case().values() if v > budget),
        }
        return [{**g, "enabled": cfg.get(g["setting"], True) if g.get("setting") else True, "hits": hits.get(g["id"], 0)}
                for g in gov.GUARDRAILS]

    # ---- 2.8 Alarme --------------------------------------------------------
    def alerts(self):
        k = self.kpis()
        th = {**gov.DEFAULT_ALERTING, **(self.store.get_setting("alerting") or {})}
        ks = self.store.get_setting("killSwitch", {})
        guards = {g["id"]: g for g in self.guardrail_status()}
        out = []

        def add(sev, title, detail, action, link=None):
            out.append({"severity": sev, "title": title, "detail": detail, "action": action, "link": link})

        active = [gov.KILL_LABELS[x] for x in gov.KILL_LABELS if ks.get(x)]
        if active:
            add("crit", "Notbremse aktiv", ", ".join(active) + " angehalten. Arbeit wird zurückgehalten, nicht verworfen.",
                "Ursache klären, dann unter Governance → Notbremse lösen.", "#/governance")
        if k["hashBlocks"]:
            add("crit", "Manipulationsversuch erkannt", f"{k['hashBlocks']} Ausführung(en) wegen abweichendem Approval-Hash blockiert.",
                "Informationssicherheit informieren, betroffene Queue-Items und Zugriffe prüfen.", "#/queues")
        if guards["G-04"]["hits"]:
            add("warn", "Prompt-Injection-Versuche", f"{guards['G-04']['hits']} KI-Aufruf(e) mit Injection-Mustern.",
                "Absender prüfen und ggf. sperren. Die Fälle wurden vom PDP gestoppt.", "#/audit")
        if k["total"] >= 5 and k["escalated"] / k["total"] > th["escalationRate"]:
            add("warn", "Hohe Eskalationsquote",
                f"{k['escalated'] / k['total']:.0%} der Cases eskaliert (Schwelle {th['escalationRate']:.0%}).",
                "Im Audit nach Komponente filtern. Häufige Ursachen: Stammdaten, Systemfehler, zu strenge Regeln.", "#/cases")
        if k["total"] >= 5 and k["policyRejects"] / k["total"] > th["rejectRate"]:
            add("warn", "Viele Policy-Ablehnungen", f"{k['policyRejects']} Case(s) durch Policy abgelehnt.",
                "Prüfen, ob ein Angriff vorliegt oder ein Werkzeug in der Allowlist fehlt (Änderungsantrag).", "#/cases")
        overdue = [t for t in self.list_tasks("PENDING") if t["slaOverdue"]]
        if len(overdue) >= th["slaOverdue"]:
            add("warn", "SLA überschritten", f"{len(overdue)} offene Aufgabe(n) über der Frist.",
                "Teamlead verteilt Aufgaben neu (Eskalationskette).", "#/action")
        if k["deadLetters"]:
            add("info", "Dead-Letter-Queue nicht leer", f"{k['deadLetters']} unlesbare Nachricht(en) geparkt.",
                "Nachrichten manuell sichten und beim Absender neu anfordern.", "#/queues")
        sysfail = self._count("SELECT COUNT(*) n FROM queue_items WHERE exception_type='System'")
        if sysfail:
            add("warn", "Systemfehler bei Ausführung", f"{sysfail} Transaktion(en) nach allen Retries fehlgeschlagen.",
                "IT-Betrieb: Erreichbarkeit von ERP bzw. Connector prüfen.", "#/queues")
        if guards["G-17"]["hits"]:
            add("info", "Token-Budget überschritten", f"{guards['G-17']['hits']} Case(s) über {th['maxTokensPerCase']} Tokens.",
                "Auf Retry-Schleifen prüfen (Audit, Art llm_call).", "#/audit")
        pending = self._count("SELECT COUNT(*) n FROM change_requests WHERE status='PENDING'")
        if pending:
            add("info", "Offene Änderungsanträge", f"{pending} Change Request(s) warten auf Genehmigung.",
                "Compliance prüft und genehmigt unter Governance → Änderungen.", "#/governance")
        return out

    def governance_view(self):
        held = {
            "intake": self._count("SELECT COUNT(*) n FROM queue_items WHERE queue=? AND status='New' AND reference LIKE 'held-%'",
                                  (self.Q_INTAKE,)),
            "agents": self._count("SELECT COUNT(*) n FROM cases WHERE state IN ('EXTRACTED','VALIDATED')"),
            "execution": self._count("SELECT COUNT(*) n FROM queue_items WHERE queue=? AND status='New'", (self.Q_EXEC,)),
        }
        return {
            "roles": gov.ROLES,
            "users": gov.USERS,
            "permissions": {k: {"label": v["label"], "roles": sorted(v["roles"])} for k, v in gov.PERMISSIONS.items()},
            "raci": {"roles": gov.RACI_ROLES, "rows": gov.RACI},
            "guardrails": self.guardrail_status(),
            "agents": AGENTS,
            "lifecycle": gov.LIFECYCLE,
            "agentEval": self.store.get_setting("agentEval"),
            "golden": gov.GOLDEN_SET,
            "killSwitch": self.store.get_setting("killSwitch", {}),
            "killLabels": gov.KILL_LABELS,
            "held": held,
            "alerts": self.alerts(),
            "changes": self.list_changes(),
            "history": self.settings_history(),
            "alerting": {**gov.DEFAULT_ALERTING, **(self.store.get_setting("alerting") or {})},
        }
