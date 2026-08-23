"""Foundry Hosted Agent — SOP reasoning engine for call-out alarm verification.

This agent receives alarm context, SOP definition, and conversation history,
then determines the next SOP step and generates a constrained utterance.
It uses Azure OpenAI GPT-4o with strict SOP guardrails.

Agentic RAG: The agent autonomously queries knowledge bases across multiple
domains (equipment, maintenance, store profiles, alarm history) to enrich
its reasoning with domain-specific context from Foundry IQ indexes.

Set LOCAL_MODE=true to run with a rule-based engine (no Azure OpenAI needed).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel

try:
    # Package mode (PYTHONPATH includes src/, run as agent.agent:app)
    from agent.knowledge_retriever import (
        VALID_DOMAINS,
        RetrievedDoc,
        format_retrieved_context,
        retrieve,
    )
except ImportError:
    # Direct mode (Docker, run as agent:app from /app/)
    from knowledge_retriever import (  # type: ignore[no-redef]
        VALID_DOMAINS,
        RetrievedDoc,
        format_retrieved_context,
        retrieve,
    )

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
LOCAL_MODE = os.environ.get("LOCAL_MODE", "false").lower() in ("true", "1", "yes")
AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
AZURE_OPENAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
RAG_ENABLED = os.environ.get("RAG_ENABLED", "true").lower() in ("true", "1", "yes")

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class ReasonRequest(BaseModel):
    alarm_context: dict
    sop_definition: dict
    conversation_history: list[dict]
    current_step: str
    customer_id: str | None = None
    unclear_count: int = 0


class ReasonResponse(BaseModel):
    next_step: str
    utterance: str
    reasoning: str
    should_escalate: bool = False
    outcome: str | None = None
    knowledge_used: list[str] | None = None  # domains queried for this turn


# ---------------------------------------------------------------------------
# Agent logic
# ---------------------------------------------------------------------------
def _load_system_prompt() -> str:
    """Load the SOP system prompt from file."""
    prompt_path = _PROMPT_DIR / "sop_system.txt"
    return prompt_path.read_text()


def _select_rag_domains(request: ReasonRequest) -> list[str]:
    """Decide which knowledge domains to query based on conversation context.

    This is the 'agentic' part — the agent autonomously selects which
    knowledge bases are relevant for the current turn.
    """
    domains: list[str] = []
    step = request.current_step
    alarm = request.alarm_context

    # Always query equipment knowledge for equipment-related steps
    if step in ("ask_awareness", "ask_awareness_retry", "inform_details"):
        domains.append("equipment")

    # Query maintenance guides when asking about or recommending actions
    if step in ("ask_action_taken", "ask_action_retry", "recommend_action"):
        domains.append("maintenance")
        domains.append("equipment")

    # Query alarm history for pattern context when greeting or at awareness steps
    if step in ("greeting", "ask_awareness"):
        domains.append("alarm_history")

    # Always include store profile for context
    domains.append("store_profiles")

    # If callee mentions technical terms, also pull equipment + maintenance
    callee_text = ""
    for turn in reversed(request.conversation_history):
        if turn.get("role") == "callee":
            callee_text = turn.get("text", "").lower()
            break

    tech_terms = [
        "compressor", "condenser", "evaporator", "defrost", "refrigerant",
        "valve", "sensor", "controller", "pressure", "leak", "fan", "coil",
    ]
    if any(term in callee_text for term in tech_terms):
        if "equipment" not in domains:
            domains.append("equipment")
        if "maintenance" not in domains:
            domains.append("maintenance")

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for d in domains:
        if d not in seen and d in VALID_DOMAINS:
            seen.add(d)
            unique.append(d)
    return unique


async def _retrieve_knowledge(
    domains: list[str], request: ReasonRequest
) -> tuple[str, list[str]]:
    """Retrieve knowledge from selected domains in parallel.

    Returns formatted context string and list of domains that returned results.
    """
    if not domains:
        return "", []

    alarm = request.alarm_context
    # Build a contextual query from the alarm and current conversation
    query_parts = [
        alarm.get("alarm_type", ""),
        alarm.get("equipment_name", ""),
        alarm.get("store_name", ""),
    ]

    # Add callee's last message for more targeted retrieval
    for turn in reversed(request.conversation_history):
        if turn.get("role") == "callee":
            query_parts.append(turn.get("text", ""))
            break

    query = " ".join(p for p in query_parts if p)

    # Multi-tenant scope: filter knowledge by customer_id when present
    customer_id = request.customer_id or alarm.get("customer_id")

    # Retrieve from all selected domains in parallel
    tasks = [retrieve(domain, query, top_k=2, customer_id=customer_id) for domain in domains]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_docs: list[RetrievedDoc] = []
    domains_used: list[str] = []
    for domain, result in zip(domains, results):
        if isinstance(result, Exception):
            logger.warning("RAG retrieval failed for domain '%s': %s", domain, result)
            continue
        if result:
            all_docs.extend(result)
            domains_used.append(domain)

    return format_retrieved_context(all_docs), domains_used


def _build_user_message(request: ReasonRequest, knowledge_context: str = "") -> str:
    """Build the user message with all context for the LLM."""
    alarm = request.alarm_context

    conversation_text = ""
    for turn in request.conversation_history:
        role = turn.get("role", "unknown")
        text = turn.get("text", "")
        conversation_text += f"[{role}]: {text}\n"

    # Extract the current step details from the SOP definition
    current_step_def = None
    for step in request.sop_definition.get("steps", []):
        if step.get("id") == request.current_step:
            current_step_def = step
            break

    return f"""## Alarm Context
