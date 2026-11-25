# agent-control-plane (AEAP) — governed AI agents for enterprise processes

A runnable **reference implementation** of a governance layer for AI agents: every proposal an agent makes goes through a
**Policy Decision Point** (allow / escalate / block), tool calls must satisfy versioned **contracts**, inputs pass a **trust layer**
(PII masking, prompt-injection patterns), external agents talk to it through a **signed bridge** (HMAC-SHA256, time window,
idempotency keys), and everything lands in an audit trail. The reference scenario is a supplier changing its bank details by
e-mail (stages S0–S10). Python standard library only, SQLite, vanilla JS UI.

* **42 tests** (`python -m unittest discover -s tests`), run in CI on Python 3.11 and 3.12.
* **What is simulated:** ERP, CRM and mail are in-memory stand-ins with fault injection (5xx, timeout after commit, selector drift).
  There is no real SAP or UiPath connection, and the UI has no login: it is meant to run locally (binds to `127.0.0.1`).
* **Optional LLM assistant:** off by default; if you enable it, text goes to the external provider you configure (after masking).
* It started as the reference app of a design document (SDD) for an enterprise agent platform; the German text below is that
  document's companion README.

---

# AEAP · Agentic Enterprise Automation Platform (Referenz-App, Deutsch)

Lauffähige Referenz-Implementierung zu **SDD Abschnitt 1 (End-to-End Target Architecture)**, **Abschnitt 2 (Enterprise Governance & Guardrails)** und **Abschnitt 3 (Agenten- und Integrationsdesign)**.
Das Referenzszenario A3 „Supplier Inbound Case“ läuft durch alle Stufen S0–S10. Dazu gibt es eine Control-Center-Oberfläche und einen **Spickzettel für Laien**.

## Starten

| Was | Wie |
|---|---|
| **App** | Doppelklick auf `start.bat` oder `python app.py` → öffnet http://127.0.0.1:8710/ |
| **Spickzettel ohne App** | Doppelklick auf `Spickzettel.html` (läuft offline im Browser, druckbar) |
| Mit leerer Datenbank | `python app.py --reset` |
| Tests | `python -m unittest discover -s tests -v` (42 Tests) |

Voraussetzung ist nur **Python 3.11+**. Es gibt keine Abhängigkeiten, nur die Standardbibliothek, SQLite und Vanilla JS.
Ist der Port belegt, nimmt die App automatisch den nächsten freien. Die Daten liegen in `data/aeap.db`.

## Der Spickzettel (für alle, auch ohne Vorwissen)

Eine Wissensquelle (`web/knowledge.js`) erscheint an drei Stellen:

1. **In der App** unter *📘 Spickzettel*, mit sechs Bereichen:
   - **Schnellstart**: die Plattform als Büro erklärt, die 8 Grundregeln, Ausprobieren in 5 Minuten
   - **Wann nutze ich was?**: Entscheidungstabellen (Agent-Typ, KI oder Robot, API oder UI, Risikostufe, wann ein Mensch ran muss, wer bei Fehlern wiederholt, welche Notbremse, wie ändere ich eine Regel) und Leitfäden pro Rolle
   - **Systeme im Detail**: jede Komponente [1]–[18], [A]–[C] mit Alltagsvergleich, Aufgaben, Aufbau, Ein/Aus, „macht bewusst nicht“ und Fehlerverhalten
   - **Ablauf S0–S10** am Beispiel einer Bankdatenänderung
   - **Glossar A–Z** mit über 100 Begriffen, Suche und Kategorien
   - **Abschnitt 2: Governance** als vollständiges SDD-Kapitel, jeder Unterabschnitt beginnt mit „Kurz gesagt“
2. **Auf jeder Seite** ein aufklappbares „💡 Was sehe ich hier?“ mit Begriffs-Links. Fährt man mit der Maus über einen unterstrichenen Begriff, erscheint die Definition.
3. **Als Datei** `Spickzettel.html` zum Weitergeben und Drucken („🖨 Alles drucken“).

## Workflow-Planer (🧭 in der App und in `Spickzettel.html`)

