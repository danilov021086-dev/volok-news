#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest_ai.py — самопроверка разбора БЕЗ обращения к площадке.

ЗАЧЕМ. Главная защита ленты — не модель, а проверка её ответа. Если
проверка однажды ослабнет, в ленте появятся выдуманные оценки, и
заметить это будет некому. Поэтому проверка проверяется отдельно и
без ключа: подсовываем заведомо негодные ответы и смотрим, что каждый
отвергнут, и отвергнут ПО ТОЙ ПРИЧИНЕ, по которой должен.

Запуск: python bot/selftest_ai.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ai as ai_mod                                             # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

LONG = "а" * 141

CASES = [
    # (что прислала модель, ждём ли годный ответ, кусок ожидаемой причины)
    ('{"impact":"plus","text":"Цена выше себестоимости — маржа растёт."}',
     True, None),
    # Рассуждающая модель написала мысли вокруг JSON — это нормально,
    # объект должен быть вырезан.
    ('Подумаем. Новость про запрет.\n'
     '{"impact":"risk","text":"Возможны проверки ферм — держите документы."}\n'
     'Надеюсь, помог.',
     True, None),
    # Обёртка в ```json — частая привычка открытых моделей.
    ('```json\n{"impact":"neutral","text":"На доход фермы не влияет."}\n```',
     True, None),
    # --- дальше заведомый брак ---
    ("Думаю, это нейтральная новость для майнеров.",
     False, "нет JSON"),
    ('{"impact":"положительно","text":"Хорошо для майнеров."}',
     False, "неизвестный impact"),
    ('{"impact":"plus","text":""}',
     False, "пустой text"),
    ('{"impact":"plus","text":"%s"}' % LONG,
     False, "длиннее"),
    ('{"impact":"plus","text":"Хороший момент, стоит купить биткоин."}',
     False, "совет"),
    ("", False, "пустой ответ"),
    ("{сломанный json", False, "нет JSON"),
]


def main():
    bad = 0
    print("САМОПРОВЕРКА РАЗБОРА (без обращения к площадке)\n")
    for raw, want_ok, why_part in CASES:
        ai, why = ai_mod.parse(raw)
        got_ok = ai is not None
        ok = (got_ok == want_ok) and (want_ok or (why_part in (why or "")))
        if not ok:
            bad += 1
        print("  %-5s %-46s -> %s" % (
            "OK" if ok else "ПЛОХО",
            (raw[:44].replace("\n", " ") or "(пусто)"),
            ("годен: %s / %s" % (ai["impact"], ai["text"][:34])) if got_ok
            else ("отказ: %s" % why)))

    print("\nвсего случаев: %d, плохих: %d" % (len(CASES), bad))
    ok_n = sum(1 for c in CASES if c[1])
    print("годных ответов принято: %d, брака отвергнуто: %d"
          % (ok_n, len(CASES) - ok_n))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
