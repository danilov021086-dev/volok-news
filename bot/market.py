# -*- coding: utf-8 -*-
"""
market.py — блок `market` для news.json: курсы, сложность, хешрейт сети
и хешпрайс.

ГЛАВНОЕ ПРАВИЛО ЗДЕСЬ — ЧЕСТНОСТЬ ИСТОЧНИКА (правило С1 проекта).
У КАЖДОГО числа стоит, откуда оно и когда снято. Источник не ответил —
поле пустое (null), плюс время последнего удачного снятия и текст
ошибки. Ноль вместо неизвестного и «примерно столько же, сколько
вчера» здесь запрещены: по этим числам считается доход фермы.

ПОЧЕМУ ИМЕННО ЭТИ ИСТОЧНИКИ
  * BTC/USDT — Bybit. На этом проекте уже выяснено, что с нашего
    сервера OKX и cbr.ru не отвечают (рукопожатие виснет), а Bybit
    работает; бот живёт на GitHub, но источник взят тот же, чтобы
    числа сходились с теми, что показывает приложение.
  * USDT/RUB — CoinGecko. Официального курса ЦБ для USDT не бывает, а
    покупатель считает выручку в рублях по бирже.
  * Сложность и хешрейт сети — mempool.space, запасной blockchain.info.
    Два источника, потому что от сложности напрямую зависит хешпрайс.

ХЕШПРАЙС. Сколько рублей в сутки приносит один терахеш:

    монет_за_TH_в_сутки = 1e12 * 86400 * награда / (сложность * 2^32)
    хешпрайс = монет_за_TH_в_сутки * цена_BTC_usd * курс_usd_rub

Сверка из задания: 06.09.2026 хешпрайс был 3,42 ₽ за TH в сутки. При
сложности ~132,76e12, награде 3,125, BTC ~84 155 $ и долларе ~84,37 ₽
формула даёт ~3,36 ₽ — сходится с поправкой на то, что сложность и
курс с тех пор изменились. Если однажды разойдётся в РАЗЫ — значит
сменилась награда (халвинг) или источник отдаёт сложность в других
единицах; смотреть надо туда, а не в формулу.
"""

import json
import time
import urllib.error
import urllib.request

UA = {"User-Agent": "volok-news/1.0 (+https://github.com/danilov021086-dev/volok-news)"}
TIMEOUT = 25
REWARD_BTC = 3.125          # после халвинга 2024 года


def thin(pts, maxn=120):
    """Проредить ряд до maxn точек. Кабинет рисует линию шириной в
    несколько сотен пикселей — 721 точка там не видна, а вес файла
    растёт, и его тянут с телефона по мобильному интернету.
    Последнюю точку сохраняем всегда: это текущая цена."""
    if not pts or len(pts) <= maxn:
        return pts
    step = len(pts) / float(maxn)
    out = [pts[int(i * step)] for i in range(maxn)]
    if out[-1] != pts[-1]:
        out[-1] = pts[-1]
    return out


