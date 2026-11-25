"""[5] Policy Decision Point: deterministisch, versioniert, ausserhalb des Modells (P2, ADR-3).

Der PDP vertraut den Risiko-Aussagen des Agents nicht. Er berechnet seine Signale
selbst aus Systems of Record und DU-Ergebnis.
"""

from .contracts import TOOL_CONTRACTS, TOOLS, iban_valid, normalize_iban, sha256_hex, validate_tool_args

DEFAULT_POLICY = {
    "rulesVersion": "2026.10.1",
    "thresholds": {
        "duClassConfidence": 0.85,
        "duFieldConfidence": 0.80,
        "r1MinConfidence": 0.95,
        "invoiceAutoLimit": 5000.0,
        "anomalyFactor": 3.0,
    },
    "sampling": {"R0": 0.01, "R1": 0.05},
    "slaHours": {"R1": 4, "R2": 8, "R3": 24},
    "allowlist": {
        "supplier-triage@1.4.2": [
            "erp.get_vendor", "erp.find_invoice", "erp.get_purchase_order",
            "erp.post_invoice", "erp.create_credit_note", "erp.update_vendor_bank", "crm.create_ticket",
        ]
    },
}

INTENT_TOOLS = {
    "BOOK_INVOICE": {"erp.post_invoice"},
    "CREATE_CREDIT_NOTE": {"erp.create_credit_note"},
    "UPDATE_BANK_DETAILS": {"erp.update_vendor_bank"},
    "ACKNOWLEDGE": {"crm.create_ticket"},
}


def sample_hit(case_id, rate):
    """Deterministische, reproduzierbare Stichprobe."""
    return int(sha256_hex("sample|" + case_id)[:8], 16) / 0xFFFFFFFF < rate


def eur(v):
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " EUR"


def _domain(addr):
    return addr.rsplit("@", 1)[-1].lower().strip(">") if addr and "@" in addr else None


