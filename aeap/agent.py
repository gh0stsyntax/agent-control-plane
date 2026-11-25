"""L2 Cognitive Layer: [A] Supplier-Triage-Agent (Agent Builder, simuliert).

Der Agent liefert ausschliesslich einen strukturierten Vorschlag (P1).
T-READ-Tools ruft er direkt auf (allowlist-geprueft), T-WRITE-Tools darf er nur beantragen.
Der Agent-Wrapper ist alleiniger Retry-Besitzer fuer LLM-Fehler (1.9).
"""

import json
import random
import time

from .contracts import TOOLS, validate_proposal
from .trust_layer import LLMRateLimited, LLMTimeout, ground

AGENTS = {
    "supplier-triage": {
        "version": "1.4.2",
        "kind": "[A] UiPath Agent Builder (autonomous)",
        "description": "Triage- und Case-Agent fuer Supplier Inbound Cases",
        "lifecycle": "ACTIVE",
        "owner": "Process Owner Kreditoren",
        "operator": "Automation CoE",
        "riskClass": "hoch (schlägt Finanz-Buchungen und Stammdatenänderungen vor)",
        "llm": "UiPath-managed via LLM Gateway (hier simuliert)",
        "approvedBy": "s.klein (Compliance)",
        "approvedAt": "2026-09-28",
    }
}

MAX_TRANSIENT_RETRIES = 2
MAX_REPAIR_RETRIES = 1


class AgentFailure(Exception):
    pass


class ToolNotPermitted(Exception):
    pass


class ToolRuntime:
    """Fuehrt nur T-READ aus und prueft die Allowlist der Agent-Version (P5)."""

    def __init__(self, systems, allowlist, store, case_id, correlation_id):
        self.systems = systems
        self.allowlist = set(allowlist)
        self.store = store
        self.case_id = case_id
        self.correlation_id = correlation_id

    def read(self, tool, **args):
        meta = TOOLS.get(tool)
        if not meta or meta["class"] != "T-READ":
            raise ToolNotPermitted(f"{tool} ist kein T-READ-Tool und darf vom Agent nicht direkt ausgefuehrt werden")
        if tool not in self.allowlist:
            raise ToolNotPermitted(f"{tool} ist fuer diese Agent-Version nicht freigegeben")
        fn = {
            "erp.get_vendor": lambda: self.systems.get_vendor(args["supplierId"]),
            "erp.find_invoice": lambda: self.systems.find_invoice(args["supplierId"], args["invoiceNo"]),
            "erp.get_purchase_order": lambda: self.systems.get_purchase_order(args["poNumber"]),
        }[tool]
        result = fn()
        self.store.add_event(self.case_id, self.correlation_id, "S4", "[A] Agent", "tool_read",
                             f"T-READ {tool}({', '.join(f'{k}={v}' for k, v in args.items())}) -> {'Treffer' if result else 'kein Treffer'}")
        return result


class SupplierTriageAgent:
    agent_id = "supplier-triage"

    def __init__(self, store, systems, gateway, rng=None):
        self.store = store
        self.systems = systems
        self.gateway = gateway
        self.rng = rng or random.Random()
        self.version = AGENTS[self.agent_id]["version"]

    def _backoff(self, attempt):
        # Exponentieller Backoff mit Jitter; in der Simulation stark verkuerzt
        delay = min(0.05, 0.005 * (2 ** attempt)) * (0.5 + self.rng.random())
        time.sleep(delay)
        return round(delay * 1000)

    def run(self, envelope, allowlist):
        case_id, corr = envelope["caseId"], envelope["correlationId"]
        runtime = ToolRuntime(self.systems, allowlist, self.store, case_id, corr)
        docs = envelope["documents"]
        primary = docs[0]["fields"] if docs else {}

        reads = {}
        sid = primary.get("supplierId")
        if sid:
            reads["erp.get_vendor"] = runtime.read("erp.get_vendor", supplierId=sid)
            if primary.get("invoiceNo"):
                reads["erp.find_invoice"] = runtime.read("erp.find_invoice", supplierId=sid, invoiceNo=primary["invoiceNo"])
        if primary.get("poNumber"):
            reads["erp.get_purchase_order"] = runtime.read("erp.get_purchase_order", poNumber=primary["poNumber"])

        prompt = {
            "agent": f"{self.agent_id}@{self.version}",
            "instructions": "Klassifiziere den Supplier Case und liefere einen JSON-Vorschlag gemaess Schema. "
                            "Fuehre keine Aktionen aus. Schreibende Tools nur als Vorschlag.",
            "case": {
                "caseId": case_id,
                "subject": envelope["source"].get("subject", ""),
                "body": envelope["source"].get("body", ""),
                "sender": envelope["source"].get("from", ""),
                "documents": docs,
            },
            "toolResults": reads,
            "grounding": ground([d["type"] for d in docs] or ["Other"]),
        }

        transient = 0
        repairs = 0
        attempts = 0
        repair = False
        guard = {"promptInjection": [], "harmful": [], "maskedEntities": {}}
        usage = {"promptTokens": 0, "completionTokens": 0}
        while True:
            attempts += 1
            try:
                res = self.gateway.call(case_id, corr, f"{self.agent_id}@{self.version}", prompt, repair=repair)
            except (LLMTimeout, LLMRateLimited) as e:
                if transient >= MAX_TRANSIENT_RETRIES:
                    raise AgentFailure(f"LLM nicht erreichbar nach {transient} Retries: {e}")
                transient += 1
                ms = self._backoff(transient)
                self.store.add_event(case_id, corr, "S4", "[A] Agent-Wrapper", "retry",
                                     f"{e} -> Retry {transient}/{MAX_TRANSIENT_RETRIES} nach {ms} ms (Backoff + Jitter)")
                continue
            for k in ("promptInjection", "harmful"):
                key = "injection" if k == "promptInjection" else "harmful"
                guard[k] = sorted(set(guard[k]) | set(res[key]))
            guard["maskedEntities"] = res["masked"]
            usage["promptTokens"] += res["usage"]["promptTokens"]
            usage["completionTokens"] += res["usage"]["completionTokens"]
            try:
                proposal = json.loads(res["raw"])
            except json.JSONDecodeError:
                proposal = None
            errors = validate_proposal(proposal) if proposal is not None else ["kein gueltiges JSON"]
            if not errors:
                proposal = res["unmask"](proposal)
                break
            if repairs >= MAX_REPAIR_RETRIES:
                raise AgentFailure("Schema-Fehler nach Repair-Retry: " + "; ".join(errors))
            repairs += 1
            repair = True
            self.store.add_event(case_id, corr, "S4", "[A] Agent-Wrapper", "schema_error",
                                 "Schema-Fehler -> Repair-Prompt: " + "; ".join(errors))

        return proposal, {
            "agentId": self.agent_id,
            "version": self.version,
            "kind": AGENTS[self.agent_id]["kind"],
            "attempts": attempts,
            "toolReads": list(reads.keys()),
            "guardrails": guard,
            "usage": usage,
        }
