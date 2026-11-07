"""Pure rendering helpers for the .help and .ping screens.

Kept free of Telethon/main imports so they can be unit-tested directly.

Visibility tiers
  * main owner  – the global OWNER_ID account (sees everything)
  * core owner  – any other logged-in core account (never sees main-owner tools)
  * sudo/member – remote callers (see only what they can run)
"""
from __future__ import annotations

# Commands that only the MAIN bot owner may use (strict owner-only or
# global-scope operations). Core owners must not see them in .help.
MAIN_OWNER_ONLY = frozenset({
    ".scan", ".scanbot", ".scanub", ".scanws", ".scanlog", ".scanids",
    ".growadd", ".mastersync", ".gcsec", ".banall", ".restart", ".fbcast",
})

_OWNER_SECTIONS = [
    ("🔥", "COMBAT", ".raid .rraid .spam .gspam .fuck .ow .ghost .sraid .stopsraid .multi .stop"),
    ("📢", "BROADCAST", ".bcast .tagall .otagall .onetag .targetlist .addtarget .rmtarget .rmtargetall .fbcast"),
    ("🏷️", "OW CUSTOM", ".owtarget .rmowtarget .addowline .rmowline .nameow .naow .lineow .stopow .stopfuck"),
    ("💫", "AUTO REACT &amp; NC", ".ar .autoreaction .raidnc .frostnc .apexnc .voidnc .snc .shutupnc .doggync .stopnc"),
    ("👑", "ADMIN", ".ban .unban .mute .unmute .promote .demote .banall .pin .unpin .purge .warn .unwarn .warnlist .delall"),
    ("🛡️", "SECURITY", ".dmsec .gcsec .safemode .sudoadd .sudoaddfull .sudorm .sudolist .mastersync .config"),
    ("🔍", "SCAN", ".scan .scanbot .scanub .scanws .scanlog .scanids"),
    ("🌱", "GROW", ".growadd .forcejoin .fjadd .fjrm"),
    ("🤖", "AI &amp; FUN", ".setmode .mimic .fun .anim .dice .hack .magic .bomb .moon .rocket .hearts"),
    ("🎭", "ANIMATIONS", ".funlist .animlist .wtfbro .funheart .funhack .iloveyou .sparkle .strhack .strbomb .hackpro .lovewave .groove .boo"),
    ("⌨️", "FAKE ACTION", ".faketyping .ftyping .stoptyping"),
    ("🛠️", "TOOLS", ".tr .weather .ytdl .calc .kang .qout .rev .upper .lower .info .whois .clone .revert .unclone .nh .id .afk"),
    ("⚙️", "SETTINGS", ".setbrand .resetbrand .speed .font .typing .startpic .cmnd .rmcmnd .cmndrmall .restart"),
    ("🎵", "MUSIC", ".play .vplay .skip .pause .resume .playforce .queue .loop .mstatus .mend .forall .forme"),
]

_USER_SECTIONS = [
    ("🔥", "COMBAT", ".raid .rraid .spam .gspam .fuck .ow .ghost .sraid .stopsraid .multi .stop"),
    ("📢", "BROADCAST", ".bcast .tagall .otagall .onetag .targetlist .addtarget .rmtarget .rmtargetall"),
    ("🌱", "GROW", ".forcejoin"),
    ("🏷️", "OW CUSTOM", ".owtarget .rmowtarget .addowline .rmowline .nameow .naow .lineow .stopow .stopfuck"),
    ("💫", "AUTO REACT &amp; NC", ".ar .autoreaction .raidnc .frostnc .apexnc .voidnc .snc .shutupnc .doggync .stopnc"),
    ("👑", "ADMIN", ".ban .unban .mute .unmute .promote .demote .pin .unpin .purge .warn .unwarn .warnlist .delall"),
    ("🛡️", "SECURITY", ".dmsec .safemode .config"),
    ("🤖", "AI &amp; FUN", ".setmode .mimic .fun .anim .dice .hack .magic .bomb .moon .rocket .hearts"),
    ("🎭", "ANIMATIONS", ".funlist .animlist .wtfbro .funheart .funhack .iloveyou .sparkle .strhack .strbomb .hackpro .lovewave .groove .boo"),
    ("🛠️", "TOOLS", ".tr .weather .ytdl .calc .kang .qout .rev .upper .lower .info .whois .clone .revert .unclone .nh .id .afk"),
    ("⚙️", "SETTINGS", ".speed .font .typing .cmnd .rmcmnd"),
    ("🎵", "MUSIC", ".play .vplay .skip .pause .resume .queue .loop .mstatus .mend"),
]

_SUDO_EXTRA = {"SECURITY": [".sudolist"], "MUSIC": [".playforce"]}


