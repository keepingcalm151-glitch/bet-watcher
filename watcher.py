# watcher.py

import json
import os
import hashlib
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import time
import re

# Часовые пояса
MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# Таблица месяцев для парсинга даты "2 Apr 10:35"
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

# 1. Загрузка конфигурации
if os.getenv("CONFIG_JSON"):
    # Берём конфиг из переменной окружения (на Railway)
    config = json.loads(os.getenv("CONFIG_JSON"))
else:
    # Резервный вариант – из файла (если запускаешь локально)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

OPENAI_API_KEY = config["openai_api_key"]
TELEGRAM_BOT_TOKEN = config["telegram_bot_token"]
TELEGRAM_CHAT_ID = config["telegram_chat_id"]
SITES = config["sites"]
THESPORTSDB_KEY = config.get("thesportsdb_key")

# 2. Работа с локальным состоянием (state.json)

def load_state():
    if not os.path.exists(STATE_PATH):
        return {}
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}

def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# 3. Утилиты

def fetch_page(url: str) -> str:
    """
    Скачиваем HTML страницы.
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

def calc_hash(text: str) -> str:
    """
    Хэш от текста для быстрого сравнения.
    """
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()

def send_telegram_message(text: str):
    """
    Отправка сообщения в Telegram.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    
def short_selection(selection: str, match: str | None = None) -> str:
    """
    Делает короткую запись ставки из длинного текста selection.
    Умеет:
    - "Over 167.5 points" -> "ТБ 167.5"
    - "Under 16.5 games" -> "ТМ 16.5"
    - "<Team> to win  Draw No Bet" -> "ДНБ <Team>"
    - "<Team> to win" (если известно, это первая/вторая команда) -> "П1" / "П2"
    - "<Team> -1.00 (AH)" -> "Ф1(-1.0)" / "Ф2(-1.0)" (если команда = хозяева/гости)
    - " +0.50 (AH)" без команды -> "AH +0.50"
    Если не распознали — возвращаем исходный selection.
    """
    if not selection:
        return ""

    text = selection.strip()

    # Попробуем вытащить имена команд из match, если есть
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

    # Draw No Bet: берем команду до "to win" или до "Draw No Bet"
    if re.search(r"Draw\s+No\s+Bet", text, re.IGNORECASE):
        m_team = re.match(r"(.+?)\s+to\s+win", text, re.IGNORECASE)
        team = m_team.group(1).strip() if m_team else text.split("Draw")[0].strip()
        return f"ДНБ {team}"

    # Победа команды: "<Team> to win"
    m_win = re.match(r"(.+?)\s+to\s+win\b", text, re.IGNORECASE)
    if m_win:
        team = m_win.group(1).strip()
        # Пытаемся понять, П1 или П2
        if home_team and team.lower() in home_team.lower():
            return "П1"
        if away_team and team.lower() in away_team.lower():
            return "П2"
        # если не поняли, просто вернем "Победа <team>"
        return f"Победа {team}"

    # Азиатский гандикап: "<Team> +0.50 (AH)" или просто "+0.50 (AH)"
    m_ah_full = re.match(r"(.+?)\s+([+-]?\d+(\.\d+)?)\s*\(AH\)", text, re.IGNORECASE)
    if m_ah_full:
        team = m_ah_full.group(1).strip()
        val = m_ah_full.group(2).replace(",", ".")
        # Определяем Ф1/Ф2 по команде
        if home_team and team.lower() in home_team.lower():
            return f"Ф1({val})"
        if away_team and team.lower() in away_team.lower():
            return f"Ф2({val})"
        # если не угадали, просто AH с числом
        return f"AH {val}"

    # Вариант без команды: "+0.50 (AH)"
    m_ah = re.search(r"([+-]?\d+(\.\d+)?)\s*\(AH\)", text, re.IGNORECASE)
    if m_ah:
        val = m_ah.group(1).replace(",", ".")
        return f"AH {val}"

    # Если ничего не узнали — вернем оригинал
    return text

