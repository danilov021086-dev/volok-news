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
  На выход — строго JSON с ОДНИМ полем:
      {"impact": "plus" | "minus" | "risk" | "neutral"}
  Фразы разбора больше нет: с 02.10.2026 мнение под новостью пишет
  редакция, одно на весь день, а модель ставит только метку. Ею красится
  заголовок и считаются бычьи/медвежьи/нейтральные в полосе дня.

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

import checks   # проверки «Коротко»/разбора (задание 30.09, этап 1)

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
    '{"impact":"plus|minus|risk|neutral"}\n'
    "impact: plus — доход фермы скорее вырастет; minus — скорее упадёт; "
    "risk — прямого влияния нет, но есть угроза (проверки, запреты, "
    "возможная коррекция курса); neutral — на доход фермы не влияет.\n"
    "Запреты: не придумывай содержание статьи сверх заголовка; "
    "если по заголовку непонятно — neutral; "
    "никаких пояснений и никакого текста — ТОЛЬКО поле impact."
)

# Слова-советы: их наличие в ответе — отказ. Запрет N3.
SOVET = ("купит", "продав", "продат", "закупа", "инвестируй", "покупай",
         "вкладывай", "шортит", "лонг")



# =====================================================================
# ВЫКЛЮЧЕНИЕ РАССУЖДЕНИЙ (замер 28.09.2026)
# =====================================================================
# ЗАМЕР, ИЗ-ЗА КОТОРОГО ЭТО ПОЯВИЛОСЬ. Первый живой прогон на варианте Б
# разобрал пачку из пяти заголовков и показал 1588 выходных токенов — при
# потолке 80 на заголовок, то есть 464 на всю пачку. Больше потолка.
# Объяснение одно: модель РАССУЖДАЛА, а рассуждения площадка считает
# выходными токенами и оплачивает, но потолком ответа не ограничивает.
#
# То есть «думающие режимы выключены» в задании до сих пор выполнялось
# лишь в том смысле, что мы их не включали. Открытые модели рассуждают по
# умолчанию, и выключать надо явно.
#
# ЧТО ПОДОШЛО (проверено на площадке тем же ключом):
#   thinking {"type":"disabled"}   — работает у DeepSeek V4 Flash и GLM-5.2
#   enable_thinking=false          — принимается, но рассуждения остаются
#   reasoning_effort=none          — HTTP 400, допустимы только low..max
#
# ПОЧЕМУ С ОТКАТОМ. Поле понимают не все модели, а список «кто понимает»
# в коде устареет. Шлём; площадка ответила 400 и жалуется на это поле —
# запоминаем модель и повторяем без него.
NOTHINK = {"type": "disabled"}
_nothink_bad = set()


def _телом_без_рассуждений(тело, модель):
    """Добавить в тело запроса выключение рассуждений, если можно."""
    if _env("VOLOK_AI_THINK", "") in ("1", "да", "yes"):
        return тело                      # нарочно оставили рассуждения
    if модель in _nothink_bad:
        return тело
    тело = dict(тело)
    тело["thinking"] = NOTHINK
    return тело


def _post_json(base, key, тело, timeout, модель):
    """Запрос с откатом, если площадка не поняла выключение рассуждений."""
    for попытка in (1, 2):
        req = urllib.request.Request(
            base.rstrip("/") + "/chat/completions",
            data=json.dumps(тело, ensure_ascii=False).encode("utf-8"),
            headers={"content-type": "application/json",
                     "authorization": "Bearer " + key})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if попытка == 1 and e.code == 400 and "thinking" in тело:
                кусок = ""
                try:
                    кусок = e.read().decode("utf-8", "replace")[:300].lower()
                except Exception:                               # noqa: BLE001
                    pass
                if "thinking" in кусок or "reasoning" in кусок:
                    _nothink_bad.add(модель)
                    print("модель %s не приняла выключение рассуждений — "
                          "дальше шлём без него" % модель)
                    тело = dict(тело)
                    тело.pop("thinking", None)
                    continue
            raise
    raise RuntimeError("запрос не удался")

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
    тело = _телом_без_рассуждений({
        "model": model,
        #  Ответ теперь — один короткий JSON с полем impact.
        #  Триста токенов под него держать незачем: платим за
        #  выход, а рассуждения модели считаются выходом.
        "max_tokens": 64,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content":
             "Источник: %s\nДата: %s\nЗаголовок: %s" % (source, date_ru, title)},
        ],
    }, model)
    t0 = time.time()
    d = _post_json(base, key, тело, timeout, model)
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
    if imp not in IMPACTS:
        return None, "неизвестный impact %r" % imp[:20]
    #  ТОЛЬКО МЕТКА (решение владельца 02.10.2026). Фразу разбора мы
    #  больше не просим и не храним: мнение под новостью пишет редакция.
    #  Если модель по привычке всё же прислала text, он проверяется на
    #  советы и выбрасывается — в ленту не попадает ни при каких
    #  условиях, и значит не может туда просочиться позже.
    txt = str(j.get("text", "")).strip()
    if txt:
        low = txt.lower()
        for bad in SOVET:
            if bad in low:
                return None, "совет покупать или продавать"
    return {"impact": imp}, None


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


