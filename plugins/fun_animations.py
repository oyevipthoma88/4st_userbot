# ══════════════════════════════════════════════
# PLUGIN: Fun Animations (Top 15 Public Userbots)
# ══════════════════════════════════════════════
from telethon import events
import asyncio

PLUGIN_NAME = "Fun Animations"
PLUGIN_VERSION = "1.0.0"
PLUGIN_CMDS = [
    ".wtf", ".think", ".heart", ".clock", ".bigoof",
    ".hack", ".fire", ".boom", ".clap", ".love",
    ".magic", ".dance", ".laugh", ".cry", ".sleep",
    ".funlist",
]

ANIM_INTERVAL = 0.35

ANIMATIONS = {
    "wtf": [
        "What", "What The", "What The F",
        "What The F Brah",
        "What The F Brah\nhttps://telegra.ph//file/f3b760e4a99340d331f9b.jpg",
    ],
    "think": [
        "THINKING", "THI&K#N₹", "T+IN@I?G", "¿H$NK∆NG",
        "¶H×NK&N*", "NGITHKIN", "T+I#K@₹G", "THINKING",
    ],
    "heart": [
        "❤️", "🧡", "💛", "💚", "💙", "💜", "🖤", "🤍", "🤎",
        "❤️", "🧡", "💛", "💚", "💙", "💜",
    ],
    "clock": [
        "🕐", "🕑", "🕒", "🕓", "🕔", "🕕", "🕖",
        "🕗", "🕘", "🕙", "🕚", "🕛", "🕐",
    ],
    "bigoof": [
        "┏━━━┓╋╋╋╋┏━━━┓\n┃┏━┓┃╋╋╋╋┃┏━┓┃\n┃┃╋┃┣┓┏┓┏┫┃╋┃┃\n┃┃╋┃┃┗┛┗┛┃┃╋┃┃\n┃┗━┛┣┓┏┓┏┫┗━┛┃\n┗━━━┛┗┛┗┛┗━━━┛",
        "╭━━━╮╱╱╱╭━╮\n┃╭━╮┃╱╱╱┃╭╯\n┃┃╱┃┣━━┳╯╰╮\n┃┃╱┃┃╭╮┣╮╭╯\n┃╰━╯┃╰╯┃┃┃\n╰━━━┻━━╯╰╯",
        "╭━━━╮╱╱╱╱╱╱╭━╮\n┃╭━╮┃╱╱╱╱╱╱┃╭╯\n┃┃╱┃┣━━┳━━┳╯╰╮\n┃┃╱┃┃╭╮┃╭╮┣╮╭╯\n┃╰━╯┃╰╯┃╰╯┃┃┃\n╰━━━┻━━┻━━╯╰╯",
    ],
    "hack": [
        "💻 Hacking...", "💻 Connecting to server...",
        "💻 Bypassing firewall...", "💻 Injecting payload...",
        "💻 Access granted ✅", "💻 100% HACKED 😈",
    ],
    "fire": [
        "🔥", "🔥🔥", "🔥🔥🔥", "🔥🔥🔥🔥", "🔥🔥🔥🔥🔥",
        "🔥🔥🔥🔥🔥🔥", "🔥🔥🔥🔥🔥🔥🔥",
    ],
    "boom": [
        "💥", "💥💥", "💥💥💥", "💥💥💥💥",
        "💥💥💥💥💥", "💥💥💥💥💥💥",
    ],
    "clap": [
        "👏", "👏👏", "👏👏👏", "👏👏👏👏",
        "👏👏👏👏👏", "👏👏👏👏👏👏",
    ],
    "love": [
        "I❤️L❤️O❤️V❤️E❤️Y❤️O❤️U❤️",
        "I❤️L❤️O❤️V❤️E❤️Y❤️O❤️U❤️❤️",
        "I❤️L❤️O❤️V❤️E❤️Y❤️O❤️U❤️❤️❤️",
    ],
    "magic": [
        "✨", "✨✨", "✨✨✨", "✨✨✨✨", "✨✨✨✨✨",
        "✨✨✨✨✨✨", "✨✨✨✨✨✨✨",
    ],
    "dance": [
        "🕺", "💃", "🕺", "💃", "🕺", "💃", "🕺", "💃",
    ],
    "laugh": [
        "😂", "🤣", "😂", "🤣", "😂", "🤣", "😂", "🤣",
    ],
    "cry": [
        "😢", "😭", "😢", "😭", "😢", "😭", "😢", "😭",
    ],
    "sleep": [
        "😴", "💤", "😴", "💤", "😴", "💤", "😴", "💤",
    ],
}

async def run_animation(event, cmd):
    frames = ANIMATIONS.get(cmd)
    if not frames:
        return
    try:
        await event.delete()
        msg = await event.respond(frames[0])
        for frame in frames[1:]:
            await asyncio.sleep(ANIM_INTERVAL)
            await msg.edit(frame)
    except Exception:
        pass

def setup(client, cfg=None, bot_logger=None, **kwargs):
    log = bot_logger or (lambda tag, msg: print(f"[{tag}] {msg}"))
    log("PLUGIN", f"Loading {PLUGIN_NAME} v{PLUGIN_VERSION}")

    # ── Fun List Command ──────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r"(?i)^\.funlist$"))
    async def fun_list(event):
        await event.delete()
        text = (
            "<b>🎉 Fun Animation Commands</b>\n\n"
            "• <code>.wtf</code> — What The F... animation\n"
            "• <code>.think</code> — THINKING glitch animation\n"
            "• <code>.heart</code> — ❤️ Hearts rotation\n"
            "• <code>.clock</code> — 🕐 Clock rotation\n"
            "• <code>.bigoof</code> — ASCII art animation\n"
            "• <code>.hack</code> — Fake hacking animation\n"
            "• <code>.fire</code> — 🔥 Fire animation\n"
            "• <code>.boom</code> — 💥 Explosion animation\n"
            "• <code>.clap</code> — 👏 Clapping animation\n"
            "• <code>.love</code> — I❤️L❤️O❤️V❤️E❤️Y❤️O❤️U❤️\n"
            "• <code>.magic</code> — ✨ Magic sparkle animation\n"
            "• <code>.dance</code> — 🕺💃 Dance animation\n"
            "• <code>.laugh</code> — 😂🤣 Laughing animation\n"
            "• <code>.cry</code> — 😢😭 Crying animation\n"
            "• <code>.sleep</code> — 😴💤 Sleeping animation\n"
        )
        await event.respond(text, parse_mode="html")

    # ── Animation Handlers ─────────────────────────
    for cmd in ANIMATIONS.keys():
        @client.on(events.NewMessage(
            outgoing=True,
            pattern=rf"(?i)^\.{cmd}$",
        ))
        async def anim_handler(event, _cmd=cmd):
            asyncio.create_task(run_animation(event, _cmd))

    log("PLUGIN", f"{PLUGIN_NAME} loaded — {len(ANIMATIONS)} animations + .funlist")
