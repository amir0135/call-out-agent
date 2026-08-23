"""Generate an animated GIF of the Call-Out Agent architecture and call flow.

Reflects the LIVE pilot deployment (azd env callout-se2):
- Orchestrator + SOP agent on Azure Container Apps (northeurope)
- ACS contoso-acs-eu2 (+1 800 555 0100) with bidirectional media streaming
- Three switchable voice engines (STREAMING_ENGINE):
    chained    Speech STT -> SOP engine + agent -> Speech TTS  (~400 ms, pilot)
    realtime   gpt-realtime-mini speech-to-speech              (~600-750 ms)
    voicelive  Voice Live API + Azure neural voice             (~650-950 ms)
- Foundry resource contoso-ai-foundry (swedencentral) serves Speech,
  gpt-realtime-mini and Voice Live

Output: docs/architecture.gif
"""
from pathlib import Path
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
from matplotlib.animation import PillowWriter
import matplotlib.animation as animation

OUT = Path(__file__).resolve().parent.parent / "docs" / "architecture.gif"
OUT.parent.mkdir(parents=True, exist_ok=True)

# ---- colors ----
C_CUST   = "#2EA043"
C_COMPUTE= "#1F6FEB"
C_AZURE  = "#0078D4"
C_AI     = "#DB61A2"
C_DATA   = "#8B5CF6"
C_PHONE  = "#D97706"
C_BG     = "#0D1117"
C_TEXT   = "#F0F6FC"
C_DIM    = "#30363D"
C_HI     = "#F78166"

# node layout: name -> (x, y, w, h, color, label)
NODES = {
    "ALARM":  (0.3, 5.6, 2.1, 1.1, C_CUST,    "Alarm source\nwebhook / test_call.sh"),
    "ORCH":   (3.1, 5.6, 2.0, 1.1, C_COMPUTE, "Orchestrator\nFastAPI"),
    "SOP":    (5.4, 5.6, 1.8, 1.1, C_DATA,    "SOPs / Contacts\n(JSON)"),
    "ENGINE": (3.1, 3.8, 2.1, 1.3, C_COMPUTE, "Media-stream worker\nchained | realtime\n| voicelive"),
    "AGT":    (5.4, 3.8, 1.8, 1.3, C_COMPUTE, "SOP Agent\ngpt-5-mini"),
    "FOUNDRY":(3.1, 1.6, 3.1, 1.2, C_AI,      "Foundry contoso-ai-foundry\nSpeech \u00b7 gpt-realtime-mini\nVoice Live"),
    "ACS":    (7.9, 5.6, 2.5, 1.1, C_AZURE,   "ACS contoso-acs-eu2\n+1 800 555 0100"),
    "PHONE":  (10.9, 5.6, 1.9, 1.1, C_PHONE,  "Store phone\n(PSTN)"),
    "COSMOS": (7.9, 1.6, 2.5, 1.2, C_DATA,    "Cosmos DB\naudit \u00b7 retry \u00b7 sessions"),
}

# edges: (from, to, label, curve)
EDGES = [
    ("ALARM",  "ORCH",    "POST /api/alarm-intent",        0.0),
    ("ORCH",   "SOP",     "load SOP + contact",            0.0),
    ("ORCH",   "ACS",     "create_call",                   0.25),
    ("ACS",    "PHONE",   "PSTN ring",                     0.25),
    ("ACS",    "ENGINE",  "bidi media WebSocket",          0.15),
    ("ENGINE", "FOUNDRY", "STT (chained)",                 0.25),
    ("ENGINE", "AGT",     "next SOP step",                 0.0),
    ("ENGINE", "FOUNDRY", "TTS / speech-to-speech",       -0.25),
    ("ENGINE", "ACS",     "PCM frames",                   -0.15),
    ("ACS",    "PHONE",   "audio (~400 ms v2v)",          -0.25),
    ("ORCH",   "COSMOS",  "audit + retry state",           0.2),
]