# =====================================================================
# «КОРОТКО» — СВОЙ ПЕРЕСКАЗ СТАТЬИ (задание /novosti/, часть 2)
# =====================================================================
# Новое решение владельца 29.09.2026: для публичной страницы новостей бот
# ЧИТАЕТ текст статьи по ссылке и пишет «Коротко» — 2–3 предложения СВОИМИ
# словами. Это отступает от прежней записи «текст статьи бот не берёт»:
# прежняя касалась ленты кабинета (оценка по заголовку), новая — публичной
# страницы. Полный текст статьи НИКОГДА не публикуется и не хранится —
# только свой пересказ (часть 2.2).
#
# ТРИ ЗАЩИТЫ:
#   1. Проверка на копирование: ни одной цепочки в 8+ слов подряд из
#      оригинала (сравнение по словам). Не прошло — переписать один раз,
#      снова не прошло — «Коротко» нет.
#   2. Статья не открылась — «Коротко» нет (С1), новость всё равно
#      публикуется с заголовком и оценкой.
#   3. Расход в общем пределе бота (5 ₽/сутки, жёсткий потолок).
KOROTKO_ON = _env("VOLOK_KOROTKO", "1") not in ("0", "", "off", "нет")
KOROTKO_MAXTOK = int(_env("VOLOK_KOROTKO_MAXTOK", "150"))
ARTICLE_MAXCHARS = int(_env("VOLOK_ARTICLE_CHARS", "2400"))

SYSTEM_KOROTKO = (
    "Ты пишешь короткий пересказ новости для владельца майнинг-фермы. "
    "Тебе дают текст статьи. Перескажи суть 2–3 предложениями СВОИМИ "
    "словами, простым русским. Строгие правила: не копируй фразы из "
    "оригинала (перефразируй); не советуй покупать или продавать; не "
    "предсказывай цену числами; не выдумывай фактов и статей законов; не "
    "добавляй ничего от себя сверх статьи. "
    "НЕ МЕНЯЙ ВРЕМЯ СОБЫТИЙ: то, что запланировано на будущее, пиши в "
    "будущем времени (запустят, выйдет, вступит в силу), а не в прошедшем. "
    "НЕ ОБРАЩАЙСЯ К ЧИТАТЕЛЮ и не пиши о себе: никаких «извините», "
    "«пришлите текст», «как ИИ», «я не могу». Если текста мало или он "
    "нечитаем — ответь одним словом: НЕТ. "
    "Только пересказ, без вступлений, заголовков и вопросов."
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)


