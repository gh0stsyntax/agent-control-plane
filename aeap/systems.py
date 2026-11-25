"""L5 Systems of Record (simuliert): ERP [16], CRM/ITSM [17], Mail-Outbox.

Alle schreibenden Operationen sind idempotent ueber einen Idempotency-Key.
Fault-Injection (Settings 'chaos') simuliert 5xx, Timeout-nach-Commit und Selektor-Drift.
"""

import json
import random

from .contracts import iban_valid, normalize_iban
from .store import now_iso


class BusinessRuleException(Exception):
    """Fachlicher Fehler: kein Retry (1.9)."""


class SystemException(Exception):
    """Technischer Fehler: Retry durch den jeweiligen Besitzer (1.9)."""

    def __init__(self, message, kind="5xx"):
        super().__init__(message)
        self.kind = kind


class SelectorNotFound(SystemException):
    def __init__(self, message):
        super().__init__(message, kind="selector")


SEED_VENDORS = [
    ("100234", "Mueller Industriebedarf GmbH", "DE89370400440532013000", "Mueller Industriebedarf GmbH", "ACTIVE", 1500.0,
     "rechnung@mueller-industrie.example", "+49 711 555 0100"),
    ("100417", "Nordlicht Logistik AG", "DE12500105170648489890", "Nordlicht Logistik AG", "ACTIVE", 9000.0,
     "billing@nordlicht-logistik.example", "+49 40 555 0417"),
    ("100555", "Schmidt & Partner KG", "DE02120300000000202051", "Schmidt & Partner KG", "ACTIVE", 800.0,
     "buchhaltung@schmidt-partner.example", "+49 89 555 0555"),
    ("100999", "Altlast Handels GmbH", "DE65760260001234567890", "Altlast Handels GmbH", "BLOCKED", 2000.0,
     "info@altlast-handel.example", "+49 30 555 0999"),
]
SEED_POS = [
    ("4500012345", "100234", 1240.00),
    ("4500012399", "100234", 3100.00),
    ("4500020001", "100417", 18750.00),
    ("4500030077", "100555", 640.00),
    ("4500040001", "100999", 900.00),
]


def seed(store):
    with store.transaction() as s:
        for v in SEED_VENDORS:
            s.execute(
                "INSERT OR IGNORE INTO erp_vendors(supplier_id, name, iban, account_holder, status, avg_invoice, email, phone) "
                "VALUES(?,?,?,?,?,?,?,?)", v)
        for po in SEED_POS:
            s.execute("INSERT OR IGNORE INTO erp_purchase_orders(po_number, supplier_id, amount) VALUES(?,?,?)", po)


