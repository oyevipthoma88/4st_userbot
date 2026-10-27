import asyncio
from telethon import events

ANIMATIONS = {
    "love": ["❤️", "🧡", "💛", "💚", "💙", "💜", "🖤", "🤍", "💖", "I LOVE YOU! 💕"],
    "magic": ["🪄", "✨", "🌟", "💫", "Abra Ka Dabra!", "🎩✨ TA-DA!"],
    "bomb": ["💣", "💣 3...", "💣 2...", "💣 1...", "💥 BOOM! 💥"],
    "hack": ["💻 Connecting to mainframe...", "📡 Bypassing Firewall...", "🔓 Decrypting Hashes...", "💉 Injecting Payload...", "✅ System Hacked Successfully!"],
    "moon": ["🌑", "🌒", "🌓", "🌔", "🌕", "🌖", "🌗", "🌘", "🌑"],
    "clock": ["🕐", "🕑", "🕒", "🕓", "🕔", "🕕", "🕖", "🕗", "🕘", "🕙", "🕚", "🕛"],
    "loading": ["[░░░░░░░░░░] 0%", "[██░░░░░░░░] 20%", "[████░░░░░░] 40%", "[██████░░░░] 60%", "[████████░░] 80%", "[██████████] 100% Complete!"],
    "rain": ["☁️", "🌧️", "⛈️", "🌩️", "⚡ Flash!", "🌈 Rainbow!"],
    "dance": ["(ง ˙ω˙)ว", "(ง ˙ω˙)ว ⁾⁾", "└( ＾ω＾ )┘", "┏(･o･)┛", "♪┏(・o･)┛♪"],
    "heart": ["🖤", "🤎", "💜", "💙", "💚", "💛", "🧡", "❤️", "💖 Love You!"],
    "car": ["🚗......", "..🚗....", "....🚗..", "......🚗💨 Zoom!"],
    "plane": ["🛫 Taking off...", "✈️ In air...", "🛬 Landed safely!"],
    "police": ["🚨 WIU WIU 🚨", "🚔 Police arriving!", "🚨 Hands in the air! 🚨"],
    "money": ["💸", "💵", "💴", "💶", "💷", "💰 Rich Mode ON! 🤑"],
    "fire": ["🪵", "🔥", "🔥🔥", "🔥🔥🔥 Too Hot! 🚒"],
    "ghost": ["👀", "👻 Boo!", "🏃‍♂️ Running away... 💨"],
    "boxing": ["🥊 (•_•)", "(•_•) 🥊 (x_x)", "🏆 K.O. Winner!"],
    "battery": ["🪫 10%", "🔋 30%", "🔋 60%", "🔋 90%", "⚡ 100% Fully Charged!"]
}

async def play_anim(event, frames, delay=0.35):
    for frame in frames:
        try:
            await event.edit(frame)
            await asyncio.sleep(delay)
        except Exception:
            break

def setup(client, **kwargs):
    @client.on(events.NewMessage(outgoing=True, pattern=r"(?i)^\.([a-zA-Z0-9_-]+)$"))
    async def anim_trigger(event):
        cmd = event.pattern_match.group(1).lower()
        if cmd in ANIMATIONS:
            await play_anim(event, ANIMATIONS[cmd])
        elif cmd in ["anim", "animations"]:
            keys = ", ".join([f".{k}" for k in sorted(ANIMATIONS.keys())])
            await event.edit(f"🎭 **Available Animations ({len(ANIMATIONS)}):**\n\n{keys}")

