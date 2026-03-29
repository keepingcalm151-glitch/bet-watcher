# watcher.py
#
# Основной воркер для Railway:
#   Procfile: worker: python watcher.py
# Использует:
#   - requests
#   - beautifulsoup4
#
# Логика:
#   - берём профили tipster'ов на bettingexpert из config["sites"]
#   - парсим активные ставки
#   - по каждой ставке заходим на страницу tip'а и парсим время матча
#   - запоминаем матчи до 7 дней вперёд
#   - если до матча ~час и есть >=2 разных авторов с одной ставкой,
#     шлём уведомление в Telegram

import json
import os
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import time
import re

# Часовые пояса
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
BETTINGEXPERT_TZ = ZoneInfo("Europe/Copenhagen")  # время на сайте bettingexpert

# Для парсинга дат вида "2 Apr 10:35"
MONTHS_EN = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

CONFIG_PATH = "config.json"
STATE_PATH = "state.json"
# ===== 1. Загрузка конфигурации =====

if os.getenv("CONFIG_JSON"):
    # В проде (Railway) можно положить весь config.json в переменную среды
    config = json.loads(os.getenv("CONFIG_JSON"))
else:
    # Локально берём из файла config.json
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

OPENAI_API_KEY = config.get("openai_api_key")       # сейчас не используем, но оставляем
TELEGRAM_BOT_TOKEN = config["telegram_bot_token"]
TELEGRAM_CHAT_ID = config["telegram_chat_id"]
THESPORTSDB_KEY = config.get("thesportsdb_key")     # опционально, оставим на будущее
SITES = config["sites"]                             # профили bettingexpert
CHECK_INTERVAL_MINUTES = config.get("check_interval_minutes", 59)


# ===== 2. Работа с локальным состоянием (state.json) =====

def load_state() -> dict:
    """
    Загружаем state.json (память о матчах / уведомлениях).
    Если файла нет или он битый – возвращаем пустой dict.
    """
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}


def save_state(state: dict) -> None:
    """
    Сохраняем state.json.
    """
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
# ===== 3. Утилиты сети и Telegram =====

def fetch_page(url: str) -> str:
    """
    Скачиваем HTML страницы с нормальным User-Agent'ом.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0 Safari/537.36"
        )
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.text


def send_telegram_message(text: str) -> None:
    """
    Отправка сообщения в Telegram (в твой чат).
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
# ===== 4. Короткая запись ставки (П1, П2, Ф1, ТБ и т.д.) =====

def short_selection(selection: str, match: str | None = None) -> str:
    """
    Делает короткую запись ставки из длинного текста selection.
    Умеет:
      - "Over 167.5 points" -> "ТБ 167.5"
      - "Under 16.5 games" -> "ТМ 16.5"
      - "<Team> to win  Draw No Bet" -> "ДНБ <Team>"
      - "<Team> to win" -> "П1"/"П2" (если можем сопоставить с хозяевами/гостями)
      - "<Team> -1.00 (AH)" -> "Ф1(-1.0)" / "Ф2(-1.0)"
      - "+0.50 (AH)" -> "AH +0.50"
    Если не распознали — возвращаем исходный selection.
    """
    if not selection:
        return ""

    text = selection.strip()

    home_team = away_team = None
    if match and " vs " in match:
        home_team, away_team = [p.strip() for p in match.split(" vs ", 1)]

    # Over / Under тотал
    m_over = re.search(r"\bOver\s+(\d+(\.\d+)?)", text, re.IGNORECASE)
    if m_over:
        val = m_over.group(1).replace(",", ".")
        return f"ТБ {val}"

    m_under = re.search(r"\bUnder\s+(\d+(\.\d+)?)", text, re.IGNORECASE)
    if m_under:
        val = m_under.group(1).replace(",", ".")
        return f"ТМ {val}"

    # Draw No Bet
    if re.search(r"Draw\s+No\s+Bet", text, re.IGNORECASE):
        m_team = re.match(r"(.+?)\s+to\s+win", text, re.IGNORECASE)
        team = m_team.group(1).strip() if m_team else text.split("Draw")[0].strip()
        return f"ДНБ {team}"

    # Победа "<Team> to win"
    m_win = re.match(r"(.+?)\s+to\s+win\b", text, re.IGNORECASE)
    if m_win:
        team = m_win.group(1).strip()
        if home_team and team.lower() in home_team.lower():
            return "П1"
        if away_team and team.lower() in away_team.lower():
            return "П2"
        return f"Победа {team}"

    # AH "<Team> +0.50 (AH)"
    m_ah_full = re.match(r"(.+?)\s+([+-]?\d+(\.\d+)?)\s*\(AH\)", text, re.IGNORECASE)
    if m_ah_full:
        team = m_ah_full.group(1).strip()
        val = m_ah_full.group(2).replace(",", ".")
        if home_team and team.lower() in home_team.lower():
            return f"Ф1({val})"
        if away_team and team.lower() in away_team.lower():
            return f"Ф2({val})"
        return f"AH {val}"

    # AH без команды "+0.50 (AH)"
    m_ah = re.search(r"([+-]?\d+(\.\d+)?)\s*\(AH\)", text, re.IGNORECASE)
    if m_ah:
        val = m_ah.group(1).replace(",", ".")
        return f"AH {val}"

    # Если ничего не распознали — возвращаем исходный текст
    return text