class Systems:
    def __init__(self, store, rng=None):
        self.store = store
        self.rng = rng or random.Random()

    # ---- Fault injection -------------------------------------------------
    def _chaos(self):
        return self.store.get_setting("chaos", {})

    def _maybe_5xx(self, op):
        if self.rng.random() < float(self._chaos().get("erp5xxRate", 0)):
            raise SystemException(f"ERP antwortet 503 Service Unavailable ({op})", kind="5xx")

    def _maybe_timeout_after_commit(self, op):
        if self.rng.random() < float(self._chaos().get("erpTimeoutAfterCommitRate", 0)):
            raise SystemException(f"Timeout nach Commit ({op}): Antwort verloren", kind="timeout")

    def _maybe_selector_drift(self, op):
        if self.rng.random() < float(self._chaos().get("selectorDriftRate", 0)):
            raise SelectorNotFound(f"Selektor <wnd app='saplogon.exe' title='Lieferant aendern*'/> nicht gefunden ({op})")

    # ---- T-READ ------------------------------------------------------------
    def get_vendor(self, supplier_id):
        return self.store.one("SELECT * FROM erp_vendors WHERE supplier_id=?", (str(supplier_id),))

    def find_invoice(self, supplier_id, invoice_no):
        return self.store.one(
            "SELECT * FROM erp_documents WHERE doc_type='INVOICE' AND supplier_id=? AND reference=?",
            (str(supplier_id), str(invoice_no)))

    def get_purchase_order(self, po_number):
        return self.store.one("SELECT * FROM erp_purchase_orders WHERE po_number=?", (str(po_number),))

    # ---- T-WRITE -----------------------------------------------------------
    def _existing_doc(self, idem):
        return self.store.one("SELECT * FROM erp_documents WHERE idempotency_key=?", (idem,))

    def _vendor_active(self, supplier_id):
        v = self.get_vendor(supplier_id)
        if not v:
            raise BusinessRuleException(f"Lieferant {supplier_id} existiert nicht")
        if v["status"] != "ACTIVE":
            raise BusinessRuleException(f"Lieferant {supplier_id} ist gesperrt ({v['status']})")
        return v

    def post_invoice(self, idem, case_id, supplierId, invoiceNo, amount, currency="EUR", poNumber=None):
        self._maybe_5xx("post_invoice")
        existing = self._existing_doc(idem)
        if existing:
            return {"docNo": existing["doc_no"], "replayed": True}
        self._vendor_active(supplierId)
        if poNumber:
            po = self.get_purchase_order(poNumber)
            if not po:
                raise BusinessRuleException(f"Bestellung {poNumber} nicht gefunden")
            if po["supplier_id"] != str(supplierId):
                raise BusinessRuleException(f"Bestellung {poNumber} gehoert nicht zu Lieferant {supplierId}")
        if self.find_invoice(supplierId, invoiceNo):
            raise BusinessRuleException(f"Rechnung {invoiceNo} ist fuer Lieferant {supplierId} bereits gebucht")
        doc_no = "51" + str(self.store.next_counter("erp_doc")).zfill(8)
        self.store.execute(
            "INSERT INTO erp_documents(doc_no, doc_type, supplier_id, reference, amount, currency, po_number, idempotency_key, case_id, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (doc_no, "INVOICE", str(supplierId), str(invoiceNo), float(amount), currency, poNumber, idem, case_id, now_iso()))
        self._maybe_timeout_after_commit("post_invoice")
        return {"docNo": doc_no, "replayed": False}

    def create_credit_note(self, idem, case_id, supplierId, reference, amount, currency="EUR"):
        self._maybe_5xx("create_credit_note")
        existing = self._existing_doc(idem)
        if existing:
            return {"docNo": existing["doc_no"], "replayed": True}
        self._vendor_active(supplierId)
        if float(amount) <= 0:
            raise BusinessRuleException("Gutschriftsbetrag muss positiv sein")
        doc_no = "52" + str(self.store.next_counter("erp_doc")).zfill(8)
        self.store.execute(
            "INSERT INTO erp_documents(doc_no, doc_type, supplier_id, reference, amount, currency, po_number, idempotency_key, case_id, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (doc_no, "CREDIT_NOTE", str(supplierId), str(reference), -abs(float(amount)), currency, None, idem, case_id, now_iso()))
        self._maybe_timeout_after_commit("create_credit_note")
        return {"docNo": doc_no, "replayed": False}

    def update_vendor_bank(self, idem, case_id, supplierId, iban, accountHolder=None):
        """UI-Fallback (SAP GUI XK02): kein stabiles API laut Annahme A2."""
        self._maybe_selector_drift("update_vendor_bank")
        done = self.store.one("SELECT * FROM erp_change_log WHERE idempotency_key=?", (idem,))
        if done:
            return {"changeId": done["id"], "replayed": True}
        v = self._vendor_active(supplierId)
        new_iban = normalize_iban(iban)
        if not iban_valid(new_iban):
            raise BusinessRuleException(f"IBAN {new_iban} ist ungueltig (Pruefziffer)")
        with self.store.transaction() as s:
            s.execute("UPDATE erp_vendors SET iban=?, account_holder=COALESCE(?, account_holder) WHERE supplier_id=?",
                      (new_iban, accountHolder, str(supplierId)))
            cur = s.execute(
                "INSERT INTO erp_change_log(ts, supplier_id, field, old_value, new_value, channel, idempotency_key, case_id) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (now_iso(), str(supplierId), "IBAN", v["iban"], new_iban, "SAP GUI XK02", idem, case_id))
            change_id = cur.lastrowid
        self._maybe_timeout_after_commit("update_vendor_bank")
        return {"changeId": change_id, "replayed": False}

    def create_ticket(self, idem, case_id, tags, summary):
        self._maybe_5xx("create_ticket")
        existing = self.store.one("SELECT * FROM tickets WHERE idempotency_key=?", (idem,))
        if existing:
            return {"ticketId": existing["id"], "replayed": True}
        cur = self.store.execute(
            "INSERT INTO tickets(ts, case_id, tags, summary, idempotency_key) VALUES(?,?,?,?,?)",
            (now_iso(), case_id, json.dumps(tags, ensure_ascii=False), summary, idem))
        return {"ticketId": cur.lastrowid, "replayed": False}

    def send_mail(self, idem, case_id, recipient, subject, body):
        existing = self.store.one("SELECT * FROM outbox WHERE idempotency_key=?", (idem,))
        if existing:
            return {"mailId": existing["id"], "replayed": True}
        cur = self.store.execute(
            "INSERT INTO outbox(ts, case_id, channel, recipient, subject, body, idempotency_key) VALUES(?,?,?,?,?,?,?)",
            (now_iso(), case_id, "Integration Service: Outlook 365", recipient, subject, body, idem))
        return {"mailId": cur.lastrowid, "replayed": False}

    # ---- Read-back fuer S9 -------------------------------------------------
    def read_back(self, tool, args, result):
        if tool in ("erp.post_invoice", "erp.create_credit_note"):
            doc = self.store.one("SELECT * FROM erp_documents WHERE doc_no=?", (result.get("docNo"),))
            if not doc:
                return False, "Beleg nicht auffindbar"
            expected = float(args["amount"]) if tool == "erp.post_invoice" else -abs(float(args["amount"]))
            if abs(doc["amount"] - expected) > 0.005 or doc["supplier_id"] != str(args["supplierId"]):
                return False, f"Beleg {doc['doc_no']} weicht ab (Ist {doc['amount']}, Soll {expected})"
            return True, f"Beleg {doc['doc_no']} gelesen, Betrag {doc['amount']:.2f} stimmt"
        if tool == "erp.update_vendor_bank":
            v = self.get_vendor(args["supplierId"])
            if v and v["iban"] == normalize_iban(args["iban"]):
                return True, "Lieferantenstamm gelesen, IBAN stimmt"
            return False, "IBAN im Stamm weicht vom Soll ab"
        if tool == "crm.create_ticket":
            t = self.store.one("SELECT * FROM tickets WHERE id=?", (result.get("ticketId"),))
            return (True, f"Ticket #{t['id']} vorhanden") if t else (False, "Ticket fehlt")
        return True, "kein Read-back definiert"
