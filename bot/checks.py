# -*- coding: utf-8 -*-
"""checks.py — проверки текста «Коротко» и разбора (задание 30.09, этап 1).

ОДИН источник правды для входной и выходной проверки. КОПИЯ этой логики
живёт в gen_novosti.py на сервере (там нет доступа к этому пакету) —
меняешь здесь, синхронизируй там (внизу файла помечено).

Проверки:
  article_ok(text, title)  — годится ли ТЕКСТ СТАТЬИ для пересказа (1.6):
      ≥80 слов, ≥70% букв, ≥2 значимых слова заголовка, не заглушка.
  korotko_ok(text, title, pub_unix) — годится ли ОТВЕТ модели (1.7, 1.8):
      нет обращений/извинений/«как ИИ»/вопроса, 15–90 слов, есть слово
      заголовка, нет будущей даты в прошедшем времени.
  razbor_ok(text, title)   — разбор ИИ (1.11): те же запреты, длина 8–60.
  copied(a, b, n=8)        — цепочка n+ слов подряд из оригинала.
"""
import re
import time

# --- обращения к читателю, извинения, самоупоминания модели, мусор ------
REFUSAL = [
    "извин", "пришлите", "пожалуйста", "к сожалению", "не могу", "не удал",
    "как ии", "как языковая модель", "языковая модель", "предоставленный текст",
    "предоставьте", "текст статьи", "нечита", "поврежд", "расшифров",
    "набор случайных", "случайных символов", "не поддаётся", "невозможно переска",
    "исправный текст", "в читаемом", "в нормальном виде", "корректный текст",
    "enable javascript", "captcha", "доступ ограничен",
]
# самоупоминание от первого лица — отдельным словом
FIRSTPERSON = re.compile(r"(?:^|[^а-яё])(я|мне|меня|мной)(?:[^а-яё]|$)", re.I)

STOP = set("""и в во не что он на я с со как а то все она так его но да ты к у же вы за
бы по только ее мне было вот от меня еще нет о из ему теперь когда даже ну вдруг ли если
уже или ни быть был него до вас нибудь опять уж вам ведь там потом себя ничего ей может они
тут где есть надо ней для мы тебя их чем была сам чтоб без будто чего раз тоже себе под будет
же тогда кто этот того потому этого какой совсем ним здесь этом один почти мой тем чтобы нее
сейчас были куда зачем всех никогда можно при наконец два об другой хоть после над больше тот
через эти нас про них какая много разве три эту моя впрочем хорошо свою этой перед иногда лучше
чуть том нельзя такой им более всегда конечно всю между это его её они оно все всё""".split())

MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "мая": 5, "май": 5,
          "мае": 5, "июн": 6, "июл": 7, "август": 8, "сентябр": 9, "октябр": 10,
          "ноябр": 11, "декабр": 12}
# ГЛАГОЛЫ СВЕРШИВШЕГОСЯ СОБЫТИЯ (прошедшее время). Узкий список нарочно:
# широкое «любое слово на -л» ложно снимало хорошие пересказы вроде
# «вступают в силу» (там нет этих глаголов) — 30.09 поймано на ЦБ.
# Проверка идёт ПО ПРЕДЛОЖЕНИЮ: событие-в-прошедшем + будущая дата в одном
# предложении = ошибка «будущее подано прошедшим» (пример: «запустили
# тестирование … 6 октября»). Настоящее/будущее время («запустят»,
# «вступают», «выйдет») сюда не попадает.
PAST_EVENT = re.compile(
    r"\b(запусти|вступи|вышел|вышла|вышло|вышли|прош[ёе]л|прошла|прошло|прошли|"
    r"состоял|начал|заверши|подписа|принял|приня|утверди|ввёл|ввел|ввели|"
    r"выпусти|откры|закры|провёл|провел|провели|получил)\w*", re.I)


def _words(s):
    return re.findall(r"[а-яёa-z0-9]+", (s or "").lower())


def _sig(word):
    return len(word) > 3 and word not in STOP


def _title_stems(title):
    return [w[:5] for w in _words(title) if _sig(w)]


