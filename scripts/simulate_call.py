#!/usr/bin/env python3
"""Interactive local simulator — play the callee in a call-out conversation.

Usage:
    python scripts/simulate_call.py

No Azure resources required. Uses the real SOP engine with a local
rule-based agent that matches keywords from the SOP branch definitions.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from orchestrator.models import (
    AgentResponse,
    AlarmContext,
    CallIntent,
    CallOutcome,
    CallSession,
    SOPStepType,
)
from orchestrator.sop_engine import SOPEngine

# ANSI colours
BLUE = "\033[94m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def local_agent_reason(
    engine: SOPEngine,
    session: CallSession,
    callee_text: str,
) -> AgentResponse:
    """Rule-based local agent — matches SOP branch keywords to decide next step."""
    current = engine.get_current_step(session)

    # Speak steps auto-advance
    if current.type == SOPStepType.SPEAK:
        next_id = current.next
        if next_id:
            next_step = engine.get_step(next_id)
            return AgentResponse(
                next_step=next_id,
                utterance=engine.render_text(next_step, session),
                reasoning=f"Auto-advance from speak step '{current.id}'",
                outcome=current.outcome,
            )
        # Terminal speak step
        return AgentResponse(
            next_step=current.id,
            utterance=engine.render_text(current, session),
            reasoning="Terminal speak step",
            outcome=current.outcome,
        )

    # Ask steps — match callee response to branches
    if current.branches:
        text_lower = callee_text.lower().strip()

        for branch in current.branches:
            # Check keywords first
            for kw in branch.keywords:
                if kw.lower() in text_lower:
                    next_step = engine.get_step(branch.next)
                    return AgentResponse(
                        next_step=branch.next,
                        utterance=engine.render_text(next_step, session),
                        reasoning=f"Matched keyword '{kw}' → branch '{branch.match}'",
                        outcome=next_step.outcome,
                    )

            # Semantic fallbacks
            if branch.match == "yes" and any(
                w in text_lower for w in ["yes", "yeah", "yep", "sure", "correct", "right", "aware", "know", "noticed"]
            ):
                next_step = engine.get_step(branch.next)
                return AgentResponse(
                    next_step=branch.next,
                    utterance=engine.render_text(next_step, session),
                    reasoning=f"Semantic match → '{branch.match}'",
                    outcome=next_step.outcome,
                )
            if branch.match == "no" and any(
                w in text_lower for w in ["no", "nope", "not", "haven't", "didn't", "nothing"]
            ):
                next_step = engine.get_step(branch.next)
                return AgentResponse(
                    next_step=branch.next,
                    utterance=engine.render_text(next_step, session),
                    reasoning=f"Semantic match → '{branch.match}'",
                    outcome=next_step.outcome,
                )
            if branch.match == "resolved" and any(
                w in text_lower for w in ["yes", "resolved", "fixed", "handled", "confirmed", "done", "ok", "okay"]
            ):
                next_step = engine.get_step(branch.next)
                return AgentResponse(
                    next_step=branch.next,
                    utterance=engine.render_text(next_step, session),
                    reasoning=f"Semantic match → '{branch.match}'",
                    outcome=next_step.outcome,
                )
            if branch.match in ("need_help", "unclear") and any(
                w in text_lower for w in ["help", "send", "dispatch", "confused", "what", "huh", "don't understand"]
            ):
                next_step = engine.get_step(branch.next)
                return AgentResponse(
                    next_step=branch.next,
                    utterance=engine.render_text(next_step, session),
                    reasoning=f"Semantic match → '{branch.match}'",
                    outcome=next_step.outcome,
                )

        # No branch matched → unclear
        engine.increment_unclear(session)
        if engine.should_escalate_unclear(session):
            return AgentResponse(
                next_step="escalate_unclear",
                utterance="I apologize, I'm having difficulty understanding. Let me connect you with a human operator.",
                reasoning="Max unclear responses exceeded",
                should_escalate=True,
                outcome="escalate_human",
            )

        # Retry — stay on current step
        return AgentResponse(
            next_step=current.id,
            utterance=f"I'm sorry, I didn't quite catch that. {engine.render_text(current, session)}",
            reasoning=f"Unclear response (count: {session.unclear_count})",
        )

    # No branches — shouldn't happen for ask steps
    return AgentResponse(
        next_step="escalate_unclear",
        utterance="I apologize, let me connect you with a human operator.",
        reasoning="No branches defined for ask step",
        should_escalate=True,
        outcome="escalate_human",
    )


def run_simulation():
    """Run the interactive call simulation."""
    print(f"\n{BOLD}{'='*60}{RESET}")
    print(f"{BOLD}  Contoso Call-Out Agent — Local Simulator{RESET}")
    print(f"{BOLD}{'='*60}{RESET}")
    print(f"{DIM}  You are the store manager receiving a call about a")
    print(f"  high temperature alarm. Type your responses naturally.{RESET}")
    print(f"{DIM}  Type 'quit' to exit at any time.{RESET}")
    print(f"{'='*60}\n")

    # Set up alarm context
    alarm = AlarmContext(
        alarm_id="ALM-SIM-001",
        alarm_type="high_temperature",
        severity="high",
        store_name="Copenhagen Central Market",
        store_id="ST-CPH-01",
        equipment_name="Walk-in Cooler #3",
        current_temp=14.2,
        threshold_temp=8.0,
        alarm_time="2026-04-16T10:30:00Z",
        customer_id="CUST-CCC-001",
    )
    intent = CallIntent(
        intent_id="INT-SIM-001",
        alarm=alarm,
        phone_number="+45-555-0100",
    )
    session = CallSession(call_id="CALL-SIM-001", intent=intent)

    # Load SOP engine
    engine = SOPEngine()
    engine.load()

    print(f"{DIM}📞 Dialing +45-555-0100 ...{RESET}")
    print(f"{DIM}📞 Call connected.{RESET}\n")

    # Start with greeting (speak step — auto-plays)
    step = engine.get_current_step(session)
    text = engine.render_text(step, session)
    print(f"{BLUE}{BOLD}🤖 Agent:{RESET} {text}\n")
    session.conversation_history.append({"role": "agent", "text": text})

    # Auto-advance past greeting
    response = local_agent_reason(engine, session, "")
    next_step, outcome = engine.advance_session(session, response)

    while True:
        if outcome:
            # Terminal state reached
            final_text = response.utterance
            print(f"{BLUE}{BOLD}🤖 Agent:{RESET} {final_text}\n")
            session.conversation_history.append({"role": "agent", "text": final_text})
            break

        # Current step should be an ask — prompt the callee
        step = engine.get_current_step(session)

        if step.type == SOPStepType.SPEAK:
            # Speak step — agent talks, then auto-advance
            text = engine.render_text(step, session)
            print(f"{BLUE}{BOLD}🤖 Agent:{RESET} {text}\n")
            session.conversation_history.append({"role": "agent", "text": text})
            response = local_agent_reason(engine, session, "")
            next_step, outcome = engine.advance_session(session, response)
            continue

        # Ask step — show the question text and wait for callee input
        text = engine.render_text(step, session)
        print(f"{BLUE}{BOLD}🤖 Agent:{RESET} {text}\n")
        session.conversation_history.append({"role": "agent", "text": text})

        try:
            callee_input = input(f"{GREEN}{BOLD}👤 You:{RESET}  ")
        except (EOFError, KeyboardInterrupt):
            print(f"\n{RED}📞 Call ended by user.{RESET}")
            return

        if callee_input.strip().lower() == "quit":
            print(f"\n{RED}📞 Call ended by user.{RESET}")
            return

        print()
        session.conversation_history.append({"role": "callee", "text": callee_input})

        # Get agent reasoning
        response = local_agent_reason(engine, session, callee_input)

        print(f"{DIM}   ├─ SOP step: {session.current_step} → {response.next_step}{RESET}")
        print(f"{DIM}   └─ Reasoning: {response.reasoning}{RESET}\n")

        next_step, outcome = engine.advance_session(session, response)

    # Call complete
    outcome_colors = {
        CallOutcome.RESOLVED: GREEN,
        CallOutcome.RETRY_LATER: YELLOW,
        CallOutcome.ESCALATE_HUMAN: RED,
    }
    color = outcome_colors.get(session.outcome, DIM)

    print(f"\n{'='*60}")
    print(f"{BOLD}📞 Call ended.{RESET}")
    print(f"   Outcome: {color}{BOLD}{session.outcome.value if session.outcome else 'unknown'}{RESET}")
    print(f"   Steps taken: {len(session.conversation_history)} turns")
    print(f"{'='*60}")

    # Show conversation transcript
    print(f"\n{DIM}{BOLD}Transcript:{RESET}")
    for turn in session.conversation_history:
        role = turn["role"]
        text = turn["text"]
        if role == "agent":
            print(f"{DIM}  🤖 {text}{RESET}")
        else:
            print(f"{DIM}  👤 {text}{RESET}")
    print()


if __name__ == "__main__":
    run_simulation()
