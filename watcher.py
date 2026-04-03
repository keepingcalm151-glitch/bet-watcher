# watcher.py
#
# Новый воркер для Railway:
#   Procfile: worker: python watcher.py
#
# Логика (новая):
#   - заходим на страницу с сегодняшними футбольными матчами (today_football_url)
#   - вытаскиваем все ссылки вида /football/...
#   - для каждой ссылки:
#       - открываем страницу матча
#       - парсим все ставки (selection) и авторов
#       - для каждого автора заходим на профиль /user/profile/<slug> и берём Win rate
#   - группируем авторов по одинаковым/похожим ставкам
#   - считаем комбинированный шанс выигрыша:
#       P = 1 - Π(1 - p_i), где p_i = winrate_i / 100
#   - если P * 100 > winrate_threshold_percent и авторов >= min_authors_per_signal,
#       шлём сигнал в Telegram:
#         - матч
#         - время матча
#         - список ставок (без имён авторов)
#         - процент победы

import json
import os
import time
import re
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Часовые пояса
BETTINGEXPERT_TZ = ZoneInfo("Europe/Copenhagen")
MOSCOW_TZ = ZoneInfo("Europe/Moscow")  # для удобства отображения времени

CONFIG_PATH = "config.json"
STATE_PATH = "state.json"

# ===== 1. Загрузка конфигурации =====

if os.getenv("CONFIG_JSON"):
    config = json.loads(os.getenv("CONFIG_JSON"))
else:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

TELEGRAM_BOT_TOKEN: str = config["telegram_bot_token"]
TELEGRAM_CHAT_ID: str = config["telegram_chat_id"]
CHECK_INTERVAL_MINUTES: int = config.get("check_interval_minutes", 15)

BASE_URL: str = config.get("base_url", "https://www.bettingexpert.com").rstrip("/")
TODAY_FOOTBALL_URL: str = config.get(
    "today_football_url",
    f"{BASE_URL}/football"
)

WINRATE_THRESHOLD_PERCENT: float = float(config.get("winrate_threshold_percent", 98.0))
MIN_AUTHORS_PER_SIGNAL: int = int(config.get("min_authors_per_signal", 2))
MIN_TIPS_PER_MATCH: int = int(config.get("min_tips_per_match", 1))
MAX_SIGNALS_PER_DAY: int = int(config.get("max_signals_per_day", 20))

# ===== 2. Работа с локальным состоянием (state.json) =====

def load_state() -> dict:
    """
    Загружаем state.json (память о уже отправленных сигналах и кэше winrate).
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


# ===== 3. HTTP-утилита и Telegram =====

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0 Safari/537.36"
        )
    }
)


def fetch_page(url: str) -> str:
    """
    Скачиваем HTML страницы с нормальным User-Agent'ом.
    Бросает исключение, если не получилось.
    """
    # если передан относительный путь типа "/football/...", добавим BASE_URL
    if url.startswith("/"):
        full_url = BASE_URL + url
    else:
        full_url = url

    resp = SESSION.get(full_url, timeout=30)
    resp.raise_for_status()
    return resp.text


def send_telegram_message(text: str) -> None:
    """
    Отправка сообщения в Telegram в указанный чат.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
# ===== 4. Структуры данных =====

@dataclass
class AuthorInfo:
    slug: str                     # профайл-слуг на сайте (nieder, Dyole, ...)
    name: str                     # отображаемое имя
    winrate_percent: Optional[float] = None  # Win rate, например 47.94
    winrate_updated_at: Optional[float] = None  # timestamp, когда обновили


@dataclass
class TipOnMatch:
    match_url: str                # относительный путь /football/...
    match_title: str              # "NK Krka - ND Triglav"
    kickoff_utc: Optional[datetime]  # время матча (UTC, если удастся достать)
    selection_raw: str            # исход как на странице ("NK Krka +0.75 (AH)")
    selection_group_key: str      # нормализованный ключ для группировки
    author_slug: str
    author_name: str
    author_winrate: Optional[float]
    odds: Optional[float]


@dataclass
class Signal:
    match_url: str
    match_title: str
    kickoff_utc: Optional[datetime]
    kickoff_moscow: Optional[datetime]
    selection_group_key: str
    selections_raw: List[str] = field(default_factory=list)
    authors: List[str] = field(default_factory=list)
    combined_win_chance_percent: float = 0.0


