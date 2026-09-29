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
    # Cloud.ru Evolution Foundation Models.
    # Числа сверены с каталогом площадки фактом 27.09.2026: она отдаёт их
    # в metadata каждой модели (prompt_tokens_cost / generated_tokens_cost).
    "anthropic/claude-opus-4.8": (854.0, 4270.0),
    "anthropic/claude-sonnet-4.6": (589.26, 2946.3),
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



# =====================================================================
# СУТОЧНЫЙ ПРЕДЕЛ РАСХОДА У БОТА (пункт 0.3 задания «стоп тратам»)
# =====================================================================
# ЗАЧЕМ ОТДЕЛЬНЫЙ ПРЕДЕЛ, ЕСЛИ ОН УЖЕ ЕСТЬ НА СЕРВЕРЕ. Предел на сервере
# сторожит сайт: консультанта, помощника, прогноз. А лента живёт НЕ на
# сервере — она собирается бегунком GitHub Actions, у неё свой ключ в
# секретах и своя касса. Про серверный журнал расхода она не знает ничего
# и знать не может.
#
# А ЛЕНТА — ЭТО ГЛАВНАЯ ПОСТОЯННАЯ ТРАТА. Замер: разбор одного заголовка
# пачкой по десять стоит на Claude Opus 4.8 около 0,30 ₽. Сорок новостей
# в сутки — это 12 ₽, то есть ОДНА ЛЕНТА съедает весь суточный предел в
# 10 ₽ и ещё немного сверху. На DeepSeek V4 Flash то же самое стоит 0,13 ₽
# в сутки — в девяносто раз меньше. Пока модель не выбрана владельцем,
# лента работает на дорогой, и без своего предела она бы тратила, сколько
# захочет.
#
# КАК СЧИТАЕТСЯ. Расход по дням лежит в самой ленте (news.json), которую
# бот и так коммитит каждый прогон — значит счётчик переживает бегунок,
# у которого никакой памяти между запусками нет. Хранится две недели.
def _env_float(name, default):
    """То же, что _env_int, но для рублей. Пустая переменная Actions — это
    пустая СТРОКА, а не отсутствие: float("") падает."""
    v = (os.environ.get(name) or "").strip().replace(",", ".")
    try:
        return float(v) if v else default
    except ValueError:
        print("переменная %s=%r не число, беру %s" % (name, v, default))
        return default


# Р2 задания v50 (решение владельца 29.09.2026): 5 ₽/сутки — ЖЁСТКИЙ
# потолок бота. Переменная Actions может предел только снизить; поднять
# выше 5 нельзя ничем, кроме правки этого файла.
BUDGET_HARD_CAP_RUB = 5.0
BUDGET_RUB_DAY = min(_env_float("VOLOK_AI_BUDGET_DAY", 5.0), BUDGET_HARD_CAP_RUB)
if BUDGET_RUB_DAY <= 0:
    BUDGET_RUB_DAY = BUDGET_HARD_CAP_RUB

# Сколько «коротко» писать за один прогон. Растягиваем на несколько суток
# (часть 2.5): маленькими порциями, чтобы не выесть предел зараз и чтобы
# первые прогоны можно было проверить глазами. _env_int определён выше.
KOROTKO_MAX_PER_RUN = _env_int("VOLOK_KOROTKO_PER_RUN", 6)
BUDGET_KEEP_DAYS = 14


def бюджет_день(prev):
    """Сегодняшний день и сколько на него уже истрачено (по ленте)."""
    день = time.strftime("%Y-%m-%d", time.gmtime())
    расход = ((prev or {}).get("расход_по_дням") or {})
    return день, float(расход.get(день) or 0.0)