def fetch_article(url, timeout=None):
    """Достать текст статьи по ссылке. Возвращает строку или ''.

    Грубо: снимаем скрипты/стили и теги, берём длинные текстовые куски.
    Точный парсинг не нужен — модель пересказывает суть, а не структуру."""
    timeout = timeout or TIMEOUT
    try:
        # Просим несжатое, но некоторые сайты (Bits.media) ВСЁ РАВНО отдают
        # gzip — 30.09 из-за этого текст приходил сжатыми байтами, модель
        # видела мусор и отвечала отказом. Поэтому распаковываем сами по
        # заголовку И по magic-байтам.
        req = urllib.request.Request(url, headers={
            "user-agent": "Mozilla/5.0 (compatible; VolokNewsBot/1.0)",
            "accept-encoding": "identity"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(1200000)
            enc = (r.headers.get("Content-Encoding") or "").lower()
        if enc == "gzip" or data[:2] == b"\x1f\x8b":
            import gzip as _gz
            data = _gz.decompress(data)
        elif enc == "deflate":
            import zlib as _zl
            try:
                data = _zl.decompress(data)
            except Exception:
                data = _zl.decompress(data, -_zl.MAX_WBITS)
        elif enc == "br":
            try:
                import brotli as _br
                data = _br.decompress(data)
            except Exception:
                return ""   # brotli не собран — пересказа не будет (С1)
        raw = data.decode("utf-8", "replace")
    except Exception:                                           # noqa: BLE001
        return ""
    # вырезаем <article> если есть — там основной текст; иначе весь body
    m = re.search(r"<article\b[^>]*>(.*?)</article>", raw, re.S | re.I)
    body = m.group(1) if m else raw
    body = _SCRIPT_RE.sub(" ", body)
    # абзацы
    paras = re.findall(r"<p\b[^>]*>(.*?)</p>", body, re.S | re.I)
    text = " ".join(_TAG_RE.sub(" ", p) for p in paras) if paras \
        else _TAG_RE.sub(" ", body)
    import html as _h
    text = _h.unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    return text[:ARTICLE_MAXCHARS]


def _words(s):
    return re.findall(r"\w+", (s or "").lower(), re.U)


def copied(peresk, original, n=8):
    """True, если в пересказе есть цепочка n+ слов подряд из оригинала."""
    pw, ow = _words(peresk), _words(original)
    if len(pw) < n:
        return False
    oset = set()
    for i in range(len(ow) - n + 1):
        oset.add(tuple(ow[i:i + n]))
    for i in range(len(pw) - n + 1):
        if tuple(pw[i:i + n]) in oset:
            return True
    return False


def _korotko_once(article, модель, база):
    """Один запрос пересказа. Возвращает (текст, usage, причина)."""
    тело = _телом_без_рассуждений({
        "model": модель,
        "max_tokens": KOROTKO_MAXTOK,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": SYSTEM_KOROTKO},
            {"role": "user", "content": "Текст статьи:\n" + article},
        ],
    }, модель)
    try:
        d = _post_json(база, KEY, тело, TIMEOUT, модель)
    except urllib.error.HTTPError as e:
        return "", {}, "HTTP %s" % e.code
    except Exception as e:                                       # noqa: BLE001
        return "", {}, "сеть: %s" % str(e)[:80]
    ch = (d.get("choices") or [{}])[0]
    txt = ((ch.get("message") or {}).get("content") or "").strip()
    return txt, (d.get("usage") or {}), None


def korotko(title, url, log, pub_unix=None):
    """«Коротко» своими словами. Возвращает (текст|None, usage).

    Защита ВХОДА (1.6): текст статьи проверяется checks.article_ok ДО
    вызова модели — на мусор денег не тратим. Защита ВЫХОДА (1.7/1.8):
    checks.korotko_ok — отказы, обращения, «как ИИ», длина, слово
    заголовка, будущая дата в прошедшем. Плюс проверка на копирование.
    Не прошло — переписать один раз; снова — «Коротко» нет (С1)."""
    if not KOROTKO_ON or not KEY:
        return None, {}
    article = fetch_article(url)
    ok, why = checks.article_ok(article, title)
    if not ok:
        log.append({"заголовок": title[:80], "коротко": "статья не годится: " + why})
        return None, {}
    usage_total = {}
    for роль, модель, база in (("основная", MODEL, BASE),
                               ("запасная", MODEL2, BASE2)):
        for попытка in (1, 2):     # 2.2: не прошло — переписать один раз
            txt, usage, why = _korotko_once(article, модель, база)
            if usage:
                usage_total = usage
            if not txt:
                if why:
                    break          # площадка не ответила — к запасной
                continue
            good, prichina = checks.korotko_ok(txt, title, pub_unix)
            if not good:
                log.append({"заголовок": title[:80],
                            "коротко": "%s (попытка %d)" % (prichina, попытка)})
                continue
            if checks.copied(txt, article):
                log.append({"заголовок": title[:80],
                            "коротко": "копирует оригинал (попытка %d)" % попытка})
                continue
            return txt, usage_total
    return None, usage_total


# =====================================================================
# ПАЧКОЙ ДО ДЕСЯТИ ЗАГОЛОВКОВ В ОДНОМ ЗАПРОСЕ (пункт 1.3 задания)
# =====================================================================
# ЗАЧЕМ. Раньше каждый заголовок стоил отдельного запроса, и в каждом
# заново уезжала инструкция на ~370 токенов. При сорока новостях в сутки
# это сорок инструкций — то есть мы платили за одно и то же сорок раз.
# Пачкой из десяти инструкция уходит ОДИН раз на десять заголовков.
#
# ЧЕГО ЗДЕСЬ НЕТ И БЫТЬ НЕ ДОЛЖНО. Пачка не «добирает» ответы: если
# модель вернула оценку не на все заголовки, остальные остаются с
# ai: null и попадут в следующий прогон. Подставить соседнюю оценку или
# шаблон — значит соврать про новость, которую модель не смотрела.
#
# ПОЧЕМУ НОМЕРА, А НЕ ЗАГОЛОВКИ В ОТВЕТЕ. Возвращать заголовок целиком
# дорого (это выходные токены) и ненадёжно: модель перепишет кавычки или
# сократит, и сопоставить будет нечем. Номер сопоставляется точно.
BATCH_MAX = int(_env("VOLOK_AI_BATCH", "10"))

SYSTEM_BATCH = (
    "Ты помогаешь владельцу майнинг-фермы на биткоине быстро понять, "
    "касается ли его новость. Тебе дают СПИСОК заголовков с номерами. "
    "Оцени влияние каждого именно на ДОХОД ФЕРМЫ: цену биткоина, "
    "сложность сети, хешпрайс, цену оборудования, электричество, законы "
    "для майнеров в России.\n"
    "Отвечай СТРОГО одним JSON-объектом, без пояснений, без рассуждений "
    "и без разметки:\n"
    "{\"items\":[{\"n\":1,\"impact\":\"plus|minus|risk|neutral\"}]}\n"
    "В items — по одной записи на КАЖДЫЙ номер из списка, номер n тот же, "
    "что во входе.\n"
    "impact: plus — доход фермы скорее вырастет; minus — скорее упадёт; "
    "risk — прямого влияния нет, но есть угроза (проверки, запреты, "
    "возможная коррекция курса); neutral — на доход фермы не влияет.\n"
    "Запреты: не придумывай содержание статьи сверх заголовка; "
    "если по заголовку непонятно — neutral; "
    "никаких пояснений и никакого текста — ТОЛЬКО поля n и impact."
)


def ask_raw_batch(записи, model=None, base=None, key=None, timeout=None,
                  maxtok_each=None):
    """Один запрос на пачку. записи — список (n, заголовок, источник, дата).

    Потолок ответа считается от числа заголовков: 80 токенов на каждый
    (столько же, сколько задание отводит одной новости) плюс небольшой
    запас на обёртку JSON."""
    model = model or MODEL
    base = (base or BASE).rstrip("/")
    key = key or KEY
    timeout = timeout or TIMEOUT
    #  Было 80 на заголовок под фразу разбора; теперь в ответе
    #  только номер и метка — хватает с запасом.
    each = int(maxtok_each or _env("VOLOK_AI_MAXTOK_NEWS", "16"))
    список = "\n".join(
        "%d. Источник: %s | Дата: %s | Заголовок: %s" % (n, s, d, t)
        for n, t, s, d in записи)
    тело = _телом_без_рассуждений({
        "model": model,
        "max_tokens": each * len(записи) + 64,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_BATCH},
            {"role": "user", "content": список},
        ],
    }, model)
    t0 = time.time()
    d = _post_json(base, key, тело, timeout, model)
    took = round(time.time() - t0, 2)
    ch = (d.get("choices") or [{}])[0]
    txt = ((ch.get("message") or {}).get("content") or "").strip()
    return txt, (d.get("usage") or {}), took


