"""Tests der deterministischen Garantien aus SDD Abschnitt 1.  Ausfuehren: python -m unittest -v"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aeap.contracts import IllegalTransition, approval_hash, check_transition, iban_valid  # noqa: E402
from aeap.governance import Forbidden  # noqa: E402
from aeap.governance_ops import GovernanceError  # noqa: E402
from aeap.platform import Platform, PlatformError  # noqa: E402
from aeap.scenarios import random_mail, scenario_mail  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Platform(os.path.join(self.tmp.name, "t.db"), seed_value=42)
        # Stichproben fuer deterministische Tests abschalten
        pol = self.p.store.get_setting("policy")
        pol["sampling"] = {"R0": 0.0, "R1": 0.0}
        self.p.store.set_setting("policy", pol)

    def tearDown(self):
        self.p.store.conn.close()
        self.tmp.cleanup()

    def send(self, sid, **kw):
        return self.p.ingest(scenario_mail(sid, **kw))

    def state(self, cid):
        return self.p.case_view(cid)["case"]["state"]

    def open_task(self, cid):
        return [t for t in self.p.case_view(cid)["tasks"] if t["status"] == "PENDING"][0]


class ContractTests(unittest.TestCase):
    def test_state_machine_rejects_illegal(self):
        check_transition("APPROVED", "EXECUTING")
        with self.assertRaises(IllegalTransition):
            check_transition("DECIDED", "EXECUTING")

    def test_hash_is_canonical(self):
        a = [{"tool": "x", "args": {"b": 1, "a": 2}}]
        b = [{"args": {"a": 2, "b": 1}, "tool": "x"}]
        self.assertEqual(approval_hash("c", "k", a), approval_hash("c", "k", b))
        self.assertNotEqual(approval_hash("c", "k", a), approval_hash("c", "k", [{"tool": "x", "args": {"a": 3, "b": 1}}]))

    def test_iban(self):
        self.assertTrue(iban_valid("DE89 3704 0044 0532 0130 00"))
        self.assertFalse(iban_valid("DE00370400440532013000"))


class RoutingTests(Base):
    def test_r1_auto_closes(self):
        r = self.send("invoice-standard")
        self.assertEqual(self.state(r["caseId"]), "CLOSED")
        env = self.p.case_view(r["caseId"])["case"]["envelope"]
        self.assertEqual(env["policy"]["riskTier"], "R1")
        self.assertEqual(env["execution"]["result"], "VERIFIED")

    def test_r0_general(self):
        r = self.send("general")
        self.assertEqual(self.state(r["caseId"]), "CLOSED")

    def test_r2_over_limit_requires_approval(self):
        r = self.send("invoice-large")
        self.assertEqual(self.state(r["caseId"]), "APPROVAL_PENDING")
        self.p.approve(self.open_task(r["caseId"])["task_id"], "a.becker")
        self.assertEqual(self.state(r["caseId"]), "CLOSED")

    def test_deny(self):
        r = self.send("complaint")
        self.p.deny(self.open_task(r["caseId"])["task_id"], "a.becker", "nein")
        self.assertEqual(self.state(r["caseId"]), "CLOSED_DENIED")

    def test_injection_rejected_by_allowlist(self):
        r = self.send("injection")
        env = self.p.case_view(r["caseId"])["case"]["envelope"]
        self.assertEqual(self.state(r["caseId"]), "REJECTED_POLICY")
        self.assertIn("R-001 TOOL_ALLOWLIST", env["policy"]["rulesFired"])
        self.assertTrue(env["agent"]["guardrails"]["promptInjection"])

    def test_injection_escalated_when_tool_absent(self):
        # Ohne Tool-Nennung greift der Guardrail-Treffer (R-003)
        mail = scenario_mail("injection")
        mail["attachments"][0] = dict(mail["attachments"][0], content=mail["attachments"][0]["content"].replace("payments.release_payment", "die Zahlung"))
        r = self.p.ingest(mail)
        self.assertEqual(self.state(r["caseId"]), "ESCALATED")

    def test_blocked_vendor_escalated(self):
        r = self.send("blocked-vendor")
        self.assertEqual(self.state(r["caseId"]), "ESCALATED")

    def test_validation_station(self):
        r = self.send("low-quality")
        self.assertEqual(self.state(r["caseId"]), "VALIDATION_PENDING")
        t = self.open_task(r["caseId"])
        d = t["payload"]["documents"][0]
        self.p.validate(t["task_id"], "j.yilmaz", [{"docId": d["docId"], "type": "Invoice", "fields": d["fields"]}])
        self.assertEqual(self.state(r["caseId"]), "CLOSED")

    def test_poison_to_dlq(self):
        self.assertEqual(self.send("poison")["status"], "DEAD_LETTER")


class ControlTests(Base):
    def test_duplicate_dropped(self):
        a = self.send("general", fixed_message_id=True)
        b = self.send("general", fixed_message_id=True)
        self.assertEqual(a["status"], "ACCEPTED")
        self.assertEqual(b["status"], "DUPLICATE")
        self.assertEqual(b["caseId"], a["caseId"])

    def test_four_eyes_and_oob(self):
        r = self.send("bank-change")
        t = self.open_task(r["caseId"])
        self.assertEqual(t["required_approvals"], 2)
        self.p.approve(t["task_id"], "a.becker")
        with self.assertRaises(PlatformError):
            self.p.approve(t["task_id"], "a.becker", oob_verified=True)  # gleiche Person
        with self.assertRaises(PlatformError):
            self.p.approve(t["task_id"], "m.wolf")  # OOB fehlt
        self.p.approve(t["task_id"], "m.wolf", oob_verified=True)
        self.assertEqual(self.state(r["caseId"]), "CLOSED")
        self.assertEqual(self.p.systems.get_vendor("100234")["iban"], "DE02100100100006820101")
        # Antwort geht an Stamm-Mail, nicht an Absender
        self.assertEqual(self.p.erp_view()["outbox"][0]["recipient"], "rechnung@mueller-industrie.example")

    def test_fraud_indicator(self):
        r = self.send("bank-change-fraud")
        t = self.open_task(r["caseId"])
        self.assertIn("R-032 SENDER_DOMAIN_MISMATCH", t["payload"]["rulesFired"])

    def test_toctou_hash_blocks_tampered_payload(self):
        self.p.store.set_setting("robot", {"autoRun": False})
        r = self.send("invoice-large")
        self.p.approve(self.open_task(r["caseId"])["task_id"], "a.becker")
        item = [i for i in self.p.case_view(r["caseId"])["queueItems"] if i["queue"] == "AEAP_Execution"][0]
        self.p.tamper(item["id"])
        res = self.p.run_robot()
        self.assertEqual(res[0]["result"], "BLOCKED_HASH_MISMATCH")
        self.assertEqual(self.state(r["caseId"]), "ESCALATED")
        self.assertEqual(len(self.p.erp_view()["documents"]), 0)

    def test_business_exception_no_retry(self):
        r = self.send("unknown-po")
        self.p.approve(self.open_task(r["caseId"])["task_id"], "a.becker")
        item = [i for i in self.p.case_view(r["caseId"])["queueItems"] if i["queue"] == "AEAP_Execution"][0]
        self.assertEqual(item["exception_type"], "Business")
        self.assertEqual(item["attempt"], 1)
        self.assertEqual(self.state(r["caseId"]), "ESCALATED")

    def test_timeout_after_commit_no_double_booking(self):
        self.p.store.set_setting("chaos", {"erpTimeoutAfterCommitRate": 1.0})
        r = self.send("invoice-standard")
        # Commit gelingt, Antwort geht verloren -> Retry trifft Idempotenz -> genau eine Buchung, Case schliesst
        self.assertEqual(self.state(r["caseId"]), "CLOSED")
        self.assertEqual(len(self.p.erp_view()["documents"]), 1)
        events = [e["message"] for e in self.p.case_view(r["caseId"])["events"]]
        self.assertTrue(any("Timeout nach Commit" in m for m in events))
        self.assertTrue(any("keine Doppelbuchung" in m for m in events))

    def test_chaos_load_no_double_booking(self):
        self.p.store.set_setting("chaos", {"llmTimeoutRate": 0.2, "llmSchemaErrorRate": 0.3, "erp5xxRate": 0.3,
                                           "erpTimeoutAfterCommitRate": 0.3, "selectorDriftRate": 0.5})
        for _ in range(60):
            self.p.ingest(random_mail(self.p.rng))
        dups = self.p.store.query("SELECT supplier_id, reference, COUNT(*) n FROM erp_documents "
                                  "WHERE doc_type='INVOICE' GROUP BY supplier_id, reference HAVING n > 1")
        self.assertEqual(dups, [])

    def test_pii_masked_in_llm_audit(self):
        r = self.send("bank-change")
        llm = [e for e in self.p.case_view(r["caseId"])["events"] if e["kind"] == "llm_call"][0]
        import json
        blob = json.dumps(llm["data"]["input"])
        self.assertNotIn("DE02100100100006820101", blob)
        self.assertNotIn("DE02 1001", blob)
        self.assertIn("<IBAN_", blob)
        # nach De-Pseudonymisierung steht die echte IBAN im Vorschlag
        env = self.p.case_view(r["caseId"])["case"]["envelope"]
        self.assertEqual(env["agent"]["proposal"]["actions"][0]["args"]["iban"], "DE02100100100006820101")

    def test_store_prompt_io_policy(self):
        self.p.store.set_setting("automationOps", {"storePromptIO": False})
        r = self.send("general")
        llm = [e for e in self.p.case_view(r["caseId"])["events"] if e["kind"] == "llm_call"][0]
        self.assertEqual(llm["data"]["input"], "[Inhalt durch Policy blockiert]")

    def test_allowlist_revocation(self):
        pol = self.p.store.get_setting("policy")
        pol["allowlist"]["supplier-triage@1.4.2"].remove("erp.post_invoice")
        self.p.store.set_setting("policy", pol)
        r = self.send("invoice-standard")
        self.assertEqual(self.state(r["caseId"]), "REJECTED_POLICY")


class GovernanceTests(Base):
    """Abschnitt 2: Rollen, Funktionstrennung, Change Requests, Release-Gate, Notbremse."""

    def test_compliance_cannot_approve_business_task(self):
        r = self.send("invoice-large")
        with self.assertRaises(Forbidden):
            self.p.approve(self.open_task(r["caseId"])["task_id"], "s.klein")

    def test_r3_requires_teamlead(self):
        r = self.send("bank-change")
        t = self.open_task(r["caseId"])
        self.p.approve(t["task_id"], "a.becker")
        with self.assertRaises(PlatformError):
            self.p.approve(t["task_id"], "j.yilmaz", oob_verified=True)
        self.p.approve(t["task_id"], "m.wolf", oob_verified=True)
        self.assertEqual(self.state(r["caseId"]), "CLOSED")

    def test_change_request_flow_with_sod(self):
        pol = self.p.store.get_setting("policy")
        pol["thresholds"]["invoiceAutoLimit"] = 20000.0
        cr = self.p.propose_change("t.braun", {"policy": pol}, "Grenze anheben")
        self.assertEqual(cr["status"], "PENDING")
        self.assertEqual(cr["evaluation"]["gate"], "REVIEW")  # invoice-large wird R1
        self.assertEqual(self.p.store.get_setting("policy")["thresholds"]["invoiceAutoLimit"], 5000.0)  # noch nicht aktiv
        with self.assertRaises(Forbidden):
            self.p.decide_change(cr["id"], "t.braun", True)  # COE darf nicht genehmigen
        done = self.p.decide_change(cr["id"], "s.klein", True)
        self.assertEqual(done["status"], "APPLIED")
        active = self.p.store.get_setting("policy")
        self.assertEqual(active["thresholds"]["invoiceAutoLimit"], 20000.0)
        self.assertEqual(active["rulesVersion"], "2026.10.2")

    def test_own_change_cannot_be_approved(self):
        cr = self.p.propose_change("s.klein", {"guardrails": {"promptInjection": True, "piiMasking": False, "harmfulContent": True}})
        with self.assertRaises(GovernanceError):
            self.p.decide_change(cr["id"], "s.klein", True)

    def test_release_gate_blocks_dangerous_allowlist(self):
        pol = self.p.store.get_setting("policy")
        pol["allowlist"]["supplier-triage@1.4.2"].append("payments.release_payment")
        cr = self.p.propose_change("t.braun", {"policy": pol})
        self.assertEqual(cr["evaluation"]["gate"], "BLOCKED")
        self.assertIn("injection", cr["evaluation"]["criticalFailed"])
        with self.assertRaises(GovernanceError):
            self.p.decide_change(cr["id"], "s.klein", True)

    def test_killswitch_agents_holds_and_resumes(self):
        self.p.set_killswitch("m.wolf", {"agents": True})
        r = self.send("invoice-standard")
        self.assertEqual(self.state(r["caseId"]), "EXTRACTED")
        res = self.p.set_killswitch("m.wolf", {"agents": False})
        self.assertEqual(res["resumed"]["agents"], 1)
        self.assertEqual(self.state(r["caseId"]), "CLOSED")

    def test_killswitch_intake_holds_mail(self):
        self.p.set_killswitch("t.braun", {"intake": True})
        self.assertEqual(self.send("general")["status"], "HELD")
        self.assertEqual(self.p.kpis()["total"], 0)
        self.p.set_killswitch("t.braun", {"intake": False})
        self.assertEqual(self.p.kpis()["total"], 1)

    def test_killswitch_execution_and_permission(self):
        with self.assertRaises(Forbidden):
            self.p.set_killswitch("a.becker", {"execution": True})
        self.p.set_killswitch("s.klein", {"execution": True})
        r = self.send("invoice-standard")
        self.assertEqual(self.state(r["caseId"]), "AUTO_APPROVED")
        self.p.set_killswitch("s.klein", {"execution": False})
        self.assertEqual(self.state(r["caseId"]), "CLOSED")

    def test_alerts_and_guardrails(self):
        self.send("injection")
        titles = [a["title"] for a in self.p.alerts()]
        self.assertIn("Prompt-Injection-Versuche", titles)
        g = {x["id"]: x for x in self.p.guardrail_status()}
        self.assertEqual(g["G-08"]["hits"], 1)

    def test_evaluation_baseline_passes(self):
        rec = self.p.run_agent_evaluation("t.braun")
        self.assertEqual(rec["result"]["gate"], "PASSED", rec["result"])


class AssistantTests(Base):
    """KI-Assistent gegen einen lokalen, OpenAI-kompatiblen Test-Provider (kein echter Netzwerkzugriff)."""

    def setUp(self):
        super().setUp()
        import json as _json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from unittest import mock
        self.received = []
        received = self.received

        class FakeProvider(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                data = _json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.headers.get("Authorization") != "Bearer test-key":
                    return self._json(401, {"error": {"message": "invalid key"}})
                self._json(200, {"data": [{"id": "moonshot-v1-8k"}, {"id": "kimi-k2-test"}]})

            def do_POST(self):
                body = _json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.append(body)
                self._json(200, {"model": body["model"], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                                 "choices": [{"message": {"role": "assistant", "content": "## Kurzantwort\nNutze <IBAN_1> nur maskiert."}}]})

        self.srv = HTTPServer(("127.0.0.1", 0), FakeProvider)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.envpatch = mock.patch.dict(os.environ, {k: "" for k in (
            "AEAP_LLM_API_KEY", "MOONSHOT_API_KEY", "KIMI_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY",
            "AEAP_LLM_BASE_URL", "AEAP_LLM_MODEL", "AEAP_LLM_PROVIDER", "AEAP_LLM_API_KEY_FILE", "AEAP_LLM_API_KEY_VAR")})
        self.envpatch.start()
        self.root = os.path.join(self.tmp.name, "proj")
        os.makedirs(self.root)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"

    def tearDown(self):
        self.envpatch.stop()
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def _assistant(self, env_lines):
        from aeap.assistant import Assistant
        from aeap.llm import LLMClient
        with open(os.path.join(self.root, ".env"), "w", encoding="utf-8") as f:
            f.write("\n".join(env_lines))
        return Assistant(self.p.store, LLMClient(self.root))

    def test_not_configured(self):
        a = self._assistant([])
        self.assertFalse(a.status()["configured"])

    def test_key_reference_file_and_masking(self):
        keyfile = os.path.join(self.tmp.name, "llm.env")
        with open(keyfile, "w", encoding="utf-8") as f:
            f.write("OTHER=1\nMOONSHOT_API_KEY=test-key\n")
        a = self._assistant([f"AEAP_LLM_API_KEY_FILE={keyfile}", "AEAP_LLM_API_KEY_VAR=MOONSHOT_API_KEY", f"AEAP_LLM_BASE_URL={self.base}"])
        st = a.status()
        self.assertTrue(st["configured"])
        self.assertNotIn("test-key", json.dumps(st))  # Schlüssel nie in der Statusausgabe
        res = a.ask([{"role": "user", "content": "Bankdaten DE02 1001 0010 0006 8201 01 von max@firma.example in SAP befüllen?"}],
                    context="# Blueprint\nTest", user="t.braun")
        self.assertEqual(res["model"], "kimi-k2-test")  # automatische Modellwahl nach Präferenz
        sent = json.dumps(self.received[-1], ensure_ascii=False)
        self.assertNotIn("DE02100100100006820101", sent)
        self.assertNotIn("max@firma.example", sent)
        self.assertIn("<IBAN_1>", sent)
        self.assertIn("DE02100100100006820101", res["answer"])  # De-Pseudonymisierung der Antwort
        ev = [e for e in self.p.store.events(limit=50) if e["kind"] == "assistant_call"]
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["data"]["usage"]["total_tokens"], 15)

    def test_provider_error_is_reported(self):
        from aeap.llm import LLMError
        a = self._assistant(["MOONSHOT_API_KEY=wrong", f"AEAP_LLM_BASE_URL={self.base}"])
        with self.assertRaises(LLMError) as cm:
            a.ask([{"role": "user", "content": "Hallo"}])
        self.assertIn("401", str(cm.exception))
        self.assertNotIn("wrong", str(cm.exception))


class BridgeTests(Base):
    """Abschnitt 3: Tool-Vertraege (R-007) und signierter Bridge-Eingang."""

    def setUp(self):
        super().setUp()
        from aeap.bridge import Bridge
        self.bridge = Bridge(self.p)
        self.secret = self.bridge.config()["secret"]

    def _call(self, body, ts=None, key="k-1", secret=None, trace=None):
        import time as _t
        from aeap.bridge import sign
        raw = json.dumps(body).encode("utf-8")
        ts = int(_t.time()) if ts is None else ts
        headers = {"X-AEAP-Timestamp": str(ts),
                   "X-AEAP-Signature": sign(secret or self.secret, ts, raw),
                   "Idempotency-Key": key}
        if trace:
            headers["traceparent"] = trace
        return self.bridge.handle(headers, raw)

    def _msg(self, subject="Allgemeine Frage"):
        return {"type": "supplier.message", "data": {"from": "info@schmidt-partner.example", "subject": subject,
                "body": "Koennen Sie uns einen Ansprechpartner nennen?", "attachments": []}}

    def test_valid_signed_message_creates_case_with_trace(self):
        trace = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
        code, res, _ = self._call(self._msg(), trace=trace)
        self.assertEqual(code, 202)
        self.assertEqual(res["traceId"], "a" * 32)
        env = self.p.case_view(res["caseId"])["case"]["envelope"]
        self.assertEqual(env["source"]["channel"], "partner-api")
        self.assertTrue(env["source"]["traceparent"].startswith("00-" + "a" * 32))

    def test_wrong_signature_rejected(self):
        code, _, _ = self._call(self._msg(), secret="falsch")
        self.assertEqual(code, 401)
        self.assertEqual(self.p.kpis()["total"], 0)

    def test_old_timestamp_rejected(self):
        import time as _t
        code, _, _ = self._call(self._msg(), ts=int(_t.time()) - 3600)
        self.assertEqual(code, 401)

    def test_replay_returns_stored_response_without_new_case(self):
        c1, r1, _ = self._call(self._msg(), key="same")
        c2, r2, extra = self._call(self._msg(), key="same")
        self.assertEqual((c1, r1), (c2, r2))
        self.assertEqual(extra.get("Idempotent-Replay"), "true")
        self.assertEqual(self.p.kpis()["total"], 1)

    def test_key_reuse_with_other_body_conflicts(self):
        self._call(self._msg("A"), key="dup")
        code, _, _ = self._call(self._msg("B"), key="dup")
        self.assertEqual(code, 409)

    def test_unknown_type_rejected(self):
        code, _, _ = self._call({"type": "payment.release", "data": {}})
        self.assertEqual(code, 422)

    def test_tool_contract_violation_escalates(self):
        from aeap.contracts import validate_tool_args
        self.assertEqual(validate_tool_args("erp.post_invoice", {"supplierId": "1", "invoiceNo": "X", "amount": "viel",
                                                                 "currency": "EUR"}), ["amount muss num sein"])
        self.assertIn("unbekanntes Feld x", validate_tool_args("crm.create_ticket", {"tags": [], "summary": "s", "x": 1}))
        env = {"caseId": "C", "idempotencyKey": "k", "source": {"from": ""},
               "documents": [{"type": "Other", "classConfidence": 1.0, "fields": {}, "fieldConfidence": {}}],
               "agent": {"agentId": "supplier-triage", "version": "1.4.2", "guardrails": {},
                         "proposal": {"intent": "ACKNOWLEDGE", "confidence": 0.9, "summary": "s",
                                      "actions": [{"tool": "crm.create_ticket", "args": {"tags": "kein-array", "summary": "s"}}]}}}
        d = self.p.pdp.decide(env)
        self.assertEqual(d["decision"], "ESCALATE")
        self.assertIn("R-007 TOOL_CONTRACT", d["rulesFired"])


if __name__ == "__main__":
    unittest.main()
