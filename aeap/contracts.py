"""Datenvertraege: Case Envelope (1.8), State Machine (1.6), Tool-Klassen (P1), Hashing (1.5)."""

import hashlib
import json
import re

# ---------------------------------------------------------------------------
# 1.6 Case-Lifecycle
# ---------------------------------------------------------------------------
TRANSITIONS = {
    "RECEIVED": {"DIGITIZED", "ESCALATED"},
    "DIGITIZED": {"CLASSIFIED", "ESCALATED"},
    "CLASSIFIED": {"EXTRACTED", "ESCALATED"},
    "EXTRACTED": {"VALIDATION_PENDING", "REASONED", "ESCALATED"},
    "VALIDATION_PENDING": {"VALIDATED"},
    "VALIDATED": {"REASONED", "ESCALATED"},
    "REASONED": {"DECIDED"},
    "DECIDED": {"AUTO_APPROVED", "APPROVAL_PENDING", "REJECTED_POLICY", "ESCALATED"},
    "APPROVAL_PENDING": {"APPROVED", "DENIED"},
    "DENIED": {"CLOSED_DENIED"},
    "AUTO_APPROVED": {"EXECUTING"},
    "APPROVED": {"EXECUTING"},
    "EXECUTING": {"VERIFIED", "FAILED_BUSINESS", "FAILED_SYSTEM", "ESCALATED"},
    "FAILED_BUSINESS": {"ESCALATED"},
    "FAILED_SYSTEM": {"ESCALATED"},
    "VERIFIED": {"CLOSED"},
}
TERMINAL_STATES = {"CLOSED", "CLOSED_DENIED", "REJECTED_POLICY", "ESCALATED"}
WAIT_STATES = {"VALIDATION_PENDING", "APPROVAL_PENDING"}
ALL_STATES = sorted(set(TRANSITIONS) | {s for v in TRANSITIONS.values() for s in v})


class IllegalTransition(Exception):
    pass


def check_transition(current, target):
    if target not in TRANSITIONS.get(current, set()):
        raise IllegalTransition(f"{current} -> {target} ist laut 1.6 nicht erlaubt")


# ---------------------------------------------------------------------------
# P1 Tool-Klassen: T-READ (direkt), T-WRITE (Deferred Execution ueber Queue)
# ---------------------------------------------------------------------------
TOOLS = {
    "erp.get_vendor": {"class": "T-READ", "system": "[16] ERP", "desc": "Lieferantenstamm lesen"},
    "erp.find_invoice": {"class": "T-READ", "system": "[16] ERP", "desc": "Dublettenpruefung Rechnung"},
    "erp.get_purchase_order": {"class": "T-READ", "system": "[16] ERP", "desc": "Bestellung lesen"},
    "erp.post_invoice": {"class": "T-WRITE", "system": "[16] ERP", "channel": "API", "desc": "Rechnung buchen (OData)"},
    "erp.create_credit_note": {"class": "T-WRITE", "system": "[16] ERP", "channel": "API", "desc": "Gutschrift anlegen (BAPI)"},
    "erp.update_vendor_bank": {"class": "T-WRITE", "system": "[16] ERP", "channel": "UI", "desc": "Bankverbindung aendern (SAP GUI XK02, UI-Fallback)"},
    "crm.create_ticket": {"class": "T-WRITE", "system": "[17] CRM/ITSM", "channel": "API", "desc": "Ticket anlegen und taggen"},
    "payments.release_payment": {"class": "T-WRITE", "system": "[16] ERP", "channel": "API", "desc": "Zahlung freigeben (nie fuer Agents freigegeben)"},
    "erp.delete_vendor": {"class": "T-WRITE", "system": "[16] ERP", "channel": "API", "desc": "Lieferant loeschen (nie fuer Agents freigegeben)"},
}