def build_sections(role: str = "member", *, is_owner: bool = False, is_main_owner: bool = False):
    """Return [(icon, title, [cmds])] for the viewer, dropping empty sections."""
    if is_owner:
        base = _OWNER_SECTIONS
    else:
        base = _USER_SECTIONS
    out = []
    for icon, title, cmds in base:
        lst = cmds.split()
        if is_owner and not is_main_owner:
            lst = [c for c in lst if c not in MAIN_OWNER_ONLY]
        if not is_owner and role in ("sudo_1", "sudo_2"):
            lst = lst + _SUDO_EXTRA.get(title, [])
        if lst:
            out.append((icon, title, lst))
    return out


def _section_text(sections) -> str:
    blocks = []
    for icon, title, cmds in sections:
        body = "  ".join(f"<code>{c}</code>" for c in cmds)
        blocks.append(f"{icon} <b>{title}</b>  <i>[{len(cmds)}]</i>\n└ {body}")
    return "\n\n".join(blocks)


def render_help(brand: str, my_id: int, *, role: str, is_owner: bool,
                is_main_owner: bool, custom_triggers: int = 0) -> str:
    sections = build_sections(role, is_owner=is_owner, is_main_owner=is_main_owner)
    total = sum(len(c) for _, _, c in sections)
    link = f"<a href='tg://user?id={my_id}'>{brand}</a>"
    if is_owner:
        title = "MASTER CONTROL" if is_main_owner else "OWNER CONTROL"
        access = "Main Owner" if is_main_owner else "Core Owner"
        head = (f"👑 <b>{brand}</b> · <b>{title}</b> 👑\n"
                "<i>✨ Elite Telegram Userbot ✨</i>")
        stats = (f"🔐 <b>Access</b> ➜ <code>{access}</code>\n"
                 f"🧩 <b>Modules</b> ➜ <code>{len(sections)}</code>\n"
                 f"⚡ <b>Commands</b> ➜ <code>{total}</code>\n"
                 f"🎯 <b>Custom Triggers</b> ➜ <code>{custom_triggers}</code>")
        who = "Owner"
    else:
        label = {"sudo_2": "⚡ Sudo Admin (L2)", "sudo_1": "⚡ Sudo User (L1)"}.get(role, "👤 Member")
        head = (f"✨ <b>{brand}</b> · <b>HELP MENU</b> ✨\n"
                "<i>⚡ Commands available to you ⚡</i>")
        stats = (f"🔐 <b>Access</b> ➜ <code>{label}</code>\n"
                 f"⚡ <b>Commands</b> ➜ <code>{total}</code>")
        who = "Bot Owner"
    return (
        f"<blockquote>{head}</blockquote>\n"
        f"<blockquote>{stats}</blockquote>\n"
        f"<blockquote expandable>{_section_text(sections)}</blockquote>\n"
        "<blockquote>"
        "💡 <code>.how &lt;cmd&gt;</code> ➜ syntax &amp; examples\n"
        f"👤 <b>{who}</b> ➜ {link}"
        "</blockquote>"
    )


def signal_bars(ms) -> int:
    if not isinstance(ms, (int, float)):
        return 0
    return 5 if ms < 100 else 4 if ms < 250 else 3 if ms < 500 else 2 if ms < 1000 else 1


def render_ping(brand: str, my_id: int, *, ms, uptime: str, music: str, safe: str,
                py: str, caller: str = "") -> str:
    bars = signal_bars(ms)
    bar = "▰" * bars + "▱" * (5 - bars)
    if isinstance(ms, (int, float)):
        grade = ("🟢 Excellent" if ms < 100 else "🟢 Fast" if ms < 250
                 else "🟡 Average" if ms < 500 else "🟠 Slow" if ms < 1000 else "🔴 Laggy")
        ms_s = f"{ms} ms"
    else:
        grade, ms_s = "🔴 Timeout", "Timeout"
    return (
        "<blockquote>"
        f"⚡ <b>{brand}</b> · <b>ONLINE</b> ⚡\n"
        "<i>✨ High-Performance Prime Engine ✨</i>"
        "</blockquote>\n"
        "<blockquote>"
        f"🏓 <b>Pong!</b> ➜ <code>{ms_s}</code>\n"
        f"📶 <b>Signal</b> ➜ <code>{bar}</code> {grade}"
        "</blockquote>\n"
        "<blockquote>"
        f"⏱️ <b>Uptime</b> ➜ <code>{uptime}</code>\n"
        f"🎵 <b>VC Music</b> ➜ {music}\n"
        f"🛡️ <b>Safe Mode</b> ➜ {safe}\n"
        f"⚙️ <b>Engine</b> ➜ <code>{py}</code>"
        "</blockquote>\n"
        "<blockquote>"
        f"👑 <b>Master</b> ➜ <a href='tg://user?id={my_id}'>{brand}</a>{caller}\n"
        "💡 <code>.help</code> all commands · <code>.how &lt;cmd&gt;</code> usage"
        "</blockquote>"
    )
