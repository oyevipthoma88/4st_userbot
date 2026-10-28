# plugins/autoreaction.py — .ar / .autoreaction
# Reacts to chat messages automatically. Logic lives in autoreact_engine.py.
import asyncio
import random
import re
import time

from telethon import events, errors, types
from telethon.tl.functions.messages import SendReactionRequest, GetFullChatRequest
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.types import ReactionEmoji

import autoreact_engine as eng
from plugins._common import is_authorized

PLUGIN_NAME = "Auto Reaction"
PLUGIN_CMDS = [".ar", ".autoreaction"]

HELP = (
    "<blockquote><b>💫 AUTO REACTION</b>\n"
    "<code>.ar on</code> / <code>.ar off</code> — is chat me chalu/band\n"
    "<code>.ar random</code> — har msg pe random reaction\n"
    "<code>.ar same 🔥</code> — sabke msg pe same reaction\n"
    "<code>.ar who all|me|others|users</code> — kiske msg pe\n"
    "   all = sab + apne, me = sirf apne, others = sirf dusre,\n"
    "   users = sirf <code>.ar user</code> wale\n"
    "<code>.ar user [reply|id] [emoji]</code> / <code>.ar unuser</code>\n"
    "<code>.ar ignore [reply|id]</code> / <code>.ar unignore</code>\n"
    "<code>.ar pool 🔥❤️😂</code> — random pool (<code>.ar pool reset</code>)\n"
    "<code>.ar chance 70</code> · <code>.ar delay 1.5</code> · "
    "<code>.ar multi 3</code> (premium) · <code>.ar bots on|off</code>\n"
    "<code>.ar status</code> · <code>.ar reset</code>\n"
    "Sabhi chats ke liye: <code>.ar -g on</code> (har command ke aage <code>-g</code>)."
    "</blockquote>"
)

_PENDING_MAX = 8
_ALLOWED_TTL = 600.0


def _norm_set(items):
    return {eng.norm_emoji(x) for x in items}