def call_openai_diff(old_html: str, new_html: str, site_name: str, site_url: str) -> str:
    """
    Отправляем старую и новую версию страницы в OpenAI и получаем описание изменений.
    """
    api_url = "https://api.openai.com/v1/chat/completions"

    system_prompt = (
        "Ты анализируешь изменения на веб-странице. "
        "Тебе дают старую и новую версию HTML. "
        "Опиши только важные изменения простым русским языком, "
        "коротко и по делу. Игнорируй косметические/технические правки."
    )

    user_prompt = (
        f"Сайт: {site_name}\nURL: {site_url}\n\n"
        "Старая версия HTML:\n"
        "-------------------\n"
        f"{old_html[:15000]}\n\n"
        "Новая версия HTML:\n"
        "-------------------\n"
        f"{new_html[:15000]}\n\n"
        "Опиши важные изменения (максимум 10 пунктов, если их много)."
    )

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }

    data = {
        "model": "gpt-4.1-mini",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.2
    }

    resp = requests.post(api_url, headers=headers, json=data, timeout=60)
    resp.raise_for_status()
    result = resp.json()

    try:
        return result["choices"][0]["message"]["content"]
    except Exception:
        return "Не удалось корректно разобрать ответ модели."
        
def parse_bettingexpert_profile(html: str, site_name: str, site_url: str) -> dict:
    """
    Простая версия парсера профиля bettingexpert.
    Пока достаём:
      - имя профиля (из шапки),
      - общее количество tips (Overall stats → Tips).
    Потом заменим на реальные Dyole Tips.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Имя профиля (сверху, рядом с Avatar)
    title_text = site_name

    # Поиск секции "Overall stats" и числа Tips
    tips_count = None
    overall_header = soup.find(lambda tag: tag.name in ["h3", "h2"] and "Overall stats" in tag.get_text(strip=True))
    if overall_header:
        # Ищем рядом число Tips
        # На bettingexpert текст "Tips" идёт как подпись под числом
        tips_label = soup.find(lambda tag: tag.name in ["div", "span", "p"] and "Tips" in tag.get_text(strip=True))
        if tips_label:
            # Берём предыдущий элемент, который содержит число
            prev = tips_label.find_previous(lambda tag: tag.name in ["div", "span", "p"] and tag.get_text(strip=True).isdigit())
            if prev:
                try:
                    tips_count = int(prev.get_text(strip=True))
                except ValueError:
                    tips_count = None

    return {
        "profile_name": title_text,
        "profile_url": site_url,
        "tips_count": tips_count
    }
    
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
        "market": None,
        "selection": "Macarthur FC +0.50 (AH)",
        "odds": 1.93,
        "author": "Dyole",
        "tip_url": "https://www.bettingexpert.com/football/macarthur-fc-vs-newcastle-jets",
      },
      ...
    ]
    """
    soup = BeautifulSoup(html, "html.parser")

    tips: list[dict] = []

    # карточки tip'ов – дивы с классами cursor-pointer и bg-white и ссылкой на матч внутри
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

        # полный URL tip'а
        if href.startswith("http"):
            tip_url = href
        else:
            tip_url = "https://www.bettingexpert.com/" + href.lstrip("/")

        # sport – первая часть пути
        sport = None
        if "/" in href:
            sport = href.split("/", 1)[0].lower()

        # команды – текст типа "Macarthur FC-Newcastle Jets"
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

        author = profile_name

        tips.append(
            {
                "match": match_name,
                "home_team": home_team,
                "away_team": away_team,
                "sport": sport,
                "market": "Unknown market",
                "selection": selection,
                "odds": odds,
                "author": author,
                "tip_url": tip_url,
            }
        )

    return tips

