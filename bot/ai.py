# -*- coding: utf-8 -*-
"""
ai.py — разбор заголовка: как новость влияет на доход майнинг-фермы.

ПЛОЩАДКА: РОССИЙСКОЕ ОБЛАКО, ОПЛАТА В РУБЛЯХ
  Anthropic из кода убран совсем: API Anthropic в России недоступен —
  аккаунт не завести, и зарубежная карта этого не меняет. Посредников и
  обходов не используем.

  Разбор идёт через OpenAI-совместимый API российского облака. Их два, и
  оба проверены фактом 27.09.2026 — отвечают и с нашего сервера, и с
  бегунка GitHub Actions (он в США, AS8075 Microsoft):

    Cloud.ru Evolution Foundation Models
        https://foundation-models.api.cloud.ru/v1
        95 моделей, среди них DeepSeek, Qwen, gpt-oss, GLM.
    Yandex AI Studio
        https://llm.api.cloud.yandex.net/v1
        DeepSeek V4 Flash, Qwen3.6 35B, gpt-oss — с OpenAI-совместимым
        путём. Модель задаётся как gpt://<каталог>/<имя>.

  Площадка и модель задаются переменными, код одинаков для обеих —
  обе говорят на диалекте OpenAI.

ЧТО НА ВХОД И ЧТО НА ВЫХОД
  На вход — ТОЛЬКО заголовок, источник и дата. Текст статьи и картинки
  бот не берёт и не хранит (решение владельца).
  На выход — строго JSON:
      {"impact": "plus" | "minus" | "risk" | "neutral",
       "text": "одна фраза до 140 знаков"}

ЧЕТЫРЕ ЗАПРЕТА, И ОНИ ВАЖНЕЕ КРАСИВОГО ОТВЕТА
  1. НЕ ВЫДУМЫВАТЬ СВЕРХ ЗАГОЛОВКА. Модель видит только заголовок —
     значит и судить может только о нём.
  2. НЕЯСНО — neutral. Половина заголовков ни о чём для фермы не
     говорит, и это нормальный ответ, а не повод натянуть оценку.
  3. НИКАКИХ СОВЕТОВ покупать или продавать.
  4. ОТВЕТ НЕ ПРОШЁЛ ПРОВЕРКУ — ai: null И ПРИЧИНА В ЛОГ. Шаблон
     («нейтрально, влияния нет») не подставляем никогда: выдуманная
     оценка под видом разбора хуже, чем честное «разбора нет».

ОСОБЕННОСТЬ ОТКРЫТЫХ МОДЕЛЕЙ
  Рассуждающие модели (DeepSeek, Qwen, GLM) любят приписать мысли до и
  после JSON, а иногда обернуть его в ```json. Поэтому ответ не
  разбирается целиком, а из него ВЫРЕЗАЕТСЯ последний объект в фигурных
  скобках. Это не «починка» ответа: если объекта нет или он не того
  вида — отказ, как и прежде.

ЕСЛИ КЛЮЧА НЕТ
  Бот работает целиком, только у новостей стоит ai: null и причина
  «ключ не задан».
"""

import json
import os
import re
import time
import urllib.error
import urllib.request

# --------------------------------------------------------------- настройки
# Пустая переменная GitHub Actions приходит ПУСТОЙ СТРОКОЙ, а не
# отсутствует, — поэтому везде «or ''» и strip().
def _env(name, default=""):
    return (os.environ.get(name) or "").strip() or default


BASE = _env("VOLOK_AI_BASE", "https://foundation-models.api.cloud.ru/v1")
KEY = _env("VOLOK_AI_KEY")
TIMEOUT = int(_env("VOLOK_AI_TIMEOUT", "60"))

# ОСНОВНАЯ И ЗАПАСНАЯ — та же пара, что у сайта (задание v31, часть Б2).
# Одна пара на весь проект: иначе лента и консультант однажды начнут
# отвечать по-разному, и объяснить это будет нечем.
#
# Запасная намеренно ДРУГОГО СЕМЕЙСТВА: если у семейства окажется общая
# беда (формат ответа, недоступность площадки для этой модели), запасная
# её не повторит.
# Пара выбрана ФАКТОМ 27.09.2026: из девяти сильнейших моделей каталога
# Cloud.ru платными на этом счёте отвечают РОВНО ДВЕ — claude-opus-4.8 и
# claude-sonnet-4.6; остальные, включая самые дешёвые, отдают 402 «не
# хватает средств». Обе одного семейства, и это вынужденно: другого
# оплаченного семейства нет. Когда счёт пополнят, запасную надо перевести
# в другое семейство — переменной VOLOK_AI_MODEL2, без правки кода.
MODEL = _env("VOLOK_AI_MODEL", "anthropic/claude-opus-4.8")
MODEL2 = _env("VOLOK_AI_MODEL2", "anthropic/claude-sonnet-4.6")
BASE2 = _env("VOLOK_AI_BASE2", BASE)

IMPACTS = ("plus", "minus", "risk", "neutral")
MAXLEN = 140