# ===== 5. Вероятности и нормализация исходов =====

def combine_independent_probabilities(ps: List[float]) -> float:
    """
    Комбинируем независимые вероятности p1, p2, ... по формуле:
      P = 1 - (1 - p1) * (1 - p2) * ...
    Аргумент ps — числа в диапазоне [0,1].
    Возвращаем P (тоже [0,1]).
    """
    if not ps:
        return 0.0
    prod_not = 1.0
    for p in ps:
        p_clamped = max(0.0, min(1.0, float(p)))
        prod_not *= (1.0 - p_clamped)
    P = 1.0 - prod_not
    return max(0.0, min(1.0, P))


def normalize_selection_for_grouping(selection: str) -> str:
    """
    Нормализует исход ставки для группировки похожих:
      - "NK Krka +0.75 (AH)" -> "NK Krka (AH)" (срезаем числовой хендикап)
      - "Over 3.5 goals" -> "Over goals"
      - "Under 2.25 goals" -> "Under goals"
      - "Gaziantep FK to win  Draw No Bet" -> "Gaziantep FK DNB"
      - "Team -1.00 (AH)" -> "Team (AH)"
      - "+0.50 (AH)" (без команды) -> "+(AH)" (отдельная группа)
    Нужно, чтобы разные авторы с чуть разными линиями,
    но тем же направлением, группировались вместе.
    """
    if not selection:
        return ""

    text = selection.strip()
    lower = text.lower()

    # Draw No Bet + команда в начале
    m_dnb = re.match(r"(.+?)\s+to\s+win\s+draw\s+no\s+bet", lower)
    if m_dnb:
        team = m_dnb.group(1).strip()
        return f"{team} DNB"

    # Over / Under goals
    m_over = re.match(r"\s*over\s+[\d.,]+\s+goals?", lower)
    if m_over:
        return "Over goals"

    m_under = re.match(r"\s*under\s+[\d.,]+\s+goals?", lower)
    if m_under:
        return "Under goals"

    # Азиатский гандикап "Team +0.75 (AH)" -> "Team (AH)"
    m_ah_team = re.match(r"(.+?)\s+[+-]?\d+(\.\d+)?\s*\(ah\)", text, flags=re.IGNORECASE)
    if m_ah_team:
        team = m_ah_team.group(1).strip()
        return f"{team} (AH)"

    # Азиатский гандикап без команды "+0.75 (AH)" -> "+(AH)"
    m_ah = re.match(r"[+-]?\d+(\.\d+)?\s*\(ah\)", text, flags=re.IGNORECASE)
    if m_ah:
        return "+(AH)"

    # Простое "Team to win"
    m_win = re.match(r"(.+?)\s+to\s+win\b", text, flags=re.IGNORECASE)
    if m_win:
        team = m_win.group(1).strip()
        return f"{team} win"

    # Тоталы без слова goals: "Over 167.5 points", "Over 2.5"
    m_over_any = re.match(r"\s*over\s+[\d.,]+", lower)
    if m_over_any:
        return "Over"

    m_under_any = re.match(r"\s*under\s+[\d.,]+", lower)
    if m_under_any:
        return "Under"

    # Если ничего не распознали — возвращаем как есть (обрежем пробелы)
    return text
# ===== 6. Парсинг списка сегодняшних матчей (страница football) =====

def parse_today_matches(html: str) -> List[Tuple[str, str, str]]:
    """
    Парсим страницу с сегодняшними матчами (например /football).

    Вёрстка типа:
      <a class="flex ... " href="/football/lancaster-city-vs-morpeth-town-fc">
        <div class="font-os text-lg uppercase mr-4">16:15</div>
        <div class="flex flex-col ...">
          <div class="font-ms truncate w-full">Lancaster City</div>
          <div class="font-ms truncate w-full">Morpeth Town FC</div>
        </div>
      </a>

    Возвращаем список кортежей:
      (relative_url, match_title, kickoff_time_str)

    Где:
      relative_url: "/football/nd-triglav-vs-nk-krka"
      match_title:  "NK Krka vs ND Triglav"
      kickoff_time_str: "17:00" (как на странице)
    """
    soup = BeautifulSoup(html, "html.parser")
    results: List[Tuple[str, str, str]] = []

    # Ищем все <a ... href="/football/...">
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("/football/"):
            continue

        # время матча, внутри <div class="font-os text-lg ...">
        time_div = a.find(
            "div",
            class_=lambda x: x and "font-os" in x.split() and "text-lg" in x.split()
        )
        if not time_div:
            continue
        kickoff_str = time_div.get_text(strip=True)

        # команды — два подряд <div class="font-ms truncate w-full">
        team_divs = a.find_all(
            "div",
            class_=lambda x: x and "font-ms" in x.split() and "truncate" in x.split()
        )
        if len(team_divs) < 2:
            continue

        home_team = team_divs[0].get_text(strip=True)
        away_team = team_divs[1].get_text(strip=True)
        match_title = f"{home_team} vs {away_team}"

        results.append((href, match_title, kickoff_str))

    return results


