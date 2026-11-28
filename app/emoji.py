"""Премиум эмодзи: обычные эмодзи в сообщениях заменяются на эмодзи из выбранного набора."""
import json
import re

from .core import Err

PACK_RE = re.compile(r"^(?:(?:https?://)?t\.me/(?:addemoji|addstickers)/)?([A-Za-z][A-Za-z0-9_]{2,63})/?$")
SKIP_RE = re.compile(r"(<code>.*?</code>|<pre>.*?</pre>|<[^>]+>)", re.S)
VS = "\ufe0f"


def pack_name(link: str) -> str:
    match = PACK_RE.match(str(link or "").strip())
    if not match:
        raise Err("Пришлите ссылку вида t.me/addemoji/название")
    return match[1]


def build_map(stickers) -> dict:
    result = {}
    for item in stickers:
        base = item.get("emoji", "").replace(VS, "")
        if base and not base.isascii() and item.get("custom_emoji_id"):
            result.setdefault(base, item["custom_emoji_id"])
    return result


class Decorator:
    def __init__(self, raw="{}"):
        self.map = json.loads(raw or "{}")
        keys = sorted(self.map, key=len, reverse=True)
        # (?<!\u200d) и (?!\u200d): не разрываем составные эмодзи
        self.regex = re.compile("(?<!\u200d)(?:" + "|".join(
            "".join(re.escape(c) + VS + "?" for c in key) for key in keys) + ")(?![\u200d\U0001f3fb-\U0001f3ff])"
        ) if keys else None

    def text(self, html: str) -> str:
        if not self.regex:
            return html
        parts = SKIP_RE.split(html)  # нечётные части — теги и код, их не трогаем
        for i in range(0, len(parts), 2):
            parts[i] = self.regex.sub(
                lambda m: f'<tg-emoji emoji-id="{self.map[m[0].replace(VS, "")]}">{m[0]}</tg-emoji>', parts[i])
        return "".join(parts)

    def markup(self, markup):
        if not self.regex or not markup:
            return markup
        rows = []
        for row in markup["inline_keyboard"]:
            items = []
            for item in row:
                match = self.regex.match(item["text"])
                rest = item["text"][match.end():].strip() if match else ""
                if rest:
                    item = {**item, "text": rest,
                            "icon_custom_emoji_id": self.map[match[0].replace(VS, "")]}
                items.append(item)
            rows.append(items)
        return {"inline_keyboard": rows}