def setup(client, cfg=None, bot_logger=None, **kwargs):
    saver = kwargs.get("save_config")
    log = bot_logger or (lambda t, m: print(f"[{t}] {m}"))
    if cfg is None:
        cfg = {}
    st = {
        "me": None,
        "locks": {},          # chat -> Lock (serialises + spaces reactions)
        "next_ok": {},        # chat -> monotonic time of next allowed reaction
        "pending": {},        # chat -> in-flight count
        "bad": {},            # chat -> {normalised emoji the chat rejected}
        "allowed": {},        # chat -> (expiry, set|None)
        "flood_until": 0.0,   # account-wide FloodWait cooldown
        "seen": set(),        # (chat, msg) already handled
    }

    def save():
        if callable(saver):
            try:
                saver(cfg)
            except Exception as e:
                log("AR_ERR", f"save failed: {e}")

    async def me_id():
        if st["me"] is None:
            st["me"] = (await client.get_me()).id
        return st["me"]

    async def say(event, text, ttl=12):
        try:
            m = await client.send_message(event.chat_id, text, parse_mode="html")
        except Exception:
            return
        if ttl:
            async def _later():
                await asyncio.sleep(ttl)
                try:
                    await m.delete()
                except Exception:
                    pass
            asyncio.create_task(_later())

    async def allowed_for(chat_id, entity):
        """Normalised set of reactions this chat accepts (None = unknown/all)."""
        hit = st["allowed"].get(chat_id)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        res = None
        try:
            if isinstance(entity, types.Channel):
                full = await client(GetFullChannelRequest(entity))
            elif isinstance(entity, types.Chat):
                full = await client(GetFullChatRequest(entity.id))
            else:
                full = None
            ar = getattr(getattr(full, "full_chat", None), "available_reactions", None)
            kind = type(ar).__name__
            if kind == "ChatReactionsNone":
                res = set()
            elif kind == "ChatReactionsSome":
                res = _norm_set(getattr(r, "emoticon", "") for r in ar.reactions
                                if getattr(r, "emoticon", ""))
        except errors.FloodWaitError as fw:
            st["flood_until"] = time.monotonic() + fw.seconds
        except Exception:
            res = None
        st["allowed"][chat_id] = (time.monotonic() + _ALLOWED_TTL, res)
        return res

    async def react(event, cands, settings):
        chat_id = event.chat_id
        lock = st["locks"].setdefault(chat_id, asyncio.Lock())
        async with lock:
            gap = max(eng.MIN_GAP, float(settings.get("delay") or 0))
            wait = st["next_ok"].get(chat_id, 0) - time.monotonic()
            await asyncio.sleep(max(0.0, wait) + random.uniform(0.0, 0.25))
            if time.monotonic() < st["flood_until"]:
                return
            bad = st["bad"].setdefault(chat_id, set())
            cands = [e for e in cands if eng.norm_emoji(e) not in bad]
            multi = int(settings.get("multi") or 1)
            while cands:
                chosen = eng.pick_many(cands, multi)
                done = False
                for variant_idx in range(3):
                    try:
                        vec = []
                        for e in chosen:
                            vs = eng.emoji_variants(e)
                            vec.append(ReactionEmoji(emoticon=vs[min(variant_idx, len(vs) - 1)]))
                        await client(SendReactionRequest(
                            peer=await event.get_input_chat(), msg_id=event.id, reaction=vec))
                        st["next_ok"][chat_id] = time.monotonic() + gap
                        return
                    except errors.FloodWaitError as fw:
                        # Never retry through a FloodWait: pause the account.
                        st["flood_until"] = time.monotonic() + fw.seconds + 1
                        log("AR_FLOOD", f"pause {fw.seconds}s")
                        return
                    except errors.RPCError as e:
                        name = type(e).__name__
                        if "ReactionsTooMany" in name and len(chosen) > 1:
                            settings["multi"] = 1   # not premium: stop asking for more
                            save()
                            chosen = chosen[:1]
                            continue
                        if "ReactionInvalid" in name or "ReactionEmpty" in name:
                            continue   # try next spelling, then next emoji
                        # message gone / no rights / reactions off -> give up quietly
                        st["next_ok"][chat_id] = time.monotonic() + gap
                        return
                    except Exception as e:
                        log("AR_ERR", f"{type(e).__name__}: {e}")
                        return
                # every spelling rejected: remember and move on
                for e in chosen:
                    bad.add(eng.norm_emoji(e))
                cands = [e for e in cands if eng.norm_emoji(e) not in bad]

    # ── passive reactor ─────────────────────────────────────────────────────
    @client.on(events.NewMessage())
    async def _auto_react(event):
        try:
            if not (cfg.get("AUTO_REACT")):
                return
            mid = await me_id()
            settings = eng.resolve(cfg, mid, event.chat_id)
            if not settings or not settings.get("on"):
                return
            msg = event.message
            if msg is None or getattr(msg, "action", None) is not None:
                return                      # service messages
            txt = (event.raw_text or "")
            if txt.startswith((".", "/")):
                return                      # commands are never reacted to
            if time.monotonic() < st["flood_until"]:
                return
            key = (event.chat_id, event.id)
            if key in st["seen"]:
                return
            st["seen"].add(key)
            if len(st["seen"]) > 4000:
                st["seen"].clear()
            is_me = bool(getattr(event, "out", False)) or event.sender_id == mid
            is_bot = False
            if settings.get("bots", True) is False:
                try:
                    snd = await event.get_sender()
                    is_bot = bool(getattr(snd, "bot", False))
                except Exception:
                    pass
            ent = None
            try:
                ent = await event.get_chat()
            except Exception:
                pass
            allowed = await allowed_for(event.chat_id, ent)
            cands = eng.decide(settings, event.sender_id, is_me, is_bot, allowed=allowed)
            if not cands:
                return
            if st["pending"].get(event.chat_id, 0) >= _PENDING_MAX:
                return                      # burst: drop instead of piling up
            st["pending"][event.chat_id] = st["pending"].get(event.chat_id, 0) + 1
            try:
                await react(event, cands, settings)
            finally:
                st["pending"][event.chat_id] -= 1
        except Exception as e:
            log("AR_ERR", f"{type(e).__name__}: {e}")

    # ── command ─────────────────────────────────────────────────────────────
    async def resolve_user(event, tokens):
        """(user_id, remaining tokens) from reply, numeric id or @username."""
        if event.is_reply:
            r = await event.get_reply_message()
            if r is not None and r.sender_id:
                return r.sender_id, tokens
        if tokens:
            head = tokens[0]
            if re.fullmatch(r"-?\d+", head):
                return int(head), tokens[1:]
            if head.startswith("@"):
                try:
                    return (await client.get_entity(head)).id, tokens[1:]
                except Exception:
                    return None, tokens[1:]
        return None, tokens

    @client.on(events.NewMessage(pattern=r"(?is)^\.(?:ar|autoreaction)(?:\s+(.*))?$"))
    async def _ar_cmd(event):
        mid = await me_id()
        if not is_authorized(event, mid, cfg):
            return
        try:
            await event.delete()
        except Exception:
            pass
        arg = (event.pattern_match.group(1) or "").strip()
        tokens = arg.split()
        glob = False
        if tokens and tokens[0].lower() in ("-g", "g", "global", "all-chats"):
            glob, tokens = True, tokens[1:]
        cid = event.chat_id
        scope = "🌐 Global" if glob else "💬 This chat"
        if not tokens or tokens[0].lower() in ("help", "?"):
            cur = eng.resolve(cfg, mid, cid)
            return await say(event, HELP + "\n<blockquote>" + eng.describe(cur, "Now") + "</blockquote>", ttl=40)
        cmd, rest = tokens[0].lower(), tokens[1:]
        s = eng.edit(cfg, mid, cid, glob)
        msg = None

        if cmd in ("on", "start", "enable"):
            s["on"] = True
            msg = f"✅ Auto reaction <b>ON</b> — {scope} ({s['mode']}, who={s['who']})"
        elif cmd in ("off", "stop", "disable"):
            s["on"] = False
            msg = f"🛑 Auto reaction <b>OFF</b> — {scope}"
        elif cmd == "random":
            s["mode"], s["on"] = "random", True
            msg = f"🎲 Random reactions ON — {scope}"
        elif cmd in ("same", "fixed", "emoji"):
            ems = eng.extract_emojis(" ".join(rest))
            if not ems:
                msg = "❌ Emoji do: <code>.ar same 🔥</code>"
            else:
                s["mode"], s["emoji"], s["on"] = "same", ems[0], True
                msg = f"{ems[0]} Sabke msg pe same reaction ON — {scope}"
        elif cmd == "who":
            v = (rest[0].lower() if rest else "")
            v = {"everyone": "all", "self": "me", "mine": "me", "other": "others", "user": "users"}.get(v, v)
            if v not in eng.WHO_VALUES:
                msg = "❌ <code>.ar who all|me|others|users</code>"
            else:
                s["who"], s["on"] = v, True
                msg = f"👥 Who = <b>{v}</b> — {scope}"
        elif cmd == "user":
            uid, left = await resolve_user(event, rest)
            if uid is None:
                msg = "❌ Reply karo ya <code>.ar user &lt;id|@user&gt; [emoji]</code>"
            else:
                ems = eng.extract_emojis(" ".join(left))
                s["users"][str(uid)] = ems[0] if ems else ""
                s["on"] = True
                msg = f"👤 User <code>{uid}</code> added {ems[0] if ems else '(random)'} — {scope}"
        elif cmd in ("unuser", "rmuser"):
            uid, _ = await resolve_user(event, rest)
            msg = ("🗑️ User removed." if uid is not None and s["users"].pop(str(uid), None) is not None
                   else "❌ Wo user list me nahi hai.")
        elif cmd == "ignore":
            uid, _ = await resolve_user(event, rest)
            if uid is None:
                msg = "❌ Reply karo ya <code>.ar ignore &lt;id|@user&gt;</code>"
            else:
                if str(uid) not in {str(x) for x in s["ignore"]}:
                    s["ignore"].append(uid)
                msg = f"🙈 Ignoring <code>{uid}</code> — {scope}"
        elif cmd == "unignore":
            uid, _ = await resolve_user(event, rest)
            before = len(s["ignore"])
            s["ignore"] = [x for x in s["ignore"] if uid is None or str(x) != str(uid)]
            msg = "✅ Unignored." if len(s["ignore"]) < before else "❌ Wo ignore list me nahi hai."
        elif cmd == "pool":
            if rest and rest[0].lower() in ("reset", "default", "clear"):
                s["pool"] = []
                msg = "♻️ Pool default pe reset."
            else:
                ems = eng.extract_emojis(" ".join(rest))
                if not ems:
                    msg = "❌ <code>.ar pool 🔥❤️😂</code>"
                else:
                    s["pool"], s["mode"], s["on"] = ems[:30], "random", True
                    msg = f"🎯 Pool: {' '.join(s['pool'])}"
        elif cmd in ("chance", "prob"):
            try:
                s["chance"] = max(1, min(100, int(rest[0].strip("%"))))
                msg = f"🎚️ Chance = <b>{s['chance']}%</b>"
            except Exception:
                msg = "❌ <code>.ar chance 1-100</code>"
        elif cmd in ("delay", "gap"):
            try:
                s["delay"] = max(0.0, min(60.0, float(rest[0])))
                msg = f"⏱️ Delay = <b>{s['delay']}s</b> (min {eng.MIN_GAP}s)"
            except Exception:
                msg = "❌ <code>.ar delay 1.5</code>"
        elif cmd == "multi":
            try:
                s["multi"] = max(1, min(3, int(rest[0])))
                msg = f"✨ Multi = <b>{s['multi']}</b> (premium; auto-falls back to 1)"
            except Exception:
                msg = "❌ <code>.ar multi 1-3</code>"
        elif cmd == "bots":
            v = (rest[0].lower() if rest else "")
            if v not in ("on", "off"):
                msg = "❌ <code>.ar bots on|off</code>"
            else:
                s["bots"] = (v == "on")
                msg = f"🤖 Bots reaction: <b>{v}</b>"
        elif cmd in ("status", "list", "info"):
            glob_s = (cfg.get("AUTO_REACT", {}).get(str(mid), {}) or {}).get(eng.GLOBAL_KEY)
            chat_s = (cfg.get("AUTO_REACT", {}).get(str(mid), {}) or {}).get(str(cid))
            return await say(event, "<blockquote>" + eng.describe(chat_s, "💬 This chat")
                             + "\n\n" + eng.describe(glob_s, "🌐 Global") + "</blockquote>", ttl=30)
        elif cmd in ("reset", "clear"):
            (cfg.setdefault("AUTO_REACT", {}).setdefault(str(mid), {})).pop(eng.scope_key(cid, glob), None)
            st["bad"].pop(cid, None)
            msg = f"🧹 Auto reaction settings cleared — {scope}"
        else:
            msg = "❌ Unknown option. <code>.ar</code> se help dekho."
        save()
        await say(event, f"<blockquote>{msg}</blockquote>")

    log("PLUGIN", f"{PLUGIN_NAME} loaded — commands: {', '.join(PLUGIN_CMDS)}")