# ---------------------------------------------------------------------------
# Abschnitt 3.2: Tool-Vertraege (Eingabe-Schema, Idempotenz, Timeout, Version)
# Typen: str, num, list, bool. "?" am Ende = optional/nullable. Unbekannte Felder sind verboten.
# ---------------------------------------------------------------------------
TOOL_CONTRACTS = {
    "erp.get_vendor": {"version": "1.0", "args": {"supplierId": "str"}, "idempotent": True, "timeoutS": 10,
                       "returns": "Lieferantenstamm oder null"},
    "erp.find_invoice": {"version": "1.0", "args": {"supplierId": "str", "invoiceNo": "str"}, "idempotent": True, "timeoutS": 10,
                         "returns": "Beleg oder null"},
    "erp.get_purchase_order": {"version": "1.0", "args": {"poNumber": "str"}, "idempotent": True, "timeoutS": 10,
                               "returns": "Bestellung oder null"},
    "erp.post_invoice": {"version": "1.1", "args": {"supplierId": "str", "invoiceNo": "str", "amount": "num", "currency": "str",
                                                     "poNumber": "str?"}, "idempotent": True, "timeoutS": 30,
                         "returns": "docNo", "errors": ["Lieferant gesperrt", "Bestellung fehlt", "Dublette"]},
    "erp.create_credit_note": {"version": "1.0", "args": {"supplierId": "str", "reference": "str", "amount": "num", "currency": "str?"},
                               "idempotent": True, "timeoutS": 30, "returns": "docNo", "errors": ["Lieferant gesperrt", "Betrag <= 0"]},
    "erp.update_vendor_bank": {"version": "1.0", "args": {"supplierId": "str", "iban": "str", "accountHolder": "str?"},
                               "idempotent": True, "timeoutS": 120, "returns": "changeId", "errors": ["IBAN ungueltig", "Lieferant gesperrt"]},
    "crm.create_ticket": {"version": "1.0", "args": {"tags": "list", "summary": "str"}, "idempotent": True, "timeoutS": 20,
                          "returns": "ticketId"},
    "payments.release_payment": {"version": "1.0", "args": {"supplierId": "str", "amount": "num", "urgent": "bool?"},
                                 "idempotent": True, "timeoutS": 30, "returns": "paymentId"},
    "erp.delete_vendor": {"version": "1.0", "args": {"supplierId": "str"}, "idempotent": True, "timeoutS": 30, "returns": "-"},
}
_TYPES = {"str": str, "num": (int, float), "list": list, "bool": bool}


def validate_tool_args(tool, args):
    """Prueft Argumente gegen den Tool-Vertrag. Gibt eine Liste von Verstoessen zurueck (leer = gueltig)."""
    contract = TOOL_CONTRACTS.get(tool)
    if not contract:
        return [f"kein Vertrag fuer {tool}"]
    if not isinstance(args, dict):
        return ["args ist kein Objekt"]
    errors = []
    for name, typ in contract["args"].items():
        optional = typ.endswith("?")
        py = _TYPES[typ.rstrip("?")]
        val = args.get(name)
        if val is None or val == "":
            if not optional:
                errors.append(f"{name} fehlt")
            continue
        if isinstance(val, bool) and typ.rstrip("?") == "num":
            errors.append(f"{name} muss {typ.rstrip('?')} sein")
        elif not isinstance(val, py):
            errors.append(f"{name} muss {typ.rstrip('?')} sein")
    for extra in set(args) - set(contract["args"]):
        errors.append(f"unbekanntes Feld {extra}")
    return errors


# ---------------------------------------------------------------------------
# Hashing / Idempotenz
# ---------------------------------------------------------------------------
def sha256_hex(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj):
    """Kanonisierung: sortierte Keys, keine Whitespaces, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def idempotency_key(message_id, attachments):
    att_hash = sha256_hex("".join(sha256_hex(a.get("content", "")) for a in attachments))
    return sha256_hex(f"{message_id}|{att_hash}")


def approval_hash(case_id, idem_key, actions):
    """1.5 Approval-Hash (TOCTOU-Schutz) ueber die kanonisierte geplante Aktion."""
    planned = {"caseId": case_id, "idempotencyKey": idem_key, "actions": actions}
    return "sha256:" + sha256_hex(canonical_json(planned))


# ---------------------------------------------------------------------------
# IBAN
# ---------------------------------------------------------------------------
IBAN_RE = re.compile(r"\b([A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,3})?)\b")


def normalize_iban(value):
    return re.sub(r"\s+", "", value or "").upper()


def iban_valid(value):
    iban = normalize_iban(value)
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", iban):
        return False
    rearranged = iban[4:] + iban[:4]
    digits = "".join(str(int(c, 36)) for c in rearranged)
    return int(digits) % 97 == 1


def mask_iban(value):
    iban = normalize_iban(value)
    return iban[:4] + "*" * max(0, len(iban) - 8) + iban[-4:] if len(iban) > 8 else iban


# ---------------------------------------------------------------------------
# Agent-Output-Schema (S4)
# ---------------------------------------------------------------------------
INTENTS = {"BOOK_INVOICE", "CREATE_CREDIT_NOTE", "UPDATE_BANK_DETAILS", "ACKNOWLEDGE", "UNKNOWN"}


def validate_proposal(p):
    """Gibt eine Liste von Schema-Fehlern zurueck (leer = gueltig)."""
    errors = []
    if not isinstance(p, dict):
        return ["Vorschlag ist kein JSON-Objekt"]
    if p.get("intent") not in INTENTS:
        errors.append(f"intent ungueltig: {p.get('intent')!r}")
    conf = p.get("confidence")
    if not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
        errors.append("confidence fehlt oder liegt nicht in [0,1]")
    if not isinstance(p.get("summary"), str) or not p.get("summary"):
        errors.append("summary fehlt")
    actions = p.get("actions")
    if not isinstance(actions, list):
        errors.append("actions muss eine Liste sein")
    else:
        for i, a in enumerate(actions):
            if not isinstance(a, dict) or not isinstance(a.get("tool"), str) or not isinstance(a.get("args"), dict):
                errors.append(f"actions[{i}] braucht tool (str) und args (object)")
    return errors
