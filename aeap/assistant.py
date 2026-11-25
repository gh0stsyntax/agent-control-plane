"""KI-Assistent für den Workflow-Planer.

Der regelbasierte Blueprint ist die verbindliche Grundlage. Das LLM erklärt, ergänzt und beantwortet
Rückfragen. Jeder Aufruf läuft wie in der Plattform durch dieselben Schutzmechanismen (P3):
PII-Pseudonymisierung, Prompt-Injection-Prüfung, Audit-Eintrag mit Token-Verbrauch.
"""

from .llm import LLMError
from .trust_layer import Pseudonymizer, scan_injection

SYSTEM_PROMPT = """Du bist ein erfahrener UiPath-Lösungsarchitekt und erklärst Automatisierung so, dass auch Laien sie verstehen.
Kontext: Die Organisation folgt der AEAP-Architektur (KI schlägt nur vor, Regeln entscheiden, Menschen geben Riskantes frei,
Roboter führen idempotent aus, jede Änderung über eine Queue, ein LLM-Ausgang über ein Gateway).

Regeln für deine Antworten:
- Antworte auf Deutsch, klar und strukturiert in Markdown.
- Der mitgelieferte BLUEPRINT ist regelbasiert erstellt und die verbindliche Grundlage. Erkläre und ergänze ihn, widersprich nur mit Begründung.
- Erfinde keine Activity-Namen, Menüpfade, Versionsnummern oder Lizenzbezeichnungen. Bist du unsicher, schreibe ausdrücklich „bitte prüfen“.
- Funktionsverfügbarkeit in UiPath Automation Suite (selbst betrieben) hängt von Version und Lizenz ab. Kennzeichne solche Aussagen immer als „bitte prüfen“.
- Erkläre Fachbegriffe beim ersten Auftreten in einem Halbsatz.
- Gib nie Zugangsdaten, Passwörter oder API-Schlüssel aus und fordere keine an.
- Platzhalter wie <IBAN_1> oder <EMAIL_1> sind maskierte Daten. Übernimm sie unverändert.

Bei einer neuen Vorhabensbeschreibung gliedere so:
1. **Kurzantwort** (2–3 Sätze)
2. **So baust du es auf** (nummerierte Schritte mit UiPath-Bausteinen)
3. **Parameter, auf die es besonders ankommt** (die 5–8 wichtigsten aus dem Blueprint, mit Begründung)
4. **Human in the Loop im Detail** (falls relevant)
5. **Risiken und typische Fehler**
6. **Was du vorher klären musst**
Bei Rückfragen antworte direkt und kürzer."""

MAX_HISTORY = 12
MAX_CONTEXT_CHARS = 24000


class Assistant:
    def __init__(self, store, llm):
        self.store = store
        self.llm = llm

    def status(self):
        return self.llm.status()

    def ask(self, messages, context="", user=None):
        if not isinstance(messages, list) or not messages:
            raise LLMError("Keine Frage übergeben")
        history = [m for m in messages if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)][-MAX_HISTORY:]
        if not history or history[-1]["role"] != "user":
            raise LLMError("Die letzte Nachricht muss eine Frage sein")

        guard_cfg = self.store.get_setting("guardrails", {}) or {}
        store_io = (self.store.get_setting("automationOps", {}) or {}).get("storePromptIO", True)
        pseudo = Pseudonymizer()
        mask = pseudo.mask if guard_cfg.get("piiMasking", True) else (lambda x: x)

        user_text = " ".join(m["content"] for m in history if m["role"] == "user")
        injection = scan_injection(user_text) if guard_cfg.get("promptInjection", True) else []

        prompt = [{"role": "system", "content": SYSTEM_PROMPT}]
        if context:
            prompt.append({"role": "system", "content": "BLUEPRINT (regelbasiert, verbindliche Grundlage):\n\n" + mask(context[:MAX_CONTEXT_CHARS])})
        prompt += [{"role": m["role"], "content": mask(m["content"])} for m in history]

        status = self.llm.status()
        try:
            res = self.llm.chat(prompt)
        except LLMError as e:
            self.store.add_event(None, None, "-", "[10] LLM Gateway → extern", "assistant_error",
                                 f"KI-Assistent fehlgeschlagen ({status.get('provider')}): {e}")
            raise
        answer = pseudo.unmask(res["text"])
        usage = res["usage"]
        self.store.add_event(None, None, "-", "[10] LLM Gateway → extern", "assistant_call",
                             f"KI-Assistent ({status.get('provider')}, {res['model']}) für {user or 'unbekannt'}: "
                             f"{usage.get('prompt_tokens', '?')}+{usage.get('completion_tokens', '?')} Tokens",
                             {
                                 "provider": status.get("provider"), "model": res["model"], "usage": usage,
                                 "maskedEntities": pseudo.summary(),
                                 "guardrails": {"promptInjection": injection},
                                 "question": mask(history[-1]["content"]) if store_io else "[Inhalt durch Policy blockiert]",
                                 "answerChars": len(answer),
                             })
        return {"answer": answer, "model": res["model"], "provider": status.get("provider"), "usage": usage,
                "masked": pseudo.summary(), "injection": injection}