def parse_kickoff_datetime_today(kickoff_time_str: str) -> Optional[datetime]:
    """
    Превращаем строку времени вида "17:00" в datetime на СЕГОДНЯ в таймзоне BETTINGEXPERT_TZ.
    Если формат странный — возвращаем None.

    Важно: это "грубое" время, реальное время старта может быть в другом месте,
    но для твоей задачи достаточно этого.
    """
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", kickoff_time_str)
    if not m:
        return None

    hour = int(m.group(1))
    minute = int(m.group(2))

    now = datetime.now(BETTINGEXPERT_TZ)
    try:
        dt = datetime(
            year=now.year,
            month=now.month,
            day=now.day,
            hour=hour,
            minute=minute,
            tzinfo=BETTINGEXPERT_TZ,
        )
    except ValueError:
        return None

    # Переводим в UTC, чтобы внутри всё сравнивать в UTC
    return dt.astimezone(timezone.utc)
# ===== 7. Парсинг страницы матча: ставки и авторы =====

def parse_match_tips(html: str, match_url: str, match_title: str) -> List[TipOnMatch]:
    """
    Парсим страницу конкретного матча /football/...

    Из HTML (пример с NK Krka +0.75 (AH)) нам нужны:
      - selection_raw: текст исхода (например "NK Krka +0.75 (AH)")
      - author_name:  "nieder"
      - author_slug:  "nieder"  (из ссылки /user/profile/nieder)
      - odds:         коэффициент, если рядом есть блок с odds (по возможности)

    Типичная вёрстка куска tip'а (упрощённо):

      <a href="football/nd-triglav-vs-nk-krka">NK Krka - ND Triglav</a>
      ...
      <div class="font-gc font-bold h-[48px] ... text-[22px]">
         NK Krka +0.75 (AH)
      </div>
      ...
      <div class="text-xs text-grey3 italic font-gc">
         3 hours ago by
      </div>
      <div class="flex flex-row items-center text-red-felix text-xs font-gc font-semibold">
         <img ...>
         <div class="mr-1">
             nieder
         </div>
         <svg ...star...>...</svg>
      </div>

    Возвращаем список TipOnMatch.
    """
    soup = BeautifulSoup(html, "html.parser")
    tips: List[TipOnMatch] = []

    # На странице матча может быть несколько tip'ов.
    # Ориентируемся на большие блоки с исходом (selection) и автором рядом.
    # Будем искать сначала все крупные заголовки исходов.
    selection_divs = soup.find_all(
        "div",
        class_=lambda x: x
        and "font-gc" in x.split()
        and "font-bold" in x.split()
    )

    # Попробуем заранее выдрать коэффициенты, если есть.
    # Часто рядом есть текст "odds 1.95" или похожее.

    def safe_parse_match_tips(html: str, match_url: str, match_title: str) -> List[TipOnMatch]:
    """
    Безопасный парсер tip'ов: ловим исключения, чтобы из-за одного матча
    не падала вся итерация.
    """
    try:
        tips = parse_match_tips(html, match_url, match_title)
        return tips
    except Exception as e:
        print(f"[ERROR] Ошибка парсинга tip'ов для матча {match_url}: {e}")
        return []

    def extract_nearby_odds(block: BeautifulSoup) -> Optional[float]:
        # ищем в пределах того же родительского блока слово "odds" и число рядом
        parent = block.parent
        if not parent:
            return None
        text = parent.get_text(" ", strip=True)
        text = text.replace(",", ".")
        # ищем подстроку вида "odds 1.95" или просто число около "odds"
        m = re.search(r"odds\s+(\d+(?:\.\d+)?)", text, flags=re.IGNORECASE)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                return None
        # fallback — любое правдоподобное число
        tokens = text.split()
        for tok in reversed(tokens):
            try:
                val = float(tok)
                if 1.01 <= val <= 100.0:
                    return val
            except ValueError:
                continue
        return None

    # Для поиска автора будем смотреть дальше по соседям после блока с исходом.
    for sel_div in selection_divs:
        selection_raw = sel_div.get_text(strip=True)
        if not selection_raw:
            continue

        # Ищем блоки после selection_div, где есть "by" и далее имя + звезда
        author_name = None
        author_slug = None

        # поднимаемся до контейнера tip'а
        container = sel_div
        for _ in range(5):
            if container.parent is None:
                break
            container = container.parent

        # внутри контейнера ищем текст "by" и блок с именем
        by_label = None
        for small in container.find_all("div"):
            txt = small.get_text(" ", strip=True).lower()
            if " by" in txt and "hour" in txt or "minute" in txt or "day" in txt:
                by_label = small
                break

        if by_label:
            # имя автора обычно в следующем div c классами text-red-felix ...
            author_block = by_label.find_next(
                "div",
                class_=lambda x: x
                and "flex" in x.split()
                and "items-center" in x.split()
                and "text-red-felix" in x
            )
        else:
            author_block = None

        if author_block:
            # сам текст имени в первом <div class="mr-1">...</div>
            name_div = author_block.find(
                "div",
                class_=lambda x: x and "mr-1" in x.split()
            )
            if name_div:
                author_name = name_div.get_text(strip=True)

            # slug берём из ссылки /user/profile/<slug>
            a_profile = author_block.find("a", href=True)
            if a_profile:
                href = a_profile["href"]
                # ожидаем что-то вроде "/user/profile/nieder"
                m_slug = re.search(r"/user/profile/([^/?#]+)", href)
                if m_slug:
                    author_slug = m_slug.group(1)

        if not author_name or not author_slug:
            # если не нашли автора — пропускаем такой tip
            continue

        # Коэффициент
        odds = extract_nearby_odds(sel_div)

        # Нормализованный ключ исхода для группировки
        group_key = normalize_selection_for_grouping(selection_raw)

        # Пока время матча сюда не подмешиваем, используем при сборке сигналов
        tip = TipOnMatch(
            match_url=match_url,
            match_title=match_title,
            kickoff_utc=None,  # заполним позже, из списка матчей по времени
            selection_raw=selection_raw,
            selection_group_key=group_key,
            author_slug=author_slug,
            author_name=author_name,
            author_winrate=None,  # позже подтянем из профиля
            odds=odds,
        )
        tips.append(tip)

    return tips
