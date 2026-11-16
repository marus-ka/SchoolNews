#!/usr/bin/env python3
"""
Ежедневная сводка новостей школьных Telegram-каналов по ключевым словам.

Читает публичные веб-версии каналов (https://t.me/s/<канал>), отбирает новые посты
с ключевыми словами и отправляет сводку в личку через Telegram-бота.

Переменные окружения (в GitHub — Settings → Secrets → Actions):
  TG_BOT_TOKEN  — токен бота от @BotFather
  TG_CHAT_ID    — ваш chat_id (куда слать сводку)
Необязательные:
  SEND_EMPTY=0  — не присылать сообщение, если совпадений нет (по умолчанию присылает)
  SHOW_ERRORS=1 — всегда показывать список каналов, которые не прочитались
                  (по умолчанию — только по понедельникам и при ручном запуске)
  DRY_RUN=1     — ничего не отправлять, напечатать сводку в лог
"""
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
CHANNELS_FILE = ROOT / "channels.txt"
KEYWORDS_FILE = ROOT / "keywords.txt"
STATE_FILE = ROOT / "state.json"

MSK = timezone(timedelta(hours=3))
FIRST_RUN_WINDOW = timedelta(hours=24)   # при первом запуске канала берём посты за сутки
MAX_AGE = timedelta(days=4)              # страховка: старше этого посты не присылаем никогда
MAX_PAGES = 5                            # сколько страниц по ~20 постов листать назад
SNIPPET_LEN = 350
TG_LIMIT = 3900                          # лимит сообщения Telegram 4096, с запасом

UA = {"User-Agent": "Mozilla/5.0 (school-news-digest)"}
LETTER = r"0-9A-Za-zА-Яа-яЁё"


# ---------- загрузка настроек ----------

def load_channels():
    channels = []
    for line in CHANNELS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, label = line.partition("|")
        name = name.strip().lstrip("@")
        name = re.sub(r"^https?://t\.me/(s/)?", "", name).strip("/")
        if name:
            channels.append((name, label.strip() or name))
    return channels


def norm(s):
    return s.lower().replace("ё", "е")


def load_keywords():
    """Возвращает список (подпись, regex, режим). режим: 'norm' — по нормализованному тексту, 'raw' — по исходному."""
    kws = []
    for line in KEYWORDS_FILE.read_text(encoding="utf-8").splitlines():
        kw = line.strip()
        if not kw or kw.startswith("#"):
            continue
        letters = re.sub(r"[^A-Za-zА-Яа-яЁё]", "", kw)
        if letters and letters.isupper() and len(letters) > 1:
            # аббревиатура: целое слово, с учётом регистра
            rx = re.compile(rf"(?<![{LETTER}]){re.escape(kw)}(?![{LETTER}])")
            kws.append((kw, rx, "raw"))
        else:
            parts = [re.escape(p) for p in norm(kw).split()]
            rx = re.compile(rf"(?<![{LETTER}])" + r"\s+".join(parts))
            kws.append((kw, rx, "norm"))
    return kws


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


# ---------- чтение канала ----------

def fetch(url, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=25)
            if r.status_code == 200:
                return r.text
            if r.status_code == 429:
                time.sleep(10 * (i + 1))
                continue
        except requests.RequestException:
            pass
        time.sleep(3 * (i + 1))
    return None


def parse_page(page_html):
    """Список постов со страницы t.me/s/: dict(id, date, text, url)."""
    soup = BeautifulSoup(page_html, "html.parser")
    posts = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        data_post = msg.get("data-post", "")
        try:
            pid = int(data_post.rsplit("/", 1)[1])
        except (IndexError, ValueError):
            continue
        t = msg.select_one("a.tgme_widget_message_date time[datetime]") or msg.select_one("time[datetime]")
        if not t:
            continue
        try:
            dt = datetime.fromisoformat(t["datetime"])
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        text_el = msg.select_one("div.tgme_widget_message_text")
        text = ""
        if text_el:
            for br in text_el.find_all("br"):
                br.replace_with("\n")
            text = text_el.get_text()
        posts.append({"id": pid, "date": dt, "text": text, "url": f"https://t.me/{data_post}"})
    return posts


def read_channel(name, last_id, now):
    """Новые посты канала. Возвращает (posts, max_id) или (None, None), если канал не читается."""
    cutoff = now - (FIRST_RUN_WINDOW if last_id is None else MAX_AGE)
    url = f"https://t.me/s/{name}"
    collected = {}
    for _ in range(MAX_PAGES):
        page = fetch(url)
        if page is None:
            return (None, None) if not collected else (list(collected.values()), max(collected))
        posts = parse_page(page)
        if not posts:
            break
        for p in posts:
            collected[p["id"]] = p
        oldest = min(posts, key=lambda p: p["id"])
        if oldest["date"] < cutoff or (last_id is not None and oldest["id"] <= last_id):
            break
        url = f"https://t.me/s/{name}?before={oldest['id']}"
        time.sleep(1)
    if not collected:
        return None, None
    max_id = max(collected)
    fresh = [p for p in collected.values()
             if p["date"] >= cutoff and (last_id is None or p["id"] > last_id)]
    return sorted(fresh, key=lambda p: p["date"]), max_id