def parse_kickoff_from_tip_html(html: str) -> datetime | None:
    """
    Пытается вытащить из HTML страницы tip'а дату и время матча.
    Возвращает datetime БЕЗ tzinfo (локальное время, как на сайте).
    НИКУДА не конвертируем, сравнивать будем с таким же локальным now.
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    # ищем что-то вроде "2 Apr 10:35" / "02 April 10:35"
    pattern = r"\b(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{1,2}):(\d{2})\b"
    match = re.search(pattern, text)
    if not match:
        # fallback: попробуем только время HH:MM "18:00"
        m_time = re.search(r"\b(\d{1,2}):(\d{2})\b", text)
        if not m_time:
            return None
        hour = int(m_time.group(1))
        minute = int(m_time.group(2))
        now = datetime.now()
        # считаем, что матч сегодня в указанное время
        try:
            return datetime(now.year, now.month, now.day, hour, minute)
        except ValueError:
            return None

    day_str, month_str, hour_str, minute_str = match.groups()
    day = int(day_str)
    month = MONTHS_EN.get(month_str.lower())
    if not month:
        return None

    hour = int(hour_str)
    minute = int(minute_str)

    now = datetime.now()
    year = now.year

    try:
        dt_local = datetime(year, month, day, hour, minute)
    except ValueError:
        return None

    # если дата сильно в прошлом (больше 7 дней) – пробуем следующий год
    if dt_local < now - timedelta(days=7):
        try:
            dt_local = dt_local.replace(year=year + 1)
        except ValueError:
            pass

    return dt_local

def get_kickoff_time_from_bettingexpert_tip(tip_url: str) -> datetime | None:
    """
    Скачивает страницу tip'а на bettingexpert и вытаскивает время матча
    через parse_kickoff_from_tip_html. Возвращает naive datetime (локальное время).
    """
    try:
        html = fetch_page(tip_url)
    except Exception as e:
        print(f"[ERROR] Не удалось скачать страницу tip'а {tip_url}: {e}")
        return None

    dt = parse_kickoff_from_tip_html(html)
    if not dt:
        print(f"[WARN] Не удалось распарсить время матча с {tip_url}")
    return dt

def get_kickoff_time_utc_thesportsdb(sport: str, home_team: str, away_team: str) -> datetime | None:
    """
    По виду спорта (football/basketball) и двум командам пытается найти ближайший матч
    через TheSportsDB и вернуть время начала в UTC. Если не нашли — None.
    """
    if not THESPORTSDB_KEY or not home_team or not away_team:
        return None

    sport = (sport or "").lower()
    base_url = "https://www.thesportsdb.com/api/v1/json"

    if sport not in ["football", "basketball"]:
        return None

    # 1) ищем id команды по имени (home_team)
    search_team_url = f"{base_url}/{THESPORTSDB_KEY}/searchteams.php"
    try:
        resp = requests.get(search_team_url, params={"t": home_team}, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[ERROR] TheSportsDB search error ({sport}): {e}")
        return None

    teams = data.get("teams") or []
    if not teams:
        return None

    team_id = teams[0].get("idTeam")
    if not team_id:
        return None

    # 2) берём ближайшие события этой команды
    events_url = f"{base_url}/{THESPORTSDB_KEY}/eventsnext.php"
    try:
        resp = requests.get(events_url, params={"id": team_id}, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[ERROR] TheSportsDB events error ({sport}): {e}")
        return None

    events = data.get("events") or []
    if not events:
        return None

    best_time = None
    away_lower = (away_team or "").lower()

    for ev in events:
        home = (ev.get("strHomeTeam") or "").lower()
        away = (ev.get("strAwayTeam") or "").lower()

        if home_team.lower() in home and away_lower in away:
            date_str = ev.get("dateEvent")  # "2026-03-27"
            time_str = ev.get("strTime")    # "20:45:00"
            if not date_str or not time_str:
                continue

            try:
                dt = datetime.fromisoformat(f"{date_str}T{time_str}").replace(tzinfo=timezone.utc)
            except Exception:
                continue

            if best_time is None or dt < best_time:
                best_time = dt

    return best_time
    
def update_upcoming_matches(state: dict, tips: list[dict]):
    """
    Обновляет в state["upcoming_matches"] информацию о будущих матчах по новым прогнозам.
    Сохраняем матчи, до которых от сейчас до 24 часов вперёд.
    """
    upcoming = state.setdefault("upcoming_matches", {})

    now = datetime.now()  # локальное "сейчас", без tz
    max_ahead = timedelta(days=7)

    for tip in tips:
        match = tip.get("match")
        market = tip.get("market")
        selection = tip.get("selection")
        author = tip.get("author")
        kickoff = tip.get("kickoff_time_utc")
        odds = tip.get("odds")

        if not match or not market or not selection or not author or not kickoff:
            continue

        try:
            kickoff_dt = datetime.fromisoformat(kickoff)
        except Exception:
            continue

        delta = kickoff_dt - now
        if not (timedelta(0) <= delta <= max_ahead):
            # если матч слишком далеко или уже прошёл — не держим
            continue

        key = f"{match}|||{market}|||{selection}"

        if key not in upcoming:
            upcoming[key] = {
                "match": match,
                "market": market,
                "selection": selection,
                "kickoff": kickoff,
                "authors": [],
                "odds_list": [],
                "notified": False
            }

        if author not in upcoming[key]["authors"]:
            upcoming[key]["authors"].append(author)
        if isinstance(odds, (int, float)):
            upcoming[key]["odds_list"].append(odds)

def process_upcoming_matches(state: dict):
    """
    Проверяет сохранённые матчи и отправляет уведомления,
    если матч начинается/начался в окне ±2.5 часа от текущего времени
    и есть минимум два разных типстера.
    """
    upcoming = state.get("upcoming_matches") or {}
    if not upcoming:
        return

    now = datetime.now(MOSCOW_TZ)
    window = timedelta(hours=2, minutes=30)

    parts = []

    for key, info in list(upcoming.items()):
        if info.get("notified"):
            continue

        match = info.get("match") or "матч неизвестен"
        market = info.get("market") or "маркет не указан"
        selection = info.get("selection") or "выбор не указан"
        kickoff_str = info.get("kickoff")
        authors = info.get("authors") or []
        odds_list = info.get("odds_list") or []

        if not kickoff_str:
            continue

        try:
            kickoff_local = datetime.fromisoformat(kickoff_str)
        except Exception:
            continue

        # считаем, что kickoff_local — это московское локальное время
        if kickoff_local.tzinfo is None:
            kickoff = kickoff_local.replace(tzinfo=MOSCOW_TZ)
        else:
            kickoff = kickoff_local.astimezone(MOSCOW_TZ)

        delta = kickoff - now
        # окно от -2.5 до +2.5 часов
        if not (-window <= delta <= window):
            continue

        unique_authors = sorted(set(authors))
        count = len(unique_authors)
        if count < 2:
            # меньше двух разных типстеров — не уведомляем
            continue
            
        avg_odds = sum(odds_list) / len(odds_list) if odds_list else None
        odds_str = f"{avg_odds:.2f}" if avg_odds is not None else "—"

        kickoff_human = kickoff.strftime("%Y-%m-%d %H:%M")

        # короткая запись ставки c учетом матча (чтобы попытаться сделать П1/П2, Ф1/Ф2)
        short_sel = short_selection(selection, match)

        # строки вида "Автор — ставка"
        authors_lines = []
        for author in unique_authors:
            authors_lines.append(f"{author} — {short_sel}")
        authors_block = "\n".join(authors_lines)

        part = (
            f"<b>Матч:</b> {match}\n"
            f"Время (МСК): {kickoff_human}\n"
            f"{authors_block}\n"
            f"Средний кэф: {odds_str}\n"
            f"{'-'*40}"
        )

        parts.append(part)

        info["notified"] = True

    if parts:
        full_message = "\n\n".join(parts)
        try:
            send_telegram_message(full_message)
            print("[INFO] Уведомление по отложенным матчам отправлено.")
        except Exception as e:
            print(f"[ERROR] Не удалось отправить сообщение по отложенным матчам: {e}")

# 4. Основная логика проверки сайтов

def check_sites_once():
    state = load_state()

    # Подполя состояния:
    # sites_state — состояние по сайтам (как раньше весь state)
    # upcoming_matches — память о будущих матчах
    sites_state = state.setdefault("sites_state", {})
    upcoming_matches = state.setdefault("upcoming_matches", {})

    changes_found = []

    for site in SITES:
        name = site["name"]
        url = site["url"]
        key = url  # ключ в sites_state

        print(f"[INFO] Проверяю сайт: {name} ({url})")

        try:
            new_html = fetch_page(url)
        except Exception as e:
            print(f"[ERROR] Не удалось скачать {url}: {e}")
            continue

        # Новый код: парсим профиль, если это bettingexpert
        parsed_profile = None
        if site.get("source") == "bettingexpert":
            parsed_profile = parse_bettingexpert_profile(new_html, name, url)
            print(f"[INFO] Профиль: {parsed_profile.get('profile_name')} — Tips: {parsed_profile.get('tips_count')}")
            
        # HTML-парсер ставок для профилей bettingexpert (пока только логируем)
        extracted_tips = []
        if site.get("source") == "bettingexpert":
            extracted_tips = parse_bettingexpert_tips(new_html, name, url)
            print(f"[INFO] HTML-парсер: найдено прогнозов: {len(extracted_tips)}")
            # Покажем максимум 2 в логах
            for tip in extracted_tips[:2]:
                print(
                    f"[INFO] HTML-прогноз: матч={tip.get('match')}, выбор={tip.get('selection')}, "
                    f"кэф={tip.get('odds')}, автор={tip.get('author')}, url={tip.get('tip_url')}"
                )

        new_hash = calc_hash(new_html)
        old_entry = sites_state.get(key)

        if old_entry is None:
            print(f"[INFO] Для {url} ещё нет сохранённой версии. Сохраняю впервые.")
            sites_state[key] = {
                "hash": new_hash,
                "html": new_html,
                "tips_count": parsed_profile.get("tips_count") if parsed_profile else None
            }
            continue

        old_hash = old_entry.get("hash")
        old_html = old_entry.get("html", "")

        if old_hash == new_hash:
            print(f"[INFO] Изменений на {url} не найдено.")
            continue

        print(f"[INFO] ОБНАРУЖЕНЫ ИЗМЕНЕНИЯ на {url}!")

        try:
            diff_text = call_openai_diff(old_html, new_html, name, url)
        except Exception as e:
            print(f"[ERROR] Ошибка при обращении к OpenAI для {url}: {e}")
            diff_text = "Ошибка при запросе к OpenAI."

        # По умолчанию — без tips
        gpt_tips = []

        # Если это профиль bettingexpert — берём прогнозы из HTML и тянем время матча со страницы tip'а
        if site.get("source") == "bettingexpert":
            try:
                # 1) список прогнозов с профиля
                html_tips = parse_bettingexpert_tips(new_html, name, url)

                # 2) обогащаем временем начала матча (локальное время) с каждой страницы tip'а
                enriched_tips = []
                now = datetime.now()  # локальное время машины (для Railway это UTC, но kickoff мы трактуем так же)
                window = timedelta(hours=2, minutes=30)

                for tip in html_tips:
                    tip_url = tip.get("tip_url")
                    if not tip_url:
                        continue

                    kickoff = get_kickoff_time_from_bettingexpert_tip(tip_url)
                    if not kickoff:
                        continue

                    # сохраняем время матча как есть (naive datetime -> ISO)
                    tip["kickoff_time_utc"] = kickoff.isoformat()
                    enriched_tips.append(tip)

                # 3) обновляем память матчей (до 7 дней вперед)
                update_upcoming_matches(state, enriched_tips)

                # 4) выбираем только те матчи, которые в окне ±2.5 часа от now
                gpt_tips = []
                for tip in enriched_tips:
                    try:
                        kickoff_local = datetime.fromisoformat(tip["kickoff_time_utc"])
                    except Exception:
                        continue

                    delta = kickoff_local - now
                    if -window <= delta <= window:
                        gpt_tips.append(tip)

                print(
                    f"[INFO] bettingexpert-HTML: всего={len(html_tips)}, "
                    f"с известным временем={len(enriched_tips)}, в окне ±2.5 часа={len(gpt_tips)}"
                )
            except Exception as e:
                print(f"[ERROR] Ошибка HTML-парсера/фильтра для {url}: {e}")
                gpt_tips = []

        changes_found.append({
            "name": name,
            "url": url,
            "diff": diff_text,
            "tips": gpt_tips
        })

        sites_state[key] = {
            "hash": new_hash,
            "html": new_html,
            "tips_count": parsed_profile.get("tips_count") if parsed_profile else None
        }

    save_state(state)

    # 1) Собираем все tips в один список
    all_tips_next_hour = []

    for ch in changes_found:
        tips = ch.get("tips") or []
        for tip in tips:
            all_tips_next_hour.append(tip)

    if not all_tips_next_hour:
        print("[INFO] Изменения есть, но матчей в окне ±2.5 часа нет — уведомление не шлём.")
        return

    # 2) Группируем по (match, market, selection)
    groups = {}

    for tip in all_tips_next_hour:
        match = tip.get("match") or "матч неизвестен"
        market = tip.get("market") or "маркет не указан"
        selection = tip.get("selection") or "выбор не указан"
        kickoff = tip.get("kickoff_time_utc")
        author = tip.get("author") or "неизвестный автор"
        odds = tip.get("odds")

        key = (match, market, selection)

        if key not in groups:
            groups[key] = {
                "match": match,
                "market": market,
                "selection": selection,
                "kickoff": kickoff,
                "authors": [],
                "odds_list": []
            }

        groups[key]["authors"].append(author)
        if isinstance(odds, (int, float)):
            groups[key]["odds_list"].append(odds)

        # если в группе ещё не было kickoff, а тут есть — запомним
        if not groups[key]["kickoff"] and kickoff:
            groups[key]["kickoff"] = kickoff

    # 3) Формируем текст для Telegram по группам
    parts = []

    for (match, market, selection), g in groups.items():
        authors = g["authors"]
        unique_authors = sorted(set(authors))
        count = len(unique_authors)

        # ВАЖНО: если меньше двух разных типстеров — пропускаем этот матч
        if count < 2:
            continue

        odds_list = g["odds_list"]
        kickoff_str = g["kickoff"]

        avg_odds = sum(odds_list) / len(odds_list) if odds_list else None
        odds_str = f"{avg_odds:.2f}" if avg_odds is not None else "—"

        if kickoff_str:
            try:
                kickoff_local = datetime.fromisoformat(kickoff_str)
            except Exception:
                kickoff_human = "время неизвестно"
            else:
                if kickoff_local.tzinfo is None:
                    kickoff_dt = kickoff_local.replace(tzinfo=MOSCOW_TZ)
                else:
                    kickoff_dt = kickoff_local.astimezone(MOSCOW_TZ)
                kickoff_human = kickoff_dt.strftime("%Y-%m-%d %H:%M")
        else:
            kickoff_human = "время неизвестно"

        # короткая запись ставки
        short_sel = short_selection(selection, match)

        # строки вида "Автор — ставка"
        authors_lines = []
        for author in unique_authors:
            authors_lines.append(f"{author} — {short_sel}")
        authors_block = "\n".join(authors_lines)

        part = (
            f"<b>Матч:</b> {match}\n"
            f"Время (МСК): {kickoff_human}\n"
            f"{authors_block}\n"
            f"Средний кэф: {odds_str}\n"
            f"{'-'*40}"
        )

        parts.append(part)

    if parts:
        full_message = "\n\n".join(parts)
        try:
            send_telegram_message(full_message)
            print("[INFO] Уведомление в Telegram отправлено (сгруппированные матчи).")
        except Exception as e:
            print(f"[ERROR] Не удалось отправить сообщение в Telegram: {e}")
    else:
        print("[INFO] После группировки подходящих матчей не осталось — уведомление не шлём.")

    # После обработки свежих изменений — проверяем отложенные матчи
    process_upcoming_matches(state)
    save_state(state)

# 5. Точка входа

if __name__ == "__main__":
    CHECK_INTERVAL_MINUTES = 59  # можно потом взять из config["check_interval_minutes"]

    while True:
        # Берём текущее время в Москве
        now_moscow = datetime.now(ZoneInfo("Europe/Moscow"))
        hour = now_moscow.hour

        # Если время с 00:00 до 06:59 по Москве — спим до следующего цикла, ничего не проверяем
        if 0 <= hour < 7:
            print(f"[INFO] Сейчас {now_moscow.strftime('%Y-%m-%d %H:%M')} по Москве. Ночной режим, проверки не выполняем.")
        else:
            print(f"[INFO] Запуск проверки сайтов (московское время: {now_moscow.strftime('%Y-%m-%d %H:%M')})...")
            try:
                check_sites_once()
            except Exception as e:
                print(f"[ERROR] Критическая ошибка в check_sites_once: {e}")

        print(f"[INFO] Сон {CHECK_INTERVAL_MINUTES} минут до следующей проверки...")
        time.sleep(CHECK_INTERVAL_MINUTES * 60)