# ===== 8. Парсинг профиля автора: Win rate =====

def parse_author_winrate(html: str) -> Optional[float]:
    """
    Из HTML профиля tipster'а достаём Win rate в процентах.

    В твоём примере вокруг Win rate примерно такая структура:

      ... (иконка шестерёнки/шилда)
      <div class="font-be text-xl flex flex-row items-center">
          47.94%
      </div>
      <div class="text-grey4 text-xs font-gc">
          Win rate
      </div>

    Стратегия:
      - Находим элемент, где текст ровно "Win rate"
      - Смотрим предыдущий соседний блок (div), берём оттуда число с %
    """
    soup = BeautifulSoup(html, "html.parser")

    label = soup.find(
        lambda tag: tag.name in ("div", "span")
        and tag.get_text(strip=True).lower() == "win rate"
    )
    if not label or not label.parent:
        return None

    # будем искать предыдущий div с числом
    prev = label.previous_sibling
    while prev is not None and getattr(prev, "name", None) is None:
        prev = prev.previous_sibling

    if not prev:
        # попробуем родителя и его предыдущий элемент
        prev = label.parent.previous_sibling
        while prev is not None and getattr(prev, "name", None) is None:
            prev = prev.previous_sibling

    if not prev:
        return None

    text = prev.get_text(" ", strip=True)
    # Ищем что-то вроде "47.94%" или "52 %"
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", text)
    if not m:
        return None

    val_str = m.group(1).replace(",", ".")
    try:
        val = float(val_str)
    except ValueError:
        return None

    # ограничим разумными пределами
    if val < 0.0 or val > 100.0:
        return None

    return val