# animation frames: list of (title, highlighted edge indices)
FRAMES = [
    ("1. Alarm fires -> POST /api/alarm-intent (202 + intent_id)",        [0]),
    ("2. Orchestrator loads SOP + contact, dials out via ACS",            [1, 2]),
    ("3. ACS rings the store phone over PSTN",                            [3]),
    ("4. CallConnected -> ACS opens the bidirectional media WebSocket",   [4]),
    ("5. Chained engine (pilot): Speech STT -> SOP agent -> Speech TTS",  [5, 6, 7]),
    ("6. Audio streams back to the caller (~400 ms voice-to-voice)",      [8, 9]),
    ("7. Or one hop: gpt-realtime-mini / Voice Live speech-to-speech",    [4, 7]),
    ("8. end_call -> auto-hangup; audit + retry state in Cosmos DB",      [10]),
    ("Complete call flow",                                                list(range(len(EDGES)))),
]

def node_center(n):
    x, y, w, h, _, _ = NODES[n]
    return x + w / 2, y + h / 2

def draw_static(ax):
    ax.set_xlim(0, 13)
    ax.set_ylim(0, 7.5)
    ax.set_facecolor(C_BG)
    ax.axis("off")

    # subgraph boxes
    ax.add_patch(FancyBboxPatch((2.9, 3.55), 4.5, 3.45,
        boxstyle="round,pad=0.1", linewidth=1.2,
        edgecolor=C_COMPUTE, facecolor="none", linestyle="--"))
    ax.text(5.15, 6.87, "Azure Container Apps - northeurope", color=C_COMPUTE,
            ha="center", fontsize=9, fontweight="bold")

    ax.add_patch(FancyBboxPatch((2.9, 1.35), 7.7, 1.75,
        boxstyle="round,pad=0.1", linewidth=1.2,
        edgecolor=C_AI, facecolor="none", linestyle="--"))
    ax.text(7.15, 2.9, "Azure AI + data - swedencentral", color=C_AI,
            ha="center", fontsize=9, fontweight="bold")

    ax.add_patch(FancyBboxPatch((7.7, 5.35), 5.3, 1.65,
        boxstyle="round,pad=0.1", linewidth=1.2,
        edgecolor=C_AZURE, facecolor="none", linestyle="--"))
    ax.text(10.35, 6.87, "Telephony - Europe", color=C_AZURE, ha="center",
            fontsize=9, fontweight="bold")

    # nodes
    for name, (x, y, w, h, color, label) in NODES.items():
        ax.add_patch(FancyBboxPatch((x, y), w, h,
            boxstyle="round,pad=0.05,rounding_size=0.15",
            linewidth=1.8, edgecolor=color, facecolor=color, alpha=0.85))
        ax.text(x + w / 2, y + h / 2, label, ha="center", va="center",
                color=C_TEXT, fontsize=8.5, fontweight="bold")

def draw_edges(ax, highlight):
    for i, (a, b, label, curve) in enumerate(EDGES):
        ax1, ay1 = node_center(a)
        bx1, by1 = node_center(b)
        active = i in highlight
        color = C_HI if active else C_DIM
        lw = 2.6 if active else 1.0
        alpha = 1.0 if active else 0.5
        arrow = FancyArrowPatch(
            (ax1, ay1), (bx1, by1),
            connectionstyle=f"arc3,rad={curve}",
            arrowstyle="-|>", mutation_scale=14,
            color=color, linewidth=lw, alpha=alpha,
        )
        ax.add_patch(arrow)
        if active:
            mx = (ax1 + bx1) / 2
            my = (ay1 + by1) / 2 + 0.25 + curve * 2
            ax.text(mx, my, label, color=C_HI, ha="center",
                    fontsize=8, fontweight="bold",
                    bbox=dict(boxstyle="round,pad=0.2",
                              facecolor=C_BG, edgecolor=C_HI, linewidth=0.8))

def main():
    fig, ax = plt.subplots(figsize=(13, 7.5), dpi=110)
    fig.patch.set_facecolor(C_BG)

    def render(frame_idx):
        ax.clear()
        draw_static(ax)
        title, hl = FRAMES[frame_idx]
        draw_edges(ax, hl)
        ax.text(6.5, 7.28, "Call-Out Agent  -  Live Pilot Call Flow",
                ha="center", color=C_TEXT, fontsize=14, fontweight="bold")
        ax.text(6.5, 0.05, title, ha="center", color=C_HI,
                fontsize=12, fontweight="bold")

    anim = animation.FuncAnimation(
        fig, render, frames=len(FRAMES), interval=1400, repeat=True,
    )
    writer = PillowWriter(fps=0.8)
    anim.save(OUT, writer=writer)
    print(f"Wrote {OUT}")

if __name__ == "__main__":
    main()