def _has_title_word(text, title):
    tw = set(_words(text))
    for st in _title_stems(title):
        if any(w.startswith(st) for w in tw):
            return True
    return False


def article_ok(text, title):
    """Годится ли текст статьи для пересказа (1.6). (ok, причина)."""
    if not text:
        return False, "пусто"
    ws = _words(text)
    if len(ws) < 80:
        return False, "меньше 80 слов (%d)" % len(ws)
    nonsp = [c for c in text if not c.isspace()]
    if nonsp:
        letters = sum(1 for c in nonsp if c.isalpha())
        if letters / len(nonsp) < 0.70:
            return False, "букв меньше 70%%"
    low = text.lower()
    for m in ("enable javascript", "captcha", "доступ ограничен",
              "включите javascript", "проверка браузера"):
        if m in low:
            return False, "похоже на заглушку (%s)" % m
    if not _has_title_word(text, title):
        return False, "нет слов заголовка в тексте"
    # хотя бы 2 значимых слова заголовка
    tw = set(_words(text))
    hit = sum(1 for st in _title_stems(title)
              if any(w.startswith(st) for w in tw))
    if hit < 2:
        return False, "меньше 2 слов заголовка"
    return True, ""


def _sentences(text):
    return re.split(r"(?<=[.!?])\s+", text or "")


def _has_future_date(sent, pub_unix):
    year = time.gmtime(int(pub_unix)).tm_year
    for m in re.finditer(r"(\d{1,2})\s+([а-яё]+)", sent.lower()):
        day = int(m.group(1))
        mon = next((num for stem, num in MONTHS.items()
                    if m.group(2).startswith(stem)), None)
        if not mon or day < 1 or day > 31:
            continue
        try:
            when = time.mktime((year, mon, day, 12, 0, 0, 0, 0, -1))
        except Exception:
            continue
        if when > int(pub_unix) + 86400:
            return True
    return False


def _future_past(text, pub_unix):
    """True, если в ОДНОМ предложении есть и глагол свершившегося события
    (прошедшее), и дата ПОЗЖЕ публикации. Это ошибка «будущее подано
    прошедшим». Настоящее/будущее время не ловится (это норма)."""
    if not pub_unix:
        return False
    for s in _sentences(text):
        if PAST_EVENT.search(s) and _has_future_date(s, pub_unix):
            return True
    return False


def _bad_phrases(text, extra_ok=False):
    low = text.lower()
    for p in REFUSAL:
        if p in low:
            return "обращение/отказ: %s" % p
    if FIRSTPERSON.search(text):
        return "от первого лица (я/мне)"
    if "?" in text:
        return "вопрос в тексте"
    return None


def korotko_ok(text, title, pub_unix=None):
    """Годится ли «Коротко» (1.7, 1.8). Возвращает (ok, причина)."""
    if not text or not text.strip():
        return False, "пусто"
    bad = _bad_phrases(text)
    if bad:
        return False, bad
    n = len(_words(text))
    if n < 15:
        return False, "короче 15 слов (%d)" % n
    if n > 90:
        return False, "длиннее 90 слов (%d)" % n
    if not _has_title_word(text, title):
        return False, "нет ни одного слова заголовка"
    if _future_past(text, pub_unix):
        return False, "будущая дата в прошедшем времени"
    return True, ""


def razbor_ok(text, title=None, pub_unix=None):
    """Разбор ИИ (1.11): те же запреты, длина 8–60 слов."""
    if not text or not text.strip():
        return False, "пусто"
    bad = _bad_phrases(text)
    if bad:
        return False, bad
    n = len(_words(text))
    if n < 8:
        return False, "короче 8 слов (%d)" % n
    if n > 60:
        return False, "длиннее 60 слов (%d)" % n
    if _future_past(text, pub_unix):
        return False, "будущая дата в прошедшем времени"
    return True, ""


def copied(peresk, original, n=8):
    pw = _words(peresk)
    ow = _words(original)
    if len(pw) < n:
        return False
    oset = {tuple(ow[i:i + n]) for i in range(len(ow) - n + 1)}
    for i in range(len(pw) - n + 1):
        if tuple(pw[i:i + n]) in oset:
            return True
    return False
