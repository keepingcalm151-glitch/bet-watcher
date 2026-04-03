# watcher.py
#
# Новый воркер для Railway:
#   Procfile: worker: python watcher.py
#
# Логика:
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
    slug: str
    name: str
    winrate_percent: Optional[float] = None
    winrate_updated_at: Optional[float] = None


@dataclass
class TipOnMatch:
    match_url: str
    match_title: str
    kickoff_utc: Optional[datetime]
    selection_raw: str
    selection_group_key: str
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
    ps — числа в диапазоне [0,1].
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
    Нормализует исход ставки для группировки похожих.
    """
    if not selection:
        return ""

    text = selection.strip()
    lower = text.lower()

    # Draw No Bet
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

    # Азиатский гандикап с командой
    m_ah_team = re.match(r"(.+?)\s+[+-]?\d+(\.\d+)?\s*\(ah\)", text, flags=re.IGNORECASE)
    if m_ah_team:
        team = m_ah_team.group(1).strip()
        return f"{team} (AH)"

    # Азиатский гандикап без команды
    m_ah = re.match(r"[+-]?\d+(\.\d+)?\s*\(ah\)", text, flags=re.IGNORECASE)
    if m_ah:
        return "+(AH)"

    # Team to win
    m_win = re.match(r"(.+?)\s+to\s+win\b", text, flags=re.IGNORECASE)
    if m_win:
        team = m_win.group(1).strip()
        return f"{team} win"

    # Over / Under без goals
    m_over_any = re.match(r"\s*over\s+[\d.,]+", lower)
    if m_over_any:
        return "Over"

    m_under_any = re.match(r"\s*under\s+[\d.,]+", lower)
    if m_under_any:
        return "Under"

    return text


# ===== 6. Парсинг списка сегодняшних матчей (страница football) =====

def parse_today_matches(html: str) -> List[Tuple[str, str, str]]:
    """
    Парсим страницу с сегодняшними матчами (например /football).
    Возвращаем список: (relative_url, match_title, kickoff_time_str)
    """
    soup = BeautifulSoup(html, "html.parser")
    results: List[Tuple[str, str, str]] = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("/football/"):
            continue

        time_div = a.find(
            "div",
            class_=lambda x: x and "font-os" in x.split() and "text-lg" in x.split()
        )
        if not time_div:
            continue
        kickoff_str = time_div.get_text(strip=True)

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
    "17:00" -> datetime на сегодня в таймзоне BETTINGEXPERT_TZ, затем в UTC.
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

    return dt.astimezone(timezone.utc)


# ===== 7. Парсинг страницы матча: ставки и авторы =====

def parse_match_tips(html: str, match_url: str, match_title: str) -> List[TipOnMatch]:
    """
    Парсим страницу конкретного матча /football/...

    Ищем такие блоки:

      <div class="font-gc font-bold h-[48px] ... text-[22px]">
          Union to win
      </div>
      ...
      <div class="text-xs text-grey3 italic font-gc">
          6 hours ago by
      </div>
      <div class="flex flex-row items-center text-red-felix text-xs font-gc font-semibold">
          <img ...>
          <div class="mr-1">logancosta</div>
          <svg ...star...>
      </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    tips: List[TipOnMatch] = []

    # Ищем все div'ы с текстом ставки
    selection_divs = soup.find_all(
        "div",
        class_=lambda x: x
        and "font-gc" in x.split()
        and "font-bold" in x.split()
        and "h-[48px]" in x.split()
    )

    def extract_nearby_odds(container) -> Optional[float]:
        # Ищем текст "odds 1.95" или просто число после слова odds в пределах контейнера
        text = container.get_text(" ", strip=True).replace(",", ".")
        m = re.search(r"odds\s+(\d+(?:\.\d+)?)", text, flags=re.IGNORECASE)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                return None
        # fallback — любое правдоподобное число
        for tok in reversed(text.split()):
            try:
                val = float(tok)
                if 1.01 <= val <= 100.0:
                    return val
            except ValueError:
                continue
        return None

    for sel_div in selection_divs:
        selection_raw = sel_div.get_text(strip=True)
        if not selection_raw:
            continue

        # поднимаемся до контейнера tip'а
        container = sel_div
        for _ in range(5):
            if container.parent is None:
                break
            container = container.parent

        # ищем div с "6 hours ago by", "15m ago by" и т.п.
        by_label = None
        for small in container.find_all("div"):
            txt = small.get_text(" ", strip=True).lower()
            if "ago by" in txt:
                by_label = small
                break

        if by_label:
            # блок автора сразу после "ago by"
            author_block = by_label.find_next(
                "div",
                class_=lambda x: x
                and "flex" in x.split()
                and "items-center" in x.split()
                and "text-red-felix" in x
            )
        else:
            author_block = None

        author_name = None
        author_slug = None

        if author_block:
            # имя автора в <div class="mr-1">...</div>
            name_div = author_block.find(
                "div",
                class_=lambda x: x and "mr-1" in x.split()
            )
            if name_div:
                author_name = name_div.get_text(strip=True)

        # если ссылки на профиль нет – делаем slug из имени
        if author_name and not author_slug:
            author_slug = re.sub(r"[^a-zA-Z0-9_-]+", "", author_name.strip())

        if not author_name or not author_slug:
            # не нашли автора — пропускаем tip
            continue

        odds = extract_nearby_odds(container)
        group_key = normalize_selection_for_grouping(selection_raw)

        tip = TipOnMatch(
            match_url=match_url,
            match_title=match_title,
            kickoff_utc=None,
            selection_raw=selection_raw,
            selection_group_key=group_key,
            author_slug=author_slug,
            author_name=author_name,
            author_winrate=None,
            odds=odds,
        )
        tips.append(tip)

    return tips


