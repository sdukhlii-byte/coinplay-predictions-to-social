"""Парсер подписи поста "COINPLAY AI PREDICTIONS", которую коллега уже
постит в Telegram-каналы (@coinplayfootballai / @coinplayesportai /
@coinplayufcai) — альтернатива coinplay_sets.py (sets.json) на случай, когда
токена к API нет (так и есть сейчас): Stan сам скрейпит эти каналы и отдаёт
нам готовый текст поста, а не сырой JSON.

До начала матча в тексте есть: кто фаворит, какой счёт/победитель чаще всего
выбирают модели, насколько они согласны (N из M), confidence и risk/danger.
ЧЕГО В ТЕКСТЕ НЕТ — построчной разбивки по каждой из 10 моделей (кто из них
как проголосовал): она есть только в картинке-постере, которую мы не
распознаём. Явное решение (см. обсуждение со Stan): не гадать и не рисовать
выдуманные построчные данные, а показывать на видео агрегат — один ряд
"COINPLAY AI PANEL" с итоговым счётом/победителем, и весь остальной разбор
(N из M, confidence, risk) — в плашке AI CONSENSUS, которая на постере уже
есть.

Поддерживает оба варианта текста, которые реально встречаются в канале:
  * football/ufc:
      "🎯 Primary call: X [счёт]"
      "✅ Winner: X"
      "📊 Panel: N of M lean X"
      "Mean model confidence: X a% · Y b% [· Draw c%]"
  * esports:
      "🎯 N of M models pick X"
      "Most common score among those picks: A-B"
      "Average model confidence: X a% · Y b%"
      "Coinplay AI, combined model estimate: X c%"
  * оба варианта:
      "🔮 Confidence: Low/Medium/High"
      "⚠️ Danger: ..." (football/ufc) или "⚠️ Risk: ..." (esports)

Если реальный формат поста чуть отличается (другая пунктуация, порядок
строк) — большинство полей необязательны: parse_caption() бросает
CaptionParseError, только если не нашла ни команд ("A vs B"), ни победителя.
"""

from __future__ import annotations

import re

VERTICALS = ("esports", "football", "ufc")

_VS_RE = re.compile(r"^(?P<home>.+?)\s+vs\.?\s+(?P<away>.+?)\s*$", re.IGNORECASE)
_SCORE_RE = re.compile(r"(\d+)\s*[-–]\s*(\d+)")
_PRIMARY_CALL_RE = re.compile(
    r"Primary call:\s*(?P<name>.+?)(?:\s+(?P<score>\d+\s*[-–]\s*\d+))?\s*$",
    re.IGNORECASE | re.MULTILINE)
_WINNER_RE = re.compile(r"Winner:\s*(?P<name>.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_PANEL_RE = re.compile(r"Panel:\s*(\d+)\s*of\s*(\d+)\s*lean\s*(.+?)\s*$",
                       re.IGNORECASE | re.MULTILINE)
_MODELS_PICK_RE = re.compile(r"(\d+)\s*of\s*(\d+)\s*(?:AI\s+)?models?\s*pick\s*(.+?)\s*$",
                             re.IGNORECASE | re.MULTILINE)
_MOST_COMMON_SCORE_RE = re.compile(r"Most common score.*?:\s*(\d+)\s*[-–]\s*(\d+)",
                                   re.IGNORECASE)
_MEAN_CONF_RE = re.compile(r"(?:Mean|Average) model confidence:\s*(.+?)\s*$",
                           re.IGNORECASE | re.MULTILINE)
_COINPLAY_AI_RE = re.compile(
    r"Coinplay AI,?\s*combined model estimate:\s*.+?\s+(\d+)%", re.IGNORECASE)
# Привязано к НАЧАЛУ строки (после необязательного эмодзи), иначе матчится
# подстрока "confidence:" из "Mean model confidence: Bayer Leverkusen 57%..."
# и "Confidence" ошибочно становится названием команды.
_CONFIDENCE_RE = re.compile(r"^[^\w\n]*Confidence:\s*(\w+)", re.IGNORECASE | re.MULTILINE)
_RISK_RE = re.compile(r"(?:Danger|Risk):\s*(\w+)", re.IGNORECASE)
_PCT_PART_RE = re.compile(r"(.+?)\s+(\d+)%")


class CaptionParseError(ValueError):
    pass


def _parse_pct_list(segment: str) -> dict[str, int]:
    """"Bayer Leverkusen 57% · RB Leipzig 22% · Draw 21%" ->
    {"Bayer Leverkusen": 57, "RB Leipzig": 22, "Draw": 21}"""
    out: dict[str, int] = {}
    for part in segment.split("·"):
        m = _PCT_PART_RE.match(part.strip())
        if m:
            out[m.group(1).strip()] = int(m.group(2))
    return out


