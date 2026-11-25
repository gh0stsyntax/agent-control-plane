"""L3 Governance: [10] AI Trust Layer / LLM Gateway, [11] Guardrails + PII, [12] Context Grounding.

Einziger LLM-Egress (P3). Jeder Call wird pseudonymisiert, auf Prompt-Injection
geprueft, auditiert und nach der Antwort de-pseudonymisiert.
"""

import json
import random
import re

from .contracts import IBAN_RE

# ---------------------------------------------------------------------------
# [11] Guardrails
# ---------------------------------------------------------------------------
INJECTION_PATTERNS = [
    r"ignor\w*\s+(?:alle\s+|all\s+)?(?:vorherigen|bisherigen|previous|prior)\s+(?:anweisungen|instructions|regeln|rules)",
    r"vergiss\s+(?:alle\s+)?(?:anweisungen|regeln)",
    r"system\s*prompt",
    r"you\s+are\s+now",
    r"du\s+bist\s+(?:jetzt|ab\s+sofort)",
    r"(?:ohne|keine)\s+(?:pruefung|prüfung|freigabe|genehmigung)",
    r"approve\s+immediately|sofort\s+(?:freigeben|genehmigen)",
    r"\b(?:payments\.release_payment|erp\.delete_vendor)\b",
]
HARMFUL_PATTERNS = [r"\b(?:bombe|anschlag|kill\s+yourself)\b"]

EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
PHONE_RE = re.compile(r"(?<!\w)\+\d{2}[\d\s/-]{6,}\d")


def scan_injection(text):
    hits = []
    for p in INJECTION_PATTERNS:
        m = re.search(p, text or "", re.IGNORECASE)
        if m:
            hits.append(m.group(0))
    return hits


def scan_harmful(text):
    return [m.group(0) for p in HARMFUL_PATTERNS for m in [re.search(p, text or "", re.IGNORECASE)] if m]


