from pathlib import Path
import re

EMOJI_RE = re.compile(
    r"(?:[\U0001F1E6-\U0001F1FF]{2})"
    r"|(?:[\U0001F300-\U0001FAFF\u2600-\u27BF\u2190-\u21FF\u2B00-\u2BFF]"
    r"[\U0000FE0F\U0001F3FB-\U0001F3FF]?"
    r"(?:\u200D[\U0001F300-\U0001FAFF\u2600-\u27BF][\U0000FE0F\U0001F3FB-\U0001F3FF]?)*)"
    r"|(?:[0-9#\\*]\U0000FE0F?\u20E3)"
)

for path in (Path("data/ow.txt"), Path("botfix/data/ow.txt")):
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = EMOJI_RE.sub("", raw)
        line = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF]", "", line)
        line = line.replace("\u200d", "").replace("\ufe0f", "")
        line = re.sub(r"(?i)(?<!\w)4st(?!\w)", "", line)
        line = re.sub(r"[ \t]{2,}", " ", line).strip()
        if line:
            lines.append(line)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    print(f"sanitized {path}: {len(lines)} lines")