def safe_parse_match_tips(html: str, match_url: str, match_title: str) -> List[TipOnMatch]:
    """
    Безопасный парсер tip'ов: ловим исключения, чтобы из-за одного матча
    не падала вся итерация.
    """
    try:
        return parse_match_tips(html, match_url, match_title)
    except Exception as e:
        print(f"[ERROR] Ошибка парсинга tip'ов для матча {match_url}: {e}")
        return []


# ===== 8. Парсинг профиля автора: Win rate =====

def parse_author_winrate(html: str) -> Optional[float]:
    """
    Из HTML профиля tipster'а достаём Win rate в процентах.
    """
    soup = BeautifulSoup(html, "html.parser")

    label = soup.find(
        lambda tag: tag.name in ("div", "span")
        and tag.get_text(strip=True).lower() == "win rate"
    )
    if not label or not label.parent:
        return None

    prev = label.previous_sibling
    while prev is not None and getattr(prev, "name", None) is None:
        prev = prev.previous_sibling

    if not prev:
        prev = label.parent.previous_sibling
        while prev is not None and getattr(prev, "name", None) is None:
            prev = prev.previous_sibling

    if not prev:
        return None

    text = prev.get_text(" ", strip=True)
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", text)
    if not m:
        return None

    val_str = m.group(1).replace(",", ".")
    try:
        val = float(val_str)
    except ValueError:
        return None

    if val < 0.0 or val > 100.0:
        return None

    return val


def get_author_winrate(state: dict, author_slug: str, author_name: str) -> Optional[float]:
    """
    Берём Win rate автора с кэшем в state.json.
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

    return winrate


# ===== 13. Безопасные обёртки вокруг парсеров (логирование) =====

def safe_parse_today_matches(html: str) -> List[Tuple[str, str, str]]:
    try:
        return parse_today_matches(html)
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
    Главная функция сбора tip'ов за сегодня.
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

        for tip in tips:
            tip.kickoff_utc = kickoff_utc
            wr = get_author_winrate(state, tip.author_slug, tip.author_name)
            tip.author_winrate = wr

        all_tips.extend(tips)
        save_state(state)

    print(f"[INFO] Собрано всего tip'ов за сегодня: {len(all_tips)}")
    return all_tips


# ===== 10. Группировка ставок и расчёт комбинированной вероятности =====

def group_tips_to_signals(tips: List[TipOnMatch]) -> List[Signal]:
    """
    Группировка tip'ов в сигналы.
    """
    grouped: Dict[Tuple[str, str], List[TipOnMatch]] = {}
    for tip in tips:
        if not tip.selection_group_key:
            continue
        key = (tip.match_url, tip.selection_group_key)
        grouped.setdefault(key, []).append(tip)

    signals: List[Signal] = []

    for (match_url, group_key), group_tips in grouped.items():
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
    Формируем текст сообщения для Telegram.
    """
    lines: List[str] = []

    lines.append(sig.match_title)

    if sig.kickoff_moscow is not None:
        local_str = sig.kickoff_moscow.strftime("%H:%M %d.%m.%Y")
        lines.append(f"Время: {local_str} (МСК)")
    elif sig.kickoff_utc is not None:
        utc_str = sig.kickoff_utc.strftime("%H:%M %d.%m.%Y UTC")
        lines.append(f"Время: {utc_str}")
    else:
        lines.append("Время: неизвестно")

    lines.append("")
    lines.append("Ставки:")

    seen = set()
    for sel in sig.selections_raw:
        if sel not in seen:
            seen.add(sel)
            lines.append(sel)

    lines.append("")
    chance_str = f"{sig.combined_win_chance_percent:.2f}%"
    lines.append(f"Комбинированный шанс: {chance_str}")

    full_match_url = BASE_URL + sig.match_url
    lines.append(full_match_url)

    return "\n".join(lines)


def send_signals_to_telegram(signals: List[Signal], state: dict) -> None:
    """
    Отправляем новые сигналы в Telegram (с лимитом в день).
    """
    sent_signals: Dict[str, dict] = state.setdefault("sent_signals", {})

    today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_bucket: Dict[str, bool] = sent_signals.setdefault(today_key, {})

    already_sent_count = len(day_bucket)
    remaining = max(0, MAX_SIGNALS_PER_DAY - already_sent_count)

    if remaining <= 0:
        print(f"[INFO] Дневной лимит сигналов ({MAX_SIGNALS_PER_DAY}) уже исчерпан.")
        return

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
    Одна полная итерация.
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
    Бесконечный цикл для Railway.
    """
    interval_sec = max(1, int(CHECK_INTERVAL_MINUTES * 60))
    print(f"[INFO] Старт главного цикла. Интервал: {CHECK_INTERVAL_MINUTES} минут.")

    while True:
        try:
            run_single_iteration()
        except Exception as e:
            print(f"[FATAL] Необработанное исключение в итерации: {e}")
        print(f"[INFO] Спим {CHECK_INTERVAL_MINUTES} минут...")
        time.sleep(interval_sec)


if __name__ == "__main__":
    main_loop()