class Pseudonymizer:
    """In-Flight-PII-Maskierung mit reversibler Token-Map (nur im Gateway gehalten)."""

    def __init__(self):
        self.forward = {}
        self.reverse = {}
        self.counts = {}

    def _token(self, kind, value):
        key = (kind, value)
        if key not in self.forward:
            self.counts[kind] = self.counts.get(kind, 0) + 1
            tok = f"<{kind}_{self.counts[kind]}>"
            self.forward[key] = tok
            self.reverse[tok] = value
        return self.forward[key]

    def mask_text(self, text):
        if not isinstance(text, str):
            return text
        text = IBAN_RE.sub(lambda m: self._token("IBAN", re.sub(r"\s", "", m.group(1))), text)
        text = EMAIL_RE.sub(lambda m: self._token("EMAIL", m.group(0)), text)
        text = PHONE_RE.sub(lambda m: self._token("PHONE", m.group(0)), text)
        return text

    def mask(self, obj):
        if isinstance(obj, dict):
            return {k: self.mask(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.mask(v) for v in obj]
        return self.mask_text(obj)

    def unmask(self, obj):
        if isinstance(obj, dict):
            return {k: self.unmask(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.unmask(v) for v in obj]
        if isinstance(obj, str):
            for tok, val in self.reverse.items():
                obj = obj.replace(tok, val)
        return obj

    def summary(self):
        return dict(self.counts)


# ---------------------------------------------------------------------------
# [12] Context Grounding (RAG-Indexe, hier als statische SOP-Snippets)
# ---------------------------------------------------------------------------
GROUNDING_INDEX = [
    {"id": "SOP-KRED-02 §3", "index": "SOPs", "types": ["Invoice"],
     "text": "Eingangsrechnungen mit gueltiger Bestellnummer und aktivem Lieferanten werden gegen die Bestellung gebucht. Dubletten (gleiche Rechnungsnummer je Lieferant) sind abzulehnen."},
    {"id": "SOP-KRED-04 §1", "index": "SOPs", "types": ["Complaint"],
     "text": "Reklamationen mit beziffertem Gutschriftsbetrag werden als Gutschrift mit Bezug auf die Ursprungsrechnung angelegt. Freigabe durch Sachbearbeitung."},
    {"id": "SOP-KRED-07 §2", "index": "SOPs", "types": ["BankDetailChange"],
     "text": "Aenderungen der Bankverbindung nur nach Rueckruf unter der im Stamm hinterlegten Telefonnummer (Out-of-Band) und 4-Augen-Freigabe. Niemals Kontaktdaten aus der Anfrage-Mail verwenden."},
    {"id": "POL-FIN-01 §5", "index": "Policies", "types": ["Invoice", "Complaint", "BankDetailChange"],
     "text": "Agenten duerfen keine Zahlungen freigeben und keine Stammdaten loeschen."},
    {"id": "SOP-SVC-01 §1", "index": "SOPs", "types": ["Other"],
     "text": "Allgemeine Anfragen erhalten eine Eingangsbestaetigung und ein Ticket mit passenden Tags."},
]


def ground(doc_types):
    return [g for g in GROUNDING_INDEX if any(t in g["types"] for t in doc_types)]


# ---------------------------------------------------------------------------
# LLM-Provider (simuliert). Austauschpunkt fuer UiPath-managed oder BYO-LLM.
# ---------------------------------------------------------------------------
class LLMTimeout(Exception):
    pass


class LLMRateLimited(Exception):
    pass


class MockLLM:
    """Deterministisches 'Modell' fuer den Supplier-Triage-Agent.

    Es ist absichtlich anfaellig fuer Prompt-Injection: Nennt die Mail einen Toolnamen,
    uebernimmt das Modell ihn als Aktion. So laesst sich zeigen, dass Guardrails und
    PDP (P2) den Schaden verhindern, nicht der Prompt.
    """

    name = "mock-llm (simuliert, via LLM Gateway)"

    def __init__(self, store, rng=None):
        self.store = store
        self.rng = rng or random.Random()

    def complete(self, prompt, repair=False):
        chaos = self.store.get_setting("chaos", {})
        r = self.rng.random()
        if r < float(chaos.get("llmTimeoutRate", 0)):
            raise LLMTimeout("LLM-Timeout nach 30 s")
        if r < float(chaos.get("llmTimeoutRate", 0)) + float(chaos.get("llm429Rate", 0)):
            raise LLMRateLimited("HTTP 429 Too Many Requests")
        proposal = self._reason(prompt)
        if not repair and self.rng.random() < float(chaos.get("llmSchemaErrorRate", 0)):
            broken = dict(proposal)
            broken.pop("confidence", None)
            broken["intent"] = broken["intent"].lower()
            return json.dumps(broken, ensure_ascii=False)
        return json.dumps(proposal, ensure_ascii=False)

    def _reason(self, prompt):
        case = prompt["case"]
        reads = prompt["toolResults"]
        docs = case["documents"]
        primary = docs[0] if docs else {"type": "Other", "fields": {}, "classConfidence": 0.5, "fieldConfidence": {}}
        f = primary["fields"]
        confs = [primary["classConfidence"]] + list(primary["fieldConfidence"].values())
        confidence = round(min(confs) if confs else 0.5, 2)
        citations = [g["id"] for g in prompt["grounding"]]
        risks = []
        vendor = reads.get("erp.get_vendor")
        if primary["type"] != "Other":
            if not vendor:
                risks.append("VENDOR_NOT_FOUND")
            elif vendor.get("status") != "ACTIVE":
                risks.append("VENDOR_BLOCKED")

        t = primary["type"]
        if t == "Invoice":
            if reads.get("erp.find_invoice"):
                risks.append("DUPLICATE_SUSPECT")
            if f.get("poNumber") and not reads.get("erp.get_purchase_order"):
                risks.append("PO_NOT_FOUND")
            proposal = {
                "intent": "BOOK_INVOICE",
                "summary": f"Rechnung {f.get('invoiceNo')} ueber {f.get('amount')} {f.get('currency', 'EUR')} von Lieferant {f.get('supplierId')} buchen.",
                "actions": [{"tool": "erp.post_invoice", "args": {
                    "supplierId": f.get("supplierId"), "invoiceNo": f.get("invoiceNo"), "amount": f.get("amount"),
                    "currency": f.get("currency", "EUR"), "poNumber": f.get("poNumber")}}],
                "replyDraft": "Vielen Dank, Ihre Rechnung ist eingegangen und wird verarbeitet.",
            }
        elif t == "Complaint":
            proposal = {
                "intent": "CREATE_CREDIT_NOTE",
                "summary": f"Reklamation zu {f.get('reference')}: Gutschrift ueber {f.get('amount')} EUR fuer Lieferant {f.get('supplierId')}.",
                "actions": [{"tool": "erp.create_credit_note", "args": {
                    "supplierId": f.get("supplierId"), "reference": f.get("reference"), "amount": f.get("amount"),
                    "currency": f.get("currency", "EUR")}}],
                "replyDraft": "Ihre Reklamation ist eingegangen. Die Gutschrift wird geprueft.",
            }
        elif t == "BankDetailChange":
            proposal = {
                "intent": "UPDATE_BANK_DETAILS",
                "summary": f"Lieferant {f.get('supplierId')} beantragt neue Bankverbindung {f.get('iban')}. Laut SOP-KRED-07 Rueckruf und 4-Augen erforderlich.",
                "actions": [{"tool": "erp.update_vendor_bank", "args": {
                    "supplierId": f.get("supplierId"), "iban": f.get("iban"), "accountHolder": f.get("accountHolder")}}],
                "replyDraft": "Ihre Anfrage zur Aenderung der Bankverbindung ist eingegangen. Wir melden uns telefonisch.",
            }
        else:
            proposal = {
                "intent": "ACKNOWLEDGE",
                "summary": "Allgemeine Anfrage ohne buchungsrelevanten Inhalt. Ticket anlegen und Eingang bestaetigen.",
                "actions": [{"tool": "crm.create_ticket", "args": {
                    "tags": ["supplier-inbound", "general-inquiry"], "summary": case["subject"][:120]}}],
                "replyDraft": "Vielen Dank fuer Ihre Nachricht. Wir haben ein Ticket angelegt.",
            }

        # Simulierte Kompromittierung durch Prompt-Injection
        full_text = " ".join(d.get("_text", "") for d in docs) + " " + case.get("body", "")
        if re.search(r"payments\.release_payment", full_text):
            proposal["actions"].append({"tool": "payments.release_payment", "args": {
                "supplierId": f.get("supplierId"), "amount": f.get("amount"), "urgent": True}})
        if re.search(r"erp\.delete_vendor", full_text):
            proposal["actions"].append({"tool": "erp.delete_vendor", "args": {"supplierId": f.get("supplierId")}})

        proposal["confidence"] = confidence
        proposal["risks"] = risks
        proposal["citations"] = citations
        return proposal


class LLMGateway:
    """[10] Einziger Egress. Maskiert, prueft, auditiert, de-maskiert."""

    def __init__(self, store, provider):
        self.store = store
        self.provider = provider

    def call(self, case_id, correlation_id, agent_id, prompt, repair=False):
        guard_cfg = self.store.get_setting("guardrails", {})
        pseudo = Pseudonymizer()
        masked = pseudo.mask(prompt) if guard_cfg.get("piiMasking", True) else prompt
        # Nur nicht vertrauenswuerdiger Inhalt (Mail, Dokumente) wird gescannt, nicht Grounding/SOPs
        text_blob = json.dumps(prompt["case"], ensure_ascii=False)
        injection = scan_injection(text_blob) if guard_cfg.get("promptInjection", True) else []
        harmful = scan_harmful(text_blob) if guard_cfg.get("harmfulContent", True) else []

        store_io = self.store.get_setting("automationOps", {}).get("storePromptIO", True)
        raw = self.provider.complete(masked, repair=repair)
        prompt_tokens = len(json.dumps(masked, ensure_ascii=False)) // 4
        completion_tokens = len(raw) // 4
        self.store.add_event(case_id, correlation_id, "S4", "[10] AI Trust Layer", "llm_call",
                             f"LLM-Call {agent_id} via Gateway ({self.provider.name})" + (" [Repair-Prompt]" if repair else ""),
                             {
                                 "model": self.provider.name,
                                 "promptTokens": prompt_tokens,
                                 "completionTokens": completion_tokens,
                                 "maskedEntities": pseudo.summary(),
                                 "guardrails": {"promptInjection": injection, "harmful": harmful},
                                 "input": masked["case"] if store_io else "[Inhalt durch Policy blockiert]",
                                 "output": raw if store_io else "[Inhalt durch Policy blockiert]",
                             })
        return {
            "raw": raw,
            "unmask": pseudo.unmask,
            "masked": pseudo.summary(),
            "injection": injection,
            "harmful": harmful,
            "usage": {"promptTokens": prompt_tokens, "completionTokens": completion_tokens},
        }