def _obj_po_skobkam(raw):
    """Самый длинный разобравшийся объект в ответе.

    Нежадное {…} здесь не годится: у ответа вида {"items":[{…}]} оно
    вернёт первую вложенную запись, где ключа items нет. Ровно на этом
    стенд сравнения моделей 27.09 забраковал пять верных прогнозов из
    пяти — ошибка была в проверке, а не в модели."""
    луч = None
    for i, ch in enumerate(raw):
        if ch != "{":
            continue
        глуб = 0
        for j in range(i, len(raw)):
            if raw[j] == "{":
                глуб += 1
            elif raw[j] == "}":
                глуб -= 1
                if глуб == 0:
                    try:
                        o = json.loads(raw[i:j + 1])
                    except Exception:                           # noqa: BLE001
                        break
                    if isinstance(o, dict) and (луч is None
                                                or j + 1 - i > луч[1]):
                        луч = (o, j + 1 - i)
                    break
    return луч[0] if луч else None


def parse_batch(raw, номера):
    """Разобрать ответ на пачку. Возвращает (словарь n -> ai, причины).

    Проверки — ТЕ ЖЕ, что у одиночного разбора: они живут в parse(), и
    вызываются здесь для каждой записи. Двух разных проверок быть не
    должно, иначе пачка пропустит то, что одиночный разбор отбивает."""
    if not raw:
        return {}, ["пустой ответ модели"]
    o = _obj_po_skobkam(raw)
    if not isinstance(o, dict) or not isinstance(o.get("items"), list):
        return {}, ["в ответе нет объекта с items"]
    вышло, причины = {}, []
    видел = set()
    for it in o["items"]:
        if not isinstance(it, dict):
            continue
        try:
            n = int(it.get("n"))
        except (TypeError, ValueError):
            причины.append("запись без номера n")
            continue
        if n not in номера:
            причины.append("номер %d, которого не было в запросе" % n)
            continue
        if n in видел:
            причины.append("номер %d пришёл дважды" % n)
            continue
        видел.add(n)
        ai, why = parse(json.dumps({"impact": it.get("impact")},
                                   ensure_ascii=False))
        if ai is None:
            причины.append("номер %d: %s" % (n, why))
            continue
        вышло[n] = ai
    пропущены = sorted(set(номера) - видел)
    if пропущены:
        причины.append("модель не ответила про номера: %s"
                       % ", ".join(str(x) for x in пропущены))
    return вышло, причины