def бюджет_записать(prev, руб):
    """Прибавить расход к сегодняшнему дню. Возвращает словарь по дням."""
    день = time.strftime("%Y-%m-%d", time.gmtime())
    расход = dict(((prev or {}).get("расход_по_дням") or {}))
    расход[день] = round(float(расход.get(день) or 0.0) + float(руб or 0.0), 4)
    # Держим две недели: дальше это уже не счёт, а архив.
    ключи = sorted(расход)
    for k in ключи[:-BUDGET_KEEP_DAYS]:
        расход.pop(k, None)
    return расход

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

    # ---------------- разбор ИИ только для новых, ПАЧКАМИ ПО ДЕСЯТЬ
    #
    # ПОЧЕМУ ПАЧКАМИ (пункт 1.3 задания «стоп тратам»). Инструкция модели
    # весит около 370 токенов, и раньше она уезжала заново на КАЖДЫЙ
    # заголовок: сорок новостей в сутки — сорок оплаченных инструкций.
    # В пачке из десяти она уходит один раз. Замер экономии — в отчёте.
    #
    # Одиночный разбор (ai_mod.analyse) НЕ УДАЛЁН: им пользуется
    # самопроверка бота, и он же остаётся запасным путём, если пачка
    # однажды перестанет разбираться у какой-нибудь модели.
    used = {"вход": 0, "выход": 0, "разобрано": 0, "руб": 0.0,
            "запросов": 0, "пачек": 0}
    # СУТОЧНЫЙ ПРЕДЕЛ. Считается по ленте прошлого прогона: у бегунка
    # GitHub Actions своей памяти между запусками нет вовсе.
    день_бюджета, истрачено_днём = бюджет_день(prev)
    предел_достигнут = False

    def разобрать_пачками(записи):
        """записи — список записей ленты. Считает расход в used.

        ПЕРЕД КАЖДОЙ ПАЧКОЙ смотрим суточный предел: пачка — это и есть
        единица траты, дробить её бессмысленно."""
        nonlocal предел_достигнут
        шаг = max(1, int(getattr(ai_mod, "BATCH_MAX", 10)))
        сделано_тут = 0
        for i in range(0, len(записи), шаг):
            уже = истрачено_днём + (used["руб"] or 0.0)
            if BUDGET_RUB_DAY > 0 and уже >= BUDGET_RUB_DAY:
                предел_достигнут = True
                ailog.append({"причина": "суточный предел расхода %.2f руб "
                                         "достигнут (истрачено %.2f) — разбор "
                                         "отложен до следующих суток"
                                         % (BUDGET_RUB_DAY, уже)})
                break
            часть = записи[i:i + шаг]
            вход = [(id(r), r["заголовок"], r["источник"], r["дата_мск"])
                    for r in часть]
            вышло, usage = ai_mod.analyse_batch(вход, ailog)
            # Токены считаем и у отказов тоже: заплачено за них одинаково.
            # Имена полей у OpenAI-совместимых площадок общие.
            used["вход"] += int(usage.get("prompt_tokens")
                                or usage.get("input_tokens") or 0)
            used["выход"] += int(usage.get("completion_tokens")
                                 or usage.get("output_tokens") or 0)
            used["запросов"] += 1
            used["пачек"] += 1
            # Рубли пересчитываем СРАЗУ после пачки, а не в конце прогона:
            # иначе предел проверялся бы по нулю и не остановил бы ничего.
            _r = price_rub(used["вход"], used["выход"])
            used["руб"] = round(_r, 4) if _r is not None else used["руб"]
            for r in часть:
                a = вышло.get(id(r))
                # Про кого модель не ответила — остаётся ai: null и
                # попадёт в следующий прогон. Подставлять соседнюю оценку
                # или шаблон нельзя: это была бы выдумка про новость.
                if a:
                    r["ai"] = a
                    used["разобрано"] += 1
                elif not r.get("ai"):
                    r["ai"] = None
                сделано_тут += 1
        return сделано_тут

    разобрать_пачками(new_items[:AI_MAX_PER_RUN])
    # ---------------- ДОГОН: новости, у которых разбора нет
    #
    # ЗАЧЕМ. Разбор шёл только для НОВЫХ новостей. Пока ключа не было, все
    # они получили ai: null — и остались бы такими навсегда: следующий
    # прогон их уже не считает новыми. В ленте это выглядело бы как «ИИ
    # включён, а оценок нет», и первый же прогон с ключом ничего бы не
    # исправил. Проверено фактом: прогон Actions 27.09 отработал с ключом
    # и дал «разобрано 0», потому что новых новостей в тот час не было.
    #
    # Догоняем с конца — свежие важнее, — и в пределах того же
    # предохранителя: за один прогон не больше AI_MAX_PER_RUN обращений
    # вообще, сколько бы старых ни накопилось.
    сделано = len(new_items[:AI_MAX_PER_RUN])
    # Идём по merged, а не по items: items собирается ниже, после разбора,
    # и обращение к нему здесь было бы ошибкой порядка.
    прежние = sorted(merged.values(),
                     key=lambda x: -int(x.get("unix") or 0))
    ждут = [r for r in прежние if not r.get("ai")][:max(0, AI_MAX_PER_RUN - сделано)]
    if ждут:
        разобрать_пачками(ждут)
        print("догнали разбор у %d прежних новостей" % len(ждут))
    print("запросов к модели за прогон: %d (пачками по %d)"
          % (used["запросов"], getattr(ai_mod, "BATCH_MAX", 10)))

    # Цена может быть неизвестна: модель новая, в таблице её нет. Это не
    # повод падать — пишем null, и в ленте честно видно, что расход не
    # посчитан. Прежняя редакция звала round() от None и роняла весь бот
    # при первой же смене модели.
    _руб = price_rub(used["вход"], used["выход"])
    used["руб"] = round(_руб, 4) if _руб is not None else None
    if len(new_items) > AI_MAX_PER_RUN:
        print("разбор отложен для %d новостей (предохранитель)"
              % (len(new_items) - AI_MAX_PER_RUN))

    # ---------------- «КОРОТКО»: свой пересказ статьи (задание /novosti/)
    #
    # Приоритет mine+ru (часть 2.5): если на всё не хватит предела, важные
    # для нас темы получают пересказ первыми. Идём только по тем, у кого
    # «коротко» ещё нет, и в том же суточном пределе, что и оценки.
    kor = {"сделано": 0, "снято": 0}
    if getattr(ai_mod, "KOROTKO_ON", False) and ai_mod.KEY:
        приоритет = lambda r: 0 if (set(r.get("метки") or []) & {"mine", "ru"}) else 1
        кандидаты = [r for r in sorted(merged.values(),
                                       key=lambda x: (приоритет(x), -int(x.get("unix") or 0)))
                     if not r.get("коротко") and r.get("ссылка")]
        for r in кандидаты[:KOROTKO_MAX_PER_RUN]:
            уже = истрачено_днём + (used["руб"] or 0.0)
            if BUDGET_RUB_DAY > 0 and уже >= BUDGET_RUB_DAY:
                ailog.append({"причина": "предел суток достигнут — «коротко» "
                                         "отложено до следующих суток"})
                break
            txt, usage = ai_mod.korotko(r["заголовок"], r["ссылка"], ailog)
            used["вход"] += int(usage.get("prompt_tokens")
                                or usage.get("input_tokens") or 0)
            used["выход"] += int(usage.get("completion_tokens")
                                 or usage.get("output_tokens") or 0)
            used["запросов"] += 1
            _r = price_rub(used["вход"], used["выход"])
            used["руб"] = round(_r, 4) if _r is not None else used["руб"]
            if txt:
                r["коротко"] = txt
                kor["сделано"] += 1
            else:
                kor["снято"] += 1
        print("«коротко»: написано %d, снято %d" % (kor["сделано"], kor["снято"]))

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
            "запросов_за_прогон": used["запросов"],
            "пачка_заголовков": getattr(ai_mod, "BATCH_MAX", 10),
            # СУТОЧНЫЙ ПРЕДЕЛ И РАСХОД ПО ДНЯМ — в самой ленте: у бегунка
            # Actions нет никакой памяти между запусками, а счётчик должен
            # переживать их все. Заодно владелец видит расход по дням
            # своими глазами, без доступа к серверу.
            "предел_суток_руб": BUDGET_RUB_DAY,
            "истрачено_за_сутки_руб": round(
                истрачено_днём + (used["руб"] or 0.0), 4),
            "предел_достигнут": предел_достигнут,
            "отказы": ailog[:40],
        },
        "расход_по_дням": бюджет_записать(prev, used["руб"] or 0.0),
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
