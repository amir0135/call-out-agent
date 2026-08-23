"""Generate an animated GIF of the PRODUCTION architecture and call flow.

Output: docs/architecture-prod.gif

Differs from the pilot gif (docs/scalability.md is the companion doc):
- Queued intake: Service Bus (msg-dedup on intent_id) + KEDA scale 1 -> 50
- Shared state in Cosmos (call_sessions, retry_state, audit) - any replica
  can serve any webhook / media-WS event
- SOP agent on gpt-5-mini, grounded in AI Search
- Three voice engines over the ACS bidirectional media WebSocket:
  chained (Speech STT/TTS) | realtime (gpt-realtime-mini) | Voice Live
- Region router: callee prefix -> local ACS resource + number
- Terminal outcome pushed back via HMAC-signed webhook + status APIs
- Managed identity + RBAC on every hop
"""
from pathlib import Path
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
from matplotlib.animation import PillowWriter
import matplotlib.animation as animation

OUT = Path(__file__).resolve().parent.parent / "docs" / "architecture-prod.gif"
OUT.parent.mkdir(parents=True, exist_ok=True)

# ---- palette ----
C_CUST   = "#2EA043"
C_AZURE  = "#0078D4"
C_COMPUTE= "#1F6FEB"
C_DATA   = "#8B5CF6"
C_AI     = "#DB61A2"
C_PHONE  = "#D97706"
C_BG     = "#0D1117"
C_TEXT   = "#F0F6FC"
C_DIM    = "#30363D"
C_HI     = "#F78166"
C_OBS    = "#58A6FF"

# node layout: name -> (x, y, w, h, color, label)
NODES = {
    # Customer side
    "ALARM": (0.4, 5.7, 2.2, 1.0, C_CUST,   "Customer alarm\nsystems (multi-tenant)"),
    "HOOK":  (0.4, 3.9, 2.2, 1.0, C_CUST,   "Customer webhook\n+ status APIs"),
    "TENANT":(0.4, 2.1, 2.2, 0.9, C_CUST,   "Tenant registry\n+ SOP catalog"),

    # Intake / compute
    "SB":    (3.2, 5.7, 2.0, 1.0, C_AZURE,  "Service Bus\nalarm-intake\n(dedup)"),
    "ORCH":  (3.2, 3.8, 2.0, 1.2, C_COMPUTE,"Orchestrator\nContainer Apps\n1-50 replicas"),
    "STORES":(3.2, 2.0, 2.0, 0.9, C_DATA,   "Cosmos DB\nsessions \u00b7 retry \u00b7 audit"),

    # AI layer
    "AGT":   (5.8, 5.7, 2.2, 1.0, C_AI,     "SOP Agent\ngpt-5-mini"),
    "FOUNDRY":(5.8, 3.8, 2.2, 1.2, C_AI,    "Foundry\nSpeech \u00b7 gpt-realtime-mini\nVoice Live"),
    "SEARCH":(5.8, 2.0, 2.2, 0.9, C_AI,     "AI Search\n(grounding)"),

    # Telephony
    "ACS":   (8.6, 4.9, 2.2, 1.1, C_AZURE,  "ACS Call Automation\nregion-routed\nnumbers"),
    "PHONE": (11.2, 4.9, 1.8, 1.1, C_PHONE, "Store phone\n(PSTN)"),

    # Data / audit
    "BLOB":  (8.6, 2.0, 2.2, 0.9, C_DATA,   "Blob Storage\nrecordings"),

    # Observability / identity
    "APPI":  (3.2, 0.4, 4.6, 0.8, C_OBS,    "Application Insights  -  Log Analytics"),
    "ENTRA": (8.6, 0.4, 4.4, 0.8, C_OBS,    "Microsoft Entra ID  -  Managed Identity + RBAC"),
}

# edges
EDGES = [
    ("ALARM",  "SB",     "POST /api/alarm-intent -> 202",  0.0),
    ("SB",     "ORCH",   "KEDA queue scaler",              0.0),
    ("ORCH",   "TENANT", "tenant ctx + SOP",               0.0),
    ("ORCH",   "STORES", "shared call state",              0.0),
    ("ORCH",   "AGT",    "reason: next step",              0.2),
    ("AGT",    "SEARCH", "ground facts",                   0.45),
    ("ORCH",   "ACS",    "create_call (region router)",    0.15),
    ("ACS",    "PHONE",  "PSTN audio",                     0.0),
    ("ACS",    "ORCH",   "bidi media WS -> any replica",  -0.25),
    ("ORCH",   "FOUNDRY","STT/TTS or speech-to-speech",    0.0),
    ("ACS",    "BLOB",   "recording",                      0.0),
    ("ORCH",   "HOOK",   "outcome POST (HMAC)",            0.15),
    ("ORCH",   "APPI",   "traces + metrics",               0.0),
    ("FOUNDRY","APPI",   "engine traces",                  0.0),
    ("ORCH",   "ENTRA",  "managed identity",               0.1),
    ("AGT",    "ENTRA",  "managed identity",               0.2),
    ("ACS",    "ENTRA",  "managed identity",               0.0),
]