def get_author_winrate(state: dict, author_slug: str, author_name: str) -> Optional[float]:
    """
    Берём Win rate автора с кэшем в state.json.

    state["authors"] имеет структуру:
      {
        "<slug>": {
          "name": "...",
          "winrate_percent": 47.94,
          "updated_at": <timestamp>
        },
        ...
      }

    Если в кэше есть запись свежее 6 часов — возвращаем её.
    Иначе скачиваем профиль, парсим Win rate, обновляем кэш.
    """
    authors_cache: Dict[str, dict] = state.setdefault("authors", {})
    now_ts = time.time()
    max_age_seconds = 6 * 60 * 60  # 6 часов

    entry = authors_cache.get(author_slug)
    if entry:
        updated_at = entry.get("updated_at", 0)
        if now_ts - updated_at <= max_age_seconds:
            wr = entry.get("winrate_percent")
            if isinstance(wr, (int, float)):
                return float(wr)

    # нужно обновить из сети
    profile_url = f"{BASE_URL}/user/profile/{author_slug}"
    try:
        html = fetch_page(profile_url)
    except Exception as e:
        print(f"[WARN] Не удалось скачать профиль автора {author_slug}: {e}")
        return None

    winrate = safe_parse_author_winrate(html, author_slug)
    if winrate is not None:
        authors_cache[author_slug] = {
            "name": author_name,
            "winrate_percent": float(winrate),
            "updated_at": now_ts,
        }
        # не забываем сохранять state снаружи (вызывающий код сделает save_state)

    return winrate

# ===== 13. Безопасные обёртки вокруг парсеров (логирование) =====

def safe_parse_today_matches(html: str) -> List[Tuple[str, str, str]]:
    try:
        matches = safe_parse_today_matches(html)
        return matches
    except Exception as e:
        print(f"[ERROR] Ошибка парсинга списка матчей: {e}")
        return []


def safe_parse_today_matches(html: str) -> List[Tuple[str, str, str]]:
    try:
        matches = parse_today_matches(html)
        return matches
    except Exception as e:
        print(f"[ERROR] Ошибка парсинга списка матчей: {e}")
        return []


def safe_parse_author_winrate(html: str, slug: str) -> Optional[float]:
    try:
        return parse_author_winrate(html)
    except Exception as e:
        print(f"[ERROR] Ошибка парсинга winrate автора {slug}: {e}")
        return None

# ===== 9. Сбор ставок по всем матчам на сегодня =====

def collect_today_tips_with_winrates(state: dict) -> List[TipOnMatch]:
    """
    Главная функция сбора:
      - грузим TODAY_FOOTBALL_URL
      - парсим список матчей
      - для каждого матча:
          - грузим страницу матча
          - парсим все tip'ы (TipOnMatch)
          - подставляем kickoff_utc из списка матчей
          - для каждого автора подтягиваем Win rate (с кэшем)
      - возвращаем общий список всех TipOnMatch за сегодня
    """
    print(f"[INFO] Загружаем список сегодняшних матчей: {TODAY_FOOTBALL_URL}")
    try:
        html = fetch_page(TODAY_FOOTBALL_URL)
    except Exception as e:
        print(f"[ERROR] Не удалось загрузить список матчей: {e}")
        return []

    matches = parse_today_matches(html)
    print(f"[INFO] Найдено матчей на сегодня: {len(matches)}")

    all_tips: List[TipOnMatch] = []

    for rel_url, match_title, kickoff_str in matches:
        kickoff_utc = parse_kickoff_datetime_today(kickoff_str)
        print(f"[INFO] Матч: {match_title} ({rel_url}), время {kickoff_str}, utc={kickoff_utc}")

        try:
            match_html = fetch_page(rel_url)
        except Exception as e:
            print(f"[WARN] Не удалось загрузить страницу матча {rel_url}: {e}")
            continue

        tips = safe_parse_match_tips(match_html, rel_url, match_title)
        print(f"[DEBUG] Для матча {match_title} нашли tip'ов: {len(tips)}")
        if not tips:
            continue

        if len(tips) < MIN_TIPS_PER_MATCH:
            print(f"[INFO] Матч {match_title} ({rel_url}) пропущен: мало tip'ов ({len(tips)})")
            continue

        # Для каждого tip'а достанем/обновим winrate автора и выставим время матча
        for tip in tips:
            tip.kickoff_utc = kickoff_utc

            wr = get_author_winrate(state, tip.author_slug, tip.author_name)
            tip.author_winrate = wr

        all_tips.extend(tips)

        # после обработки каждого матча сразу сохраняем state (кэш winrate авторов)
        save_state(state)

    print(f"[INFO] Собрано всего tip'ов за сегодня: {len(all_tips)}")
    return all_tips