class PolicyDecisionPoint:
    def __init__(self, store, systems):
        self.store = store
        self.systems = systems

    def policy(self):
        return self.store.get_setting("policy", DEFAULT_POLICY)

    def signals(self, envelope):
        """Deterministische Kontext-Signale (unabhaengig vom Agent)."""
        docs = envelope["documents"]
        f = docs[0]["fields"] if docs else {}
        sig = {}
        sid = f.get("supplierId")
        vendor = self.systems.get_vendor(sid) if sid else None
        sig["vendorExists"] = bool(vendor)
        sig["vendorStatus"] = vendor["status"] if vendor else None
        sender = envelope["source"].get("from", "")
        sig["senderDomain"] = _domain(sender)
        sig["vendorDomain"] = _domain(vendor["email"]) if vendor else None
        sig["senderDomainMatch"] = bool(vendor) and sig["senderDomain"] == sig["vendorDomain"]
        if f.get("invoiceNo") and sid:
            sig["duplicateInvoice"] = bool(self.systems.find_invoice(sid, f["invoiceNo"]))
        if f.get("poNumber"):
            po = self.systems.get_purchase_order(f["poNumber"])
            sig["poExists"] = bool(po)
            sig["poMatchesVendor"] = bool(po) and po["supplier_id"] == str(sid)
        if f.get("amount") is not None and vendor and vendor.get("avg_invoice"):
            sig["amountVsAvg"] = round(float(f["amount"]) / vendor["avg_invoice"], 2)
        if f.get("iban"):
            sig["ibanValid"] = iban_valid(f["iban"])
            sig["ibanChanged"] = bool(vendor) and normalize_iban(f["iban"]) != vendor["iban"]
        confs = [d["classConfidence"] for d in docs] + [c for d in docs for c in d["fieldConfidence"].values()]
        sig["minDuConfidence"] = min(confs) if confs else 0
        sig["humanValidated"] = any(d.get("validatedBy") for d in docs)
        return sig

    def decide(self, envelope):
        pol = self.policy()
        th = pol["thresholds"]
        agent = envelope["agent"]
        proposal = agent["proposal"]
        intent = proposal["intent"]
        actions = proposal.get("actions", [])
        key = f"{agent['agentId']}@{agent['version']}"
        allow = set(pol["allowlist"].get(key, []))
        sig = self.signals(envelope)
        fired, reasons = [], []

        def result(tier, decision, extra=None):
            out = {
                "riskTier": tier,
                "decision": decision,
                "rulesVersion": pol["rulesVersion"],
                "rulesFired": fired,
                "reasons": reasons,
                "signals": sig,
                "sampled": False,
            }
            out.update(extra or {})
            return out

        # R-001 Allowlist (P5): jedes Tool muss fuer genau diese Agent-Version freigegeben sein
        denied = [a["tool"] for a in actions if a["tool"] not in allow]
        if denied:
            fired.append("R-001 TOOL_ALLOWLIST")
            reasons.append(f"Nicht freigegebene Tools fuer {key}: {', '.join(denied)}")
            return result("R3", "REJECT")

        # R-002 Nur T-WRITE als Aktion (T-READ hat keinen Seiteneffekt und gehoert nicht in den Vorschlag)
        bad = [a["tool"] for a in actions if TOOLS.get(a["tool"], {}).get("class") != "T-WRITE"]
        if bad:
            fired.append("R-002 ACTION_CLASS")
            reasons.append(f"Aktion ohne T-WRITE-Klasse: {', '.join(bad)}")
            return result("R3", "ESCALATE")

        # R-007 Tool-Vertrag (3.2): Argumente muessen dem Eingabe-Schema des Werkzeugs entsprechen
        for a in actions:
            violations = validate_tool_args(a["tool"], a["args"])
            if violations:
                fired.append("R-007 TOOL_CONTRACT")
                reasons.append(f"{a['tool']} verletzt Tool-Vertrag v{TOOL_CONTRACTS[a['tool']]['version']}: " + ", ".join(violations))
                return result("R2", "ESCALATE")

        # R-003 Guardrails
        g = agent.get("guardrails", {})
        if g.get("promptInjection") or g.get("harmful"):
            fired.append("R-003 GUARDRAIL_HIT")
            reasons.append("Guardrail-Treffer: " + ", ".join(g.get("promptInjection", []) + g.get("harmful", [])))
            return result("R3", "ESCALATE")

        # R-004 Intent/Aktion-Konsistenz
        expected = INTENT_TOOLS.get(intent)
        if not expected or {a["tool"] for a in actions} != expected:
            fired.append("R-004 INTENT_ACTION_MISMATCH")
            reasons.append(f"Intent {intent} passt nicht zu Aktionen {[a['tool'] for a in actions]}")
            return result("R3", "ESCALATE")

        # R-005 Argumente muessen den extrahierten (ggf. validierten) Feldern entsprechen (Anti-Halluzination)
        fields = envelope["documents"][0]["fields"] if envelope["documents"] else {}
        for a in actions:
            for k, v in a["args"].items():
                if k in ("supplierId", "amount", "iban", "invoiceNo", "reference") and k in fields:
                    fv = fields[k]
                    same = (normalize_iban(str(v)) == normalize_iban(str(fv))) if k == "iban" else (str(v) == str(fv))
                    if not same:
                        fired.append("R-005 ARGS_MATCH_EXTRACTION")
                        reasons.append(f"Argument {k}={v!r} weicht vom extrahierten Wert {fv!r} ab")
                        return result("R3", "ESCALATE")

        # R-006 Stammdaten
        if intent != "ACKNOWLEDGE":
            if not sig["vendorExists"]:
                fired.append("R-006 VENDOR_UNKNOWN")
                reasons.append("Lieferant im ERP unbekannt")
                return result("R2", "ESCALATE")
            if sig["vendorStatus"] != "ACTIVE":
                fired.append("R-006 VENDOR_BLOCKED")
                reasons.append(f"Lieferant ist {sig['vendorStatus']}")
                return result("R2", "ESCALATE")

        conf = min(float(proposal.get("confidence", 0)), 1.0 if sig["humanValidated"] else sig["minDuConfidence"])

        if intent == "ACKNOWLEDGE":
            fired.append("R-010 TIER_R0")
            reasons.append("Eingangsbestaetigung / Ticket-Tagging")
            if sample_hit(envelope["idempotencyKey"], pol["sampling"]["R0"]):
                fired.append("R-090 SAMPLING_R0")
                reasons.append(f"Stichprobe {pol['sampling']['R0']:.0%} getroffen")
                return result("R0", "REQUIRE_APPROVAL", {"sampled": True})
            return result("R0", "AUTO_EXECUTE")

        if intent == "UPDATE_BANK_DETAILS":
            if not sig.get("ibanValid"):
                fired.append("R-031 IBAN_INVALID")
                reasons.append("IBAN-Pruefziffer ungueltig")
                return result("R3", "ESCALATE")
            fired.append("R-030 TIER_R3_BANK_CHANGE")
            reasons.append("Bankdatenaenderung: 4-Augen plus Out-of-Band-Verifikation (SOP-KRED-07)")
            if not sig["senderDomainMatch"]:
                fired.append("R-032 SENDER_DOMAIN_MISMATCH")
                reasons.append(f"Absender-Domain {sig['senderDomain']} weicht vom Stamm ({sig['vendorDomain']}) ab: Betrugsindikator")
            return result("R3", "REQUIRE_4EYES", {"requireOob": True})

        if intent == "CREATE_CREDIT_NOTE":
            fired.append("R-020 TIER_R2_CREDIT_NOTE")
            reasons.append("Gutschrift: 1x Approval")
            return result("R2", "REQUIRE_APPROVAL")

        # BOOK_INVOICE
        if sig.get("duplicateInvoice"):
            fired.append("R-011 DUPLICATE_INVOICE")
            reasons.append("Rechnungsnummer bereits gebucht")
            return result("R2", "ESCALATE")
        amount = float(fields.get("amount") or 0)
        r2 = []
        if amount > th["invoiceAutoLimit"]:
            r2.append(f"Betrag {eur(amount)} > Grenze {eur(th['invoiceAutoLimit'])}")
        if conf < th["r1MinConfidence"]:
            r2.append(f"Konfidenz {conf} < {th['r1MinConfidence']}")
        if not fields.get("poNumber"):
            r2.append("keine Bestellnummer")
        elif not sig.get("poExists"):
            r2.append(f"Bestellung {fields['poNumber']} nicht im ERP gefunden")
        elif not sig.get("poMatchesVendor"):
            r2.append("Bestellung gehoert zu anderem Lieferanten")
        if sig.get("amountVsAvg") and sig["amountVsAvg"] > th["anomalyFactor"]:
            r2.append(f"Anomalie: {sig['amountVsAvg']}x Durchschnitt des Lieferanten")
        if r2:
            fired.append("R-021 TIER_R2_INVOICE")
            reasons.extend(r2)
            return result("R2", "REQUIRE_APPROVAL")
        fired.append("R-012 TIER_R1_INVOICE")
        reasons.append("Standardrechnung unter Grenze, Konfidenz hoch, Bestellung passt")
        if sample_hit(envelope["idempotencyKey"], pol["sampling"]["R1"]):
            fired.append("R-091 SAMPLING_R1")
            reasons.append(f"Stichprobe {pol['sampling']['R1']:.0%} getroffen")
            return result("R1", "REQUIRE_APPROVAL", {"sampled": True})
        return result("R1", "AUTO_EXECUTE")
