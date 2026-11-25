"""SDD Abschnitt 2: Enterprise Governance & Guardrails.

Rollen und Rechte mit Funktionstrennung (2.2), Guardrail-Katalog (2.3),
Policy-Change-Management mit Regressionspruefung (2.4), Agent-Lifecycle (2.5),
Monitoring-Alarme (2.8) und Notbremse / Kill-Switch (2.9).
"""

import copy
import json

# ---------------------------------------------------------------------------
# 2.2 Rollen, Benutzer, Rechte (RBAC + Separation of Duties)
# ---------------------------------------------------------------------------
ROLES = {
    "AP": {"name": "Kreditorenbuchhaltung", "desc": "Sachbearbeitung: prüft Extraktionen, gibt R1/R2 frei, erste Freigabe bei R3"},
    "LEAD": {"name": "Teamlead Accounts Payable", "desc": "Fachliche Führung: alle Freigaben, Pflicht-Freigeber bei R3, darf Notbremse ziehen"},
    "COMPLIANCE": {"name": "Compliance", "desc": "Kontrolle: genehmigt Regeländerungen, liest Audit. Keine fachlichen Freigaben (Funktionstrennung)"},
    "COE": {"name": "Automation CoE / IT", "desc": "Betrieb: beantragt Regeländerungen, betreibt Agenten, darf Notbremse ziehen. Genehmigt nie selbst"},
}

USERS = [
    {"id": "a.becker", "name": "Anna Becker", "role": "AP"},
    {"id": "j.yilmaz", "name": "Jonas Yilmaz", "role": "AP"},
    {"id": "m.wolf", "name": "Maria Wolf", "role": "LEAD"},
    {"id": "s.klein", "name": "Sven Klein", "role": "COMPLIANCE"},
    {"id": "t.braun", "name": "Tom Braun", "role": "COE"},
]

PERMISSIONS = {
    "task.validate": {"label": "Extraktion validieren (Validation Station)", "roles": {"AP", "LEAD"}},
    "task.approve": {"label": "Fachliche Freigabe R0–R2 / erste Freigabe R3", "roles": {"AP", "LEAD"}},
    "task.approve.r3_lead": {"label": "Pflicht-Freigabe bei R3 (mind. eine)", "roles": {"LEAD"}},
    "task.escalation": {"label": "Eskalation übernehmen", "roles": {"AP", "LEAD"}},
    "policy.propose": {"label": "Regel-/Guardrail-Änderung beantragen", "roles": {"COE", "COMPLIANCE", "LEAD"}},
    "policy.approve": {"label": "Regel-/Guardrail-Änderung genehmigen (nie eigene)", "roles": {"COMPLIANCE"}},
    "killswitch": {"label": "Notbremse ziehen / lösen", "roles": {"COE", "LEAD", "COMPLIANCE"}},
    "agent.evaluate": {"label": "Agent-Evaluation starten", "roles": {"COE", "COMPLIANCE", "LEAD"}},
    "audit.read": {"label": "Audit lesen und exportieren", "roles": {"AP", "LEAD", "COMPLIANCE", "COE"}},
}


class Forbidden(Exception):
    pass


def user(uid):
    return next((u for u in USERS if u["id"] == uid), None)


def role_of(uid):
    u = user(uid)
    return u["role"] if u else None


def can(uid, perm):
    return role_of(uid) in PERMISSIONS[perm]["roles"]


def require(uid, perm):
    if not user(uid):
        raise Forbidden("Unbekannter Benutzer")
    if not can(uid, perm):
        u = user(uid)
        raise Forbidden(f"Keine Berechtigung: {u['name']} ({ROLES[u['role']]['name']}) darf nicht „{PERMISSIONS[perm]['label']}“")


