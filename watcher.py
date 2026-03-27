# watcher.py

import json
import os
import hashlib
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import time

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
    
def parse_bettingexpert_tips(html: str, profile_name: str) -> list[dict]:
    """
    Парсер блока 'XXX Tips' на bettingexpert для профиля.

    Возвращает список словарей:
    [
      {
        "match": "...",
        "selection": "...",
        "odds": 1.93,
        "author": "Имя профиля"
      },
      ...
    ]
    """
    soup = BeautifulSoup(html, "html.parser")

    tips = []

    # Ищем заголовок вида "Dyole Tips", "Pacopick Tips" и т.п.
    header = soup.find(
        lambda tag: tag.name in ["h2", "h3", "h4"]
        and tag.get_text(strip=True).endswith("Tips")
        and profile_name.split()[0] in tag.get_text(strip=True)
    )

    if not header:
        # fallback: любой заголовок, который оканчивается на "Tips"
        header = soup.find(
            lambda tag: tag.name in ["h2", "h3", "h4"]
            and tag.get_text(strip=True).endswith("Tips")
        )

    if not header:
        return tips  # Не нашли блок с Tips

    # Берём контейнер после заголовка
    tips_container = header.find_next(
        lambda tag: tag.name in ["div", "section", "ul", "ol"]
    )
    if not tips_container:
        return tips

    # Ищем "карточки" ставок: элементы, где в тексте есть " vs "
    tip_blocks = tips_container.find_all(
        lambda tag: tag.name in ["div", "article", "li"]
        and " vs " in tag.get_text(" ", strip=True)
    )

    for block in tip_blocks:
        text = block.get_text(" ", strip=True)

        # Пытаемся выдрать матч по " vs "
        match = None
        if " vs " in text:
            parts = text.split(" vs ")
            left = parts[0].split()
            right = parts[1].split()
            # Берём по несколько слов слева и справа
            left_part = " ".join(left[-3:])
            right_part = " ".join(right[:3])
            match = (left_part + " vs " + right_part).strip()

        # Находим коэффициент как первое float-число с точкой
        odds = None
        for token in text.replace(",", ".").split():
            try:
                val = float(token)
                if 1.01 <= val <= 100.0:
                    odds = val
                    break
            except ValueError:
                continue

        # В качестве selection пока берём весь текст блока
        selection = text

        if match and odds:
            tips.append(
                {
                    "match": match,
                    "selection": selection,
                    "odds": odds,
                    "author": profile_name
                }
            )

    return tips
    
def extract_tips_with_gpt(html: str, site_name: str, site_url: str) -> list[dict]:
    """
    Используем OpenAI, чтобы вытащить структурированный список актуальных прогнозов с профиля bettingexpert.

    Возвращает список словарей вида:
    [
      {
        "match": "Macarthur FC vs Newcastle Jets",
        "market": "Asian handicap",
        "selection": "Macarthur FC +0.50",
        "odds": 1.93,
        "author": "Dyole"
      },
      ...
    ]
    """
    api_url = "https://api.openai.com/v1/chat/completions"

    system_prompt = (
    "You are a betting tip parser for bettingexpert profiles. "
    "You receive the HTML of a tipster profile page. "
    "Your goal is to extract ONLY currently active tips of this author.\n\n"
    "For EACH tip, return a JSON object with EXACTLY these fields:\n"
    "{\n"
    '  "match": "Boston Celtics vs Atlanta Hawks",\n'
    '  "home_team": "Boston Celtics",\n'
    '  "away_team": "Atlanta Hawks",\n'
    '  "league": "NBA",\n'
    '  "sport": "basketball",\n'
    '  "market": "Asian handicap",\n'
    '  "selection": "Atlanta Hawks +8.50 (AH)",\n'
    '  "odds": 1.90,\n'
    '  "author": "Dyole"\n'
    "}\n\n"
    "Return a JSON ARRAY of such objects.\n"
    "- sport MUST be in English: 'football' or 'basketball' when possible.\n"
    "- If some field is unknown, set it to null.\n"
    "- Do NOT include any other fields.\n"
    "- Output ONLY the JSON array, no explanations or text around it."
    )

    user_prompt = (
        f"Профиль: {site_name}\nURL: {site_url}\n\n"
        "Вот HTML страницы профиля:\n"
        "--------------------------\n"
        f"{html[:15000]}\n\n"
        "Верни JSON-массив с прогнозами в точном формате:\n"
        "[\n"
        "  {\n"
        "    \"match\": \"...\",\n"
        "    \"market\": \"...\",\n"
        "    \"selection\": \"...\",\n"
        "    \"odds\": 1.93,\n"
        "    \"author\": \"...\"\n"
        "  },\n"
        "  ...\n"
        "]\n"
        "Если прогнозов нет, верни пустой массив []. Никакого текста вокруг, только JSON."
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
        "temperature": 0.1
    }

    resp = requests.post(api_url, headers=headers, json=data, timeout=90)
    resp.raise_for_status()
    result = resp.json()

    try:
        content = result["choices"][0]["message"]["content"]
        # Пытаемся распарсить как JSON
        tips = json.loads(content)
        if isinstance(tips, list):
            return tips
        else:
            return []
    except Exception:
        print("[ERROR] Не удалось корректно разобрать JSON с прогнозами от OpenAI.")
        return []
        
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
    
