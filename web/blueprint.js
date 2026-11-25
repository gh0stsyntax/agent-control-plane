/* AEAP Workflow-Planer: aus einer Vorhabensbeschreibung einen UiPath-Blueprint mit allen Parametern erzeugen.
 * Regelbasiert (keine KI): gleiche Eingabe → gleiche Empfehlung. Läuft in App und Spickzettel.html.
 * Hinweis: Produktverfügbarkeiten (v. a. in Automation Suite) sind als "Prüfen" markiert, nicht behauptet.
 */
"use strict";

(function () {
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const T = (name, label) => (window.kbTermLink ? window.kbTermLink(name, label) : esc(label || name));
  const STORE_KEY = "aeap.planner.v1";
  const DONE_KEY = "aeap.planner.done.v1";

  const DEFAULTS = {
    goal: "", name: "", env: "unknown", target: "unknown", input: "email", trigger: "event",
    volume: 200, peak: 2, aht: 3, window: 10,
    hitl: ["approval"], risk: "medium", pii: false, ai: false, reversible: true,
  };

  const OPTIONS = {
    env: { cloud: "Automation Cloud (SaaS)", suite: "Automation Suite (selbst betrieben)", unknown: "noch unklar" },
    target: { api: "hat eine API (REST/OData/BAPI)", web: "Web-Oberfläche (Browser)", sapgui: "SAP GUI", desktop: "Desktop-Anwendung", citrix: "Citrix / Remote-Desktop", unknown: "unbekannt" },
    input: { email: "E-Mail (Text/Anhang)", documents: "Dokumente (PDF/Scan)", structured: "strukturiert (Excel/CSV/DB)", form: "Formular / Antrag", api: "Aufruf eines anderen Systems" },
    trigger: { event: "Ereignis (z. B. neue Mail/Datei)", time: "Zeitplan (z. B. täglich)", manual: "manuell durch Mitarbeiter", api: "API-Aufruf / Webhook" },
    risk: { low: "gering: Fehler leicht korrigierbar", medium: "mittel: Aufwand/Ärger bei Fehlern", high: "hoch: Geld, Stammdaten, rechtliche Folgen" },
    hitl: { approval: "Freigabe vor dem Schreiben", validation: "Daten prüfen/korrigieren", exception: "Ausnahmen klären", sampling: "Stichprobe nach Automatik" },
  };

  // =====================================================================
  // 1. Freitext analysieren
  // =====================================================================
  const KNOWN_API = /\b(salesforce|servicenow|workday|dynamics|jira|hubspot|sharepoint|teams|zendesk|successfactors|coupa|ariba|datev|rest|odata|bapi|graphql|api)\b/i;

  function analyze(text) {
    const t = (text || "").toLowerCase();
    const found = {}, notes = [];
    if (!t.trim()) return { found, notes };

    if (/automation suite|on-?prem|selbst gehostet|self-?hosted|eigene[mn]? rechenzentrum/.test(t)) { found.env = "suite"; notes.push("Umgebung: Automation Suite"); }
    else if (/automation cloud|\bcloud\b|saas/.test(t)) { found.env = "cloud"; notes.push("Umgebung: Automation Cloud"); }

    const hitl = new Set();
    if (/human in the loop|\bhitl\b|freigabe|freigeben|genehmig|approval|absegn|4-augen|vier-augen|bestätig/.test(t)) hitl.add("approval");
    if (/validier|korrigier|nachprüf|kontrollier|überprüf/.test(t)) hitl.add("validation");
    if (/ausnahme|sonderfall|klärung|klären|eskal/.test(t)) hitl.add("exception");
    if (/stichprobe/.test(t)) hitl.add("sampling");
    if (/human in the loop|\bhitl\b/.test(t) && hitl.size === 0) hitl.add("approval");
    if (hitl.size) { found.hitl = [...hitl]; notes.push("Human in the Loop: " + found.hitl.map((h) => OPTIONS.hitl[h]).join(", ")); }

    // Zielsystem: Wort(e) vor "befüllen/eintragen/anlegen/pflegen/übertragen"
    const m = text.match(/(?:in|ins|im|das|die|den|dem)\s+([\wÄÖÜäöüß./-]+(?:\s[\wÄÖÜäöüß./-]+)?)\s+(?:zu\s+)?(?:befüll|einzutragen|eintrag|anzulegen|anleg|pfleg|übertrag|schreib|aktualisier|erfass)/i);
    if (m) {
      const cand = m[1].replace(/^(system|tool|programm)\s+/i, "").trim();
      if (!/^(daten|felder|werte|formular)$/i.test(cand)) { found.name = cand.length <= 30 ? cand : cand.slice(0, 30); notes.push(`Zielsystem: ${found.name}`); }
    }
    if (!found.name) {
      const m2 = text.match(/\b([\wÄÖÜäöüß.-]{2,30})\s+zu\s+(?:befüll|füll|pflegen|erfassen)/i);
      if (m2 && !/^(daten|felder|werte|formular|ziel)$/i.test(m2[1])) { found.name = m2[1].toUpperCase() === "XY" ? "XY" : m2[1]; notes.push(`Zielsystem: ${found.name}`); }
    }
    if (/\bsap\b/.test(t)) {
      found.name = found.name || "SAP";
      found.target = /bapi|odata|\bapi\b|s\/4.*api/.test(t) ? "api" : "sapgui";
    } else if (/citrix|remote-?desktop|terminalserver/.test(t)) found.target = "citrix";
    else if (KNOWN_API.test(t)) found.target = "api";
    else if (/\bweb\b|portal|browser|webseite|website|online-?formular|weboberfläche/.test(t)) found.target = "web";
    else if (/desktop|windows-anwendung|fat ?client|excel befüll|in excel/.test(t)) found.target = "desktop";
    if (found.target) notes.push("Zugriff auf Zielsystem: " + OPTIONS.target[found.target]);

    if (/pdf|scan|rechnung|beleg|dokument|lieferschein|vertrag als datei/.test(t)) found.input = "documents";
    else if (/e-?mail|\bmail|postfach/.test(t)) found.input = "email";
    else if (/excel|csv|tabelle|liste|datenbank/.test(t)) found.input = "structured";
    else if (/formular|antrag/.test(t)) found.input = "form";
    if (found.input) notes.push("Eingang: " + OPTIONS.input[found.input]);

    if (/täglich|nachts|stündlich|wöchentlich|monatlich|jeden (tag|morgen|abend|montag)|zeitplan|um \d{1,2}(:\d{2})? uhr/.test(t)) found.trigger = "time";
    else if (/sobald|eingeht|eintrifft|eingang|bei neuer|bei neuem/.test(t)) found.trigger = "event";
    else if (/manuell|knopfdruck|startet selbst|auf anfrage/.test(t)) found.trigger = "manual";
    else if (/webhook|api-?aufruf|wird aufgerufen/.test(t)) found.trigger = "api";
    if (found.trigger) notes.push("Auslöser: " + OPTIONS.trigger[found.trigger]);

    const vol = t.match(/(\d[\d.]*)\s*(?:[a-zäöüß-]+\s)?(?:pro|am|je|\/)\s*tag/);
    if (vol) { found.volume = Number(vol[1].replace(/\./g, "")); notes.push(`Volumen: ${found.volume} pro Tag`); }

    if (/bank|iban|zahlung|überweis|stammdaten|gehalt|lohn|kredit|vertrag|kündigung|rechtlich/.test(t)) { found.risk = "high"; notes.push("Risiko: hoch (Geld, Stammdaten oder rechtlich)"); }
    if (/personal|mitarbeiter|patient|bewerber|kunde|adresse|geburt|gesundheit|iban|gehalt/.test(t)) { found.pii = true; notes.push("Personenbezogene Daten: ja"); }
    if (/verstehen|freitext|klassifizier|\bki\b|agent|llm|interpretier|zusammenfass|erkennen was/.test(t)) { found.ai = true; notes.push("KI-Verständnis nötig: ja"); }
    if (/nicht rückgängig|endgültig|unwiderruflich|zahlung|überweis/.test(t)) { found.reversible = false; notes.push("Nicht rückgängig machbar"); }
    return { found, notes };
  }

  // =====================================================================
  // 2. Blueprint ableiten
  // =====================================================================
  function derive(c) {
    const docs = c.input === "documents";
    const ui = ["web", "sapgui", "desktop", "citrix"].includes(c.target);
    const hitl = new Set(c.hitl || []);
    let tier = c.risk === "high" ? "R3" : c.risk === "medium" ? "R2" : hitl.has("approval") ? "R2" : (c.ai || docs) ? "R1" : "R0";
    if (!c.reversible && tier !== "R3") tier = hitl.has("approval") || c.risk !== "low" ? "R3" : "R2";
    const minutes = Math.max(1, Number(c.volume) || 0) * Math.max(1, Number(c.peak) || 1) * Math.max(0.1, Number(c.aht) || 1);
    const robots = Math.max(1, Math.ceil(minutes / (Math.max(1, Number(c.window) || 8) * 60)));
    const name = (c.name || "").trim() || "System XY";
    return { ...c, docs, ui, hitlSet: hitl, tier, robots, name, suite: c.env === "suite", cloud: c.env === "cloud", envUnknown: c.env === "unknown" };
  }

  const M = "Pflicht", E = "Empfohlen", P = "Prüfen";

  // Jeder Eintrag: [Abschnitt, Bedingung(ctx), Parameter, Empfehlung(ctx), Warum (Klartext), Wo einstellen, Stufe]
  const CATALOG = [
    // ---- 1 Umgebung & Infrastruktur -----------------------------------------
    ["env", (x) => x.suite, "Feature-Verfügbarkeit in eurer Suite-Version",
      (x) => `Prüfen, ob ${["Action Center", x.docs && "Document Understanding", x.target === "api" && "Integration Service", x.ai && "AI Trust Layer / Agenten", "Maestro"].filter(Boolean).join(", ")} in der installierten Version aktiviert und lizenziert sind.`,
      "Automation Suite wird selbst betrieben. Nicht jede Funktion der Cloud ist in jeder Suite-Version enthalten oder eingeschaltet. Das entscheidet über das Muster.", "Automation-Suite-Installation / Admin-Portal (aktivierte Produkte), Lizenzübersicht", P],
    ["env", (x) => x.suite, "Betriebsmodell (Single-Node / Multi-Node HA)",
      () => "Produktion mit Fristen und wartenden Freigaben: Multi-Node mit Hochverfügbarkeit. Single-Node nur für Test.",
      "Fällt der Server aus, warten Freigaben und Robots. Hochverfügbarkeit verhindert, dass ein Ausfall Fristen reißt.", "Sizing/Installationsplanung der Automation Suite", E],
    ["env", (x) => x.suite, "Backup und Wiederherstellung",
      () => "Regelmäßige Sicherung von Datenbanken und Objektspeicher. Wiederherstellung mindestens einmal testen.",
      "Offene Freigaben, Queue-Items und Protokolle liegen in eurer Infrastruktur. Ohne Backup gehen sie bei einem Ausfall verloren.", "Betriebskonzept der Suite (Datenbank, Storage)", M],
    ["env", (x) => x.suite, "Netzwerk und Zertifikate",
      (x) => `TLS-Zertifikate, Proxy und Firewall-Freigaben: Robot-Maschinen → Orchestrator und Robot-Maschinen → ${x.name}.`,
      "Robots müssen die Plattform und das Zielsystem erreichen. Fehlt eine Freigabe, scheitert jeder Lauf.", "Netzwerkplanung, Zertifikatsverwaltung der Suite", M],
    ["env", (x) => x.suite, "Update-Plan",
      () => "Festen Rhythmus für Suite-Updates festlegen und Kompatibilität von Studio- und Robot-Versionen prüfen.",
      "In der Suite aktualisiert ihr selbst. Veraltete Versionen bedeuten fehlende Funktionen und Sicherheitslücken.", "Betriebskonzept / Change-Management", E],
    ["env", (x) => x.suite && x.ai, "Zugang zu einem Sprachmodell (LLM)",
      () => "Klären, welches LLM aus eurem Rechenzentrum erreichbar ist (z. B. eigenes Modell / BYO-LLM) und ob Datenresidenz und Datenschutz das erlauben.",
      "Ohne erreichbares Modell funktioniert kein KI-Schritt. Bei selbst betriebener Plattform ist das nicht automatisch gegeben.", "AI Trust Layer / LLM-Konfiguration (falls verfügbar), IT-Sicherheit", P],
    ["env", (x) => x.suite && x.docs, "Rechenleistung für Dokumentenerkennung",
      () => "Ressourcen für die ML-Modelle laut Sizing-Vorgaben einplanen (Volumen × Seiten).",
      "Die Erkennung läuft auf euren Servern. Zu wenig Leistung bedeutet lange Wartezeiten.", "Sizing der Automation Suite", P],
    ["env", (x) => x.cloud, "Region und Datenresidenz",
      () => "Tenant-Region passend zu Datenschutzvorgaben (z. B. EU) wählen bzw. bestätigen.",
      "Bestimmt, wo eure Daten verarbeitet werden.", "Automation Cloud → Organisation / Tenant", P],
    ["env", (x) => x.envUnknown, "Cloud oder Suite entscheiden",
      () => "Zuerst klären: Automation Cloud (Anbieter betreibt) oder Automation Suite (ihr betreibt).",
      "Davon hängen Betrieb, Funktionsumfang, Update-Aufwand und Datenhaltung ab.", "Architektur-Entscheidung (O-1)", M],
    ["env", () => true, "Getrennte Umgebungen",
      () => "Entwicklung, Test und Produktion getrennt (eigene Tenants oder mindestens eigene Folders).",
      "Tests dürfen nie echte Daten in Produktion verändern.", "Orchestrator: Tenants / Folders", M],
    ["env", () => true, "Robot-Kapazität",
      (x) => `Schätzung: ${x.robots} Unattended Robot(s) (+1 Reserve). Basis: ${x.volume}/Tag × Spitze ${x.peak} × ${x.aht} min ÷ ${x.window} h.`,
      "Zu wenige Robots stauen die Queue, zu viele kosten Lizenzen. Die Wartezeit auf Menschen zählt nicht mit, wenn das Warten persistent gelöst ist.", "Orchestrator: Machines / Robots, Lizenzen", E],
    ["env", (x) => x.hitlSet.size > 0, "Lizenzen für Bearbeiter",
      () => "Prüfen, welche Lizenz die Menschen für das Bearbeiten von Aufgaben im Action Center brauchen.",
      "Ohne passende Lizenz sehen Bearbeiter ihre Aufgaben nicht.", "Admin → Lizenzen / Benutzer", P],

    // ---- 2 Identität, Rechte, Geheimnisse ------------------------------------
    ["iam", () => true, "Anmeldung der Menschen über den Firmen-IdP",
      () => "Entra ID / LDAP / SAML anbinden, Gruppen statt Einzelpersonen berechtigen.",
      "Ein Login für alle und zentraler Entzug von Rechten beim Austritt.", "Admin → Sicherheit / Identity Provider", M],
    ["iam", () => true, "Technischer Benutzer (Robot-Konto)",
      (x) => `Eigenes Robot-Konto für diesen Prozess mit genau den Rechten, die er in ${x.name} braucht.`,
      "Wenn etwas schiefgeht, ist klar, dass es der Robot war, und der Schaden bleibt begrenzt (Least Privilege).", `Orchestrator: Robot Accounts · Berechtigungen in ${"Zielsystem"}`, M],
    ["iam", () => true, "Zugangsdaten",
      (x) => `Zugangsdaten zu ${x.name} als Credential-Asset bzw. in einem Credential Store (z. B. CyberArk, Azure Key Vault). Nie im Workflow oder in Konfigurationsdateien.`,
      "Passwörter im Code landen in Paketen, Logs und Git und sind dann kompromittiert.", "Orchestrator: Assets (Credential) / Credential Stores", M],
    ["iam", () => true, "Folder und Rollen",
      () => "Eigener Folder für den Prozess. Rollen getrennt: Entwickler, Betrieb, Fachbereich.",
      "Trennt Rechte und Sichtbarkeit sauber zwischen Prozessen.", "Orchestrator: Folders → Benutzer/Rollen", E],
    ["iam", (x) => x.hitlSet.has("approval"), "Funktionstrennung bei Freigaben",
      () => "Wer einen Datensatz erstellt oder beantragt, darf ihn nicht selbst freigeben. Entwickler geben in Produktion nichts frei.",
      "Das Vier-Augen-Prinzip funktioniert nur, wenn es wirklich vier verschiedene Augen sind.", "Action Center: Zuweisung · Prozesslogik", M],

    // ---- 3 Auslöser & Eingang --------------------------------------------------
    ["intake", () => true, "Auslöser (Trigger)",
      (x) => ({ event: "Event-Trigger (Integration Service), z. B. „neue Mail“, alternativ Polling-Dispatcher als Fallback", time: "Time-Trigger (Zeitplan) außerhalb der Lastspitzen der Zielsysteme", manual: "Start durch Mitarbeiter (Attended / Assistant) oder über ein Formular", api: "Start per Orchestrator-API bzw. Webhook, abgesichert mit Signatur/Token" })[x.trigger],
      "Legt fest, wann der Prozess startet. Ereignisse sind schneller als Zeitpläne, Zeitpläne leichter planbar.", "Orchestrator: Triggers / Integration Service: Triggers", M],
    ["intake", () => true, "Dispatcher und Performer trennen",
      () => "Prozess 1 (Dispatcher) liest ein und legt pro Vorgang ein Queue-Item an. Prozess 2 (Performer) arbeitet die Items ab.",
      "Skaliert auf mehrere Robots, Fehler betreffen nur ein Item, und alles ist wiederholbar.", "Zwei Prozesse in Studio, verbunden über eine Queue", E],
    ["intake", () => true, "Idempotency-Key",
      (x) => ({ email: "Message-ID + Hash des Anhangs", documents: "Dokument-Hash (+ Absender)", structured: "fachlicher Schlüssel der Zeile (z. B. Nummer + Datum)", form: "Antrags-ID", api: "Request-ID des aufrufenden Systems" })[x.input],
      "Eine eindeutige Kennung je Vorgang verhindert, dass derselbe Vorgang zweimal in das Zielsystem geschrieben wird.", "Dispatcher-Logik → Queue-Item Reference", M],
    ["intake", () => true, "Eindeutige Referenzen in der Queue",
      () => "Bei der Queue „eindeutige Referenzen erzwingen“ aktivieren. Reference = Idempotency-Key.",
      "Die Plattform lehnt Duplikate dann selbst ab. Das funktioniert auch, wenn der Dispatcher zweimal läuft.", "Orchestrator: Queue → Einstellungen (Unique Reference)", M],
    ["intake", () => true, "Kaputte Eingänge",
      () => "Unlesbare oder leere Eingänge in einen eigenen Bereich verschieben (Dead-Letter) und melden.",
      "Ein kaputter Eingang darf nicht den ganzen Prozess blockieren.", "Dispatcher-Logik, separate Queue oder Ordner", E],
    ["intake", (x) => x.input === "email", "Postfach-Anbindung",
      () => "Shared Mailbox über einen Connector anbinden. Verarbeitete Mails in einen Ordner verschieben. Anhänge archivieren.",
      "So ist nachvollziehbar, welche Mail schon verarbeitet wurde.", "Integration Service: Outlook/Exchange-Connector", E],
    ["intake", (x) => x.input === "structured", "Eingabedatei prüfen",
      () => "Spalten, Datentypen und Pflichtwerte vor dem Einlesen prüfen. Fehlerhafte Zeilen als Business Exception markieren.",
      "Falsche Eingaben früh abfangen ist billiger als falsche Daten im Zielsystem.", "Dispatcher-Logik", M],
    ["intake", (x) => x.input === "form", "Pflichtfelder im Formular",
      () => "Pflichtfelder und Formate direkt im Formular erzwingen.",
      "Was gar nicht erst falsch eingegeben werden kann, muss niemand später korrigieren.", "Formular-Design", E],

    // ---- 4 Queue-Design --------------------------------------------------------
    ["queue", () => true, "Queue-Name",
      (x) => `Namenskonvention, z. B. ${x.name.replace(/\W+/g, "")}_Befuellen`,
      "Einheitliche Namen machen Betrieb und Monitoring übersichtlich.", "Orchestrator: Queues", E],
    ["queue", () => true, "Inhalt eines Queue-Items",
      () => "Nur die benötigten Felder und ein Schema-Versionsfeld. Keine Passwörter. Personenbezogene Daten so wenig wie möglich.",
      "Queue-Inhalte sind für Betreiber sichtbar und werden gespeichert.", "Dispatcher: Add Queue Item → SpecificContent", M],
    ["queue", () => true, "Genau ein Retry-Besitzer",
      () => "Entweder Auto-Retry der Queue (z. B. max. 2–3 bei System Exceptions) und im REFramework MaxRetryNumber = 0, oder umgekehrt. Nie beides.",
      "Wiederholen zwei Ebenen gleichzeitig, wird doppelt geschrieben.", "Orchestrator: Queue → Auto Retry · REFramework Config (MaxRetryNumber)", M],
    ["queue", () => true, "SLA der Queue",
      (x) => `SLA und Risk-SLA aus Frist und Volumen ableiten (z. B. Bearbeitung innerhalb von ${x.tier === "R3" ? "24" : x.tier === "R2" ? "8" : "4"} h).`,
      "Die Plattform warnt dann rechtzeitig, bevor Fristen reißen.", "Orchestrator: Queue → SLA", E],
    ["queue", () => true, "Queue-Trigger für den Performer",
      (x) => `Startet den Performer bei neuen Items. Max. parallele Jobs = ${x.robots}.`,
      "Arbeit beginnt sofort, ohne dass jemand starten muss.", "Orchestrator: Triggers → Queue Trigger", E],

    // ---- 5 Daten verstehen -----------------------------------------------------
    ["understand", (x) => x.docs, "Dokumenttypen und Felder",
      () => "Für jeden Dokumenttyp die Felder festlegen, davon die Pflichtfelder markieren.",
      "Die Erkennung kann nur liefern, was vorher definiert wurde.", "Document Understanding: Taxonomie", M],
    ["understand", (x) => x.docs, "Extraktionsverfahren",
      () => "Vortrainiertes Modell, eigenes trainiertes Modell oder Vorlage/Regex für feste Layouts. Mit echten Beispieldokumenten testen.",
      "Feste Formulare brauchen kein KI-Modell. Wechselnde Layouts schon.", "Document Understanding: Extraktoren", E],
    ["understand", (x) => x.docs, "Konfidenzschwellen",
      () => "Start: 0,80 pro Feld. Kritische Felder (Beträge, IBAN, Kennzeichen) höher ansetzen. Darunter → Validierung durch Menschen.",
      "Die Schwelle entscheidet, wie viel ein Mensch prüfen muss und wie viele Fehler durchrutschen.", "DU-Konfiguration / Workflow-Logik", M],
    ["understand", (x) => x.docs, "Plausibilitätsregeln",
      () => "Prüfziffern (IBAN), Summen, Datumsbereiche und Abgleich mit Stammdaten.",
      "Fängt Lesefehler, die trotz hoher Konfidenz passieren.", "Workflow-Logik nach der Extraktion", E],
    ["understand", (x) => x.ai, "Agent-Typ",
      () => "Standardaufgabe: Agent Builder. Mehrstufige Logik: Coded Agent. Fremd-Framework: External Bridge.",
      "Low-Code ist schneller und besser governed, Code ist flexibler.", "Studio Web (Agent Builder) / Python-Projekt", E],
    ["understand", (x) => x.ai, "Antwortformat der KI",
      () => "Festes JSON-Schema (z. B. intent, fields, confidence, begründung). Antwort prüfen, bei Formatfehler 1× nachbessern lassen, sonst an einen Menschen.",
      "Freitext-Antworten lassen sich nicht sicher weiterverarbeiten.", "Agent-Definition / Wrapper-Logik", M],
    ["understand", (x) => x.ai, "Werkzeuge der KI",
      (x) => `Die KI darf nur lesen (z. B. Stammdaten nachschlagen). Das Schreiben in ${x.name} schlägt sie nur vor, ausgeführt wird es vom Performer.`,
      "Eine KI kann sich irren oder manipuliert werden. Sie darf deshalb nichts direkt verändern.", "Agent: Tools · Allowlist", M],
    ["understand", (x) => x.ai, "Guardrails",
      (x) => `Personenbezogene Daten maskieren und Prompt-Injection erkennen${x.suite ? ". Verfügbarkeit in der Suite prüfen, sonst eigene Maskierung" : ""}.`,
      "Schützt Daten und verhindert, dass Texte in Dokumenten die KI umprogrammieren.", "AI Trust Layer / Guardrails", M],
    ["understand", (x) => x.ai, "Testset für die KI",
      () => "Vor dem Go-Live 20–50 echte, anonymisierte Fälle mit bekanntem richtigem Ergebnis, inkl. Angriffs- und Grenzfällen.",
      "Nur so weiß man, wie gut die KI wirklich ist, und merkt, wenn eine Änderung sie verschlechtert.", "Testprojekt / Golden Set", M],

    // ---- 6 Regeln & Risikostufe -----------------------------------------------
    ["rules", () => true, "Risikostufe",
      (x) => ({ R0: "R0: vollautomatisch, 1 % Stichprobe", R1: "R1: automatisch, 5 % Stichprobe + Anomalie-Check", R2: "R2: Mensch gibt jede Änderung 1× frei", R3: "R3: zwei verschiedene Personen geben frei, bei Geld/Stammdaten mit Rückruf über einen zweiten Kanal" })[x.tier],
      "Je größer der mögliche Schaden, desto mehr menschliche Kontrolle.", "Prozessdesign / Regelwerk", M],
    ["rules", () => true, "Automatik-Grenzen",
      () => "Schriftlich festlegen, welche Fälle ohne Mensch durchlaufen dürfen (z. B. Betrag unter Grenze, alle Felder sicher erkannt, Datensatz existiert).",
      "Klare Grenzen machen die Automatik prüfbar und vertrauenswürdig.", "Fachkonzept, mit Compliance abgestimmt", M],
    ["rules", () => true, "Regeln außerhalb von Code und Prompt",
      () => "Grenzwerte und Regeln als versionierte Konfiguration (Asset, Konfigurationsdatei, Entscheidungstabelle). Änderung nur nach Freigabe.",
      "Regeln ändern sich öfter als Code. Im Prompt wären sie manipulierbar.", "Orchestrator Assets / Konfiguration / ggf. DMN", M],
    ["rules", () => true, "Prüfung vor dem Schreiben",
      (x) => `Vor jeder Änderung in ${x.name}: Existiert der Datensatz? Gibt es eine Dublette? Ist der Status erlaubt?`,
      "Verhindert Fehler, bevor sie im Zielsystem landen.", "Performer: Prüfschritt (lesend)", M],

    // ---- 7 Human in the Loop -------------------------------------------------
    ["hitl", (x) => x.hitlSet.size > 0, "Wie der Prozess auf Menschen wartet",
      (x) => `Orchestration Process (Long-running Workflow) mit aktivierter Persistenz: „Create Form Task“ + „Wait For Form Task And Resume“. Der Robot ist während der Wartezeit frei.${x.suite ? " Ob Maestro als Alternative verfügbar ist, in der Suite-Version prüfen." : x.cloud ? " Alternative: Maestro (User Task), falls lizenziert." : ""}`,
      "Ein Robot, der stundenlang auf eine Freigabe wartet, blockiert Lizenz und Maschine. Persistenz parkt den Prozess.", "Studio: Projekttyp Orchestration Process · Projekteinstellungen „Supports Persistence“", M],
    ["hitl", (x) => x.hitlSet.size > 0, "Aufgaben-Typ",
      (x) => [x.hitlSet.has("approval") && "Freigabe: Form Task", x.hitlSet.has("validation") && (x.docs ? "Daten prüfen: Document Validation Task" : "Daten prüfen: Form Task mit editierbaren Feldern"), x.hitlSet.has("exception") && "Ausnahme: Form Task „Klärung“ (oder External Task, wenn außerhalb UiPath bearbeitet)", x.hitlSet.has("sampling") && "Stichprobe: Form Task nach der Ausführung"].filter(Boolean).join(" · "),
      "Der Aufgabentyp bestimmt, was der Mensch sieht und tun kann.", "Action Center / Persistence-Activities in Studio", M],
    ["hitl", (x) => x.hitlSet.has("approval") || x.hitlSet.has("validation"), "Inhalt der Aufgabe (Entscheidungsgrundlage)",
      (x) => `Originaldaten bzw. Link zum Dokument, die geplante Änderung in ${x.name} (alt → neu), Begründung/Regel, Risikostufe und Frist.`,
      "Ein Mensch kann nur gut entscheiden, wenn er sieht, was genau passieren wird und warum.", "Form-Designer der Aufgabe", M],
    ["hitl", (x) => x.hitlSet.has("approval"), "Entscheidungsmöglichkeiten",
      () => "Freigeben / Ablehnen / Zurück zur Korrektur. Kommentar Pflicht bei Ablehnung.",
      "Klare Optionen statt Freitext machen das Ergebnis automatisch weiterverarbeitbar.", "Form-Designer (Buttons/Outcome)", E],
    ["hitl", (x) => x.hitlSet.size > 0, "Zuweisung",
      () => "Über einen Action Catalog an eine Gruppe (mit Vertretung), nicht an Einzelpersonen. Nie an den Ersteller des Vorgangs.",
      "Urlaub und Krankheit dürfen keine Aufgaben liegen lassen.", "Action Center: Action Catalogs / Zuweisung", M],
    ["hitl", (x) => x.hitlSet.size > 0, "Frist und Eskalation",
      (x) => `Frist je Risikostufe (Platzhalter: R1 4 h, R2 8 h, R3 24 h, aktuell ${x.tier}). Danach Erinnerung, dann Eskalation an Teamlead.`,
      "Ohne Frist liegen Aufgaben, und der Prozess steht.", "Task-Fälligkeit · Timer im Orchestration Process", M],
    ["hitl", (x) => x.tier === "R3", "Vier-Augen-Prinzip",
      () => "Zwei Freigaben durch zwei verschiedene Personen, eine davon mit Leitungsfunktion. Bei Geld/Stammdaten zusätzlich Rückruf über einen bekannten Kanal.",
      "Schützt gegen Fehler und Betrug, z. B. gefälschte Änderungsanfragen.", "Zwei Tasks nacheinander, Prüfung „anderer Bearbeiter“", M],
    ["hitl", (x) => x.hitlSet.has("approval"), "Schutz vor nachträglicher Änderung",
      () => "Prüfsumme (Hash) der freigegebenen Änderung speichern und vor dem Schreiben neu berechnen. Weicht sie ab → nicht ausführen.",
      "Garantiert, dass exakt das Freigegebene geschrieben wird.", "Performer: Hash-Vergleich vor dem Schreiben", (x) => (x.tier === "R3" ? M : E)],
    ["hitl", (x) => x.hitlSet.has("approval"), "Ablehnung sauber beenden",
      () => "Bei Ablehnung: nichts schreiben, Vorgang schließen, Absender bzw. Auftraggeber informieren, Grund protokollieren.",
      "Auch das Nein muss nachvollziehbar sein.", "Orchestration Process: Zweig „Abgelehnt“", E],

    // ---- 8 Zielsystem befüllen -------------------------------------------------
    ["target", () => true, "Zugriffsweg auf das Zielsystem",
      (x) => ({ api: `API von ${x.name} über Connector (Integration Service) oder HTTP-Request. Authentifizierung per OAuth/Token aus dem Credential Store.`, web: `UI-Automation im Browser mit Object Repository und robusten Selektoren. Vorher klären, ob ${x.name} nicht doch eine API hat.`, sapgui: "Besser BAPI/OData, falls vorhanden. Sonst SAP-GUI-Automation (SAP GUI Scripting muss serverseitig erlaubt sein).", desktop: "UI-Automation der Desktop-Anwendung mit Object Repository. Feste Bildschirmauflösung der Robot-Maschine.", citrix: "Bildbasierte Automation (Computer Vision). Fehleranfällig, deshalb eng überwachen und API-Alternative suchen.", unknown: `Zuerst klären: Hat ${x.name} eine API? Wenn ja, unbedingt nutzen.` })[x.target],
      "API ist schneller, stabiler und sicherer. Bildschirm-Automation bricht bei jeder Oberflächenänderung.", "Studio: Activities / Integration Service: Connections", M],
    ["target", (x) => x.ui, "Selektoren und Healing",
      () => "Object Repository nutzen, stabile Attribute statt Positionen. Healing Agent aktivieren, falls verfügbar.",
      "Robuste Selektoren reduzieren Ausfälle nach Updates des Zielsystems.", "Studio: Object Repository · Robot-Einstellungen", E],
    ["target", () => true, "Feld-Zuordnung",
      (x) => `Tabelle Quelle → Zielfeld in ${x.name} mit Formaten (Datum, Dezimaltrennzeichen), Pflichtfeldern, Längen und Wertelisten.`,
      "Die meisten Fehler beim Befüllen sind Formatfehler.", "Fachkonzept / Mapping-Dokument", M],
    ["target", () => true, "Kein Doppelt-Schreiben",
      (x) => `Vor dem Anlegen prüfen, ob der Datensatz schon existiert (z. B. über Referenzfeld = Idempotency-Key in ${x.name}). Bei API: Idempotenz-Schlüssel mitsenden, falls unterstützt.`,
      "Nach einem Timeout weiß der Robot nicht, ob es geklappt hat. Die Prüfung verhindert Dubletten bei der Wiederholung.", "Performer-Logik", M],
    ["target", () => true, "Nachprüfen (Read-back)",
      (x) => `Nach dem Schreiben den Datensatz in ${x.name} erneut lesen und mit dem Soll vergleichen.`,
      "Bestätigt, dass wirklich alles richtig angekommen ist.", "Performer: Verifikationsschritt", M],
    ["target", () => true, "Rückgängig machen",
      (x) => x.reversible ? `Storno- bzw. Korrekturweg in ${x.name} dokumentieren (wer, wie, wann).` : "Nicht rückgängig machbar → Freigabe vor dem Schreiben ist Pflicht, Risikostufe mindestens R2.",
      "Bei Fehlern muss klar sein, wie man sie wieder beseitigt.", "Fachkonzept / Runbook", M],
    ["target", () => true, "Ein Item = ein Datensatz",
      () => "Jedes Queue-Item erzeugt genau einen Datensatz bzw. eine Änderung.",
      "Fehler bleiben klein und einzeln wiederholbar.", "Performer-Design", E],
    ["target", (x) => x.ui || x.target === "unknown", "Gleichzeitige Bearbeitung",
      (x) => `Klären, ob Menschen denselben Datensatz in ${x.name} parallel bearbeiten (Sperren, Konflikte).`,
      "Gleichzeitiges Bearbeiten führt zu Fehlermeldungen oder überschriebenen Daten.", "Fachbereich / Systemverantwortliche", P],

    // ---- 9 Fehlerbehandlung ----------------------------------------------------
    ["errors", () => true, "Fachliche vs. technische Fehler",
      () => "Liste der fachlichen Fehler (Business Rule Exception: kein Retry, Mensch entscheidet) und der technischen Fehler (System Exception: Retry).",
      "Fachliche Fehler werden durch Wiederholen nicht besser, technische oft schon.", "REFramework: Process.xaml / Exception-Typen", M],
    ["errors", () => true, "Wer wiederholt was",
      (x) => [`System Exception beim Schreiben: Queue Auto-Retry (2–3×)`, x.ai && "KI-Timeout/Formatfehler: Agent-Wrapper (2× / 1× Repair)", x.ui && "Selektor fehlt: Healing Agent, danach System Exception", "Business Exception: niemand"].filter(Boolean).join(" · "),
      "Jeder Fehlertyp hat genau einen Besitzer. Sonst drohen Doppelbuchungen.", "Retry-Konzept (siehe Spickzettel „Wer kümmert sich?“)", M],
    ["errors", () => true, "Benachrichtigung",
      () => "Alarme bei fehlgeschlagenen Jobs, Queue-Stau und SLA-Risiko an Betrieb und Fachbereich.",
      "Fehler, die niemand bemerkt, werden zu Fristversäumnissen.", "Orchestrator: Alerts / Monitoring", E],
    ["errors", (x) => x.ui, "Screenshot bei Fehlern",
      (x) => `Screenshot bei System Exception für die Analyse${x.pii ? ", aber in geschütztem Speicher, weil personenbezogene Daten sichtbar sein können" : ""}.`,
      "Beschleunigt die Fehlersuche bei Bildschirm-Automation.", "REFramework: Fehlerbehandlung", E],

    // ---- 10 Logging, Audit, Monitoring ----------------------------------------
    ["logging", () => true, "Korrelations-ID",
      () => "Eine Vorgangs-ID in jeden Logeintrag schreiben (Dispatcher, Freigabe, Performer).",
      "So findet man alle Schritte eines Vorgangs über Prozessgrenzen hinweg.", "Studio: Add Log Fields", M],
    ["logging", () => true, "Keine sensiblen Daten in Logs",
      () => "Keine Passwörter, keine vollständigen IBANs oder Gesundheitsdaten loggen. Log-Level „Info“ in Produktion.",
      "Logs sind für viele lesbar und werden lange aufbewahrt.", "Studio: Log Message · Robot-Log-Level", M],
    ["logging", (x) => x.hitlSet.size > 0, "Nachweis der menschlichen Entscheidungen",
      () => "Wer hat wann was entschieden, mit welchem Kommentar, und was war die Entscheidungsgrundlage.",
      "Prüfer und Revision fragen genau danach.", "Action Center (Task-Historie) + eigene Protokollierung", M],
    ["logging", () => true, "Kennzahlen",
      () => "Durchsatz, Anteil ohne Mensch (STP), Freigabedauer, Fehlerquote, Dubletten. Als Dashboard aufbereiten.",
      "Zeigt, ob sich die Automatisierung lohnt und wo es hakt.", "Insights (falls verfügbar) / eigenes Reporting", E],

    // ---- 11 Test, Release, Betrieb ---------------------------------------------
    ["ops", () => true, "Testfälle",
      (x) => ["Normalfall", "fachlicher Fehler", "technischer Fehler mit Retry", "Duplikat", x.hitlSet.has("approval") && "Freigabe abgelehnt", x.hitlSet.size && "Frist abgelaufen", x.docs && "schlecht lesbares Dokument", x.ai && "Angriffstext im Inhalt"].filter(Boolean).join(" · "),
      "Die Ausnahmen sind der Normalfall des Betriebs. Sie müssen getestet sein.", "Test Manager / Testprojekt in Studio", M],
    ["ops", () => true, "Testdaten",
      () => "In Entwicklung und Test keine echten personenbezogenen Daten. Anonymisierte oder synthetische Daten nutzen.",
      "Datenschutz gilt auch im Test.", "Testdatenkonzept", M],
    ["ops", () => true, "Versionierung und Auslieferung",
      () => "Git-Anbindung, Pakete über Feeds, Weg Entwicklung → Test → Produktion mit Freigabe.",
      "Jede produktive Version ist nachvollziehbar und kann zurückgerollt werden.", "Studio: Source Control · Orchestrator: Packages · CI/CD", M],
    ["ops", () => true, "Plattform-Richtlinien",
      () => "Governance-Policies für Studio und Robots (z. B. erlaubte Pakete, Analyzer-Regeln) aktivieren.",
      "Verhindert unsichere Bausteine schon in der Entwicklung.", "Automation Ops: Governance Policies", E],
    ["ops", () => true, "Betriebshandbuch und Notbremse",
      () => "Wer betreibt, wer wird gerufen, was tun bei Störung. Notbremse: Trigger deaktivieren bzw. Queue-Verarbeitung anhalten, ohne Items zu löschen.",
      "Im Ernstfall muss jeder wissen, wie man stoppt, ohne Daten zu verlieren.", "Runbook · Orchestrator: Triggers deaktivieren", M],
    ["ops", () => true, "Verantwortliche benennen",
      () => "Process Owner (fachlich), Betrieb (technisch), Freigeber und Vertretung namentlich festlegen.",
      "Ohne Verantwortliche bleiben Fehler und Änderungen liegen.", "RACI (siehe Abschnitt 2.1)", M],
    ["ops", () => true, "Begleitete Startphase",
      () => "Nach dem Go-Live 2–4 Wochen mit erhöhter Stichprobe und täglicher Durchsicht.",
      "Die ersten echten Fälle zeigen, was im Test fehlte.", "Projektplan", E],

    // ---- 12 Datenschutz & Compliance -------------------------------------------
    ["privacy", (x) => x.pii || x.risk === "high", "Datenklassifizierung",
      () => "Für jedes Feld festlegen: öffentlich, intern, vertraulich oder streng vertraulich.",
      "Bestimmt, wo das Feld gespeichert, geloggt oder an eine KI gegeben werden darf.", "Datenschutzkonzept (Abschnitt 2.6)", M],
    ["privacy", (x) => x.pii, "Abstimmung mit dem Datenschutz",
      () => "Verarbeitungszweck, Rechtsgrundlage und Eintrag ins Verarbeitungsverzeichnis mit dem Datenschutz klären.",
      "Pflicht nach DSGVO bei personenbezogenen Daten.", "Datenschutzbeauftragte(r)", P],
    ["privacy", (x) => x.pii || x.risk === "high", "Aufbewahrung und Löschung",
      () => "Fristen für Queue-Items, Logs, Aufgaben und Dokumente festlegen und technisch umsetzen.",
      "Daten dürfen nicht länger als nötig liegen. Gesetzliche Aufbewahrungspflichten bleiben bestehen.", "Orchestrator-Aufbewahrung / Löschkonzept", M],
    ["privacy", (x) => x.ai, "KI-Kompetenz der Bearbeiter",
      () => "Bearbeiter kurz schulen: Was kann die KI, wo irrt sie, worauf achten sie bei der Freigabe?",
      "Vorgabe des EU AI Act (Art. 4). Und Freigaben sind nur so gut wie das Verständnis der Freigebenden.", "Schulung / dieser Spickzettel", E],
    ["privacy", (x) => x.ai && x.pii, "Pseudonymisierung vor der KI",
      () => "Namen, IBANs und Kontaktdaten vor dem KI-Aufruf durch Platzhalter ersetzen.",
      "Das Sprachmodell braucht für das Verständnis meist keine echten Personendaten.", "AI Trust Layer / eigene Maskierung", M],
  ];

  const SECTIONS = {
    env: "1 · Umgebung & Infrastruktur", iam: "2 · Identität, Rechte, Zugangsdaten", intake: "3 · Auslöser & Eingang (Dispatcher)",
    queue: "4 · Queue-Design", understand: "5 · Daten verstehen (Dokumente / KI)", rules: "6 · Regeln & Risikostufe",
    hitl: "7 · Human in the Loop (Action Center)", target: "8 · Zielsystem befüllen (Performer)", errors: "9 · Fehlerbehandlung",
    logging: "10 · Logging, Audit, Kennzahlen", ops: "11 · Test, Auslieferung, Betrieb", privacy: "12 · Datenschutz & Compliance",
  };

  function steps(x) {
    const s = [];
    s.push(["Auslöser", OPTIONS.trigger[x.trigger], "det"]);
    s.push(["Dispatcher", "Eingang lesen, Idempotency-Key bilden, Queue-Item anlegen (eindeutige Referenz)", "det"]);
    if (x.docs) s.push(["Dokumentenerkennung", "Digitalisieren, Typ erkennen, Felder mit Konfidenz auslesen", "prob"]);
    if (x.docs || x.hitlSet.has("validation")) s.push(["Mensch: Daten prüfen", "nur bei unsicheren Feldern (Validation Task)", x.hitlSet.has("validation") || x.docs ? "human" : "det"]);
    if (x.ai) s.push(["KI-Agent", "Inhalt verstehen und einen strukturierten Vorschlag machen (schreibt nichts)", "prob"]);
    s.push(["Regeln & Prüfung", `Plausibilität, Dubletten, Risikostufe ${x.tier}`, "det"]);
    if (x.hitlSet.has("approval")) s.push(["Mensch: Freigabe", x.tier === "R3" ? "zwei verschiedene Personen (4 Augen), ggf. Rückruf" : "eine Person gibt frei oder lehnt ab", "human"]);
    s.push([`${x.name} befüllen`, `Performer schreibt über ${x.target === "api" ? "API" : x.ui ? "Bildschirm-Automation" : "API (zu klären)"}, vorher Hash-/Dublettenprüfung`, "det"]);
    s.push(["Nachprüfen", "Datensatz erneut lesen und mit dem Soll vergleichen", "det"]);
    if (x.hitlSet.has("exception")) s.push(["Mensch: Ausnahmen", "fachliche Fehler werden als Klärungsaufgabe zugewiesen", "human"]);
    if (x.hitlSet.has("sampling") || x.tier === "R1" || x.tier === "R0") s.push(["Mensch: Stichprobe", `${x.tier === "R0" ? "1" : "5"} % der automatischen Fälle nachkontrollieren`, "human"]);
    s.push(["Abschluss", "Protokoll, Rückmeldung, Kennzahlen", "det"]);
    return s;
  }

  function decisions(x) {
    const d = [];
    d.push(["Wer orchestriert?", x.hitlSet.size ? `Orchestration Process mit Persistenz${x.cloud ? " oder Maestro" : x.suite ? " (Maestro-Verfügbarkeit prüfen)" : ""}` : "Normaler Prozess (REFramework)", x.hitlSet.size ? "Wegen der Wartezeit auf Menschen darf der Robot nicht blockieren." : "Ohne Wartezeiten reicht ein klassischer Ablauf."]);
    d.push(["Brauche ich KI?", x.ai ? "Ja, aber nur für das Verstehen und Vorschlagen" : x.docs ? "Nur Dokumentenerkennung, kein Agent" : "Nein, feste Regeln reichen", x.ai ? "Freitext oder wechselnde Inhalte müssen interpretiert werden." : "Regeln sind billiger, schneller und vorhersagbar."]);
    d.push(["Wie schreibe ich in " + x.name + "?", x.target === "api" ? "API" : x.ui ? "Bildschirm-Automation (Fallback)" : "Noch offen: API klären!", "API ist stabiler und schneller (Prinzip P8)."]);
    d.push(["Wie viel Kontrolle?", `Risikostufe ${x.tier}`, OPTIONS.risk[x.risk] + (x.reversible ? "" : ", nicht rückgängig machbar")]);
    d.push(["Wie viele Robots?", `${x.robots} + 1 Reserve`, `${x.volume}/Tag, Spitze ×${x.peak}, ${x.aht} min je Vorgang, ${x.window} h Betriebsfenster`]);
    return d;
  }

  function openQuestions(x) {
    const q = [];
    if (x.envUnknown) q.push("Automation Cloud oder Automation Suite?");
    if (x.target === "unknown") q.push(`Hat ${x.name} eine API (REST/OData/BAPI)? Welche Oberfläche sonst?`);
    if (!x.name || x.name === "System XY") q.push("Wie heißt das Zielsystem genau, und wer ist dort verantwortlich?");
    if (x.risk === "high" && !x.hitlSet.has("approval")) q.push("⚠ Hohes Risiko, aber keine Freigabe vor dem Schreiben vorgesehen. Ist das bewusst so?");
    if (!x.reversible && !x.hitlSet.has("approval")) q.push("⚠ Nicht rückgängig machbar, aber ohne Freigabe. Bitte prüfen.");
    if (x.input === "email" && !x.ai) q.push("Sind die Mails immer gleich aufgebaut (Vorlage) oder Freitext? Bei Freitext „KI muss Inhalte verstehen“ ankreuzen.");
    if (x.suite) q.push("Welche Suite-Version ist installiert, und welche Produkte sind aktiviert/lizenziert?");
    if (x.ai && x.suite) q.push("Welches Sprachmodell ist aus dem Rechenzentrum erlaubt und erreichbar?");
    if (x.hitlSet.size) q.push("Wer sind die Bearbeiter, wer die Vertretung, welche Fristen gelten?");
    q.push("Welche Felder sind Pflicht, und welche Formate erwartet das Zielsystem?");
    q.push("Wer ist Process Owner, und wer betreibt den Prozess?");
    return q;
  }

  function antiPatterns(x) {
    const a = [
      ["Retries in Queue und REFramework gleichzeitig", "führt zu doppelten Einträgen im Zielsystem"],
      ["Zugangsdaten im Workflow oder in Excel-Configs", "landen in Paketen, Logs und Git"],
      ["Kein eindeutiger Schlüssel je Vorgang", "Wiederholungen erzeugen Dubletten"],
    ];
    if (x.hitlSet.size) {
      a.push(["Freigabe per E-Mail-Antwort oder Message Box", "kein sauberer Nachweis, keine Fristen, keine Zuweisung"]);
      a.push(["Robot wartet blockierend auf den Menschen", "Lizenz und Maschine sind stundenlang belegt"]);
      a.push(["Freigeber = Ersteller", "das Vier-Augen-Prinzip ist dann wirkungslos"]);
    }
    if (x.ai) a.push(["KI schreibt direkt ins Zielsystem", "Irrtum oder Manipulation wird sofort wirksam"]);
    if (x.ui) a.push(["UI-Automation, obwohl eine API existiert", "instabil bei jedem Update der Oberfläche"]);
    return a;
  }

  function build(input) {
    const x = derive({ ...DEFAULTS, ...input });
    const items = CATALOG.filter(([, cond]) => cond(x)).map(([sec, , p, v, why, where, lvl], i) => ({
      id: `${sec}-${i}`, sec, p, v: typeof v === "function" ? v(x) : v, why, where: where.replace("Zielsystem", x.name),
      lvl: typeof lvl === "function" ? lvl(x) : lvl,
    }));
    return { x, items, steps: steps(x), decisions: decisions(x), questions: openQuestions(x), anti: antiPatterns(x) };
  }

  // =====================================================================
  // 3. Export als Markdown
  // =====================================================================
  function toMarkdown(bp) {
    const { x } = bp;
    const L = [];
    L.push(`# Blueprint: ${x.name} befüllen${x.hitlSet.size ? " mit Human in the Loop" : ""}`, "");
    if (x.goal) L.push(`> ${x.goal}`, "");
    L.push(`**Umgebung:** ${OPTIONS.env[x.env]} · **Risikostufe:** ${x.tier} · **Robots:** ${x.robots} + 1 Reserve`, "");
    L.push("## Ablauf", "");
    bp.steps.forEach(([n, d, k], i) => L.push(`${i + 1}. **${n}** (${{ det: "deterministisch", prob: "KI/ML", human: "Mensch" }[k]}): ${d}`));
    L.push("", "## Entscheidungen", "", "| Frage | Empfehlung | Warum |", "|---|---|---|");
    bp.decisions.forEach((d) => L.push(`| ${d.join(" | ")} |`));
    Object.entries(SECTIONS).forEach(([k, title]) => {
      const it = bp.items.filter((i) => i.sec === k);
      if (!it.length) return;
      L.push("", `## ${title}`, "", "| ✓ | Parameter | Empfehlung | Warum | Wo einstellen | Stufe |", "|---|---|---|---|---|---|");
      it.forEach((i) => L.push(`| ☐ | ${i.p} | ${i.v} | ${i.why} | ${i.where} | ${i.lvl} |`));
    });
    L.push("", "## Offene Fragen", "", ...bp.questions.map((q) => `- ${q}`));
    L.push("", "## Typische Fehler vermeiden", "", ...bp.anti.map(([a, b]) => `- **${a}**: ${b}`));
    L.push("", "_Erzeugt mit dem AEAP Workflow-Planer. Regelbasierte Empfehlung; Produktverfügbarkeit und Grenzwerte im Projekt prüfen._");
    return L.join("\n");
  }

  // =====================================================================
  // 4. Oberfläche
  // =====================================================================
  function load() { try { return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(STORE_KEY) || "{}") }; } catch { return { ...DEFAULTS }; } }
  function save(s) { try { localStorage.setItem(STORE_KEY, JSON.stringify(s)); } catch { /* ignore */ } }
  function loadDone() { try { return JSON.parse(localStorage.getItem(DONE_KEY) || "{}"); } catch { return {}; } }
  function saveDone(d) { try { localStorage.setItem(DONE_KEY, JSON.stringify(d)); } catch { /* ignore */ } }

  const sel = (key, val) => `<select data-pl="${key}">${Object.entries(OPTIONS[key]).map(([k, l]) => `<option value="${k}" ${k === val ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>`;
  const lvlPill = (l) => `<span class="pill ${l === M ? "bad" : l === P ? "warn" : "info"}">${l}</span>`;
  const kindPill = { det: `<span class="kb-tag">deterministisch</span>`, prob: `<span class="kb-tag prob">KI / ML</span>`, human: `<span class="kb-tag human">Mensch</span>` };

  window.renderPlanner = function () {
    const s = load();
    return `
      <div class="kb-lead">Beschreibe dein Vorhaben in einem Satz. Der Planer erkennt die Eckdaten und erstellt einen <b>Blueprint mit allen Parametern</b>, die du bedenken solltest, jeweils mit Begründung in Klartext und dem Ort, wo man sie in UiPath einstellt.</div>
      <div class="pl-input">
        <textarea id="pl-goal" rows="3" placeholder="z. B.: Ich möchte in der Automation Suite einen Workflow bauen, der Lieferantenstammdaten aus E-Mails in SAP befüllt und vor dem Speichern eine Freigabe durch einen Menschen braucht. Ca. 150 pro Tag.">${esc(s.goal)}</textarea>
        <div class="row"><button class="btn primary" id="pl-analyze">Analysieren &amp; Blueprint erstellen</button>
          <button class="btn sm" id="pl-example">Beispiel einsetzen</button><button class="btn sm ghost" id="pl-reset">Zurücksetzen</button>
          <span class="small muted" id="pl-detected"></span></div>
      </div>
      <details class="pl-form" open><summary>Eckdaten prüfen und anpassen</summary>
        <div class="form-grid" style="margin-top:10px">
          <label class="field">Zielsystem (Name)<input data-pl="name" value="${esc(s.name)}" placeholder="z. B. SAP, Salesforce, XY"></label>
          <label class="field">Umgebung ${sel("env", s.env)}</label>
          <label class="field">Zugriff auf das Zielsystem ${sel("target", s.target)}</label>
          <label class="field">Eingangsdaten ${sel("input", s.input)}</label>
          <label class="field">Auslöser ${sel("trigger", s.trigger)}</label>
          <label class="field">Risiko bei Fehlern ${sel("risk", s.risk)}</label>
          <label class="field">Vorgänge pro Tag<input data-pl="volume" type="number" min="1" value="${s.volume}"></label>
          <label class="field">Spitzenfaktor<input data-pl="peak" type="number" min="1" step="0.5" value="${s.peak}"></label>
          <label class="field">Minuten Robot-Zeit je Vorgang<input data-pl="aht" type="number" min="0.1" step="0.5" value="${s.aht}"></label>
          <label class="field">Betriebsfenster (h/Tag)<input data-pl="window" type="number" min="1" max="24" value="${s.window}"></label>
        </div>
        <div class="row" style="margin-top:10px;gap:18px">
          <span class="small"><b>Human in the Loop:</b></span>
          ${Object.entries(OPTIONS.hitl).map(([k, l]) => `<label class="row small" style="gap:5px"><input type="checkbox" data-pl-hitl="${k}" ${s.hitl.includes(k) ? "checked" : ""}>${esc(l)}</label>`).join("")}
        </div>
        <div class="row" style="margin-top:8px;gap:18px">
          <label class="row small" style="gap:5px"><input type="checkbox" data-pl="ai" ${s.ai ? "checked" : ""}>KI muss Inhalte verstehen</label>
          <label class="row small" style="gap:5px"><input type="checkbox" data-pl="pii" ${s.pii ? "checked" : ""}>personenbezogene Daten</label>
          <label class="row small" style="gap:5px"><input type="checkbox" data-pl="reversible" ${s.reversible ? "checked" : ""}>Änderung rückgängig machbar</label>
        </div>
      </details>
      <div id="pl-out"></div>`;
  };

  function renderOut(root) {
    const s = load();
    const bp = build(s);
    const { x } = bp;
    const done = loadDone();
    const total = bp.items.length, must = bp.items.filter((i) => i.lvl === M).length;
    const doneN = bp.items.filter((i) => done[i.p]).length;
    root.querySelector("#pl-out").innerHTML = `
      <div class="pl-head">
        <div><div class="kb-label">Blueprint</div><h2 class="kb-h" style="margin:2px 0 6px">${esc(x.name)} befüllen${x.hitlSet.size ? " mit Human in the Loop" : ""}</h2>
          <div class="row" style="gap:6px">
            <span class="pill info">${esc(OPTIONS.env[x.env])}</span><span class="tier ${x.tier}">${x.tier}</span>
            <span class="pill">Zugriff: ${esc(OPTIONS.target[x.target])}</span>
            <span class="pill">${x.robots} Robot(s) + 1 Reserve</span>
            <span class="pill">${total} Parameter · ${must} Pflicht</span>
          </div></div>
        <div class="pl-actions"><div class="small muted" style="text-align:right">${doneN}/${total} erledigt</div>
          <div class="pl-progress"><div style="width:${total ? (doneN / total) * 100 : 0}%"></div></div>
          <div class="row" style="justify-content:flex-end;margin-top:6px"><button class="btn sm" id="pl-copy">Als Markdown kopieren</button><button class="btn sm" id="pl-dl">.md herunterladen</button><button class="btn sm" id="pl-print">Drucken</button></div></div>
      </div>

      <h3 class="kb-h">Empfohlener Ablauf</h3>
      <ol class="pl-steps">${bp.steps.map(([n, d, k]) => `<li class="k-${k}"><b>${esc(n)}</b> ${kindPill[k]}<div class="small">${esc(d)}</div></li>`).join("")}</ol>

      <h3 class="kb-h">Die wichtigsten Entscheidungen</h3>
      <div class="kb-table-wrap"><table class="kb-table"><thead><tr><th>Frage</th><th>Empfehlung</th><th>Warum</th></tr></thead>
        <tbody>${bp.decisions.map(([a, b, c]) => `<tr><td><b>${esc(a)}</b></td><td>${esc(b)}</td><td class="small">${esc(c)}</td></tr>`).join("")}</tbody></table></div>

      <h3 class="kb-h">Alle Parameter <span class="small muted" style="font-weight:400">· Haken setzen, was erledigt ist (wird im Browser gemerkt)</span></h3>
      <div class="row small" style="gap:12px;margin-bottom:8px">${lvlPill(M)} muss entschieden/umgesetzt sein ${lvlPill(E)} Best Practice ${lvlPill(P)} im Projekt klären/verifizieren</div>
      ${Object.entries(SECTIONS).map(([k, title]) => {
        const it = bp.items.filter((i) => i.sec === k);
        if (!it.length) return "";
        return `<details class="pl-sec" open><summary><b>${esc(title)}</b> <span class="small muted">${it.filter((i) => done[i.p]).length}/${it.length}</span></summary>
          <div class="kb-table-wrap"><table class="kb-table pl-table"><thead><tr><th style="width:34px"></th><th style="width:20%">Parameter</th><th>Empfehlung</th><th style="width:24%">Warum (Klartext)</th><th style="width:17%">Wo einstellen</th><th style="width:90px">Stufe</th></tr></thead>
          <tbody>${it.map((i) => `<tr class="${done[i.p] ? "pl-done" : ""}"><td><input type="checkbox" data-done="${esc(i.p)}" ${done[i.p] ? "checked" : ""}></td>
            <td><b>${esc(i.p)}</b></td><td>${esc(i.v)}</td><td class="small">${esc(i.why)}</td><td class="small mono-ish">${esc(i.where)}</td><td>${lvlPill(i.lvl)}</td></tr>`).join("")}</tbody></table></div></details>`;
      }).join("")}

      <div class="grid g2" style="margin-top:18px">
        <div class="kb-card"><div class="kb-card-who">❓ Offene Fragen klären</div><ul>${bp.questions.map((q) => `<li>${esc(q)}</li>`).join("")}</ul></div>
        <div class="kb-card"><div class="kb-card-who">⛔ Typische Fehler vermeiden</div><ul>${bp.anti.map(([a, b]) => `<li><b>${esc(a)}</b>: ${esc(b)}</li>`).join("")}</ul></div>
      </div>
      <div class="kb-note" style="margin-top:14px">💡 Der Planer ist regelbasiert und liefert für gleiche Eingaben immer dieselbe Empfehlung. Punkte mit „Prüfen“ hängen von eurer Version, Lizenz oder Organisation ab. Begriffe erklärt das ${T("Action Center", "Glossar")}.</div>
      ${assistantPanel()}`;

    root.querySelectorAll("[data-done]").forEach((c) => c.onchange = () => {
      const d = loadDone(); if (c.checked) d[c.dataset.done] = true; else delete d[c.dataset.done]; saveDone(d); renderOut(root);
    });
    const md = () => toMarkdown(build(load()));
    root.querySelector("#pl-copy").onclick = async (e) => {
      try { await navigator.clipboard.writeText(md()); e.target.textContent = "Kopiert ✓"; } catch { e.target.textContent = "Kopieren nicht erlaubt"; }
      setTimeout(() => { e.target.textContent = "Als Markdown kopieren"; }, 1800);
    };
    root.querySelector("#pl-dl").onclick = () => {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([md()], { type: "text/markdown" }));
      a.download = `Blueprint-${(load().name || "XY").replace(/\W+/g, "_")}.md`;
      a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    };
    root.querySelector("#pl-print").onclick = () => window.print();
    initAssistant(root);
  }

  function readForm(root) {
    const s = load();
    root.querySelectorAll("[data-pl]").forEach((el) => {
      const k = el.dataset.pl;
      s[k] = el.type === "checkbox" ? el.checked : el.type === "number" ? Number(el.value) : el.value;
    });
    s.hitl = [...root.querySelectorAll("[data-pl-hitl]")].filter((c) => c.checked).map((c) => c.dataset.plHitl);
    s.goal = root.querySelector("#pl-goal").value;
    return s;
  }

  window.bindPlanner = function (root, rerender) {
    if (!root.querySelector("#pl-goal")) return;
    root.querySelectorAll("[data-pl], [data-pl-hitl]").forEach((el) => el.onchange = () => { save(readForm(root)); renderOut(root); });
    root.querySelector("#pl-analyze").onclick = () => {
      const goal = root.querySelector("#pl-goal").value;
      const { found, notes } = analyze(goal);
      save({ ...DEFAULTS, ...found, goal });
      rerender();
      const det = root.querySelector("#pl-detected");
      if (det) det.textContent = notes.length ? "Erkannt: " + notes.join(" · ") : "Nichts Konkretes erkannt. Bitte die Eckdaten unten auswählen.";
      root.querySelector("#pl-out")?.scrollIntoView({ behavior: "smooth", block: "start" });
    };
    root.querySelector("#pl-example").onclick = () => {
      root.querySelector("#pl-goal").value = "Ich möchte in der Automation Suite einen Workflow bauen, der Lieferantenstammdaten aus E-Mails in SAP befüllt und vor dem Speichern eine Freigabe durch einen Menschen braucht. Ca. 150 pro Tag.";
      root.querySelector("#pl-analyze").click();
    };
    root.querySelector("#pl-reset").onclick = () => { save({ ...DEFAULTS }); saveDone({}); rerender(); };
    renderOut(root);
  };

  // =====================================================================
  // 5. KI-Assistent (optional, nur in der laufenden App)
  // =====================================================================
  const chat = [];
  let chatBusy = false;

  function inline(s) {
    return s.replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<i>$2</i>");
  }

  function mdToHtml(md) {
    const lines = esc(md).split(/\r?\n/);
    const out = [];
    let list = null, para = [], inCode = false, code = [], table = [];
    const flushPara = () => { if (para.length) { out.push(`<p>${inline(para.join(" "))}</p>`); para = []; } };
    const flushList = () => { if (list) { out.push(`<${list.t}>${list.items.map((i) => `<li>${inline(i)}</li>`).join("")}</${list.t}>`); list = null; } };
    const flushTable = () => {
      if (!table.length) return;
      const rows = table.filter((r) => !/^\|?\s*:?-{2,}/.test(r)).map((r) => r.replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim())));
      out.push(`<div class="kb-table-wrap"><table class="kb-table"><thead><tr>${rows[0].map((c) => `<th>${c}</th>`).join("")}</tr></thead><tbody>${rows.slice(1).map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
      table = [];
    };
    const flushAll = () => { flushPara(); flushList(); flushTable(); };
    for (const raw of lines) {
      const line = raw.trimEnd();
      if (/^```/.test(line)) { if (inCode) { out.push(`<pre class="json" style="border-radius:8px">${code.join("\n")}</pre>`); code = []; inCode = false; } else { flushAll(); inCode = true; } continue; }
      if (inCode) { code.push(raw); continue; }
      if (/^\s*\|.*\|\s*$/.test(line)) { flushPara(); flushList(); table.push(line.trim()); continue; } else flushTable();
      const h = line.match(/^(#{1,4})\s+(.*)$/);
      if (h) { flushAll(); out.push(`<h${Math.min(6, h[1].length + 2)} class="pl-md-h">${inline(h[2])}</h${Math.min(6, h[1].length + 2)}>`); continue; }
      const ul = line.match(/^\s*[-*•]\s+(.*)$/), ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
      if (ul || ol) {
        flushPara();
        const t = ul ? "ul" : "ol";
        if (!list || list.t !== t) { flushList(); list = { t, items: [] }; }
        list.items.push((ul || ol)[1]);
        continue;
      }
      if (!line.trim()) { flushAll(); continue; }
      if (list && /^\s{2,}/.test(raw)) { list.items[list.items.length - 1] += " " + line.trim(); continue; }
      flushList();
      para.push(line.trim());
    }
    if (inCode) out.push(`<pre class="json">${code.join("\n")}</pre>`);
    flushAll();
    return out.join("");
  }

  function assistantPanel() {
    return `<div class="pl-ai"><h3 class="kb-h">🤖 KI-Assistent <span class="small muted" style="font-weight:400">optional · erklärt den Blueprint und beantwortet Rückfragen</span></h3>
      <div id="pl-ai-body"><div class="muted small">Prüfe Verbindung …</div></div></div>`;
  }

  function setupHelp(st) {
    return `<div class="callout info" style="display:grid;gap:8px">
      <div><b>Noch kein KI-Schlüssel hinterlegt.</b> Der regelbasierte Blueprint oben funktioniert trotzdem vollständig. Für den Assistenten eine der drei Varianten wählen und danach „Erneut prüfen“ klicken:</div>
      ${st && st.keySource ? `<div class="small"><b>Hinweis:</b> ${esc(st.keySource)}</div>` : ""}
      <div class="small"><b>A) Auf vorhandene Datei verweisen (Schlüssel wird nicht kopiert).</b> Im Projektordner eine Datei <code>.env</code> anlegen mit:</div>
      <pre class="json" style="border-radius:6px">AEAP_LLM_API_KEY_FILE=C:\\Pfad\\zu\\secrets\\llm.env
AEAP_LLM_API_KEY_VAR=MOONSHOT_API_KEY</pre>
      <div class="small"><b>B) Schlüssel direkt in <code>.env</code></b> im Projektordner: <code>MOONSHOT_API_KEY=…</code> (oder <code>OPENAI_API_KEY</code>, <code>DEEPSEEK_API_KEY</code>, <code>OPENROUTER_API_KEY</code>)</div>
      <div class="small"><b>C) Umgebungsvariable</b> vor dem Start setzen, z. B. in PowerShell: <code>$env:MOONSHOT_API_KEY="…"; python app.py</code></div>
      <div class="small muted">Optional: <code>AEAP_LLM_MODEL</code> (sonst automatische Auswahl), <code>AEAP_LLM_BASE_URL</code> (z. B. https://api.moonshot.cn/v1). Vorlage: <code>.env.example</code>. Die <code>.env</code> wird nie angezeigt oder übertragen.</div>
      <div><button class="btn sm" id="pl-ai-recheck">Erneut prüfen</button></div>
    </div>`;
  }

  function renderThread() {
    return chat.map((m) => m.role === "user"
      ? `<div class="pl-msg user"><div class="pl-msg-who">Du</div><div>${esc(m.content).replace(/\n/g, "<br>")}</div></div>`
      : `<div class="pl-msg bot ${m.error ? "err" : ""}"><div class="pl-msg-who">KI-Assistent${m.meta ? ` <span class="muted small" style="font-weight:400">${esc(m.meta)}</span>` : ""}</div><div class="pl-md">${m.error ? esc(m.content) : mdToHtml(m.content)}</div></div>`).join("")
      + (chatBusy ? `<div class="pl-msg bot"><div class="pl-msg-who">KI-Assistent</div><div class="muted">denkt nach … (kann 10–40 Sekunden dauern)</div></div>` : "");
  }

  async function initAssistant(root) {
    const body = root.querySelector("#pl-ai-body");
    if (!body) return;
    if (window.AEAP_KB_STANDALONE) {
      body.innerHTML = `<div class="callout info small">Der KI-Assistent ist nur in der laufenden App verfügbar (<code>start.bat</code>). Der regelbasierte Blueprint oben funktioniert auch hier vollständig.</div>`;
      return;
    }
    let st;
    try {
      const r = await fetch("/api/assistant/status");
      st = await r.json();
    } catch {
      body.innerHTML = `<div class="callout warn small">Server nicht erreichbar.</div>`;
      return;
    }
    if (!st.configured) {
      body.innerHTML = setupHelp(st);
      body.querySelector("#pl-ai-recheck").onclick = () => initAssistant(root);
      return;
    }
    const goal = load().goal || "";
    body.innerHTML = `
      <div class="small muted" style="margin-bottom:8px">Verbunden: <b>${esc(st.provider)}</b>${st.model ? ` · Modell <code>${esc(st.model)}</code>` : " · Modell wird automatisch gewählt"} · Schlüsselquelle: ${esc(st.keySource)}</div>
      <div class="callout warn small" style="margin-bottom:10px">Externer Dienst: Frage und Blueprint werden an ${esc(st.provider)} gesendet. IBAN, E-Mail und Telefonnummern werden vorher maskiert, jeder Aufruf steht im Audit. Bitte keine vertraulichen Inhalte eingeben. KI-Antworten können Fehler enthalten. Verbindlich ist der regelbasierte Blueprint.</div>
      <div class="pl-thread" id="pl-thread">${renderThread()}</div>
      <textarea id="pl-ai-q" rows="3" placeholder="Frage an den Assistenten, z. B.: Wie richte ich die Freigabe im Action Center konkret ein?">${chat.length ? "" : esc(goal)}</textarea>
      <div class="row" style="margin-top:6px">
        <button class="btn primary" id="pl-ai-send" ${chatBusy ? "disabled" : ""}>${chat.length ? "Senden" : "Blueprint erklären lassen"}</button>
        <button class="btn sm ghost" id="pl-ai-clear" ${chat.length ? "" : "disabled"}>Verlauf leeren</button>
        <span class="spacer"></span><span class="small muted">Strg+Enter sendet</span>
      </div>`;
    const send = async () => {
      const ta = root.querySelector("#pl-ai-q");
      const q = ta.value.trim() || (chat.length ? "" : `Erkläre mir den Blueprint für: ${load().name || "das Vorhaben"}`);
      if (!q || chatBusy) return;
      chat.push({ role: "user", content: q });
      chatBusy = true;
      initAssistant(root);
      let user = null;
      try { user = localStorage.getItem("aeap.user"); } catch { /* ignore */ }
      try {
        const r = await fetch("/api/assistant", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ messages: chat.filter((m) => !m.error).map(({ role, content }) => ({ role, content })), context: toMarkdown(build(load())), user }),
        });
        const data = await r.json();
        if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
        const masked = Object.values(data.masked || {}).reduce((a, b) => a + b, 0);
        chat.push({ role: "assistant", content: data.answer,
          meta: `${data.model} · ${data.usage?.total_tokens ?? "?"} Tokens${masked ? ` · ${masked} Angabe(n) maskiert` : ""}${data.injection?.length ? " · ⚠ Injection-Muster erkannt" : ""}` });
      } catch (e) {
        chat.push({ role: "assistant", content: "Fehler: " + e.message, error: true });
      }
      chatBusy = false;
      await initAssistant(root);
      root.querySelector("#pl-thread")?.lastElementChild?.scrollIntoView({ behavior: "smooth", block: "start" });
    };
    root.querySelector("#pl-ai-send").onclick = send;
    root.querySelector("#pl-ai-q").onkeydown = (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) send(); };
    root.querySelector("#pl-ai-clear").onclick = () => { chat.length = 0; initAssistant(root); };
  }

  // Für Tests / Konsole
  window.AEAP_PLANNER = { analyze, build, toMarkdown, mdToHtml, DEFAULTS };
})();