SYSTEM = (
    "Ты помогаешь владельцу майнинг-фермы на биткоине быстро понять, "
    "касается ли его новость. Тебе дают ТОЛЬКО заголовок, источник и дату. "
    "Оцени влияние именно на ДОХОД ФЕРМЫ: цену биткоина, сложность сети, "
    "хешпрайс, цену оборудования, электричество, законы для майнеров в "
    "России.\n"
    "Отвечай СТРОГО одним JSON-объектом, без пояснений, без рассуждений и "
    "без разметки:\n"
    '{"impact":"plus|minus|risk|neutral","text":"одна фраза до 140 знаков"}\n'
    "impact: plus — доход фермы скорее вырастет; minus — скорее упадёт; "
    "risk — прямого влияния нет, но есть угроза (проверки, запреты, "
    "возможная коррекция курса); neutral — на доход фермы не влияет.\n"
    "Запреты: не придумывай содержание статьи сверх заголовка; "
    "если по заголовку непонятно — neutral; "
    "никаких советов покупать или продавать; "
    "text не длиннее 140 знаков."
)

# Слова-советы: их наличие в ответе — отказ. Запрет N3.
SOVET = ("купит", "продав", "продат", "закупа", "инвестируй", "покупай",
         "вкладывай", "шортит", "лонг")


def ask_raw(title, source, date_ru, model=None, base=None, key=None,
            timeout=None):
    """Один запрос к модели. Возвращает (текст ответа, usage, сек).

    Вынесен отдельно, потому что этим же пользуется слепое сравнение
    моделей (tools/sravnenie_modeley.py): сравнивать надо ровно тем
    кодом и ровно той инструкцией, что работают в бою."""
    model = model or MODEL
    base = (base or BASE).rstrip("/")
    key = key or KEY
    timeout = timeout or TIMEOUT
    body = json.dumps({
        "model": model,
        "max_tokens": 300,
        "temperature": 0,          # оценка должна быть воспроизводимой
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content":
             "Источник: %s\nДата: %s\nЗаголовок: %s" % (source, date_ru, title)},
        ],
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base + "/chat/completions", data=body, headers={
        "content-type": "application/json",
        "authorization": "Bearer " + key,
    })
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8"))
    took = round(time.time() - t0, 2)
    ch = (d.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    txt = msg.get("content") or ""
    # Рассуждающие модели кладут мысли в отдельное поле — в разбор они
    # не идут, но пусть будут видны в сравнении.
    if not txt and msg.get("reasoning_content"):
        txt = ""
    return txt.strip(), (d.get("usage") or {}), took


def parse(raw):
    """Вытащить и проверить ответ. Возвращает (ai, причина отказа).

    Проверка одна на бой и на сравнение — иначе сравнение показало бы
    одно, а бот делал другое."""
    if not raw:
        return None, "пустой ответ модели"
    # Последний объект в фигурных скобках: рассуждающие модели часто
    # пишут мысли ДО ответа, а иногда и после.
    ms = re.findall(r"\{[^{}]*\}", raw, re.S)
    if not ms:
        ms = re.findall(r"\{.*\}", raw, re.S)
    if not ms:
        return None, "в ответе нет JSON"
    j = None
    for cand in reversed(ms):
        try:
            j = json.loads(cand)
            if isinstance(j, dict) and "impact" in j:
                break
            j = None
        except Exception:                                       # noqa: BLE001
            j = None
    if j is None:
        return None, "JSON не разобрался"

    imp = str(j.get("impact", "")).strip().lower()
    txt = str(j.get("text", "")).strip()
    if imp not in IMPACTS:
        return None, "неизвестный impact %r" % imp[:20]
    if not txt:
        return None, "пустой text"
    if len(txt) > MAXLEN:
        # Обрезать — значит подделать ответ. Отказываемся.
        return None, "text длиннее %d знаков (%d)" % (MAXLEN, len(txt))
    low = txt.lower()
    for bad in SOVET:
        if bad in low:
            return None, "совет покупать или продавать"
    return {"impact": imp, "text": txt}, None


def analyse(title, source, date_ru, log):
    """Вернуть (ai, usage). ai — словарь или None. Причина отказа — в log.

    ПЕРЕКЛЮЧЕНИЕ НА ЗАПАСНУЮ. Основная не ответила, ответила негодным
    или упала — пробуем запасную. Обе молчат — ai: null и причина в
    журнал. Шаблон не подставляем никогда: выдуманная оценка под видом
    разбора хуже, чем честное отсутствие разбора."""
    if not KEY:
        log.append({"заголовок": title[:80], "причина": "ключ не задан"})
        return None, {}
    usage_total = {}
    for роль, модель, база in (("основная", MODEL, BASE),
                               ("запасная", MODEL2, BASE2)):
        ai, usage, why = _try_one(title, source, date_ru, модель, база)
        if usage:
            usage_total = usage
        if ai is not None:
            ai["кто"] = роль
            return ai, usage_total
        log.append({"заголовок": title[:80], "кто": роль, "модель": модель,
                    "причина": why})
    return None, usage_total


def _try_one(title, source, date_ru, модель, база):
    """Одна попытка одной моделью. Возвращает (ai, usage, причина отказа)."""
    try:
        raw, usage, took = ask_raw(title, source, date_ru, model=модель, base=база)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:120]
        except Exception:                                       # noqa: BLE001
            pass
        return None, {}, "HTTP %s: %s" % (e.code, detail)
    except Exception as e:                                      # noqa: BLE001
        return None, {}, "сеть: %s" % str(e)[:120]

    ai, why = parse(raw)
    if ai is None:
        return None, usage, why
    # У каждой оценки — кто её дал и когда. Через месяц это единственный
    # способ понять, почему две соседние новости оценены по-разному.
    ai["модель"] = модель
    ai["площадка"] = база
    ai["разобрано_unix"] = int(time.time())
    ai["секунд"] = took
    return ai, usage, None