def _get(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _field(value, source, ok, err=None, prev_unix=None):
    """Одно число со своей биографией: значение, источник, когда снято,
    и — если не снялось — почему и когда получалось в последний раз."""
    out = {"значение": value, "источник": source,
           "снято_unix": int(time.time()) if ok else None}
    if not ok:
        out["ошибка"] = err
        out["последний_удачный_unix"] = prev_unix
    return out


def _bybit(interval, limit):
    u = ("https://api.bybit.com/v5/market/kline?category=spot"
         "&symbol=BTCUSDT&interval=%s&limit=%d" % (interval, limit))
    rows = (_get(u).get("result") or {}).get("list") or []
    if not rows:
        raise ValueError("Bybit вернул пустой список")
    # Bybit отдаёт от новых к старым — разворачиваем, чтобы график рисовался
    # слева направо, как читают люди.
    return [[int(r[0]) // 1000, float(r[4])] for r in rows][::-1]


def _coingecko_btc(days, tail=None):
    u = ("https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
         "?vs_currency=usd&days=%d" % days)
    pts = [[int(p[0]) // 1000, float(p[1])] for p in (_get(u).get("prices") or [])]
    if not pts:
        raise ValueError("CoinGecko вернул пустой ряд")
    return pts[-tail:] if tail else pts


def btc_series():
    """Ряды BTC/USDT за час, сутки и 30 дней. Свеча — [unix, цена].

    ДВА ИСТОЧНИКА, И ЭТО НЕ ПЕРЕСТРАХОВКА. Первый прогон на GitHub
    показал: Bybit отдаёт бегунку Actions **403 Forbidden** — биржи
    закрываются от дата-центров. С домашней машины тот же запрос
    проходит, поэтому в разработке это не видно вовсе.

    Порядок такой: сперва Bybit (тот же источник, что у приложения —
    значит числа на сайте и в телефоне сходятся), при отказе —
    CoinGecko. В ответе честно написано, КТО дал число: если однажды
    сайт и приложение разойдутся на десяток долларов, причина будет
    видна сразу, а не после часа поисков."""
    out, errs, src = {}, {}, {}
    plan = (("1h", "1", 60, 1, 60), ("24h", "15", 96, 1, None),
            ("30d", "D", 30, 30, None))
    for name, interval, limit, cg_days, cg_tail in plan:
        try:
            out[name] = thin(_bybit(interval, limit))
            src[name] = "Bybit"
        except Exception as e1:                                 # noqa: BLE001
            try:
                out[name] = thin(_coingecko_btc(cg_days, cg_tail))
                src[name] = "CoinGecko"
                errs[name] = "Bybit: %s — взято у CoinGecko" % e1
            except Exception as e2:                             # noqa: BLE001
                out[name] = None
                src[name] = None
                errs[name] = "Bybit: %s; CoinGecko: %s" % (e1, e2)
    return out, errs, src


def rub_series():
    """USDT/RUB за час, сутки и 30 дней. CoinGecko отдаёт ряд точками,
    шаг выбирает сам по длине окна — нам достаточно."""
    out, errs = {}, {}
    plan = (("1h", 1), ("24h", 1), ("30d", 30))
    for name, days in plan:
        try:
            u = ("https://api.coingecko.com/api/v3/coins/tether/market_chart"
                 "?vs_currency=rub&days=%d" % days)
            d = _get(u)
            pts = [[int(p[0]) // 1000, float(p[1])] for p in (d.get("prices") or [])]
            if name == "1h":
                pts = pts[-60:]
            out[name] = thin(pts)
        except Exception as e:                                  # noqa: BLE001
            out[name] = None
            errs[name] = str(e)
    return out, errs


# Высота блока нужна для двух чисел на сайте: сколько осталось до
# пересчёта сложности и сколько до халвинга. Оба считаются ПО ВЫСОТЕ, а
# не по календарю: сеть идёт своим темпом, и «через 5 дней» из макета —
# это снимок одного дня, а не постоянная величина.
BLOCKS_PER_DAY = 144.0          # 10 минут на блок — проектный темп сети
RETARGET = 2016                 # блоков между пересчётами сложности
HALVING_EVERY = 210000          # блоков между халвингами


def height():
    """Высота последнего блока. Два источника, как у сложности."""
    try:
        return int(_get_text("https://mempool.space/api/blocks/tip/height")), "mempool.space"
    except Exception:                                           # noqa: BLE001
        try:
            return int(_get_text("https://blockchain.info/q/getblockcount")), "blockchain.info"
        except Exception:                                       # noqa: BLE001
            return None, None


def _get_text(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8").strip()


def network():
    """Сложность и хешрейт сети. Два источника: второй — страховка."""
    try:
        d = _get("https://mempool.space/api/v1/mining/hashrate/3d")
        diff = float(d.get("currentDifficulty"))
        hr = float(d.get("currentHashrate"))     # H/s
        return diff, hr, "mempool.space", None
    except Exception as e1:                                     # noqa: BLE001
        try:
            diff = float(_get("https://blockchain.info/q/getdifficulty"))
            hr = float(_get("https://blockchain.info/q/hashrate")) * 1e9
            return diff, hr, "blockchain.info", None
        except Exception as e2:                                 # noqa: BLE001
            return None, None, None, "mempool: %s; blockchain: %s" % (e1, e2)


def build(prev=None):
    """Собрать блок market. prev — прошлый блок: из него берём время
    последнего удачного снятия для полей, которые сейчас не дались."""
    prev = prev or {}

    def prev_ok(path):
        try:
            node = prev
            for k in path:
                node = node[k]
            return node.get("снято_unix") or node.get("последний_удачный_unix")
        except Exception:                                       # noqa: BLE001
            return None

    btc, btc_err, btc_src = btc_series()
    rub, rub_err = rub_series()
    diff, nethash, netsrc, neterr = network()
    h, hsrc = height()

    # Текущая цена — последняя точка суточного ряда.
    btc_now = btc["24h"][-1][1] if btc.get("24h") else None
    rub_now = rub["24h"][-1][1] if rub.get("24h") else None

    hashprice = None
    hp_note = None
    if btc_now and rub_now and diff:
        coins = 1e12 * 86400.0 * REWARD_BTC / (diff * 4294967296.0)
        hashprice = round(coins * btc_now * rub_now, 4)
    else:
        missing = [n for n, v in (("цена BTC", btc_now), ("курс доллара", rub_now),
                                  ("сложность", diff)) if not v]
        hp_note = "не хватает: " + ", ".join(missing)

    return {
        "btc_usdt": {
            "текущая": _field(btc_now, btc_src.get("24h") or "Bybit",
                              btc_now is not None, btc_err.get("24h"),
                              prev_ok(("btc_usdt", "текущая"))),
            "источники_рядов": btc_src,
            "ряды": {"1ч": btc.get("1h"), "24ч": btc.get("24h"), "30д": btc.get("30d")},
            "ошибки": btc_err or None,
        },
        "usdt_rub": {
            "текущая": _field(rub_now, "CoinGecko", rub_now is not None,
                              rub_err.get("24h"), prev_ok(("usdt_rub", "текущая"))),
            "ряды": {"1ч": rub.get("1h"), "24ч": rub.get("24h"), "30д": rub.get("30d")},
            "ошибки": rub_err or None,
        },
        "сеть": {
            "сложность": _field(diff, netsrc or "—", diff is not None, neterr,
                                prev_ok(("сеть", "сложность"))),
            "хешрейт_hs": _field(nethash, netsrc or "—", nethash is not None, neterr,
                                 prev_ok(("сеть", "хешрейт_hs"))),
            "награда_btc": REWARD_BTC,
        },
        "блок": {
            "высота": _field(h, hsrc or "—", h is not None,
                             "оба источника молчат" if h is None else None,
                             prev_ok(("блок", "высота"))),
            # До пересчёта сложности: сколько блоков осталось до конца
            # текущего окна в 2016 блоков, и сколько это суток при
            # проектных 144 блоках в сутки.
            "до_пересчёта_блоков": (RETARGET - (h % RETARGET)) if h else None,
            "до_пересчёта_суток": round((RETARGET - (h % RETARGET)) / BLOCKS_PER_DAY, 1) if h else None,
            # До халвинга: то же, но до конца окна в 210 000 блоков.
            "до_халвинга_блоков": (HALVING_EVERY - (h % HALVING_EVERY)) if h else None,
            "до_халвинга_суток": round((HALVING_EVERY - (h % HALVING_EVERY)) / BLOCKS_PER_DAY) if h else None,
        },
        "хешпрайс_руб_за_th_в_сутки": _field(
            hashprice, "расчёт из сложности, цены BTC и курса доллара",
            hashprice is not None, hp_note,
            prev_ok(("хешпрайс_руб_за_th_в_сутки",))),
    }