- Alarm ID: {alarm.get('alarm_id')}
- Type: {alarm.get('alarm_type')}
- Severity: {alarm.get('severity')}
- Store: {alarm.get('store_name')}
- Equipment: {alarm.get('equipment_name')}
- Current Temperature: {alarm.get('current_temp')}°
- Threshold: {alarm.get('threshold_temp')}°
- Alarm Time: {alarm.get('alarm_time')}

## Current SOP Step
ID: {request.current_step}
Definition: {json.dumps(current_step_def, indent=2) if current_step_def else 'N/A'}

## Unclear Response Count
{request.unclear_count}

## Conversation History
{conversation_text if conversation_text else '(No conversation yet)'}

## Available SOP Steps
{json.dumps([s.get('id') for s in request.sop_definition.get('steps', [])], indent=2)}

## Retrieved Knowledge Context
{knowledge_context if knowledge_context else '(No domain knowledge retrieved)'}

Based on the callee's last response and the current SOP step, determine the next step.
Use the retrieved knowledge context to enrich your utterances with relevant technical
details when appropriate (e.g., likely causes, recommended actions), but stay within
the SOP structure. Respond with the JSON schema specified in the system prompt."""


async def reason(request: ReasonRequest) -> ReasonResponse:
    """Route to local rule-based or Azure OpenAI reasoning, with agentic RAG."""
    # Agentic RAG: autonomously select and query relevant knowledge domains
    knowledge_context = ""
    domains_used: list[str] = []

    if RAG_ENABLED:
        domains = _select_rag_domains(request)
        if domains:
            logger.info("Agentic RAG: querying domains %s", domains)
            knowledge_context, domains_used = await _retrieve_knowledge(domains, request)
            logger.info("RAG retrieved context from: %s", domains_used)

    if LOCAL_MODE:
        result = _reason_local(request)
    else:
        result = _reason_openai(request, knowledge_context)

    # Tag the response with which knowledge domains were used
    result.knowledge_used = domains_used if domains_used else None
    return result


def _reason_local(request: ReasonRequest) -> ReasonResponse:
    """Rule-based SOP reasoning — matches branch keywords without any LLM."""
    steps = {s["id"]: s for s in request.sop_definition.get("steps", [])}
    current = steps.get(request.current_step)

    if not current:
        return ReasonResponse(
            next_step="escalate_unclear",
            utterance="I apologize, let me connect you with a human operator.",
            reasoning="Unknown current step",
            should_escalate=True,
            outcome="escalate_human",
        )

    # Speak steps auto-advance
    if current.get("type") == "speak":
        next_id = current.get("next")
        if next_id and next_id in steps:
            next_step = steps[next_id]
            return ReasonResponse(
                next_step=next_id,
                utterance=next_step.get("text", ""),
                reasoning=f"Auto-advance from speak step '{current['id']}'",
                outcome=current.get("outcome"),
            )
        return ReasonResponse(
            next_step=current["id"],
            utterance=current.get("text", ""),
            reasoning="Terminal speak step",
            outcome=current.get("outcome"),
        )

    # Ask steps — get callee's last response
    callee_text = ""
    for turn in reversed(request.conversation_history):
        if turn.get("role") == "callee":
            callee_text = turn.get("text", "").lower()
            break

    # Match against branches
    for branch in current.get("branches", []):
        for kw in branch.get("keywords", []):
            if kw.lower() in callee_text:
                next_step = steps.get(branch["next"], {})
                return ReasonResponse(
                    next_step=branch["next"],
                    utterance=next_step.get("text", ""),
                    reasoning=f"Matched keyword '{kw}' → branch '{branch['match']}'",
                    outcome=next_step.get("outcome"),
                )

        # Semantic fallbacks
        match = branch.get("match", "")
        hit = False
        if match == "yes" and any(w in callee_text for w in
                ["yes", "yeah", "yep", "yup", "sure", "correct", "right", "aware", "know", "noticed", "ok", "okay"]):
            hit = True
        elif match == "no" and any(w in callee_text for w in
                ["no", "nope", "nah", "not", "haven't", "didn't", "nothing"]):
            hit = True
        elif match == "action_taken" and any(w in callee_text for w in
                ["yes", "yeah", "yep", "yup", "sure", "checked", "looked", "done", "fixed", "handled",
                 "sorted", "technician", "called", "contacted", "working", "took care", "reset", "defrost"]):
            hit = True
        elif match == "no_action" and any(w in callee_text for w in
                ["no", "nope", "nah", "not yet", "haven't", "hasn't", "nobody", "no one", "didn't", "nothing"]):
            hit = True
        elif match == "resolved" and any(w in callee_text for w in
                ["yes", "yeah", "yep", "sure", "resolved", "fixed", "handled", "confirmed", "done", "ok", "okay", "will do", "on it"]):
            hit = True
        elif match == "follow_up" and any(w in callee_text for w in
                ["later", "call back", "follow up", "again", "not sure", "check back", "need time", "unsure"]):
            hit = True
        elif match in ("need_help", "unclear") and any(w in callee_text for w in
                ["help", "send", "dispatch", "confused", "what", "huh"]):
            hit = True

        if hit:
            next_step = steps.get(branch["next"], {})
            return ReasonResponse(
                next_step=branch["next"],
                utterance=next_step.get("text", ""),
                reasoning=f"Semantic match → '{match}'",
                outcome=next_step.get("outcome"),
            )

    # No keyword/semantic match. Follow the SOP step's own "unclear" branch
    # (which routes to a retry step, then to escalation). IMPORTANT: do NOT
    # return ``request.current_step`` here — evaluate_transition treats a
    # step that isn't one of its own branch targets as invalid and escalates
    # immediately, which made a single mishearing jump straight to a human.
    unclear_next = None
    for branch in current.get("branches", []):
        if branch.get("match") == "unclear":
            unclear_next = branch.get("next")
            break

    if unclear_next and unclear_next in steps:
        next_step = steps[unclear_next]
        outcome = next_step.get("outcome")
        return ReasonResponse(
            next_step=unclear_next,
            utterance=next_step.get("text", ""),
            reasoning=f"No clear match → SOP unclear branch '{unclear_next}'",
            should_escalate=(outcome == "escalate_human"),
            outcome=outcome,
        )

    # SOP step has no unclear branch — safe fallback to a human.
    return ReasonResponse(
        next_step="escalate_unclear",
        utterance="I apologize, let me connect you with a human operator.",
        reasoning="No match and no unclear branch defined",
        should_escalate=True,
        outcome="escalate_human",
    )


def _reason_openai(request: ReasonRequest, knowledge_context: str = "") -> ReasonResponse:
    """Invoke Azure OpenAI to reason about the next SOP step."""
    from azure.identity import DefaultAzureCredential
    from openai import AzureOpenAI

    credential = DefaultAzureCredential()
    token = credential.get_token("https://cognitiveservices.azure.com/.default")

    client = AzureOpenAI(
        azure_endpoint=AZURE_OPENAI_ENDPOINT,
        azure_ad_token=token.token,
        api_version=AZURE_OPENAI_API_VERSION,
    )

    system_prompt = _load_system_prompt()
    user_message = _build_user_message(request, knowledge_context)

    # gpt-5/o-series reasoning models reject temperature!=1 and max_tokens
    # (they require max_completion_tokens and default temperature).
    is_reasoning_model = any(
        AZURE_OPENAI_DEPLOYMENT.startswith(p) for p in ("gpt-5", "o1", "o3", "o4")
    )
    if is_reasoning_model:
        completion_kwargs = {"max_completion_tokens": 2000}
    else:
        completion_kwargs = {"temperature": 0.1, "max_tokens": 500}

    response = client.chat.completions.create(
        model=AZURE_OPENAI_DEPLOYMENT,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        response_format={"type": "json_object"},
        **completion_kwargs,
    )

    content = response.choices[0].message.content or "{}"
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        logger.error("Agent returned non-JSON response: %s", content)
        return ReasonResponse(
            next_step="escalate_unclear",
            utterance="I apologize, I'm having a technical issue. Let me connect you with a human operator.",
            reasoning="Failed to parse agent response",
            should_escalate=True,
            outcome="escalate_human",
        )

    return ReasonResponse(**data)


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(title="Call-Out SOP Agent", version="0.1.0")


@app.get("/health")
async def health():
    return {"status": "ok", "agent": "callout-sop-agent"}


@app.post("/api/reason")
async def reason_endpoint(request: ReasonRequest) -> ReasonResponse:
    """SOP reasoning endpoint — invoked by the orchestrator."""
    logger.info(
        "Reasoning request: step=%s, history_len=%d",
        request.current_step,
        len(request.conversation_history),
    )
    result = await reason(request)
    logger.info(
        "Reasoning result: next=%s, escalate=%s, knowledge=%s",
        result.next_step,
        result.should_escalate,
        result.knowledge_used,
    )
    return result