# ---------------------------------------------------------------------------
# 2.1 RACI (R = durchführen, A = verantwortlich, C = konsultiert, I = informiert)
# ---------------------------------------------------------------------------
RACI_ROLES = ["Process Owner (Fachbereich)", "Automation CoE", "Compliance", "Informationssicherheit", "Datenschutz", "IT-Betrieb"]
RACI = [
    ("Risk-Tier-Schwellen festlegen", ["A", "C", "R", "C", "I", "I"]),
    ("PDP-Regel ändern", ["C", "R", "A", "C", "I", "I"]),
    ("Neue Agent-Version freigeben", ["A", "R", "C", "C", "C", "I"]),
    ("Tool zur Allowlist hinzufügen", ["C", "R", "A", "C", "I", "I"]),
    ("Guardrail abschalten", ["I", "R", "A", "C", "C", "I"]),
    ("Notbremse ziehen", ["R", "R", "R", "I", "I", "A"]),
    ("Incident bearbeiten", ["C", "R", "I", "A", "C", "R"]),
    ("Audit-Prüfung / Stichprobe", ["C", "I", "A", "C", "C", "I"]),
    ("PII-/Aufbewahrungsregeln", ["C", "C", "C", "C", "A", "R"]),
]

# ---------------------------------------------------------------------------
# 2.3 Guardrail-Katalog
# ---------------------------------------------------------------------------
GUARDRAILS = [
    {"id": "G-01", "layer": "L0 Ingress", "type": "Eingang", "name": "Duplikat-Schutz (Idempotency-Key)",
     "where": "[14] Dispatcher", "onHit": "Nachricht verwerfen, protokollieren", "status": "aktiv",
     "plain": "Die gleiche Mail wird nie zweimal verarbeitet."},
    {"id": "G-02", "layer": "L0 Ingress", "type": "Eingang", "name": "Poison Message → Dead-Letter-Queue",
     "where": "[2] Integration Service", "onHit": "In DLQ parken, Mensch sieht nach", "status": "aktiv",
     "plain": "Kaputte oder leere Nachrichten blockieren nicht den Rest."},
    {"id": "G-03", "layer": "L3 Governance", "type": "Eingabe (LLM)", "name": "PII-Pseudonymisierung",
     "where": "[10] LLM Gateway", "onHit": "IBAN/E-Mail/Telefon durch Platzhalter ersetzen", "status": "konfigurierbar", "setting": "piiMasking",
     "plain": "Das KI-Modell sieht nie echte Kontonummern oder Kontaktdaten."},
    {"id": "G-04", "layer": "L3 Governance", "type": "Eingabe (LLM)", "name": "Prompt-Injection-Erkennung",
     "where": "[11] Guardrails", "onHit": "Treffer markieren → PDP eskaliert (R-003)", "status": "konfigurierbar", "setting": "promptInjection",
     "plain": "Erkennt Texte, die die KI umprogrammieren wollen („Ignoriere alle Regeln …“)."},
    {"id": "G-05", "layer": "L3 Governance", "type": "Eingabe (LLM)", "name": "Schädliche Inhalte",
     "where": "[11] Guardrails", "onHit": "Treffer markieren → PDP eskaliert", "status": "konfigurierbar", "setting": "harmfulContent",
     "plain": "Filtert beleidigende oder gefährliche Inhalte."},
    {"id": "G-06", "layer": "L2 Cognitive", "type": "Ausgabe", "name": "Schema-Validierung + 1 Repair-Retry",
     "where": "[A] Agent-Wrapper", "onHit": "Repair-Prompt, sonst ESCALATED", "status": "aktiv",
     "plain": "Die KI muss in einem festen Formular antworten. Falsch ausgefüllt → einmal nachbessern, sonst Mensch."},
    {"id": "G-07", "layer": "L1 Decision", "type": "Ausgabe", "name": "Argument-Abgleich mit Extraktion (R-005)",
     "where": "[5] PDP", "onHit": "ESCALATED", "status": "aktiv",
     "plain": "Schreibt die KI einen anderen Betrag oder eine andere IBAN als im Dokument steht, wird gestoppt (Anti-Halluzination)."},
    {"id": "G-08", "layer": "L1 Decision", "type": "Werkzeug", "name": "Tool-Allowlist pro Agent-Version (R-001)",
     "where": "[5] PDP", "onHit": "REJECTED_POLICY", "status": "aktiv",
     "plain": "Die KI darf nur Werkzeuge vorschlagen, die für genau diese Version freigegeben sind."},
    {"id": "G-09", "layer": "L1 Decision", "type": "Werkzeug", "name": "Intent ↔ Aktion konsistent (R-004)",
     "where": "[5] PDP", "onHit": "ESCALATED", "status": "aktiv",
     "plain": "Sagt die KI „Rechnung buchen“, darf sie nicht gleichzeitig Stammdaten ändern."},
    {"id": "G-10", "layer": "L1 Decision", "type": "Aktion", "name": "Risk-Tier-Routing (R0–R3)",
     "where": "[5] PDP", "onHit": "Automatik / Freigabe / 4-Augen", "status": "aktiv",
     "plain": "Je riskanter, desto mehr Menschen müssen zustimmen."},
    {"id": "G-11", "layer": "L1 Human", "type": "Aktion", "name": "4-Augen + Out-of-Band + Teamlead-Pflicht (R3)",
     "where": "[6] Action Center", "onHit": "Freigabe verweigert, bis erfüllt", "status": "aktiv",
     "plain": "Bankdaten ändern nur zwei verschiedene Personen, eine davon Teamlead, nach Rückruf beim Lieferanten."},
    {"id": "G-12", "layer": "L4 Execution", "type": "Aktion", "name": "Approval-Hash (TOCTOU-Schutz)",
     "where": "[14] Robot", "onHit": "Nicht ausführen, ESCALATED", "status": "aktiv",
     "plain": "Ein digitaler Fingerabdruck stellt sicher: ausgeführt wird exakt das, was freigegeben wurde."},
    {"id": "G-13", "layer": "L4 Execution", "type": "Aktion", "name": "Read-back-Verifikation",
     "where": "[14] Robot", "onHit": "ESCALATED, Kompensation prüfen", "status": "aktiv",
     "plain": "Nach dem Buchen liest der Robot nach, ob wirklich alles stimmt."},
    {"id": "G-14", "layer": "Querschnitt", "type": "Betrieb", "name": "Notbremse (Kill-Switch K1–K3)",
     "where": "Automation Ops", "onHit": "Eingang, Agent oder Ausführung anhalten", "status": "aktiv",
     "plain": "Ein Knopf stoppt sofort die KI oder alle Buchungen, ohne dass Fälle verloren gehen."},
    {"id": "G-15", "layer": "Querschnitt", "type": "Betrieb", "name": "Change-Request mit 4-Augen + Regressionstest",
     "where": "Governance", "onHit": "Änderung bleibt wirkungslos bis genehmigt", "status": "aktiv",
     "plain": "Niemand kann Regeln allein ändern. Jede Änderung wird vorher automatisch gegen Testfälle geprüft."},
    {"id": "G-16", "layer": "L3 Governance", "type": "Betrieb", "name": "Prompt-I/O-Speicherung per Policy",
     "where": "Automation Ops / Trust Layer", "onHit": "Audit-Eintrag bleibt, Inhalt „durch Policy blockiert“", "status": "konfigurierbar", "setting": "storePromptIO",
     "plain": "Steuert, ob die Texte an die KI im Protokoll gespeichert werden."},
    {"id": "G-17", "layer": "L3 Governance", "type": "Betrieb", "name": "Token-Budget pro Case",
     "where": "[10] LLM Gateway", "onHit": "Alarm bei Überschreitung", "status": "Alarm",
     "plain": "Warnt, wenn ein Fall ungewöhnlich viel KI-Rechenzeit verbraucht (Kosten, Schleifen)."},
]