def analyse_batch(записи, log):
    """Разобрать пачку. записи — список (ключ, заголовок, источник, дата).

    Возвращает (словарь ключ -> ai, usage). Ключей, про которые модель не
    ответила, в словаре НЕТ — вызывающий оставит им ai: null, и следующий
    прогон попробует снова. Переключение на запасную — как у одиночного
    разбора: пачку целиком отдаём второй модели."""
    if not KEY:
        log.append({"причина": "ключ не задан", "пачка": len(записи)})
        return {}, {}
    if not записи:
        return {}, {}
    ном = {i + 1: z[0] for i, z in enumerate(записи)}
    вход = [(i + 1, z[1], z[2], z[3]) for i, z in enumerate(записи)]
    usage_total = {}
    for роль, модель, база in (("основная", MODEL, BASE),
                               ("запасная", MODEL2, BASE2)):
        try:
            raw, usage, took = ask_raw_batch(вход, model=модель, base=база)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:120]
            except Exception:                                   # noqa: BLE001
                pass
            log.append({"кто": роль, "модель": модель, "пачка": len(записи),
                        "причина": "HTTP %s: %s" % (e.code, detail)})
            continue
        except Exception as e:                                   # noqa: BLE001
            log.append({"кто": роль, "модель": модель, "пачка": len(записи),
                        "причина": "сеть: %s" % str(e)[:120]})
            continue
        if usage:
            usage_total = usage
        вышло, причины = parse_batch(raw, set(ном))
        for p in причины:
            log.append({"кто": роль, "модель": модель, "причина": p})
        if вышло:
            итог = {}
            for n, ai in вышло.items():
                ai["кто"] = роль
                ai["модель"] = модель
                ai["площадка"] = база
                ai["разобрано_unix"] = int(time.time())
                ai["секунд"] = took
                ai["пачкой"] = len(записи)
                итог[ном[n]] = ai
            return итог, usage_total
        # Ни одной годной оценки — пробуем запасную той же пачкой.
    return {}, usage_total