Vorhaben in einem Satz beschreiben, z. B. *„Workflow in der Automation Suite, der XY befüllt und Human in the Loop braucht“*. Der Planer:

1. **erkennt die Eckdaten** (Umgebung, Zielsystem und Zugriffsweg, Eingang, Auslöser, Volumen, Art des Human in the Loop, Risiko, personenbezogene Daten). Alles ist danach im Formular korrigierbar.
2. **erzeugt einen Blueprint**: empfohlener Ablauf, die wichtigsten Entscheidungen, Robot-Kapazität und **bis zu 60 Parameter in 12 Bereichen**. Jeder Parameter hat eine Empfehlung, eine Begründung in Klartext, den Ort, wo man ihn in UiPath einstellt, und eine Stufe (Pflicht / Empfohlen / Prüfen). Suite-spezifische Punkte erscheinen nur bei Automation Suite.
3. ergänzt **offene Fragen** und **typische Fehler**, eine Abhakliste (wird im Browser gemerkt) und **Export als Markdown** oder zum Drucken.

Der Planer ist regelbasiert (`web/blueprint.js`): gleiche Eingabe, gleiche Empfehlung. Produktverfügbarkeiten der Suite werden als „Prüfen“ markiert, nicht behauptet.

### Optional: KI-Assistent (Kimi, OpenAI, DeepSeek, OpenRouter …)

Unter dem Blueprint kann ein Sprachmodell den Bauplan erklären und Rückfragen beantworten. Der regelbasierte Blueprint wird dabei als verbindliche Grundlage mitgeschickt. Jeder Aufruf läuft durch dieselben Schutzmechanismen wie die Plattform: IBAN, E-Mail und Telefonnummern werden maskiert, Injection-Muster erkannt, und Token-Verbrauch sowie Frage landen im Audit (Art `assistant_call`).

Einrichtung: `.env.example` nach `.env` kopieren und **eine** Variante aktivieren:

| Variante | Eintrag in `.env` |
|---|---|
| A) auf vorhandene Datei verweisen, Schlüssel wird **nicht kopiert** | `AEAP_LLM_API_KEY_FILE=C:\Users\<name>\secrets\llm.env` und `AEAP_LLM_API_KEY_VAR=MOONSHOT_API_KEY` |
| B) Schlüssel direkt | `MOONSHOT_API_KEY=…` (oder `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`) |
| C) Umgebungsvariable | PowerShell: `$env:MOONSHOT_API_KEY="…"; python app.py` |

Das Modell wird automatisch über `/models` gewählt (oder `AEAP_LLM_MODEL` setzen). Kimi-Schlüssel der chinesischen Plattform werden automatisch auf `api.moonshot.cn` umgeleitet. Der Schlüssel wird nie angezeigt, gespeichert oder geloggt. `.env` steht in `.gitignore`.
Hinweis: Der Provider ist ein externer Dienst. Bei Kimi/Moonshot erfolgt die Verarbeitung außerhalb der EU. Für echte Projektdaten vorher mit dem Datenschutz klären (vgl. O-1, 2.6).

## Abschnitt 3 in der App (Integration)

| Bereich | Was passiert |
|---|---|
| **Tool-Verträge (R-007)** | Jedes Werkzeug hat einen Vertrag (Felder, Typen, Pflicht/optional, Idempotenz, Zeitlimit, Version). Der PDP eskaliert jeden KI-Vorschlag, der ihn verletzt, z. B. unbekannte Felder oder Text statt Zahl. |
| **External Agent Bridge** | `POST /api/bridge/inbound` für externe Agenten und Partner. Prüfreihenfolge: Zeitfenster (±300 s) → HMAC-SHA256-Signatur über `Zeitstempel.Body` → Idempotency-Key (gleicher Inhalt = gespeicherte Antwort, anderer Inhalt = 409) → Nachrichtentyp → normaler Ingress. `traceparent` (W3C) wird übernommen und im Case gespeichert. |
| **Bridge-Testkonsole** | Unter *Integration*: gültig senden, wiederholen, falsche Signatur, alter Zeitstempel, Key-Konflikt, jeweils mit Erklärung, gesendeten Headern und Antwort. Secret-Rotation per Knopf. |
| **Spickzettel** | Kapitel „Abschnitt 3: Integration“ (3.0–3.10): Agent-Zuschnitt, Tool-Verträge, Ein-/Ausgabevertrag, Bridge, SAP-Integrationsmuster, Webhooks, Fehlervertrag, Trace-Weitergabe, ADR 11–14, offene Punkte O-11 bis O-14. |

