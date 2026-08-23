"""Tests for the knowledge retriever and agentic RAG integration."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# Force LOCAL_MODE for tests
import os
os.environ["LOCAL_MODE"] = "true"
os.environ["RAG_ENABLED"] = "true"

from agent.knowledge_retriever import (
    VALID_DOMAINS,
    RetrievedDoc,
    _search_local,
    format_retrieved_context,
    retrieve,
)
from agent.agent import (
    ReasonRequest,
    ReasonResponse,
    _select_rag_domains,
    _retrieve_knowledge,
    reason,
)


# ---------------------------------------------------------------------------
# Knowledge Retriever Tests
# ---------------------------------------------------------------------------

class TestKnowledgeRetriever:
    def test_valid_domains(self):
        """All expected domains are registered."""
        assert "equipment" in VALID_DOMAINS
        assert "maintenance" in VALID_DOMAINS
        assert "store_profiles" in VALID_DOMAINS
        assert "alarm_history" in VALID_DOMAINS

    def test_local_search_returns_docs(self):
        """Local KB returns documents for valid domains."""
        docs = _search_local("equipment", "high temperature")
        assert len(docs) >= 1
        assert all(isinstance(d, RetrievedDoc) for d in docs)
        assert all(d.domain == "equipment" for d in docs)

    def test_local_search_empty_for_invalid_domain(self):
        """Local KB returns empty for nonexistent domain."""
        docs = _search_local("nonexistent", "test")
        assert docs == []

    def test_local_search_top_k(self):
        """Top-k limits results."""
        docs = _search_local("equipment", "test", top_k=1)
        assert len(docs) == 1

    @pytest.mark.asyncio
    async def test_retrieve_local_mode(self):
        """retrieve() uses local KB in LOCAL_MODE."""
        docs = await retrieve("equipment", "high temperature alarm")
        assert len(docs) >= 1
        assert docs[0].domain == "equipment"
        assert docs[0].content  # non-empty

    @pytest.mark.asyncio
    async def test_retrieve_invalid_domain(self):
        """retrieve() returns empty for invalid domain."""
        docs = await retrieve("fake_domain", "test")
        assert docs == []

    def test_format_retrieved_context_with_docs(self):
        """format_retrieved_context produces markdown sections."""
        docs = [
            RetrievedDoc(domain="equipment", title="Test Doc", content="Some content", score=0.9),
            RetrievedDoc(domain="maintenance", title="Guide", content="Steps here", score=0.8),
        ]
        result = format_retrieved_context(docs)
        assert "### [EQUIPMENT] Test Doc" in result
        assert "### [MAINTENANCE] Guide" in result
        assert "Some content" in result
        assert "Steps here" in result

    def test_format_retrieved_context_empty(self):
        """format_retrieved_context handles empty list."""
        result = format_retrieved_context([])
        assert "No relevant knowledge" in result


# ---------------------------------------------------------------------------
# Agentic RAG Domain Selection Tests
# ---------------------------------------------------------------------------

def _make_request(step: str, history: list[dict] | None = None) -> ReasonRequest:
    """Helper to create a ReasonRequest for testing."""
    return ReasonRequest(
        alarm_context={
            "alarm_id": "ALM-001",
            "alarm_type": "high_temperature",
            "severity": "high",
            "store_name": "Test Store",
            "store_id": "ST-001",
            "equipment_name": "Cooler Unit A",
            "current_temp": 12.5,
            "threshold_temp": 8.0,
            "alarm_time": "2026-04-16T10:30:00Z",
            "customer_id": "CUST-001",
        },
        sop_definition={
            "sop_id": "SOP-HTA-001",
            "steps": [
                {"id": "greeting", "type": "speak", "text": "Hello", "next": "ask_awareness"},
                {
                    "id": "ask_awareness",
                    "type": "ask",
                    "text": "Are you aware?",
                    "branches": [
                        {"match": "yes", "keywords": ["yes"], "next": "ask_action_taken"},
                        {"match": "no", "keywords": ["no"], "next": "inform_details"},
                        {"match": "unclear", "keywords": [], "next": "ask_awareness_retry"},
                    ],
                },
                {"id": "ask_action_taken", "type": "ask", "text": "Action taken?", "branches": [
                    {"match": "action_taken", "keywords": ["yes", "checked"], "next": "confirm_resolution"},
                    {"match": "no_action", "keywords": ["no", "nothing"], "next": "recommend_action"},
                ]},
                {"id": "recommend_action", "type": "speak", "text": "We recommend...", "next": "confirm_resolution"},
                {"id": "confirm_resolution", "type": "ask", "text": "Is it resolved?", "branches": [
                    {"match": "resolved", "keywords": ["yes", "resolved"], "next": "close_resolved"},
                ]},
                {"id": "close_resolved", "type": "speak", "text": "Great, closing.", "outcome": "resolved"},
                {"id": "escalate_unclear", "type": "speak", "text": "Escalating.", "outcome": "escalate_human"},
            ],
        },
        conversation_history=history or [],
        current_step=step,
    )


class TestAgenticRAGDomainSelection:
    def test_greeting_selects_alarm_history(self):
        """Greeting step queries alarm history for pattern context."""
        req = _make_request("greeting")
        domains = _select_rag_domains(req)
        assert "alarm_history" in domains
        assert "store_profiles" in domains

    def test_awareness_selects_equipment(self):
        """Awareness step queries equipment knowledge."""
        req = _make_request("ask_awareness")
        domains = _select_rag_domains(req)
        assert "equipment" in domains
        assert "alarm_history" in domains

    def test_action_step_selects_maintenance(self):
        """Action steps query maintenance guides."""
        req = _make_request("ask_action_taken")
        domains = _select_rag_domains(req)
        assert "maintenance" in domains
        assert "equipment" in domains

    def test_recommend_action_selects_maintenance(self):
        """Recommend action step queries maintenance."""
        req = _make_request("recommend_action")
        domains = _select_rag_domains(req)
        assert "maintenance" in domains

    def test_technical_terms_add_equipment_domain(self):
        """Technical terms in callee response add equipment domain."""
        req = _make_request(
            "confirm_resolution",
            history=[{"role": "callee", "text": "The compressor seems to be running fine now"}],
        )
        domains = _select_rag_domains(req)
        assert "equipment" in domains
        assert "maintenance" in domains

    def test_store_profiles_always_included(self):
        """Store profiles are always included."""
        for step in ["greeting", "ask_awareness", "confirm_resolution"]:
            req = _make_request(step)
            domains = _select_rag_domains(req)
            assert "store_profiles" in domains

    def test_no_duplicate_domains(self):
        """Domain list has no duplicates."""
        req = _make_request(
            "ask_awareness",
            history=[{"role": "callee", "text": "The compressor is broken"}],
        )
        domains = _select_rag_domains(req)
        assert len(domains) == len(set(domains))


# ---------------------------------------------------------------------------
# Agentic RAG Integration Tests
# ---------------------------------------------------------------------------

class TestAgenticRAGIntegration:
    @pytest.mark.asyncio
    async def test_retrieve_knowledge_returns_context(self):
        """_retrieve_knowledge returns formatted context."""
        req = _make_request("ask_awareness")
        context, domains_used = await _retrieve_knowledge(["equipment", "alarm_history"], req)
        assert context  # non-empty
        assert "equipment" in domains_used
        assert "alarm_history" in domains_used

    @pytest.mark.asyncio
    async def test_retrieve_knowledge_empty_domains(self):
        """Empty domain list returns empty context."""
        req = _make_request("greeting")
        context, domains_used = await _retrieve_knowledge([], req)
        assert context == ""
        assert domains_used == []

    @pytest.mark.asyncio
    async def test_reason_with_rag_local_mode(self):
        """reason() works in LOCAL_MODE with RAG enabled."""
        req = _make_request(
            "ask_awareness",
            history=[{"role": "callee", "text": "yes, I'm aware of the alarm"}],
        )
        result = await reason(req)
        assert isinstance(result, ReasonResponse)
        assert result.next_step == "ask_action_taken"
        # knowledge_used should be populated from the RAG step
        assert result.knowledge_used is not None
        assert len(result.knowledge_used) > 0

    @pytest.mark.asyncio
    async def test_reason_tags_knowledge_domains(self):
        """reason() tags the response with which knowledge domains were used."""
        req = _make_request("greeting")
        result = await reason(req)
        assert isinstance(result, ReasonResponse)
        # Greeting queries alarm_history and store_profiles
        if result.knowledge_used:
            assert any(d in result.knowledge_used for d in ["alarm_history", "store_profiles"])

    @pytest.mark.asyncio
    async def test_reason_still_follows_sop(self):
        """RAG doesn't break SOP flow — greeting auto-advances to ask_awareness."""
        req = _make_request("greeting")
        result = await reason(req)
        assert result.next_step == "ask_awareness"

    @pytest.mark.asyncio
    async def test_escalation_still_works_with_rag(self):
        """Escalation logic is preserved with RAG enabled."""
        req = _make_request(
            "ask_awareness",
            history=[{"role": "callee", "text": "something completely unrelated about the weather"}],
        )
        req.unclear_count = 3
        result = await reason(req)
        assert result.should_escalate is True
        assert result.next_step == "escalate_unclear"
