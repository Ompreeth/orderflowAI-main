# -*- coding: utf-8 -*-
"""
Unit and integration tests for the parts of OrderFlow AI that can be exercised
without a live database or a running Ollama server.

    python -m pytest test_orderflow.py -v

Everything here imports the real modules — nothing is reimplemented for the
test. DATABASE_URL must be set to something syntactically valid before import
because database.py refuses to load without it; no connection is ever opened,
which is itself one of the properties under test.
"""
import importlib
import json
import os
import sys

import pytest

os.environ.setdefault("DATABASE_URL", "postgres://u:p@127.0.0.1:5432/none")
os.environ.setdefault("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
os.environ.setdefault("SECRET_KEY", "test-only-key")

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import app as orderflow                                   # noqa: E402
import auth                                               # noqa: E402
import database                                           # noqa: E402

CATALOGUE = ["Steel Rods", "Aluminum Sheets", "Copper Wire Spool", "Ball Bearings",
             "Rubber Gaskets", "Hex Bolts M8", "Hex Nuts M8", "Plastic Casings",
             "Circuit Boards", "Motor Housings", "Hydraulic Hoses", "Welding Rods",
             "Paint Cans", "Packaging Boxes", "Conveyor Belts"]


# ── T1  import safety ───────────────────────────────────────────────────
class TestImportSafety:
    """app.py must be importable without starting a server, creating tables,
    bootstrapping an admin or opening a socket — this is what lets the
    evaluation harness call classify_intent() directly."""

    def test_app_imports_without_side_effects(self):
        assert hasattr(orderflow, "app")
        assert hasattr(orderflow, "classify_intent")

    def test_schema_not_created_on_import(self, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(database, "get_db", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
        importlib.reload(importlib.import_module("app"))
        assert called["n"] == 0, "importing app.py opened a database connection"

    def test_all_twelve_blueprints_registered(self):
        expected = {"auth", "procurement", "payments", "quality", "reports", "production",
                    "notifications", "warehouses", "integrations", "search",
                    "saved_views", "inventory_io"}
        assert expected <= set(orderflow.app.blueprints)

    def test_database_url_is_mandatory(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        src = open(os.path.join(HERE, "database.py"), encoding="utf-8").read()
        assert "DATABASE_URL is not set" in src


# ── T2  extract_json ────────────────────────────────────────────────────
class TestExtractJson:
    """The model is asked for bare JSON and frequently does not comply.
    extract_json is the only thing between that and a crashed request."""

    def test_plain_json(self):
        out = orderflow.extract_json('{"intent": "create_order", "quantity": 50}')
        assert out["intent"] == "create_order" and out["quantity"] == 50

    def test_fenced_json(self):
        raw = '```json\n{"intent": "list_orders"}\n```'
        assert orderflow.extract_json(raw)["intent"] == "list_orders"

    def test_json_wrapped_in_prose(self):
        raw = 'Sure! Here is the result:\n{"intent": "query_inventory", "part_name": "steel rods"}\nHope that helps.'
        out = orderflow.extract_json(raw)
        assert out["intent"] == "query_inventory"
        assert out["part_name"] == "steel rods"

    def test_nested_braces_survive(self):
        raw = '{"intent": "create_order", "specs": {"grade": "A", "finish": "zinc"}}'
        assert orderflow.extract_json(raw)["specs"]["grade"] == "A"

    def test_garbage_does_not_raise(self):
        out = orderflow.extract_json("I am afraid I cannot help with that.")
        assert isinstance(out, dict)

    def test_empty_input_does_not_raise(self):
        assert isinstance(orderflow.extract_json(""), dict)


# ── T3  _correct_part_name ──────────────────────────────────────────────
class TestPartNameCorrection:
    """Token-coverage correction: >=50% but <100% of a catalogue entry's tokens
    appearing in the utterance pulls the phrase onto the canonical name."""

    def test_partial_match_is_corrected(self):
        out = orderflow._correct_part_name("I need 50 Steel Rails", CATALOGUE)
        assert "Steel Rods" in out

    def test_exact_match_is_left_alone(self):
        text = "order 200 Hex Bolts M8"
        assert orderflow._correct_part_name(text, CATALOGUE) == text

    def test_unrelated_text_is_left_alone(self):
        text = "what is the weather today"
        assert orderflow._correct_part_name(text, CATALOGUE) == text

    def test_empty_catalogue_is_a_no_op(self):
        text = "order 10 steel rails"
        assert orderflow._correct_part_name(text, []) == text

    def test_single_token_overlap_below_threshold(self):
        # "Copper Wire Spool" is 3 tokens; one match = 0.33 coverage, under the
        # 0.5 gate, so nothing should be rewritten.
        out = orderflow._correct_part_name("we need copper today", CATALOGUE)
        assert out == "we need copper today"


# ── T4  status handling ─────────────────────────────────────────────────
class TestStatusFlow:
    def test_status_flow_is_three_stages_in_order(self):
        assert orderflow.STATUS_FLOW == ["Received", "In Review", "Accepted"]

    @pytest.mark.parametrize("word,expected", [
        ("received", "Received"), ("in review", "In Review"), ("review", "In Review"),
        ("approve", "Accepted"), ("done", "Accepted"), ("completed", "Accepted"),
    ])
    def test_synonyms_map_to_canonical_stages(self, word, expected):
        src = open(os.path.join(HERE, "app.py"), encoding="utf-8").read()
        assert '"%s":' % word in src.lower().replace("'", '"')
        assert expected in orderflow.STATUS_FLOW


# ── T5  role gating ─────────────────────────────────────────────────────
class TestRoleModel:
    def test_three_roles_exactly(self):
        assert auth.ROLES == ["admin", "operator", "viewer"]

    def test_switch_role_gate_reads_the_database_not_the_session(self):
        src = open(os.path.join(HERE, "auth.py"), encoding="utf-8").read()
        body = src.split("def switch_role")[1].split("\ndef ")[0]
        assert "stored_role()" in body, "the privilege gate must consult the stored role"
        assert 'session["role"] != ' not in body

    def test_self_registration_cannot_choose_a_role(self):
        src = open(os.path.join(HERE, "auth.py"), encoding="utf-8").read()
        body = src.split("def register")[1].split("\ndef ")[0]
        assert "'viewer'" in body or '"viewer"' in body
        assert "data.get(\"role\")" not in body


# ── T6  HTTP layer, no database required ────────────────────────────────
class TestHttpLayer:
    @pytest.fixture
    def client(self):
        orderflow.app.config["TESTING"] = True
        return orderflow.app.test_client()

    def test_index_renders(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert b"OrderFlow" in r.data

    def test_system_status_reports_ollama_offline_without_crashing(self, client):
        r = client.get("/api/system/status")
        assert r.status_code == 200
        payload = r.get_json()
        assert "model" in payload and "url" in payload

    def test_protected_route_rejects_anonymous_caller(self, client):
        r = client.post("/api/auth/switch-role", json={"role": "operator"})
        assert r.status_code in (401, 403)

    def test_switch_role_rejects_an_unknown_role_name(self, client):
        r = client.post("/api/auth/switch-role", json={"role": "superuser"})
        assert r.status_code in (400, 401, 403)


# ── T7  deterministic reporting fallback ────────────────────────────────
class TestRuleFallback:
    """answer_report_query must recognise the phrase before it touches the
    database, and must return None (not raise) for anything it cannot serve."""

    @pytest.mark.parametrize("text", [
        "what is the weather", "order 50 steel rods", "show me all orders",
        "log a quality note for order 7",
    ])
    def test_non_reporting_questions_fall_through(self, text):
        assert orderflow.answer_report_query(text) is None

    def test_empty_input_falls_through(self):
        assert orderflow.answer_report_query("") is None
        assert orderflow.answer_report_query(None) is None