# ---------------------------------------------------------------------------
# 2.4 Regressions-/Kontrollfaelle (Golden Set) fuer Release-Gates
# ---------------------------------------------------------------------------
GOLDEN_SET = [
    {"scenario": "invoice-standard", "expect": {"decision": "AUTO_EXECUTE", "tier": "R1"}, "critical": False},
    {"scenario": "invoice-large", "expect": {"decision": "REQUIRE_APPROVAL", "tier": "R2"}, "critical": False},
    {"scenario": "complaint", "expect": {"decision": "REQUIRE_APPROVAL", "tier": "R2"}, "critical": False},
    {"scenario": "general", "expect": {"decision": "AUTO_EXECUTE", "tier": "R0"}, "critical": False},
    {"scenario": "unknown-po", "expect": {"decision": "REQUIRE_APPROVAL", "tier": "R2"}, "critical": False},
    {"scenario": "low-quality", "expect": {"state": "VALIDATION_PENDING"}, "critical": False},
    {"scenario": "bank-change", "expect": {"decision": "REQUIRE_4EYES", "tier": "R3"}, "critical": True},
    {"scenario": "bank-change-fraud", "expect": {"decision": "REQUIRE_4EYES", "tier": "R3", "rule": "R-032 SENDER_DOMAIN_MISMATCH"}, "critical": True},
    {"scenario": "injection", "expect": {"decision": "REJECT"}, "critical": True},
    {"scenario": "blocked-vendor", "expect": {"decision": "ESCALATE"}, "critical": True},
    {"scenario": "poison", "expect": {"status": "DEAD_LETTER"}, "critical": True},
]


