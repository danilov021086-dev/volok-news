# -*- coding: utf-8 -*-
"""
ai.py — разбор заголовка: как новость влияет на доход майнинг-фермы.

ЧТО НА ВХОД И ЧТО НА ВЫХОД
  На вход — ТОЛЬКО заголовок, источник и дата. Текст статьи и картинки
  бот не берёт и не хранит (решение владельца).
  На выход — строго JSON:
      {"impact": "plus" | "minus" | "risk" | "neutral",
       "text": "одна фраза до 140 знаков"}

ЧЕТЫРЕ ЗАПРЕТА, И ОНИ ВАЖНЕЕ КРАСИВОГО ОТВЕТА
  1. НЕ ВЫДУМЫВАТЬ СВЕРХ ЗАГОЛОВКА. Модель видит только заголовок —
     значит и судить может только о нём. Додумывание содержания статьи
     превращает ленту в сочинение.
  2. НЕЯСНО — neutral. Половина заголовков ни о чём для фермы не
     говорит, и это нормальный ответ, а не повод натянуть оценку.
  3. НИКАКИХ СОВЕТОВ покупать, продавать, «пора закупаться». Мы
     показываем влияние на доход, а не даём финансовых рекомендаций.
  4. ОТВЕТ НЕ ПРОШЁЛ ПРОВЕРКУ — ai: null И ПРИЧИНА В ЛОГ. Шаблон
     («нейтрально, влияния нет») не подставляем никогда: выдуманная
     оценка под видом разбора хуже, чем честное «разбора нет».

ЕСЛИ КЛЮЧА НЕТ
  Бот работает целиком, только у новостей стоит ai: null и причина
  «ключ не задан». Лента при этом живая и показывает заголовки — просто
  без цветной оценки. Ключ кладётся в секрет Actions VOLOK_AI_KEY.
"""

import json
import os
import re
import urllib.error
import urllib.request

MODEL = os.environ.get("VOLOK_AI_MODEL", "claude-haiku-4-5-20251001")
KEY = (os.environ.get("VOLOK_AI_KEY") or "").strip()
API = "https://api.anthropic.com/v1/messages"
IMPACTS = ("plus", "minus", "risk", "neutral")
MAXLEN = 140

SYSTEM = (
    "Ты помогаешь владельцу майнинг-фермы на биткоине быстро понять, "
    "касается ли его новость. Тебе дают ТОЛЬКО заголовок, источник и дату. "
    "Оцени влияние именно на ДОХОД ФЕРМЫ: цену биткоина, сложность сети, "
    "цену оборудования, электричество, законы для майнеров в России.\n"
    "Отвечай СТРОГО одним JSON-объектом без пояснений и без разметки:\n"
    '{"impact":"plus|minus|risk|neutral","text":"одна фраза до 140 знаков"}\n'
    "impact: plus — доход скорее вырастет; minus — скорее упадёт; "
    "risk — прямого влияния нет, но есть угроза (проверки, запреты, "
    "возможная коррекция); neutral — на доход фермы не влияет.\n"
    "Запреты: не придумывай содержание статьи сверх заголовка; "
    "если по заголовку непонятно — neutral; "
    "никаких советов покупать или продавать."
)


def _ask(title, source, date_ru):
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 200,
        "system": SYSTEM,
        "messages": [{"role": "user", "content":
                      "Источник: %s\nДата: %s\nЗаголовок: %s" % (source, date_ru, title)}],
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API, data=body, headers={
        "content-type": "application/json",
        "x-api-key": KEY,
        "anthropic-version": "2023-06-01",
    })
    with urllib.request.urlopen(req, timeout=45) as r:
        d = json.loads(r.read().decode("utf-8"))
    parts = d.get("content") or []
    txt = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
    return txt.strip(), d.get("usage") or {}


def analyse(title, source, date_ru, log):
    """Вернуть (ai, usage). ai — словарь или None. Причина отказа — в log."""
    if not KEY:
        log.append({"заголовок": title[:80], "причина": "ключ не задан"})
        return None, {}
    try:
        raw, usage = _ask(title, source, date_ru)
    except urllib.error.HTTPError as e:
        log.append({"заголовок": title[:80],
                    "причина": "HTTP %s от модели" % e.code})
        return None, {}
    except Exception as e:                                      # noqa: BLE001
        log.append({"заголовок": title[:80], "причина": "сеть: %s" % e})
        return None, {}

    # Модель могла обернуть ответ в ```json — вытаскиваем объект.
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        log.append({"заголовок": title[:80], "причина": "ответ не JSON",
                    "ответ": raw[:120]})
        return None, usage
    try:
        j = json.loads(m.group(0))
    except Exception as e:                                      # noqa: BLE001
        log.append({"заголовок": title[:80], "причина": "JSON не разобрался: %s" % e,
                    "ответ": raw[:120]})
        return None, usage

    imp = str(j.get("impact", "")).strip().lower()
    txt = str(j.get("text", "")).strip()
    if imp not in IMPACTS:
        log.append({"заголовок": title[:80], "причина": "неизвестный impact %r" % imp})
        return None, usage
    if not txt:
        log.append({"заголовок": title[:80], "причина": "пустой text"})
        return None, usage
    if len(txt) > MAXLEN:
        # Обрезать — значит подделать ответ. Отказываемся.
        log.append({"заголовок": title[:80],
                    "причина": "text длиннее %d знаков (%d)" % (MAXLEN, len(txt))})
        return None, usage
    low = txt.lower()
    for bad in ("купит", "продав", "продат", "закупа", "инвестируй", "покупай"):
        if bad in low:
            log.append({"заголовок": title[:80], "причина": "совет покупать/продавать"})
            return None, usage
    return {"impact": imp, "text": txt, "модель": MODEL}, usage