# ===== 5. Парсинг активных ставок с профиля bettingexpert =====

def parse_bettingexpert_tips(html: str, profile_name: str, profile_url: str) -> list[dict]:
    """
    Парсер блока активных tips на bettingexpert‑профиле.

    Возвращает список словарей:
    [
      {
        "match": "Macarthur FC vs Newcastle Jets",
        "home_team": "Macarthur FC",
        "away_team": "Newcastle Jets",
        "sport": "football",
        "selection": "Macarthur FC +0.50 (AH)",
        "odds": 1.93,
        "author": "Dyole",
        "tip_url": "https://www.bettingexpert.com/football/macarthur-fc-vs-newcastle-jets",
        "result": result,
      },
      ...
    ]
    """
    soup = BeautifulSoup(html, "html.parser")

    tips: list[dict] = []

    # Карточки tip'ов – div с классами cursor-pointer и bg-white и ссылкой внутри
    tip_blocks = soup.find_all(
        lambda tag: tag.name == "div"
        and "cursor-pointer" in tag.get("class", [])
        and "bg-white" in tag.get("class", [])
        and tag.find("a", href=True)
    )

    for block in tip_blocks:
        link_tag = block.find("a", href=True)
        if not link_tag:
            continue

        href = link_tag["href"]  # например: "football/macarthur-fc-vs-newcastle-jets"

        # Полный URL tip'а
        if href.startswith("http"):
            tip_url = href
        else:
            tip_url = "https://www.bettingexpert.com/" + href.lstrip("/")

        # sport – первая часть пути
        sport = None
        if "/" in href:
            sport = href.split("/", 1)[0].lower()

        # Название матча: "Macarthur FC-Newcastle Jets"
        match_span = link_tag.find("span")
        if not match_span:
            continue

        match_raw = match_span.get_text(strip=True)
        if "-" in match_raw:
            home_raw, away_raw = match_raw.split("-", 1)
            home_team = home_raw.strip()
            away_team = away_raw.strip()
            match_name = f"{home_team} vs {away_team}"
        else:
            home_team = None
            away_team = None
            match_name = match_raw

        # selection – синий жирный текст после флага
        selection_span = block.find(
            lambda tag: tag.name == "span"
            and "font-ms" in tag.get("class", [])
            and "font-bold" in tag.get("class", [])
            and "text-blue-felix" in tag.get("class", [])
        )
        if not selection_span:
            continue
        selection = selection_span.get_text(strip=True)

        # odds – из блока с текстом "odds ... 1.93"
        odds_span = block.find(
            lambda tag: tag.name == "span"
            and "uppercase" in tag.get("class", [])
            and "odds" in tag.get_text(strip=True).lower()
        )
        odds = None
        if odds_span:
            tokens = odds_span.get_text(" ", strip=True).replace(",", ".").split()
            for token in reversed(tokens):
                try:
                    val = float(token)
                    if 1.01 <= val <= 100.0:
                        odds = val
                        break
                except ValueError:
                    continue
                    
        # Результат ставки (для завершённых ставок, с серым фоном и ярлыком Won/Lost/Void)
        result = None

        result_badge = block.find(
            lambda tag: tag.name == "div"
            and "w-[36px]" in tag.get("class", [])
            and "font-gc" in tag.get("class", [])
        )
        if result_badge:
            span = result_badge.find("span")
            if span:
                text = span.get_text(strip=True).lower()
                if text == "won":
                    result = "won"
                elif text == "lost":
                    result = "lost"
                elif text in ("void", "returned", "refunded", "push"):
                    result = "void"
                else:
                    result = text

        author = profile_name

        tips.append(
            {
                "match": match_name,
                "home_team": home_team,
                "away_team": away_team,
                "sport": sport,
                "selection": selection,
                "odds": odds,
                "author": author,
                "tip_url": tip_url,
                "result": result,  # <-- добавили результат
            }
        )

    return tips