# ---------- поиск и форматирование ----------

def find_keywords(text, keywords):
    ntext = norm(text)
    hits = []
    first_pos = None
    for label, rx, mode in keywords:
        m = rx.search(ntext if mode == "norm" else text)
        if m:
            hits.append(label)
            if first_pos is None or m.start() < first_pos:
                first_pos = m.start()
    return hits, first_pos


def make_snippet(text, pos):
    text = re.sub(r"[ \t]+", " ", text).strip()
    text = re.sub(r"\n{2,}", "\n", text)
    if len(text) <= SNIPPET_LEN:
        return text
    start = max(0, (pos or 0) - SNIPPET_LEN // 3)
    end = min(len(text), start + SNIPPET_LEN)
    start = max(0, end - SNIPPET_LEN)
    return ("…" if start > 0 else "") + text[start:end].strip() + ("…" if end < len(text) else "")


def format_item(label, name, post, hits, pos):
    when = post["date"].astimezone(MSK).strftime("%d.%m, %H:%M")
    snippet = html.escape(make_snippet(post["text"], pos))
    return (f"🏫 <b>{html.escape(label)}</b> · {when}\n"
            f"{snippet}\n"
            f"🔑 {html.escape(', '.join(hits))} · <a href=\"{post['url']}\">открыть пост</a>")


def split_messages(header, items, footer):
    messages, cur = [], header
    for it in items:
        block = "\n\n" + it
        if len(cur) + len(block) > TG_LIMIT:
            messages.append(cur)
            cur = it
        else:
            cur += block
    if footer:
        if len(cur) + len(footer) + 2 > TG_LIMIT:
            messages.append(cur)
            cur = footer
        else:
            cur += "\n\n" + footer
    messages.append(cur)
    return messages


def send(text, token, chat_id):
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }, timeout=30)
    if r.status_code != 200:
        print("Ошибка отправки:", r.status_code, r.text[:500], file=sys.stderr)
        return False
    return True


# ---------- главное ----------

def main():
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TG_CHAT_ID", "").strip()
    dry = os.environ.get("DRY_RUN") == "1"
    if not dry and (not token or not chat_id):
        sys.exit("Не заданы TG_BOT_TOKEN и/или TG_CHAT_ID")

    now = datetime.now(timezone.utc)
    channels = load_channels()
    keywords = load_keywords()
    state = load_state()

    items, failed, checked = [], [], 0
    for name, label in channels:
        last_id = state.get(name)
        posts, max_id = read_channel(name, last_id, now)
        if posts is None:
            failed.append(f"{label} (@{name})")
            print(f"[!] {name}: не прочитан")
            continue
        checked += 1
        for p in posts:
            hits, pos = find_keywords(p["text"], keywords)
            if hits:
                items.append((p["date"], format_item(label, name, p, hits, pos), name))
        if max_id is not None:
            state[name] = max(max_id, last_id or 0)
        print(f"[ok] {name}: новых постов {len(posts)}")
        time.sleep(1)

    items.sort(key=lambda x: x[0], reverse=True)
    today = now.astimezone(MSK).strftime("%d.%m.%Y")
    n_ch = len({x[2] for x in items})

    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    show_errors = failed and (os.environ.get("SHOW_ERRORS") == "1" or manual
                              or now.astimezone(MSK).weekday() == 0)
    footer = ""
    if show_errors:
        footer = ("⚠️ <b>Не удалось прочитать</b> (закрытый канал, нет веб-версии или опечатка в имени): "
                  + html.escape("; ".join(failed)))

    if items:
        header = f"📬 <b>Школьные новости · {today}</b>\nНайдено постов: {len(items)} в каналах: {n_ch}"
        messages = split_messages(header, [x[1] for x in items], footer)
    elif os.environ.get("SEND_EMPTY", "1") != "0":
        messages = [f"📭 <b>{today}</b>: за сутки совпадений нет (проверено каналов: {checked})"
                    + ("\n\n" + footer if footer else "")]
    else:
        messages = [footer] if footer else []

    ok = True
    for m in messages:
        if dry:
            print("=" * 40 + "\n" + m)
        else:
            ok &= send(m, token, chat_id)
            time.sleep(1)

    # состояние сохраняем только если сводка ушла, иначе завтра посты придут повторно
    if ok and not dry:
        save_state(state)
    print(f"Готово: каналов прочитано {checked}, не прочитано {len(failed)}, совпадений {len(items)}")
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