def filter_tips_next_hour(tips: list[dict]) -> list[dict]:
    """
    Оставляет только те прогнозы (football/basketball), у которых матч начнётся в ближайший час.
    """
    now = datetime.now(timezone.utc)
    max_before = timedelta(minutes=80)
    result = []

    for tip in tips:
        sport = (tip.get("sport") or "").lower()
        home_team = tip.get("home_team")
        away_team = tip.get("away_team")

        kickoff = get_kickoff_time_utc_thesportsdb(sport, home_team, away_team)
        if not kickoff:
            continue

        delta = kickoff - now
        if timedelta(0) <= delta <= max_before:
            tip["kickoff_time_utc"] = kickoff.isoformat()
            result.append(tip)

    return result
    
def update_upcoming_matches(state: dict, tips: list[dict]):
    """
    Обновляет в state["upcoming_matches"] информацию о будущих матчах по новым прогнозам.
    Сохраняем матчи, до которых от сейчас до 24 часов вперёд.
    """
    upcoming = state.setdefault("upcoming_matches", {})

    now = datetime.now(timezone.utc)
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
    Проверяет сохранённые будущие матчи и отправляет уведомления,
    если до матча сейчас 0–80 минут и есть минимум два разных типстера.
    """
    upcoming = state.get("upcoming_matches") or {}
    if not upcoming:
        return

    now = datetime.now(timezone.utc)
    max_before = timedelta(minutes=80)

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
            kickoff = datetime.fromisoformat(kickoff_str)
        except Exception:
            continue

        delta = kickoff - now
        if not (timedelta(0) <= delta <= max_before):
            # либо ещё рано (>80 мин), либо уже поздно (<0) — ждём или пропускаем
            continue

        unique_authors = sorted(set(authors))
        count = len(unique_authors)
        if count < 2:
            # меньше двух разных типстеров — не уведомляем
            continue

        avg_odds = sum(odds_list) / len(odds_list) if odds_list else None
        odds_str = f"{avg_odds:.2f}" if avg_odds is not None else "—"
        kickoff_human = kickoff_str

        part = (
            f"<b>Матч:</b> {match}\n"
            f"Время (UTC): {kickoff_human}\n"
            f"Маркет: {market}\n"
            f"Выбор: {selection}\n"
            f"Поставили: {count} типстер(ов)\n"
            f"Имена: {', '.join(unique_authors)}\n"
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
            extracted_tips = parse_bettingexpert_tips(new_html, name)
            print(f"[INFO] HTML-парсер: найдено прогнозов: {len(extracted_tips)}")
            # Покажем максимум 2 в логах
            for tip in extracted_tips[:2]:
                print(
                    f"[INFO] HTML-прогноз: матч={tip.get('match')}, выбор={tip.get('selection')}, "
                    f"кэф={tip.get('odds')}, автор={tip.get('author')}"
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

        # По умолчанию — без GPT-tips
        gpt_tips = []

        # Если это профиль bettingexpert — пробуем вытащить прогнозы через GPT
        if site.get("source") == "bettingexpert":
            try:
                # 1) берём все прогнозы из GPT
                raw_tips = extract_tips_with_gpt(new_html, name, url)

                # 2) обогащаем их временем начала матча (kickoff_time_utc),
                #    даже если матч ещё далеко
                enriched_tips = []
                for tip in raw_tips:
                    sport = (tip.get("sport") or "").lower()
                    home_team = tip.get("home_team")
                    away_team = tip.get("away_team")

                    kickoff = get_kickoff_time_utc_thesportsdb(sport, home_team, away_team)
                    if not kickoff:
                        continue

                    tip["kickoff_time_utc"] = kickoff.isoformat()
                    enriched_tips.append(tip)

                # 3) обновляем "память матчей" по этим прогнозам
                update_upcoming_matches(state, enriched_tips)

                # 4) из этих же обогащённых tips берём только те, что в ближайшие 80 минут
                gpt_tips = []
                now = datetime.now(timezone.utc)
                max_before = timedelta(minutes=80)
                for tip in enriched_tips:
                    kickoff = datetime.fromisoformat(tip["kickoff_time_utc"])
                    delta = kickoff - now
                    if timedelta(0) <= delta <= max_before:
                        gpt_tips.append(tip)

                print(f"[INFO] GPT-парсер: всего={len(raw_tips)}, с известным временем={len(enriched_tips)}, в ближайшие 80 минут={len(gpt_tips)}")
            except Exception as e:
                print(f"[ERROR] Ошибка GPT-парсера/фильтра для {url}: {e}")
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
        print("[INFO] Изменения есть, но матчей в ближайший час нет — уведомление не шлём.")
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
        kickoff = g["kickoff"]

        avg_odds = sum(odds_list) / len(odds_list) if odds_list else None
        odds_str = f"{avg_odds:.2f}" if avg_odds is not None else "—"
        kickoff_str = kickoff or "время неизвестно"

        part = (
            f"<b>Матч:</b> {match}\n"
            f"Время (UTC): {kickoff_str}\n"
            f"Маркет: {market}\n"
            f"Выбор: {selection}\n"
            f"Поставили: {count} типстер(ов)\n"
            f"Имена: {', '.join(unique_authors)}\n"
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