# ===== 6. Время начала матча: страница tip'а bettingexpert =====

def parse_kickoff_from_tip_html(html: str) -> datetime | None:
    """
    Пытается вытащить из HTML страницы tip'а дату и время матча.
    Ищем паттерны вроде "2 Apr 10:35" или "02 April 18:00".
    Возвращаем datetime с таймзоной BETTINGEXPERT_TZ.
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    # Пример: "2 Apr 10:35" / "02 April 10:35"
    pattern = r"\b(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{1,2}):(\d{2})\b"
    match = re.search(pattern, text)
    if not match:
        # fallback: есть только время HH:MM – считаем, что матч сегодня
        m_time = re.search(r"\b(\d{1,2}):(\d{2})\b", text)
        if not m_time:
            return None
        hour = int(m_time.group(1))
        minute = int(m_time.group(2))
        now = datetime.now(BETTINGEXPERT_TZ)
        try:
            return datetime(
                now.year, now.month, now.day, hour, minute, tzinfo=BETTINGEXPERT_TZ
            )
        except ValueError:
            return None

    day_str, month_str, hour_str, minute_str = match.groups()
    day = int(day_str)
    month = MONTHS_EN.get(month_str.lower())
    if not month:
        return None

    hour = int(hour_str)
    minute = int(minute_str)

    now = datetime.now(BETTINGEXPERT_TZ)
    year = now.year

    try:
        dt_local = datetime(year, month, day, hour, minute, tzinfo=BETTINGEXPERT_TZ)
    except ValueError:
        return None

    # Если дата ушла далеко в прошлое – пробуем следующий год (редкий кейс)
    if dt_local < now - timedelta(days=7):
        try:
            dt_local = dt_local.replace(year=year + 1)
        except ValueError:
            pass

    return dt_local


def get_kickoff_time_from_bettingexpert_tip(tip_url: str) -> datetime | None:
    """
    Скачивает страницу tip'а bettingexpert и возвращает время начала матча в UTC.
    """
    try:
        tip_html = fetch_page(tip_url)
    except Exception as e:
        print(f"[ERROR] Не удалось скачать tip {tip_url}: {e}")
        return None

    dt_local = parse_kickoff_from_tip_html(tip_html)
    if not dt_local:
        print(f"[WARN] Не удалось распарсить время начала матча из {tip_url}")
        return None

    # Переводим в UTC
    if dt_local.tzinfo is None:
        dt_local = dt_local.replace(tzinfo=BETTINGEXPERT_TZ)
    return dt_local.astimezone(timezone.utc)
# ===== 7. Память по будущим матчам (upcoming_matches) =====

def update_upcoming_matches(state: dict, tips: list[dict]) -> None:
    """
    Обновляет state["upcoming_matches"] по новым прогнозам.
    Держим матчи до 7 дней вперёд.

    Ключ матча: "<match>|||<selection>"
    (т.е. конкретный матч + конкретный выбор, независимо от коеффа).
    """
    upcoming = state.setdefault("upcoming_matches", {})
    now_utc = datetime.now(timezone.utc)
    max_ahead = timedelta(days=7)

    for tip in tips:
        match = tip.get("match")
        selection = tip.get("selection")
        author = tip.get("author")
        kickoff = tip.get("kickoff_time_utc")
        odds = tip.get("odds")

        if not match or not selection or not author or not kickoff:
            continue

        try:
            kickoff_dt = datetime.fromisoformat(kickoff)
        except Exception:
            continue

        if kickoff_dt.tzinfo is None:
            kickoff_dt = kickoff_dt.replace(tzinfo=timezone.utc)

        delta = kickoff_dt - now_utc
        if not (timedelta(0) <= delta <= max_ahead):
            # слишком далеко или уже прошло
            continue

        key = f"{match}|||{selection}"

        if key not in upcoming:
            upcoming[key] = {
                "match": match,
                "selection": selection,
                "kickoff": kickoff,   # ISO‑строка в UTC
                "authors": [],
                "odds_list": [],
                "notified": False,
            }

        if author not in upcoming[key]["authors"]:
            upcoming[key]["authors"].append(author)
        if isinstance(odds, (int, float)):
            upcoming[key]["odds_list"].append(odds)
            
def update_author_bets(state: dict, tips: list[dict]) -> None:
    """
    Обновляет state["bets_by_author"] по завершённым ставкам (есть result).

    Структура:
      state["bets_by_author"] = {
        "Pacopick": {
          "https://www.bettingexpert.com/...": {
              "match": "...",
              "selection": "...",
              "odds": 1.93,
              "result": "won" | "lost" | "void" | др.,
              "kickoff": "2026-03-29T12:00:00+00:00",
              "updated_at": "2026-03-29T10:15:00+00:00",
          },
          ...
        },
        "Dyole": {
          ...
        },
        ...
      }
    tip_url используем как уникальный ключ ставки для автора.
    """
    bets_by_author = state.setdefault("bets_by_author", {})
    now_iso = datetime.now(timezone.utc).isoformat()

    for tip in tips:
        author = tip.get("author")
        tip_url = tip.get("tip_url")
        result = tip.get("result")

        # Нужны автор, url и непустой result (won/lost/void/...)
        if not author or not tip_url or not result:
            continue

        author_bets = bets_by_author.setdefault(author, {})

        # Обновляем/создаём запись по конкретной ставке
        author_bets[tip_url] = {
            "match": tip.get("match"),
            "selection": tip.get("selection"),
            "odds": tip.get("odds"),
            "result": result,
            "kickoff": tip.get("kickoff_time_utc"),
            "updated_at": now_iso,
        }
# ===== 8. Обработка отложенных матчей и отправка уведомлений =====

def process_upcoming_matches(state: dict) -> None:
    """
    Проверяет state["upcoming_matches"] и отправляет уведомления,
    если до матча сейчас от -40 до +80 минут по МСК
    и есть минимум два разных типстера с одной и той же ставкой.
    """
    upcoming = state.get("upcoming_matches") or {}
    if not upcoming:
        return

    now_msk = datetime.now(MOSCOW_TZ)
    max_before = timedelta(minutes=80)
    max_after = timedelta(minutes=40)

    parts: list[str] = []

    for key, info in list(upcoming.items()):
        if info.get("notified"):
            continue

        match = info.get("match") or "матч неизвестен"
        selection = info.get("selection") or "выбор не указан"
        kickoff_str = info.get("kickoff")
        authors = info.get("authors") or []
        odds_list = info.get("odds_list") or []

        if not kickoff_str:
            continue

        try:
            kickoff_utc = datetime.fromisoformat(kickoff_str)
        except Exception:
            continue

        if kickoff_utc.tzinfo is None:
            kickoff_utc = kickoff_utc.replace(tzinfo=timezone.utc)

        kickoff_msk = kickoff_utc.astimezone(MOSCOW_TZ)

        # 1) матч должен быть в тот же календарный день по МСК
        if kickoff_msk.date() != now_msk.date():
            continue

        # 2) дальше проверяем окно по времени
        delta = kickoff_msk - now_msk
        if not (-max_after <= delta <= max_before):
            continue

        # Условие "окна": от -40 до +80 минут относительно текущего времени
        if not (-max_after <= delta <= max_before):
            continue

        unique_authors = sorted(set(authors))
        if len(unique_authors) < 2:
            # Меньше двух разных авторов – пропускаем
            continue

        avg_odds = sum(odds_list) / len(odds_list) if odds_list else None
        odds_str = f"{avg_odds:.2f}" if avg_odds is not None else "—"

        kickoff_human = kickoff_msk.strftime("%Y-%m-%d %H:%M")

        # Короткая запись выборa (П1, Ф1, ТБ и т.п.)
        short_sel = short_selection(selection, match)

        authors_lines = [f"{author} — {short_sel}" for author in unique_authors]
        authors_block = "\n".join(authors_lines)

        part = (
            f"<b>Матч:</b> {match}\n"
            f"Время (МСК): {kickoff_human}\n"
            f"{authors_block}\n"
            f"Средний кэф: {odds_str}\n"
            f"{'-'*40}"
        )
        parts.append(part)

        # Помечаем как уже уведомлённый, чтобы второй раз не шлём
        info["notified"] = True

    if parts:
        full_message = "\n\n".join(parts)
        try:
            send_telegram_message(full_message)
            print("[INFO] Уведомление по отложенным матчам отправлено.")
        except Exception as e:
            print(f"[ERROR] Не удалось отправить сообщение по отложенным матчам: {e}")
# ===== 9. Один проход по всем профилям bettingexpert =====

def check_profiles_once() -> None:
    """
    1) Загружает state.json.
    2) Для каждого профиля bettingexpert из SITES:
       - качает HTML профиля,
       - парсит активные ставки,
       - для каждой ставки тянет страницу tip'а и парсит время начала,
       - конвертирует время в UTC.
    3) Обновляет память по матчам (upcoming_matches).
    4) Вызывает process_upcoming_matches (окно -40..80 мин, >=2 авторов).
    5) Сохраняет state.json.
    """
    state = load_state()
    all_enriched_tips: list[dict] = []

    for site in SITES:
        name = site["name"]          # строка с именем + %: "Pacopick 53%"
        url = site["url"]            # URL профиля
        source = site.get("source")  # должно быть "bettingexpert"

        # Вытаскиваем чистое имя автора из name, убирая "%", если нужно
        # Например, "Pacopick 53%" -> "Pacopick"
        author_name = name.split()[0]

        if source != "bettingexpert":
            print(f"[WARN] Профиль {name} не помечен как bettingexpert, пропускаю.")
            continue

        print(f"[INFO] Проверяю профиль: {author_name} ({url})")

        try:
            html = fetch_page(url)
        except Exception as e:
            print(f"[ERROR] Не удалось скачать {url}: {e}")
            continue

        try:
            tips = parse_bettingexpert_tips(html, author_name, url)
        except Exception as e:
            print(f"[ERROR] Ошибка парсинга профиля {url}: {e}")
            continue

        print(f"[INFO] Найдено прогнозов у {author_name}: {len(tips)}")

        # обогащаем ставки временем начала матча (UTC)
        enriched: list[dict] = []
        for tip in tips:
            tip_url = tip.get("tip_url")
            if not tip_url:
                continue

            kickoff_utc = get_kickoff_time_from_bettingexpert_tip(tip_url)
            if not kickoff_utc:
                continue

            tip["kickoff_time_utc"] = kickoff_utc.isoformat()
            enriched.append(tip)

        print(
            f"[INFO] Профиль {author_name}: ставок с известным временем матчей: "
            f"{len(enriched)}"
        )

        all_enriched_tips.extend(enriched)

    # Обновляем статистику ставок по авторам (завершённые ставки с result)
    update_author_bets(state, all_enriched_tips)

    # Обновляем память о будущих матчах (до 7 дней вперёд)
    update_upcoming_matches(state, all_enriched_tips)

    # Проверяем, есть ли матчи, попадающие в окно -40..80 минут и >=2 авторов
    process_upcoming_matches(state)

    # Сохраняем состояние
    save_state(state)
# ===== 10. Точка входа (главный цикл) =====

if __name__ == "__main__":
    while True:
        now_moscow = datetime.now(MOSCOW_TZ)
        hour = now_moscow.hour

        # Ночной режим: с 00:00 до 06:59 по Москве не трогаем сайты
        if 0 <= hour < 7:
            print(
                f"[INFO] {now_moscow.strftime('%Y-%m-%d %H:%M')} МСК. "
                f"Ночной режим, проверки не выполняем."
            )
        else:
            print(
                f"[INFO] Запуск проверки профилей "
                f"(МСК: {now_moscow.strftime('%Y-%m-%d %H:%M')})..."
            )
            try:
                check_profiles_once()
            except Exception as e:
                print(f"[ERROR] Критическая ошибка в check_profiles_once: {e}")

        print(f"[INFO] Сон {CHECK_INTERVAL_MINUTES} минут до следующей проверки...")
        time.sleep(CHECK_INTERVAL_MINUTES * 60)
