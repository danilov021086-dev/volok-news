#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_news.py — собрать ленту новостей для кабинета ВОЛОК.

ЧТО ДЕЛАЕТ
  1. Читает RSS источников (только заголовок, ссылку, дату, источник —
     текст статей и картинки не берутся и не хранятся).
  2. Отбирает по словарю bot/keywords.py и расставляет метки mine/btc/ru.
  3. Выбрасывает дубли: по ссылке и по похожести заголовков.
  4. Для КАЖДОЙ НОВОЙ новости просит у модели оценку влияния на доход
     фермы (bot/ai.py). Уже разобранные не переспрашиваются — иначе
     каждый получасовой прогон платил бы заново за те же 200 новостей.
  5. Добавляет блок market (bot/market.py): курсы, сложность, хешрейт
     сети, хешпрайс — у каждого числа источник и время.
  6. Пишет news.json: 200 последних новостей, время московское.

ПОЧЕМУ ВРЕМЯ МОСКОВСКОЕ
  Лента читается владельцем и его клиентами в России. Время в UTC в
  такой ленте читается неверно ровно на три часа, и заметить это можно
  только сверив с часами — то есть никогда.

Запуск:  python bot/fetch_news.py
"""

import difflib
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ai as ai_mod                                             # noqa: E402
import keywords as kw                                           # noqa: E402
import market as market_mod                                     # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "news.json")
MSK = timezone(timedelta(hours=3))
KEEP = 200
UA = {"User-Agent": "volok-news/1.0 (+https://github.com/danilov021086-dev/volok-news)"}

SOURCES = [
    ("ForkLog", "https://forklog.com/feed"),
    ("Bits.media", "https://bits.media/rss2/"),
]

# Сколько новых заголовков разбираем за один прогон. Предохранитель от
# счёта: если источник вдруг выдаст сотню новостей разом, бот не уйдёт
# в неожиданные расходы — остальные разберутся следующим прогоном.
def _env_int(name, default):
    """Незаданная переменная Actions приходит ПУСТОЙ СТРОКОЙ, а не
    отсутствует — int("") падает. Поймано первым же боевым прогоном."""
    v = (os.environ.get(name) or "").strip()
    try:
        return int(v) if v else default
    except ValueError:
        print("переменная %s=%r не число, беру %d" % (name, v, default))
        return default


AI_MAX_PER_RUN = _env_int("VOLOK_AI_MAX", 25)

# ЦЕНА ТОКЕНОВ, рублей за МИЛЛИОН (вход, выход). Снято с официальных
# страниц 27.09.2026. Нужна ровно для одного: посчитать настоящий расход
# за прогон и показать владельцу цену за месяц по ФАКТУ, а не по оценке.
# Модели нет в таблице — цена не считается (пишем null), а не берётся
# «примерно такая же»: выдуманный рубль в отчёте не лучше выдуманного
# числа на экране.
PRICE_RUB_PER_M = {
    # Cloud.ru Evolution Foundation Models
    "deepseek-ai/DeepSeek-V4-Flash": (18.53, 37.08),
    "deepseek-ai/DeepSeek-V4.1-Flash": (64.94, 194.81),
    "deepseek-ai/DeepSeek-V4-Pro": (183.0, 732.0),
    "deepseek/deepseek-chat-v3-0324": (37.32, 170.89),
    "Qwen/Qwen3.6-35B-A3B": (219.6, 329.4),
    "Qwen/Qwen3-30B-A3B": (13.91, 55.61),
    "Qwen/Qwen3-32B": (37.08, 148.29),
    "openai/gpt-oss-120b": (15.86, 61.0),
    "openai/gpt-oss-20b": (5.89, 27.50),
    "z-ai/glm-4.6": (102.48, 375.76),
    # Yandex AI Studio: в прайсе цена за 1000 токенов, здесь приведена
    # к миллиону (0,3 ₽ за 1000 = 300 ₽ за миллион).
    "deepseek-v4-flash": (300.0, 500.0),
    "qwen3.6-35b-a3b": (200.0, 300.0),
    "gpt-oss-120b": (300.0, 300.0),
    "gpt-oss-20b": (100.0, 100.0),
}


def price_rub(tin, tout, model=None):
    """Рублей за указанные токены. Модель неизвестна — None."""
    m = model or ai_mod.MODEL
    # У Яндекса модель приходит как gpt://<каталог>/<имя> — берём имя.
    short = m.rsplit("/", 1)[-1] if m.startswith("gpt://") else m
    p = PRICE_RUB_PER_M.get(m) or PRICE_RUB_PER_M.get(short)
    if not p:
        return None
    return tin / 1e6 * p[0] + tout / 1e6 * p[1]


# ----------------------------------------------------------------- время
def parse_date(s):
    """RSS-дата -> unix. Форматы у источников разные, поэтому пробуем
    несколько; не разобралось — берём текущее время и помечаем это."""
    s = (s or "").strip()
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            d = datetime.strptime(s, fmt)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return int(d.timestamp()), True
        except ValueError:
            continue
    return int(time.time()), False


def msk(unix):
    return datetime.fromtimestamp(unix, MSK).strftime("%Y-%m-%d %H:%M")


# ----------------------------------------------------------------- отбор
def norm(s):
    s = (s or "").lower().replace("ё", "е")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s)).strip()


def tags_of(title):
    t = " " + norm(title) + " "
    for bad in kw.EXCLUDE:
        if norm(bad) in t:
            return [], "исключено по слову %r" % bad
    out = []
    for tag, words in kw.TAGS.items():
        for w in words:
            wn = norm(w)
            if not wn:
                continue
            if w in kw.STRICT:
                if re.search(r"\b%s\b" % re.escape(wn), t):
                    out.append(tag)
                    break
            elif wn in t:
                out.append(tag)
                break
    return out, None


def norm_link(u):
    u = (u or "").strip()
    u = re.sub(r"[?#].*$", "", u)          # utm-хвосты — это тот же материал
    return u.rstrip("/")


# ------------------------------------------------------------------ RSS
def fetch(name, url, errors):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        root = ET.fromstring(data)
    except Exception as e:                                      # noqa: BLE001
        errors.append({"источник": name, "ошибка": str(e)})
        return []
    out = []
    for it in root.findall(".//item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not title or not link:
            continue
        unix, exact = parse_date(it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date"))
        out.append({"источник": name, "заголовок": title, "ссылка": link,
                    "unix": unix, "дата_точная": exact})
    return out


# ------------------------------------------------------------------ main
def main():
    started = int(time.time())
    old = {}
    prev_market = None
    if os.path.exists(OUT):
        try:
            prev = json.load(open(OUT, encoding="utf-8"))
            for n in prev.get("новости", []):
                old[norm_link(n.get("ссылка"))] = n
            prev_market = prev.get("market")
        except Exception as e:                                  # noqa: BLE001
            print("прежний news.json не прочитался: %s" % e)

    errors, ailog = [], []
    raw = []
    for name, url in SOURCES:
        got = fetch(name, url, errors)
        print("%-11s получено %d" % (name, len(got)))
        raw.append((name, got))

    # ---------------- отбор и метки
    picked, skipped = [], 0
    for _name, items in raw:
        for it in items:
            tags, why = tags_of(it["заголовок"])
            if why or len(tags) < kw.MIN_TAGS:
                skipped += 1
                continue
            it["метки"] = sorted(set(tags))
            picked.append(it)
    print("подошло по словарю: %d, отсеяно: %d" % (len(picked), skipped))

    # ---------------- дубли: по ссылке и по похожести заголовка
    by_link, fresh = {}, []
    for it in sorted(picked, key=lambda x: -x["unix"]):
        L = norm_link(it["ссылка"])
        if L in by_link:
            continue
        by_link[L] = it
        fresh.append(it)

    kept = []
    for it in fresh:
        n = norm(it["заголовок"])
        dup = False
        for other in kept:
            if difflib.SequenceMatcher(None, n, norm(other["заголовок"])).ratio() > 0.85:
                dup = True
                break
        if not dup:
            kept.append(it)
    print("после снятия дублей: %d" % len(kept))

    # ---------------- склейка со старым
    merged = {}
    for n in old.values():
        merged[norm_link(n.get("ссылка"))] = n
    new_items = []
    for it in kept:
        L = norm_link(it["ссылка"])
        if L in merged:
            # уже была: обновим метки, разбор не трогаем
            merged[L]["метки"] = it["метки"]
            continue
        rec = {"источник": it["источник"], "заголовок": it["заголовок"],
               "ссылка": it["ссылка"], "unix": it["unix"],
               "дата_мск": msk(it["unix"]), "дата_точная": it["дата_точная"],
               "метки": it["метки"], "ai": None}
        merged[L] = rec
        new_items.append(rec)
    print("новых новостей: %d" % len(new_items))

    # ---------------- разбор ИИ только для новых
    used = {"вход": 0, "выход": 0, "разобрано": 0, "руб": 0.0}
    for rec in new_items[:AI_MAX_PER_RUN]:
        a, usage = ai_mod.analyse(rec["заголовок"], rec["источник"],
                                  rec["дата_мск"], ailog)
        rec["ai"] = a
        # Токены считаем и у отказов тоже: заплачено за них одинаково.
        # Имена полей у OpenAI-совместимых площадок общие.
        vin = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        vout = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        used["вход"] += vin
        used["выход"] += vout
        if a:
            used["разобрано"] += 1
    used["руб"] = round(price_rub(used["вход"], used["выход"]), 4)
    if len(new_items) > AI_MAX_PER_RUN:
        print("разбор отложен для %d новостей (предохранитель)"
              % (len(new_items) - AI_MAX_PER_RUN))

    # ---------------- market
    try:
        market = market_mod.build(prev_market)
    except Exception as e:                                      # noqa: BLE001
        errors.append({"источник": "market", "ошибка": str(e)})
        market = prev_market

    # ---------------- запись
    items = sorted(merged.values(), key=lambda x: -int(x.get("unix") or 0))[:KEEP]
    doc = {
        "версия": 1,
        "обновлено_unix": started,
        "обновлено_мск": msk(started),
        "часовой_пояс": "МСК (UTC+3)",
        "источники": [{"имя": n, "адрес": u} for n, u in SOURCES],
        "новостей": len(items),
        "разбор_ии": {
            "включён": bool(ai_mod.KEY),
            "площадка": ai_mod.BASE if ai_mod.KEY else None,
            "модель": ai_mod.MODEL if ai_mod.KEY else None,
            "разобрано_за_прогон": used["разобрано"],
            "отказов_за_прогон": len(ailog),
            "токенов_вход": used["вход"],
            "токенов_выход": used["выход"],
            # Расход считается ЗДЕСЬ, а не на глаз: по нему владелец
            # увидит настоящую цену за месяц, а не мою оценку.
            "цена_за_прогон_руб": used["руб"],
            "отказы": ailog[:40],
        },
        "ошибки": errors or None,
        "market": market,
        "новости": items,
    }
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print("записано %s: новостей %d, размер %d байт"
          % (OUT, len(items), os.path.getsize(OUT)))
    if errors:
        print("ошибки источников:", json.dumps(errors, ensure_ascii=False))


if __name__ == "__main__":
    main()
