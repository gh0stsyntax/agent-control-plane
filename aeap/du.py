"""[15] Document Understanding (simuliert): Digitize, Classify, Extract mit Konfidenzen.

Die "ML"-Konfidenz wird deterministisch aus Keyword-Treffern, Regex-Treffern und
der simulierten Scan-Qualitaet des Anhangs abgeleitet.
"""

import re

from .contracts import IBAN_RE, iban_valid, normalize_iban, sha256_hex

CLASS_KEYWORDS = {
    "Invoice": ["rechnung", "rechnungsnummer", "zahlbar", "invoice", "nettobetrag"],
    "Complaint": ["reklamation", "gutschrift", "beschaedigt", "mangel", "fehlmenge"],
    "BankDetailChange": ["bankverbindung", "neue iban", "kontoinhaber", "bankdaten"],
}

REQUIRED_FIELDS = {
    "Invoice": ["supplierId", "invoiceNo", "amount"],
    "Complaint": ["supplierId", "reference", "amount"],
    "BankDetailChange": ["supplierId", "iban", "accountHolder"],
    "Other": [],
}

PATTERNS = {
    "supplierId": r"Lieferanten(?:nummer|nr\.?)\s*[:#]?\s*(\d{6})",
    "invoiceNo": r"Rechnungs(?:nummer|nr\.?)\s*[:#]?\s*([A-Z0-9][A-Z0-9-]{2,})",
    "amount": r"(?:Gesamtbetrag|Gutschriftsbetrag|Betrag|Summe)\s*:?\s*(?:EUR\s*)?(\d{1,3}(?:\.\d{3})*,\d{2})",
    "poNumber": r"Bestell(?:nummer|nr\.?)\s*[:#]?\s*(45\d{8})",
    "reference": r"(?:Bezug|Referenz)(?:\s+Rechnung)?\s*[:#]?\s*([A-Z0-9][A-Z0-9-]{2,})",
    "accountHolder": r"Kontoinhaber\s*:?\s*([^\n\r]+)",
}


def parse_amount(text):
    return round(float(text.replace(".", "").replace(",", ".")), 2)


def _noise(seed_text, quality):
    """Deterministische Konfidenz-Streuung abhaengig von der Scan-Qualitaet."""
    if quality >= 0.95:
        return 0.0
    h = int(sha256_hex(seed_text)[:4], 16) / 0xFFFF
    return (1 - quality) * (0.6 + 0.8 * h)


def digitize(attachment):
    """Digitize: Text + Metadaten. Echte OCR wird nicht simuliert, nur die Qualitaet."""
    content = attachment.get("content", "")
    return {
        "filename": attachment.get("filename", "mail-body.txt"),
        "text": content,
        "pages": max(1, content.count("\f") + 1),
        "quality": float(attachment.get("quality", 1.0)),
        "sha256": sha256_hex(content),
    }


def classify(doc):
    text = doc["text"].lower()
    scores = {cls: sum(1 for k in kws if k in text) for cls, kws in CLASS_KEYWORDS.items()}
    best = max(scores, key=scores.get)
    hits = scores[best]
    if hits == 0:
        return "Other", round(0.9 * doc["quality"], 2)
    total = sum(scores.values())
    base = 0.80 + 0.06 * min(hits, 3) - 0.08 * (total - hits)
    conf = max(0.05, min(0.99, base)) - _noise(doc["sha256"] + "cls", doc["quality"]) * 0.5
    return best, round(max(0.05, conf), 2)


def extract(doc, doc_type):
    text = doc["text"]
    fields, conf = {}, {}
    for name, pattern in PATTERNS.items():
        m = re.search(pattern, text, re.IGNORECASE)
        if not m:
            continue
        raw = m.group(1).strip()
        value = parse_amount(raw) if name == "amount" else raw
        fields[name] = value
        conf[name] = round(max(0.05, 0.99 - _noise(doc["sha256"] + name, doc["quality"])), 2)

    iban_m = re.search(r"(?:neue\s+)?IBAN\s*:?\s*" + IBAN_RE.pattern, text, re.IGNORECASE)
    if iban_m:
        iban = normalize_iban(iban_m.group(1))
        fields["iban"] = iban
        c = 0.99 - _noise(doc["sha256"] + "iban", doc["quality"])
        conf["iban"] = round(c if iban_valid(iban) else min(c, 0.40), 2)

    for req in REQUIRED_FIELDS.get(doc_type, []):
        if req not in fields:
            fields[req] = None
            conf[req] = 0.0
    if "currency" not in fields and "amount" in fields:
        fields["currency"] = "EUR"
    return fields, conf


def process(attachments, body):
    """Fuehrt S3 fuer alle Anhaenge aus. Ohne Anhang wird der Mail-Body verarbeitet."""
    sources = attachments or [{"filename": "mail-body.txt", "content": body, "quality": 1.0}]
    documents = []
    for i, att in enumerate(sources, start=1):
        d = digitize(att)
        doc_type, class_conf = classify(d)
        fields, field_conf = extract(d, doc_type)
        documents.append({
            "docId": f"d-{i:03d}",
            "filename": d["filename"],
            "sha256": d["sha256"],
            "scanQuality": d["quality"],
            "type": doc_type,
            "classConfidence": class_conf,
            "fields": fields,
            "fieldConfidence": field_conf,
            "_text": d["text"],
        })
    return documents


def needs_validation(documents, class_threshold, field_threshold):
    reasons = []
    for d in documents:
        if d["classConfidence"] < class_threshold:
            reasons.append(f"{d['docId']}: Klassenkonfidenz {d['classConfidence']} < {class_threshold}")
        for f, c in d["fieldConfidence"].items():
            if c < field_threshold:
                reasons.append(f"{d['docId']}.{f}: Konfidenz {c} < {field_threshold}")
    return reasons
