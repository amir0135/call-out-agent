"""Generate the demo + architecture walkthrough as a Word document."""
from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, RGBColor, Inches

OUT = Path(__file__).resolve().parent.parent / "docs" / "Call-Out-Agent-Demo-Script.docx"


def style(doc: Document) -> None:
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)


def h1(doc, text):
    p = doc.add_heading(text, level=1)
    for r in p.runs:
        r.font.color.rgb = RGBColor(0x1F, 0x3A, 0x5F)


def h2(doc, text):
    p = doc.add_heading(text, level=2)
    for r in p.runs:
        r.font.color.rgb = RGBColor(0x2E, 0x5A, 0x8A)


def h3(doc, text):
    doc.add_heading(text, level=3)


def p(doc, text, bold=False, italic=False):
    para = doc.add_paragraph()
    run = para.add_run(text)
    run.bold = bold
    run.italic = italic
    return para


def say(doc, text):
    """A block meant to be spoken aloud — italic, indented."""
    para = doc.add_paragraph()
    para.paragraph_format.left_indent = Inches(0.3)
    run = para.add_run(f"SAY: \u201c{text}\u201d")
    run.italic = True
    run.font.color.rgb = RGBColor(0x0B, 0x5E, 0x2E)


def do(doc, text):
    """A block meant to be done (action)."""
    para = doc.add_paragraph(style="List Bullet")
    run = para.add_run("DO: ")
    run.bold = True
    run.font.color.rgb = RGBColor(0xB0, 0x40, 0x00)
    para.add_run(text)


def note(doc, text):
    para = doc.add_paragraph()
    run = para.add_run(f"Note: {text}")
    run.italic = True
    run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)


def code(doc, text):
    para = doc.add_paragraph()
    para.paragraph_format.left_indent = Inches(0.25)
    run = para.add_run(text)
    run.font.name = "Consolas"
    run.font.size = Pt(9)


def bullet(doc, text):
    doc.add_paragraph(text, style="List Bullet")