def parse_caption(text: str, vertical: str) -> dict:
    """Текст поста -> словарь с разобранными полями.

    Бросает CaptionParseError, если не нашла ни строку "A vs B", ни
    победителя — без них рисовать нечего. Остальные поля опциональны.
    """
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        raise CaptionParseError("пустой текст")

    vs_line = next((ln for ln in lines if _VS_RE.match(ln)), None)
    if vs_line is None:
        raise CaptionParseError('не нашёл строку "A vs B" в тексте поста')
    vm = _VS_RE.match(vs_line)
    home, away = vm.group("home").strip(), vm.group("away").strip()

    idx = lines.index(vs_line)
    competition = lines[idx + 1] if idx + 1 < len(lines) else ""

    joined = "\n".join(lines)

    winner = None
    score: tuple[int, int] | None = None
    panel_agree = panel_total = 0

    m = _PRIMARY_CALL_RE.search(joined)
    if m:
        winner = m.group("name").strip()
        if m.group("score"):
            sm = _SCORE_RE.search(m.group("score"))
            if sm:
                score = (int(sm.group(1)), int(sm.group(2)))

    wm = _WINNER_RE.search(joined)
    if wm:
        winner = wm.group("name").strip()

    pm = _PANEL_RE.search(joined)
    if pm:
        panel_agree, panel_total = int(pm.group(1)), int(pm.group(2))
        winner = winner or pm.group(3).strip()
    else:
        mp = _MODELS_PICK_RE.search(joined)
        if mp:
            panel_agree, panel_total = int(mp.group(1)), int(mp.group(2))
            winner = winner or mp.group(3).strip()

    if score is None:
        sm2 = _MOST_COMMON_SCORE_RE.search(joined)
        if sm2:
            score = (int(sm2.group(1)), int(sm2.group(2)))

    if not winner:
        raise CaptionParseError(f"не нашёл победителя в посте {home!r} vs {away!r}")

    conf_by_name: dict[str, int] = {}
    cm = _MEAN_CONF_RE.search(joined)
    if cm:
        conf_by_name = _parse_pct_list(cm.group(1))

    ai_pct = None
    am = _COINPLAY_AI_RE.search(joined)
    if am:
        ai_pct = int(am.group(1))

    confidence_label = None
    cfm = _CONFIDENCE_RE.search(joined)
    if cfm:
        confidence_label = cfm.group(1).strip()

    risk_label = None
    rm = _RISK_RE.search(joined)
    if rm:
        risk_label = rm.group(1).strip()

    return {
        "home": home, "away": away, "competition": competition,
        "winner": winner.strip(), "score": score,
        "panel_agree": panel_agree, "panel_total": panel_total,
        "confidence_pct_by_name": conf_by_name, "ai_pct": ai_pct,
        "confidence_label": confidence_label, "risk_label": risk_label,
        "vertical": vertical,
    }


def match_from_caption(text: str, vertical: str, match_id: str = "") -> dict:
    """parse_caption() -> словарь match, который понимает generate.run().

    Одна агрегированная строка в таблице вместо 10 построчных моделей (см.
    docstring модуля — этих данных просто нет в тексте поста).
    """
    p = parse_caption(text, vertical)
    home, away, winner = p["home"], p["away"], p["winner"]

    is_home = winner.strip().lower() == home.strip().lower()
    is_away = winner.strip().lower() == away.strip().lower()
    if p["score"] is not None:
        h, a = p["score"]
    elif is_home or is_away:
        # Как и в coinplay_sets.py для UFC: счёта нет — честно кодируем
        # только победителя (1 ему, 0 проигравшему), а не выдумываем счёт.
        h, a = (1, 0) if is_home else (0, 1)
    else:
        h, a = 0, 0  # ничья или не распознали сторону — показать нечего

    row = {"label": "COINPLAY AI PANEL", "icon": "ai", "model": "coinplay-ai-panel",
           "home": h, "away": a, "reason": ""}

    total = p["panel_total"] or len(p["confidence_pct_by_name"]) or 0
    agree = p["panel_agree"] or total
    note_bits = []
    if total:
        note_bits.append(f"{agree} of {total} models agree")
    if p["risk_label"]:
        note_bits.append(f"{p['risk_label']} risk")
    consensus_note = " · ".join(note_bits)

    pct = p["ai_pct"]
    if pct is None:
        pct = p["confidence_pct_by_name"].get(winner)
    if pct is None and total:
        pct = round(100 * agree / total)
    pct = pct or 0

    subtitle = f"{agree} OF {total} MODELS AGREE" if total else "COINPLAY AI PANEL"

    return {
        "id": f"coinplay-caption-{match_id}" if match_id else "",
        "home": home, "away": away, "home_flag": "", "away_flag": "",
        "competition": p["competition"], "date": "",
        "vertical": vertical,
        "rows": [row],
        "subtitle": subtitle,
        "consensus": winner.upper(), "consensus_note": consensus_note,
        "consensus_pct": pct,
    }


def match_from_file(path: str, vertical: str, match_id: str = "") -> dict:
    """Читает текст поста из файла (Stan копирует/сохраняет его сам из
    своего Telegram-скрейпера) и строит match тем же способом."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    return match_from_caption(text, vertical, match_id=match_id)
