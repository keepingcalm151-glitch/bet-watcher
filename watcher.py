# watcher.py

import json
import os
import hashlib
import requests
from bs4 import BeautifulSoup

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

# 4. Основная логика проверки сайтов

def check_sites_once():
    state = load_state()
    changes_found = []

    for site in SITES:
        name = site["name"]
        url = site["url"]
        key = url  # ключ в state

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

        new_hash = calc_hash(new_html)
        old_entry = state.get(key)

        if old_entry is None:
            print(f"[INFO] Для {url} ещё нет сохранённой версии. Сохраняю впервые.")
            state[key] = {
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

        changes_found.append({
            "name": name,
            "url": url,
            "diff": diff_text
        })

        state[key] = {
            "hash": new_hash,
            "html": new_html,
            "tips_count": parsed_profile.get("tips_count") if parsed_profile else None
        }

    save_state(state)

    if changes_found:
        parts = []
        for ch in changes_found:
            part = (
                f"<b>Изменения на сайте:</b> {ch['name']}\n"
                f"URL: {ch['url']}\n\n"
                f"{ch['diff']}\n"
                f"{'-'*40}"
            )
            parts.append(part)

        full_message = "\n\n".join(parts)

        try:
            send_telegram_message(full_message)
            print("[INFO] Уведомление в Telegram отправлено.")
        except Exception as e:
            print(f"[ERROR] Не удалось отправить сообщение в Telegram: {e}")
    else:
        print("[INFO] Изменений ни на одном сайте не найдено.")

# 5. Точка входа

if __name__ == "__main__":
    check_sites_once()