def build():
    doc = Document()
    style(doc)

    # Title
    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run("Call-Out Agent PoC")
    run.bold = True
    run.font.size = Pt(26)
    run.font.color.rgb = RGBColor(0x1F, 0x3A, 0x5F)

    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = sub.add_run("Demo Walkthrough & Production Architecture")
    r.italic = True
    r.font.size = Pt(14)

    doc.add_paragraph()
    tagline = doc.add_paragraph()
    tagline.alignment = WD_ALIGN_PARAGRAPH.CENTER
    tr = tagline.add_run(
        "\u201cAn SOP-constrained AI voice agent that places outbound calls itself, "
        "follows existing procedures exactly, logs everything, and escalates only when needed.\u201d"
    )
    tr.italic = True

    doc.add_page_break()

    # ------------------------------------------------------------------
    # Section 0 — how to use this document
    # ------------------------------------------------------------------
    h1(doc, "How to use this document")
    bullet(doc, "SAY (green, italic) = what to say out loud, roughly word-for-word.")
    bullet(doc, "DO (orange) = the action to perform: run a command, open a file, click a button.")
    bullet(doc, "Note (grey) = backstage reminder for you, do not read out loud.")
    p(doc, "Pause briefly after each bolded beat. Don\u2019t rush. Silence while the call is dialing is fine \u2014 it builds suspense.")

    # ------------------------------------------------------------------
    # Section 1 — setup before the audience arrives
    # ------------------------------------------------------------------
    h1(doc, "1. Pre-demo setup (do this 10 minutes before the audience sits down)")

    h3(doc, "1.0 Required .env keys")
    p(doc, "Before anything else, confirm these keys exist in .env. Without them the stack starts but the call is silent or fails at 502:", italic=True)
    bullet(doc, "ACS_ENDPOINT  (e.g. https://contoso-acs.unitedstates.communication.azure.com)")
    bullet(doc, "ACS_PHONE_NUMBER  (the source caller ID, e.g. +18005550100)")
    bullet(doc, "ACS_CONNECTION_STRING  (full connection string; the container has no AAD creds so this is required)")
    bullet(doc, "COGNITIVE_SERVICES_ENDPOINT  (Speech resource endpoint; required at call-create time for TTS/STT)")
    bullet(doc, "AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_DEPLOYMENT  (for the reasoning agent)")
    bullet(doc, "AZURE_AI_SEARCH_ENDPOINT  (for knowledge RAG)")
    note(doc, "If ACS_CONNECTION_STRING is missing, test_call.sh returns HTTP 502 with 'DefaultAzureCredential failed to retrieve a token'. If COGNITIVE_SERVICES_ENDPOINT is missing, the call connects but ACS rejects TTS with error 8522 and the callee hears silence.")
    do(doc, "Fetch the ACS connection string with:")
    code(doc, "az communication list-key -n contoso-acs -g rg-contoso-callout --query primaryConnectionString -o tsv")
    do(doc, "Confirm the Speech resource endpoint with:")
    code(doc, "az cognitiveservices account show -n speech-contoso -g rg-contoso-callout --query properties.endpoint -o tsv")
    note(doc, "The ACS managed identity must have the 'Cognitive Services User' role on the Speech resource. It already does in rg-contoso-callout; check with: az role assignment list --assignee <acsPrincipalId> --scope <speechResourceId>.")

    h3(doc, "1.1 Open your environment")
    do(doc, "Start Docker Desktop. Wait until the whale icon stops animating.")
    do(doc, "Open VS Code at /Users/Amira/Desktop/call-out-agent.")
    do(doc, "Open these files in tabs, left to right, in this exact order so you can walk them on stage:")
    bullet(doc, "README.md  (big picture)")
    bullet(doc, "infra/main.bicep  (architecture)")
    bullet(doc, "sops/high_temp_alarm.json  (the contract)")
    bullet(doc, "src/agent/prompts/sop_system.txt  (the guardrail)")
    bullet(doc, "src/orchestrator/app.py  (the state machine, optional reference)")
    do(doc, "Open docs/architecture.gif and docs/architecture-prod.gif in two browser tabs as a visual fallback.")

    h3(doc, "1.2 Start the whole stack with ONE command")
    do(doc, "In Terminal A, from the project root:")
    code(doc, "cd /Users/Amira/Desktop/call-out-agent\n./scripts/demo.sh")
    p(doc, "demo.sh handles everything that used to bite you:", italic=True)
    bullet(doc, "Kills whatever else is holding port 8000 (e.g. daily-trends-workflow).")
    bullet(doc, "Cleans up any stale call-out containers.")
    bullet(doc, "Reuses an existing devtunnel if one is running, otherwise starts a new one.")
    bullet(doc, "Writes the current tunnel URL into CALLBACK_BASE_URL in .env.")
    bullet(doc, "Builds and starts both containers (agent + orchestrator) detached.")
    bullet(doc, "Waits for /health on localhost, then on the tunnel.")
    bullet(doc, "Prints the exact test_call.sh command with TUNNEL_URL pre-filled.")
    note(doc, "Expected last line in the output: 'Trigger a test call:' followed by a block with TUNNEL_URL=... and ./scripts/test_call.sh ...")

    h3(doc, "1.3 Open three terminals in this layout")
    p(doc, "Before going live, lay out the terminals so the audience can see each role clearly:", italic=True)
    bullet(doc, "Terminal A \u2014 was used to run demo.sh. Leave visible; contains the green 'Demo is ready' summary.")
    bullet(doc, "Terminal B \u2014 log tail (orchestrator AUDIT events). Start with:")
    code(doc, "docker compose logs -f orchestrator | grep --line-buffered -E 'AUDIT|recognized|utterance|escalat'")
    bullet(doc, "Terminal C \u2014 the trigger terminal. Use the one-liner below (it reads the URL demo.sh already wrote to /tmp) but DO NOT press Enter yet:")
    code(doc, "export TUNNEL_URL=\"$(cat /tmp/callout-tunnel-url)\"\n./scripts/test_call.sh +4512345678 \"$TUNNEL_URL\"")
    note(doc, "Alternatively, copy the exact 'export TUNNEL_URL=...' line that demo.sh printed under 'Trigger a test call' in Terminal A \u2014 it already has the real URL substituted.")

    h3(doc, "1.4 Final preflight")
    do(doc, "Verify health one last time:")
    code(doc, "./scripts/demo.sh status")
    note(doc, "Both 'Local /health' and 'Tunnel /health' must show {\"status\":\"ok\",...}. If the tunnel line is blank, open the URL in a browser once (anonymous devtunnels sometimes need a consent warm-up) and re-run status.")
    do(doc, "Put the phone on the table, silent ringer off, at +45 29 22 94 22. Speakerphone-ready.")
    do(doc, "Have a glass of water nearby. The call takes ~60\u2013120 seconds; don\u2019t narrate over the agent\u2019s voice.")

    # ------------------------------------------------------------------
    # Section 2 — Opening (30s)
    # ------------------------------------------------------------------
    h1(doc, "2. Opening \u2014 frame the problem (\u224830 seconds)")
    say(doc,
        "Today, when an alarm fires at a customer site \u2014 say a freezer going above its "
        "temperature threshold \u2014 a human operator has to call the store, read a checklist, "
        "wait for a response, and decide what to do. It\u2019s repetitive, it\u2019s 24/7, and most "
        "calls end with \u2018yep, we\u2019re on it.\u2019 We built an SOP-constrained voice agent "
        "that makes that call itself, follows the exact same checklist, and only escalates to a "
        "human when it actually needs to.")
    say(doc,
        "The key word here is SOP-constrained. The AI is not free to improvise. The Standard "
        "Operating Procedure is the contract \u2014 the LLM is only allowed to pick the next step "
        "from that contract. That\u2019s how we get the speed of AI with the safety of a checklist.")

    # ------------------------------------------------------------------
    # Section 3 — the live demo
    # ------------------------------------------------------------------
    h1(doc, "3. Live demo \u2014 narrate each beat as it happens")

    h2(doc, "Beat 1 \u2014 Show the SOP (the contract)")
    do(doc, "Switch to sops/high_temp_alarm.json in VS Code.")
    say(doc,
        "This is the SOP. It\u2019s structured JSON \u2014 states, prompts, keywords we accept, "
        "and where to go next. A human wrote this once. The agent never deviates from it.")
    do(doc, "Scroll to the 'ask_awareness' step and point at the 'branches' array.")
    say(doc,
        "Notice each branch has explicit keywords. If the caller says something we can\u2019t "
        "match, we fall to 'unclear' and retry. After two unclear responses, we escalate. No "
        "guessing.")

    h2(doc, "Beat 2 \u2014 Show the guardrail prompt")
    do(doc, "Switch to src/agent/prompts/sop_system.txt.")
    say(doc,
        "This is the system prompt for the reasoning agent. Temperature zero-point-one, and "
        "it\u2019s told it can only return one of the next steps allowed by the SOP. If it tries "
        "to invent a step, the orchestrator refuses it. The SOP engine is the safety boundary, "
        "not the LLM.")

    h2(doc, "Beat 3 \u2014 Show the architecture at a glance")
    do(doc, "Switch to README.md and scroll to the Architecture section.")
    say(doc,
        "Two services. On the left, the orchestrator \u2014 Azure Container Apps, Python, "
        "FastAPI. It owns the call lifecycle and walks the SOP state machine. On the right, the "
        "Foundry hosted agent \u2014 GPT-4o \u2014 which only reasons about the next step. In "
        "between, Azure Communication Services handles the actual phone call, the speech-to-text, "
        "and the text-to-speech. Everything is logged to Cosmos DB; recordings go to Blob "
        "Storage; traces in App Insights.")

    h2(doc, "Beat 4 \u2014 Trigger the alarm")
    do(doc, "Move focus to Terminal C (the trigger terminal, command already typed).")
    say(doc,
        "I\u2019m about to post an alarm intent to the orchestrator. This is the exact same "
        "payload the existing alarm system would send \u2014 we didn\u2019t change anything "
        "upstream.")
    do(doc, "Press Enter. The pre-typed command is:")
    code(doc, "./scripts/test_call.sh +4512345678 \"$TUNNEL_URL\"")
    note(doc, "Expect: HTTP 200 with a JSON body containing a call_id. If you get anything else, the orchestrator is the problem \u2014 see section 8.")
    note(doc, "Payload: alarm for Copenhagen Central Market, Walk-in Cooler #3, 14.2\u00b0C vs 8.0\u00b0C threshold, severity high.")

    h2(doc, "Beat 5 \u2014 Watch the orchestrator logs")
    do(doc, "Point at Terminal B as AUDIT lines stream in.")
    say(doc,
        "You can see every step of the call being logged in real time. "
        "Call initiated. Call connected. SOP step executed. Speech recognized. "
        "Agent response. Every turn is an audit event. In production these go to Cosmos DB; "
        "in dev they go to stdout \u2014 same structure, same data.")

    h2(doc, "Beat 6 \u2014 The phone rings, answer it")
    do(doc, "Put the phone on speaker so the audience can hear.")
    say(doc, "Answer when it rings. I\u2019ll play the role of the store manager.")
    note(doc,
        "Suggested happy-path answers: \u2018Yes, this is the manager speaking.\u2019 / "
        "\u2018Yes, I\u2019m aware.\u2019 / \u2018A technician is on the way.\u2019 / "
        "\u2018Roughly one hour.\u2019 End with a thank-you.")
    note(doc,
        "If you want to show escalation instead: on the second question, answer with gibberish "
        "or silence. After two unclear responses the agent will say it\u2019s escalating.")

    h2(doc, "Beat 7 \u2014 Show the call timeline")
    do(doc, "Switch back to Terminal B once the call ends.")
    say(doc,
        "Here\u2019s the full call timeline \u2014 every utterance, every recognized transcript, "
        "every decision the agent made, every SOP state we moved through. Timestamps on "
        "everything. This is what gets written to Cosmos DB in production, and it\u2019s what an "
        "auditor or a support engineer would look at to replay exactly what happened.")

    # ------------------------------------------------------------------
    # Section 4 — Why the architecture looks this way
    # ------------------------------------------------------------------
    h1(doc, "4. Architecture walkthrough \u2014 why each box is there")
    do(doc, "Open infra/main.bicep. Walk it top to bottom; for each module say the line below.")

    h3(doc, "Two services, not one monolith")
    say(doc,
        "We deliberately split the orchestrator from the reasoning agent. The orchestrator owns "
        "the state machine \u2014 deterministic, testable, no AI. The agent owns language "
        "understanding. If the LLM misbehaves, the orchestrator refuses the step. The SOP is the "
        "contract, the LLM is a suggestion engine.")

    h3(doc, "Azure Communication Services")
    say(doc,
        "Native outbound PSTN calling, built-in Call Automation SDK, STT and TTS. No third-party "
        "telephony stack, no Twilio, no separate SIP trunk. Everything stays in Azure \u2014 one "
        "identity model, one bill, one support channel.")

    h3(doc, "Foundry hosted agent + Azure OpenAI GPT-4o")
    say(doc,
        "We deploy the agent as a Foundry hosted container so we get managed tracing, versioning, "
        "and evaluation out of the box. Temperature is 0.1 for determinism. We picked GPT-4o for "
        "latency \u2014 we need to get a full STT-agent-TTS round-trip under two seconds or the "
        "conversation feels awkward.")

    h3(doc, "Cosmos DB (Serverless, NoSQL)")
    say(doc,
        "Append-only audit store. Every turn of every call is an event document \u2014 partitioned "
        "by callId so reconstructing a call is a single-partition query. Serverless because PoC "
        "traffic is bursty and this keeps cost proportional to actual alarms, not to uptime.")

    h3(doc, "Blob Storage")
    say(doc,
        "Call recordings live here, with the consent notice spoken in the greeting. We retain "
        "them for the period legal requires and then lifecycle-manage them to cool or archive "
        "tiers.")

    h3(doc, "Application Insights + Foundry traces")
    say(doc,
        "End-to-end latency per turn, per call, per tenant. Our SLO target is sub-two-second "
        "turns. This is also where we\u2019ll wire alerts \u2014 for example, if escalation rate "
        "jumps, page the on-call operator.")

    h3(doc, "Azure AI Search + knowledge retriever")
    say(doc,
        "Grounding for customer and equipment context. The agent pulls facts from here rather "
        "than hallucinating store details. If the store has specific quiet hours, a preferred "
        "contact, or equipment quirks, they come from here, not from the model\u2019s training "
        "data.")

    h3(doc, "Managed identity + RBAC everywhere")
    say(doc,
        "Zero connection strings, zero secrets in config. Every service authenticates with its "
        "own managed identity and has exactly the roles it needs \u2014 no broad access, no "
        "shared keys. Cosmos has local auth disabled; only Entra can read the data plane.")

    h3(doc, "Container Apps + ACR")
    say(doc,
        "Scales to zero when idle, scales out on concurrent alarms. An outage that floods the "
        "queue doesn\u2019t take us down; we just spin up more replicas.")

    # ------------------------------------------------------------------
    # Section 5 — Production build-up roadmap
    # ------------------------------------------------------------------
    h1(doc, "5. Production build-up \u2014 PoC today \u2192 pilot \u2192 production")
    say(doc,
        "What you just saw is intentionally narrow. One customer, one alarm type, one SOP. That\u2019s "
        "on purpose \u2014 the hard part isn\u2019t the happy path, it\u2019s proving the "
        "guardrails hold. Here\u2019s how this grows.")

    table = doc.add_table(rows=1, cols=4)
    table.style = "Light Grid Accent 1"
    hdr = table.rows[0].cells
    hdr[0].text = "Layer"
    hdr[1].text = "PoC (today)"
    hdr[2].text = "Pilot"
    hdr[3].text = "Production"
    rows = [
        ("Scope", "1 customer, 1 SOP", "3\u20135 customers, 3 SOP types", "Multi-tenant, SOP catalog"),
        ("SOP authoring", "JSON in repo", "Authoring UI, versioned in Cosmos", "Approval workflow + A/B per tenant"),
        ("Telephony", "1 ACS number", "Number pool, caller-ID per customer", "Regional numbers, DTMF, inbound"),
        ("Voice", "Neural TTS default", "Per-tenant voice, SSML tuning", "Multi-language"),
        ("Reliability", "Retry + escalate", "Circuit breaker, dead-letter queue", "Active-active multi-region"),
        ("Safety", "Hard SOP + low temp", "Red-team eval suite, output guardrails", "Continuous eval, drift detection"),
        ("Compliance", "Consent in greeting", "Legal-approved scripts per region", "Consent matrix, PII redaction, retention"),
        ("Identity", "Managed identity + RBAC", "Private endpoints, VNet integration", "CMK, Entra-only, no public ingress"),
        ("Ops", "App Insights", "SLOs on latency + escalation rate", "Runbooks, chaos testing, call KPIs"),
        ("Cost", "Scale-to-zero ACA, serverless Cosmos", "Reserved OpenAI PTU if volume warrants", "Token budget per call, TTS pre-caching"),
    ]
    for r in rows:
        cells = table.add_row().cells
        for i, val in enumerate(r):
            cells[i].text = val

    # ------------------------------------------------------------------
    # Section 6 — Closing
    # ------------------------------------------------------------------
    h1(doc, "6. Closing \u2014 what the PoC proves (\u224830 seconds)")
    say(doc, "One. AI can reliably place outbound calls.")
    say(doc, "Two. AI follows SOPs exactly \u2014 no deviation.")
    say(doc, "Three. It reduces human call load for routine verification.")
    say(doc, "Four. Nothing critical is missed \u2014 escalation is guaranteed, and everything is auditable.")
    say(doc,
        "The PoC is narrow by design. The architecture scales. What we\u2019re validating is the "
        "safety boundary \u2014 and today you saw it hold.")

    # ------------------------------------------------------------------
    # Section 7 — Q&A cheat sheet
    # ------------------------------------------------------------------
    h1(doc, "7. Q&A cheat sheet \u2014 likely questions and answers")

    h3(doc, "\u201cWhy not just let GPT-4o free-talk?\u201d")
    say(doc,
        "Liability, auditability, and regulation. The SOP is a contract with the customer \u2014 "
        "the LLM doesn\u2019t get to renegotiate it mid-call. And if something goes wrong, we "
        "need to show exactly which step the system executed and why.")

    h3(doc, "\u201cWhat about latency?\u201d")
    say(doc,
        "Target is sub-two-second turns, measured per turn in App Insights. STT plus agent plus "
        "TTS. If we ever need more, we pre-generate TTS for the fixed SOP prompts \u2014 only the "
        "dynamic utterances go through the model at call time.")

    h3(doc, "\u201cWhat happens if the agent is down?\u201d")
    say(doc,
        "Circuit breaker in the Foundry client. If the agent is unavailable, the orchestrator "
        "falls back to escalation \u2014 a human is called. We never silently fail a safety "
        "check.")

    h3(doc, "\u201cHow do you handle consent and recording?\u201d")
    say(doc,
        "The greeting explicitly states the call may be recorded. Recordings live in Blob "
        "Storage with retention and lifecycle policies. For production we\u2019ll have a consent "
        "matrix per region, because the rules differ between Denmark, Germany, the UK, and the "
        "US.")

    h3(doc, "\u201cCan the model be jailbroken during the call?\u201d")
    say(doc,
        "Even if the caller tries, the orchestrator only accepts one of the SOP\u2019s allowed "
        "next steps. If the model returns something else, we ignore it and fall to the unclear "
        "branch, which eventually escalates. Jailbreak at worst becomes escalation.")

    h3(doc, "\u201cWhat does it cost per call?\u201d")
    say(doc,
        "Dominated by OpenAI tokens and ACS minutes. Order of magnitude, a 60\u2013120 second "
        "call is a few cents. Compare that to a human operator answering at 2 AM \u2014 the "
        "business case is about freeing humans for the calls that actually need a human, not "
        "replacing them entirely.")

    # ------------------------------------------------------------------
    # Section 8 — if things go wrong
    # ------------------------------------------------------------------
    h1(doc, "8. If things go wrong during the demo")
    h3(doc, "Anything network-related fails")
    bullet(doc, "First, stop and restart from scratch \u2014 demo.sh handles port conflicts, stale containers, and a dead tunnel in one go:")
    code(doc, "./scripts/demo.sh stop && ./scripts/demo.sh")
    bullet(doc, "Check state at any time with: ./scripts/demo.sh status")
    h3(doc, "Tunnel URL needs refreshing (ACS callback 404s)")
    bullet(doc, "Re-host just the tunnel and let the script sync .env, then recreate the orchestrator:")
    code(doc, "./scripts/demo.sh tunnel\ndocker compose up -d --force-recreate orchestrator")
    h3(doc, "Phone doesn\u2019t ring")
    bullet(doc, "Check orchestrator logs: docker compose logs --tail 80 orchestrator")
    bullet(doc, "Common causes: ACS phone number de-provisioned, CALLBACK_BASE_URL stale, or ACS_ENDPOINT wrong in .env.")
    bullet(doc, "Fall back to docs/architecture.gif (dev flow) or docs/architecture-prod.gif (prod flow) and walk it verbally.")
    h3(doc, "test_call.sh returns HTTP 502 with 'DefaultAzureCredential failed'")
    bullet(doc, "The orchestrator container has no AAD creds. Make sure ACS_CONNECTION_STRING is in .env and propagated to the container:")
    code(doc, "grep ACS_CONNECTION_STRING .env\ndocker compose exec orchestrator printenv ACS_CONNECTION_STRING | head -c 40")
    bullet(doc, "If missing, fetch and recreate the orchestrator:")
    code(doc, "az communication list-key -n contoso-acs -g rg-contoso-callout --query primaryConnectionString -o tsv\n# paste the value into .env as ACS_CONNECTION_STRING=...\ndocker compose up -d --force-recreate orchestrator")
    h3(doc, "Call connects but the callee hears silence (ACS error 8522)")
    bullet(doc, "Means Cognitive Services was not linked at call setup. Confirm COGNITIVE_SERVICES_ENDPOINT is set in both .env and inside the container:")
    code(doc, "grep COGNITIVE_SERVICES_ENDPOINT .env\ndocker compose exec orchestrator printenv COGNITIVE_SERVICES_ENDPOINT")
    bullet(doc, "Then confirm ACS managed identity has 'Cognitive Services User' on the Speech resource:")
    code(doc, "ACS_MI=$(az communication show -n contoso-acs -g rg-contoso-callout --query identity.principalId -o tsv)\nCOG=$(az cognitiveservices account show -n speech-contoso -g rg-contoso-callout --query id -o tsv)\naz role assignment list --assignee \"$ACS_MI\" --scope \"$COG\" --query \"[].roleDefinitionName\" -o tsv")
    bullet(doc, "Recreate orchestrator after any fix: docker compose up -d --force-recreate orchestrator")
    h3(doc, "Agent says something unexpected")
    bullet(doc, "Use it as a teaching moment: \u201cThat\u2019s why the orchestrator validates every step \u2014 watch, the SOP engine refused that and retried.\u201d")
    h3(doc, "Cosmos shows no data")
    bullet(doc, "In dev we log to stdout, not Cosmos \u2014 show the AUDIT lines in the logs tail instead.")
    bullet(doc, "For prod: the portal\u2019s Data Explorer needs a Cosmos data-plane role on your user (role id 00000000-0000-0000-0000-000000000002). Skip during the demo, offer a walkthrough after.")

    # ------------------------------------------------------------------
    # Section 9 — command cheat sheet
    # ------------------------------------------------------------------
    h1(doc, "9. Command cheat sheet (print this separately if you like)")

    h3(doc, "Lifecycle")
    code(doc, "./scripts/demo.sh          # start everything (idempotent)\n./scripts/demo.sh status   # containers + health + tunnel URL\n./scripts/demo.sh tunnel   # re-host tunnel, sync .env\n./scripts/demo.sh stop     # tear down containers + tunnel")

    h3(doc, "Trigger a call")
    code(doc, "export TUNNEL_URL=\"$(cat /tmp/callout-tunnel-url)\"\n./scripts/test_call.sh +4512345678 \"$TUNNEL_URL\"")

    h3(doc, "Watch audit events")
    code(doc, "docker compose logs -f orchestrator | grep --line-buffered -E 'AUDIT|recognized|utterance|escalat'")

    h3(doc, "Inspect a specific container")
    code(doc, "docker compose logs --tail 100 orchestrator\ndocker compose logs --tail 100 agent")

    h3(doc, "Health probes")
    code(doc, "curl -sf http://localhost:8000/health && echo LOCAL_OK\ncurl -sf \"$TUNNEL_URL/health\" && echo TUNNEL_OK")

    h3(doc, "Full reset (nuclear option)")
    code(doc, "./scripts/demo.sh stop\ndocker system prune -f\n./scripts/demo.sh")

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------
    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc.save(OUT)
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    build()