# ===== 10. Группировка ставок и расчёт комбинированной вероятности =====

def group_tips_to_signals(tips: List[TipOnMatch]) -> List[Signal]:
    """
    На входе: все TipOnMatch за сегодня (с уже заполненным author_winrate и kickoff_utc).
    Задача:
      - сгруппировать по (match_url, selection_group_key)
      - внутри группы взять всех авторов с известным winrate
      - посчитать комбинированную вероятность:
          p_i = winrate_i / 100
          P = 1 - Π(1 - p_i)
      - отфильтровать только те группы, где:
          - авторов с winrate >= MIN_AUTHORS_PER_SIGNAL
          - P * 100 >= WINRATE_THRESHOLD_PERCENT
    Возвращаем список Signal.
    """
    # 1) склеиваем по ключу (match_url, selection_group_key)
    grouped: Dict[Tuple[str, str], List[TipOnMatch]] = {}
    for tip in tips:
        if not tip.selection_group_key:
            continue
        key = (tip.match_url, tip.selection_group_key)
        grouped.setdefault(key, []).append(tip)

    signals: List[Signal] = []

    for (match_url, group_key), group_tips in grouped.items():
        # отфильтруем авторов, у кого есть winrate
        probs: List[float] = []
        authors: List[str] = []
        selections_raw: List[str] = []

        match_title = group_tips[0].match_title
        kickoff_utc = group_tips[0].kickoff_utc

        for tip in group_tips:
            if tip.author_winrate is None:
                continue
            p = float(tip.author_winrate) / 100.0
            if p <= 0.0:
                continue

            probs.append(p)
            authors.append(tip.author_name)
            selections_raw.append(tip.selection_raw)

        if len(probs) < MIN_AUTHORS_PER_SIGNAL:
            continue

        combined_p = combine_independent_probabilities(probs)
        combined_percent = combined_p * 100.0

        if combined_percent < WINRATE_THRESHOLD_PERCENT:
            continue

        # переводим время в московский, если есть
        kickoff_moscow = None
        if kickoff_utc is not None:
            kickoff_moscow = kickoff_utc.astimezone(MOSCOW_TZ)

        signal = Signal(
            match_url=match_url,
            match_title=match_title,
            kickoff_utc=kickoff_utc,
            kickoff_moscow=kickoff_moscow,
            selection_group_key=group_key,
            selections_raw=selections_raw,
            authors=authors,
            combined_win_chance_percent=combined_percent,
        )
        signals.append(signal)

    return signals
# ===== 11. Форматирование сигнала и отправка в Telegram =====

def format_signal_message(sig: Signal) -> str:
    """
    Делаем короткое сообщение по твоей структуре:

      матч
      время матча
      ставки (каждая с новой строки, без имён авторов)
      процент победы

    Пример:

      ND Triglav vs NK Krka
      Время: 17:00 (МСК)

      Ставки:
      NK Krka +0.75 (AH)
      NK Krka +0.75 (AH)
      NK Krka win

      Комбинированный шанс: 99.3%

    """
    lines: List[str] = []

    # 1) матч
    lines.append(sig.match_title)

    # 2) время матча (если смогли определить)
    if sig.kickoff_moscow is not None:
        local_str = sig.kickoff_moscow.strftime("%H:%M %d.%m.%Y")
        lines.append(f"Время: {local_str} (МСК)")
    elif sig.kickoff_utc is not None:
        utc_str = sig.kickoff_utc.strftime("%H:%M %d.%m.%Y UTC")
        lines.append(f"Время: {utc_str}")
    else:
        lines.append("Время: неизвестно")

    lines.append("")  # пустая строка

    # 3) ставки (подряд без имён авторов)
    lines.append("Ставки:")
    # чтобы не было дубликатов по 10 раз — слегка уберём повторы, но сохраним порядок
    seen = set()
    for sel in sig.selections_raw:
        if sel not in seen:
            seen.add(sel)
            lines.append(sel)

    lines.append("")  # пустая строка

    # 4) процент победы
    chance_str = f"{sig.combined_win_chance_percent:.2f}%"
    lines.append(f"Комбинированный шанс: {chance_str}")

    # Можно добавить ссылку на матч
    full_match_url = BASE_URL + sig.match_url
    lines.append(full_match_url)

    return "\n".join(lines)