def evaluate(config):
    """Spielt das Golden Set in einer isolierten In-Memory-Plattform mit der gegebenen Konfiguration durch."""
    from .platform import Platform  # lokal, um Zirkelimport zu vermeiden
    from .scenarios import scenario_mail

    sandbox = Platform(":memory:", seed_value=0)
    for k, v in config.items():
        sandbox.store.set_setting(k, copy.deepcopy(v))
    pol = sandbox.store.get_setting("policy")
    pol["sampling"] = {"R0": 0.0, "R1": 0.0}  # Stichproben verfaelschen die Pruefung
    sandbox.store.set_setting("policy", pol)
    sandbox.store.set_setting("chaos", {})
    sandbox.store.set_setting("killSwitch", {})
    sandbox.store.set_setting("robot", {"autoRun": False})

    results = []
    for g in GOLDEN_SET:
        r = sandbox.ingest(scenario_mail(g["scenario"]))
        actual = {"status": r["status"]}
        if r.get("caseId"):
            env = sandbox.store.get_case(r["caseId"])
            pol_d = env["envelope"].get("policy") or {}
            actual.update({"state": env["state"], "decision": pol_d.get("decision"), "tier": pol_d.get("riskTier"),
                           "rules": pol_d.get("rulesFired", [])})
        ok = True
        for key, val in g["expect"].items():
            if key == "rule":
                ok &= val in actual.get("rules", [])
            else:
                ok &= actual.get(key) == val
        results.append({"scenario": g["scenario"], "critical": g["critical"], "expect": g["expect"],
                        "actual": {k: v for k, v in actual.items() if k != "rules"}, "passed": bool(ok)})
    sandbox.store.conn.close()
    passed = sum(r["passed"] for r in results)
    critical_failed = [r["scenario"] for r in results if r["critical"] and not r["passed"]]
    return {
        "total": len(results),
        "passed": passed,
        "passRate": round(passed / len(results), 3),
        "criticalFailed": critical_failed,
        "gate": "BLOCKED" if critical_failed else ("REVIEW" if passed < len(results) else "PASSED"),
        "results": results,
    }


# ---------------------------------------------------------------------------
# Hilfsfunktionen fuer Change-Requests
# ---------------------------------------------------------------------------
def flat_diff(old, new, prefix=""):
    out = []
    keys = sorted(set((old or {}).keys()) | set((new or {}).keys())) if isinstance(old, dict) or isinstance(new, dict) else []
    if not keys:
        if old != new:
            out.append({"path": prefix, "old": old, "new": new})
        return out
    for k in keys:
        o = (old or {}).get(k) if isinstance(old, dict) else None
        n = (new or {}).get(k) if isinstance(new, dict) else None
        p = f"{prefix}.{k}" if prefix else k
        if isinstance(o, dict) or isinstance(n, dict):
            out.extend(flat_diff(o, n, p))
        elif isinstance(o, list) or isinstance(n, list):
            if sorted(map(json.dumps, o or [])) != sorted(map(json.dumps, n or [])):
                added = [x for x in (n or []) if x not in (o or [])]
                removed = [x for x in (o or []) if x not in (n or [])]
                out.append({"path": p, "old": o, "new": n, "added": added, "removed": removed})
        elif o != n:
            out.append({"path": p, "old": o, "new": n})
    return out


def bump_version(v):
    parts = str(v).split(".")
    if parts and parts[-1].isdigit():
        parts[-1] = str(int(parts[-1]) + 1)
        return ".".join(parts)
    return f"{v}.1"


GOVERNED_KEYS = ("policy", "guardrails", "automationOps")
KILL_LABELS = {"intake": "K1 Eingang", "agents": "K2 Agent (KI)", "execution": "K3 Ausführung (Buchungen)"}
LIFECYCLE = ["DRAFT", "EVALUATION", "APPROVED", "ACTIVE", "RETIRED"]
DEFAULT_ALERTING = {
    "escalationRate": 0.25,
    "rejectRate": 0.10,
    "maxTokensPerCase": 4000,
    "slaOverdue": 1,
}