Aufruf-Beispiel für Partner (Python):

    import hashlib, hmac, json, time, urllib.request
    body = json.dumps({"type": "supplier.message", "data": {"from": "a@b.example", "subject": "Hallo", "body": "Text", "attachments": []}}).encode()
    ts = str(int(time.time()))
    sig = "sha256=" + hmac.new(SECRET.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    req = urllib.request.Request("http://127.0.0.1:8710/api/bridge/inbound", data=body, method="POST", headers={
        "Content-Type": "application/json", "X-AEAP-Timestamp": ts, "X-AEAP-Signature": sig, "Idempotency-Key": "partner-123"})
    print(urllib.request.urlopen(req).read())

## Abschnitt 2 in der App (Governance)

| Bereich | Was passiert |
|---|---|
| **Rollen & Rechte** | 5 Personen in 4 Rollen. Das System erzwingt die Funktionstrennung: Compliance gibt keine Zahlungen frei, Antragsteller genehmigen nie selbst, und bei R3 muss mindestens eine Freigabe von einer Teamlead kommen. |
| **Änderungsanträge** | Regeln und Guardrails ändern sich nur per Change Request: Diff, automatische Versionserhöhung, Wirkungsanalyse gegen das Golden Set, Release-Gate (PASSED/REVIEW/BLOCKED), Genehmigung durch Compliance, Historie. |
| **Release-Gate** | 11 Kontrollfälle, 5 davon kritisch. Würde z. B. `payments.release_payment` freigegeben, kippt der Injection-Fall → BLOCKED → Genehmigung unmöglich. |
| **Notbremse K1–K3** | Eingang, KI oder Ausführung anhalten. Es geht nichts verloren, beim Lösen läuft alles automatisch weiter. Ein roter Banner zeigt den Zustand überall an. |
| **Guardrail-Katalog** | G-01 bis G-17 mit Schicht, Klartext-Erklärung und Trefferzähler. |
| **Alarme** | Mit „Was tun“-Empfehlung, auch in der Übersicht. |
| **Agenten & Evaluation** | Lifecycle (DRAFT → ACTIVE → RETIRED), Steckbrief, Evaluation per Knopf. |
| **Demo-Steuerung** | Störungen simulieren (nur Vorführung, wirkt ohne Antrag). |

## Demo-Rundgang (ca. 15 Minuten)

1. **Eingang**: Alle Szenarien senden, dann 25 Nachrichten generieren.
2. **Übersicht**: Alarme, Kennzahlen, Live-Zähler je Komponente.
3. **Action Center → Bankdatenänderung (R3)**: Als *Anna Becker* freigeben. Als *Jonas Yilmaz* mit Rückruf freigeben → abgelehnt (Teamlead-Pflicht). Als *Maria Wolf* mit Rückruf freigeben → ausgeführt. Als *Sven Klein* (Compliance) freigeben → keine Berechtigung.
4. **TOCTOU**: Unter *Governance → Demo-Steuerung* den Robot-Autostart aus, einen R2-Case freigeben, unter *Orchestrator* den Payload manipulieren, Robot ausführen → `APPROVAL_HASH_MISMATCH`.
5. **Change Request**: Als *Tom Braun* unter *Governance → Regeln* die Betragsgrenze auf 20.000 setzen → Gate REVIEW. Tom kann nicht genehmigen, *Sven Klein* schon.
6. **Release-Gate blockiert**: Als Tom `payments.release_payment` in die Allowlist aufnehmen → Gate BLOCKED.
7. **Notbremse**: Als Maria K2 ziehen, eine Rechnung senden (wartet vor der KI), K2 lösen → läuft automatisch weiter.

## Was echt ist und was simuliert

| Echt implementiert (deterministisch) | Simuliert |
|---|---|
| Case Envelope, State Machine, Idempotenz, Unique Reference | LLM (Mock-Modell hinter dem Gateway) |
| PDP mit versionierten Regeln, T-READ/T-WRITE, Allowlist pro Agent-Version | Document Understanding (Regex + Konfidenzmodell) |
| Approval-Hash (TOCTOU), 4-Augen + Out-of-Band + Teamlead-Pflicht | SAP, CRM, Outlook (SQLite-Tabellen) |
| Retry-Ownership (1.9), Read-back, Dead-Letter-Queue, Audit-Pack | UiPath Maestro, Orchestrator, Action Center, Robots (nachgebaut) |
| PII-Pseudonymisierung, Prompt-Injection-Guardrail, Prompt-I/O-Policy | Healing Agent |
| RBAC + Funktionstrennung, Change Requests, Golden-Set-Release-Gate, Notbremse, Alarme | |

## Struktur

```
app.py                 Starter (HTTP-Server + Browser)
start.bat              Windows-Doppelklick-Start
Spickzettel.html       Spickzettel ohne Server (offline, druckbar)
aeap/
  contracts.py         Envelope, State Machine, Tool-Klassen, Hashing, IBAN, Output-Schema
  store.py             SQLite-Persistenz
  du.py                [15] Document Understanding (simuliert)
  trust_layer.py       [10] LLM Gateway, [11] Guardrails/PII, [12] Context Grounding, Mock-LLM
  agent.py             [A] Supplier-Triage-Agent, Agent-Wrapper, Agent-Steckbrief
  pdp.py               [5] Policy Decision Point
  platform.py          [4] Maestro, [6] Action Center, [13] Queues, [14] Robot, S0–S10
  governance.py        Abschnitt 2: Rollen, Rechte, RACI, Guardrail-Katalog, Golden Set, Evaluation
  governance_ops.py    Abschnitt 2: Change Requests, Notbremse, Alarme, Guardrail-Status
  systems.py           [16]–[18] ERP/CRM/Outbox mit Idempotenz und Fault Injection
  scenarios.py         Referenzszenarien + Lastgenerator
  server.py            REST-API
web/
  knowledge.js         Spickzettel-Inhalte + Rendering (eine Quelle für alles)
  index.html, app.js, styles.css
tests/test_aeap.py     35 Tests (Abschnitt 1 + 2, KI-Assistent)
```

## REST-API (Auszug)

| Methode | Pfad | Zweck |
|---|---|---|
| POST | `/api/ingest`, `/api/simulate` | Mail einspielen, Last erzeugen |
| GET | `/api/cases`, `/api/cases/{id}`, `/api/cases/{id}/auditpack` | Cases, Detail, Audit-Pack |
| POST | `/api/tasks/{id}/approve\|deny\|validate\|acknowledge` | Action Center (`user` im Body, Rechte werden geprüft) |
| GET/POST | `/api/queues`, `/api/queues/{id}/tamper`, `/api/robot/run` | Orchestrator |
| GET | `/api/governance`, `/api/alerts` | Governance-Gesamtsicht, Alarme |
| POST | `/api/governance/changes`, `/api/governance/changes/{id}/approve\|reject` | Change Requests |
| POST | `/api/governance/killswitch`, `/api/governance/evaluate` | Notbremse, Evaluation |
| PUT | `/api/settings` | Demo-Steuerung direkt, Regeln/Guardrails → Change Request |
| GET | `/api/audit?caseId=&correlationId=` | Unified Audit |

## Offene Punkte

Abschnitt 1: O-1 Cloud oder Suite/Region · O-2 OTel nativ · O-3 Bridge-Egress · O-4 PDP-Ort · O-5 Schwellen.
Abschnitt 2: O-6 Aufbewahrung/Löschung · O-7 Einstufung EU AI Act · O-8 SIEM und Alarm-Empfänger · O-9 Pflege des Golden Sets · O-10 Notbremse außerhalb der Geschäftszeiten.