def send_signals_to_telegram(signals: List[Signal], state: dict) -> None:
    """
    Отправляем все новые сигналы в Telegram.
    Чтобы не спамить одинаковым матчем/исходом — запоминаем отправленные сигналы в state.
    Ключ: match_url + selection_group_key + дата.

    Дополнительно: ограничиваем общее число сигналов в день MAX_SIGNALS_PER_DAY.
    """
    sent_signals: Dict[str, dict] = state.setdefault("sent_signals", {})

    today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_bucket: Dict[str, bool] = sent_signals.setdefault(today_key, {})

    already_sent_count = len(day_bucket)
    remaining = max(0, MAX_SIGNALS_PER_DAY - already_sent_count)

    if remaining <= 0:
        print(f"[INFO] Дневной лимит сигналов ({MAX_SIGNALS_PER_DAY}) уже исчерпан.")
        return

    # Чтобы сначала отправлять самые сильные – отсортируем по убыванию шанса
    signals_sorted = sorted(
        signals,
        key=lambda s: s.combined_win_chance_percent,
        reverse=True,
    )

    sent_now = 0

    for sig in signals_sorted:
        if sent_now >= remaining:
            print(f"[INFO] Достигнут лимит отправки сигналов на сегодня: {MAX_SIGNALS_PER_DAY}")
            break

        uniq = f"{sig.match_url}||{sig.selection_group_key}"
        if day_bucket.get(uniq):
            # уже отправляли сегодня
            continue

        text = format_signal_message(sig)
        try:
            print(
                f"[INFO] Отправляем сигнал по матчу {sig.match_title} "
                f"(шанс {sig.combined_win_chance_percent:.2f}%)"
            )
            send_telegram_message(text)
            day_bucket[uniq] = True
            save_state(state)
            sent_now += 1
        except Exception as e:
            print(f"[ERROR] Не удалось отправить сигнал в Telegram: {e}")

def run_single_iteration() -> None:
    """
    Одна полная итерация:
      - загружаем state.json
      - собираем все ставки за сегодня с winrate авторов
      - группируем в сигналы
      - отправляем новые сигналы в Telegram
    """
    print("=" * 60)
    print(f"[INFO] Запуск проверки в {datetime.now(MOSCOW_TZ).strftime('%H:%M:%S %d.%m.%Y (%Z)')}")

    state = load_state()

    tips = collect_today_tips_with_winrates(state)
    if not tips:
        print("[INFO] Нет tip'ов за сегодня или не удалось их собрать.")
        return

    signals = group_tips_to_signals(tips)
    print(f"[INFO] Сформировано сигналов (до фильтра по уже отправленным): {len(signals)}")

    if not signals:
        print("[INFO] Нет сигналов, прошедших порог вероятности и кол-во авторов.")
        return

    send_signals_to_telegram(signals, state)


def main_loop() -> None:
    """
    Бесконечный цикл для Railway:
      - запускаем run_single_iteration()
      - спим CHECK_INTERVAL_MINUTES
    """
    interval_sec = max(1, int(CHECK_INTERVAL_MINUTES * 60))
    print(f"[INFO] Старт главного цикла. Интервал: {CHECK_INTERVAL_MINUTES} минут.")

    while True:
        try:
            run_single_iteration()
        except Exception as e:
            # Чтобы воркер не падал полностью из‑за одной ошибки
            print(f"[FATAL] Необработанное исключение в итерации: {e}")
        print(f"[INFO] Спим {CHECK_INTERVAL_MINUTES} минут...")
        time.sleep(interval_sec)


if __name__ == "__main__":
    main_loop()