FRAMES = [
    ("1. Alarm hits the intake API -> queued in Service Bus (idempotent 202)",
        [0]),
    ("2. KEDA drains the queue - orchestrator scales 1 -> 50 replicas",
        [1]),
    ("3. Tenant context, SOP and shared call state (any replica can serve)",
        [2, 3]),
    ("4. SOP agent reasons with gpt-5-mini, grounded in AI Search",
        [4, 5]),
    ("5. Region router picks the local ACS + number; outbound call placed",
        [6, 7]),
    ("6. Bidi media WebSocket - chained | realtime | voicelive engines",
        [8, 9]),
    ("7. Recording to Blob; every turn audited in Cosmos",
        [10, 3]),
    ("8. Terminal outcome pushed back - HMAC-signed webhook + status APIs",
        [11]),
    ("9. Traces and latency metrics stream to App Insights",
        [12, 13]),
    ("10. Every hop authenticates with managed identity via Entra",
        [14, 15, 16]),
    ("Full production topology - 200k calls/month",
        list(range(len(EDGES)))),
]


def node_center(n):
    x, y, w, h, _, _ = NODES[n]
    return x + w / 2, y + h / 2


def draw_static(ax):
    ax.set_xlim(0, 13.4)
    ax.set_ylim(0, 7.6)
    ax.set_facecolor(C_BG)
    ax.axis("off")

    # subgraph: Customer
    ax.add_patch(FancyBboxPatch((0.2, 1.8), 2.6, 5.2,
        boxstyle="round,pad=0.1", linewidth=1.2,
        edgecolor=C_CUST, facecolor="none", linestyle="--"))
    ax.text(1.5, 6.9, "Customer / integration", color=C_CUST,
            ha="center", fontsize=9, fontweight="bold")

    # subgraph: Azure platform
    ax.add_patch(FancyBboxPatch((3.0, 1.7), 5.2, 5.3,
        boxstyle="round,pad=0.1", linewidth=1.2,
        edgecolor=C_COMPUTE, facecolor="none", linestyle="--"))
    ax.text(5.6, 6.9, "Azure - Compute & AI (queued, scale-out)",
            color=C_COMPUTE, ha="center", fontsize=9, fontweight="bold")

    # subgraph: Telephony + Data
    ax.add_patch(FancyBboxPatch((8.4, 1.7), 4.8, 4.6,
        boxstyle="round,pad=0.1", linewidth=1.2,
        edgecolor=C_AZURE, facecolor="none", linestyle="--"))
    ax.text(10.8, 6.15, "Azure - Telephony (per region), Recordings",
            color=C_AZURE, ha="center", fontsize=9, fontweight="bold")

    # nodes
    for name, (x, y, w, h, color, label) in NODES.items():
        ax.add_patch(FancyBboxPatch((x, y), w, h,
            boxstyle="round,pad=0.05,rounding_size=0.15",
            linewidth=1.8, edgecolor=color, facecolor=color, alpha=0.85))
        ax.text(x + w / 2, y + h / 2, label, ha="center", va="center",
                color=C_TEXT, fontsize=8.2, fontweight="bold")


def draw_edges(ax, highlight):
    for i, (a, b, label, curve) in enumerate(EDGES):
        ax1, ay1 = node_center(a)
        bx1, by1 = node_center(b)
        active = i in highlight
        color = C_HI if active else C_DIM
        lw = 2.6 if active else 0.9
        alpha = 1.0 if active else 0.35
        arrow = FancyArrowPatch(
            (ax1, ay1), (bx1, by1),
            connectionstyle=f"arc3,rad={curve}",
            arrowstyle="-|>", mutation_scale=13,
            color=color, linewidth=lw, alpha=alpha,
        )
        ax.add_patch(arrow)
        if active:
            mx = (ax1 + bx1) / 2
            my = (ay1 + by1) / 2 + 0.22 + curve * 2
            ax.text(mx, my, label, color=C_HI, ha="center",
                    fontsize=7.5, fontweight="bold",
                    bbox=dict(boxstyle="round,pad=0.2",
                              facecolor=C_BG, edgecolor=C_HI, linewidth=0.8))


def main():
    fig, ax = plt.subplots(figsize=(13.4, 7.6), dpi=110)
    fig.patch.set_facecolor(C_BG)

    def render(frame_idx):
        ax.clear()
        draw_static(ax)
        title, hl = FRAMES[frame_idx]
        draw_edges(ax, hl)
        ax.text(6.7, 7.35, "Call-Out Agent  -  Production Architecture (200k calls/mo)",
                ha="center", color=C_TEXT, fontsize=14, fontweight="bold")
        ax.text(6.7, 0.03, title, ha="center", color=C_HI,
                fontsize=11.5, fontweight="bold")

    anim = animation.FuncAnimation(
        fig, render, frames=len(FRAMES), interval=1600, repeat=True,
    )
    writer = PillowWriter(fps=0.7)
    anim.save(OUT, writer=writer)
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
