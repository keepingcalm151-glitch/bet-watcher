{\rtf1\ansi\ansicpg1251\cocoartf2868
\cocoatextscaling0\cocoaplatform0{\fonttbl\f0\fswiss\fcharset0 Helvetica;}
{\colortbl;\red255\green255\blue255;}
{\*\expandedcolortbl;;}
\paperw11900\paperh16840\margl1440\margr1440\vieww11520\viewh8400\viewkind0
\pard\tx720\tx1440\tx2160\tx2880\tx3600\tx4320\tx5040\tx5760\tx6480\tx7200\tx7920\tx8640\pardirnatural\partightenfactor0

\f0\fs24 \cf0 # watcher.py\
\
import json\
import os\
import hashlib\
import requests\
\
CONFIG_PATH = "config.json"\
STATE_PATH = "state.json"\
\
# 1. \uc0\u1047 \u1072 \u1075 \u1088 \u1091 \u1079 \u1082 \u1072  \u1082 \u1086 \u1085 \u1092 \u1080 \u1075 \u1091 \u1088 \u1072 \u1094 \u1080 \u1080 \
if os.getenv("CONFIG_JSON"):\
    # \uc0\u1041 \u1077 \u1088 \u1105 \u1084  \u1082 \u1086 \u1085 \u1092 \u1080 \u1075  \u1080 \u1079  \u1087 \u1077 \u1088 \u1077 \u1084 \u1077 \u1085 \u1085 \u1086 \u1081  \u1086 \u1082 \u1088 \u1091 \u1078 \u1077 \u1085 \u1080 \u1103  (\u1085 \u1072  Railway)\
    config = json.loads(os.getenv("CONFIG_JSON"))\
else:\
    # \uc0\u1056 \u1077 \u1079 \u1077 \u1088 \u1074 \u1085 \u1099 \u1081  \u1074 \u1072 \u1088 \u1080 \u1072 \u1085 \u1090  \'96 \u1080 \u1079  \u1092 \u1072 \u1081 \u1083 \u1072  (\u1077 \u1089 \u1083 \u1080  \u1079 \u1072 \u1087 \u1091 \u1089 \u1082 \u1072 \u1077 \u1096 \u1100  \u1083 \u1086 \u1082 \u1072 \u1083 \u1100 \u1085 \u1086 )\
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:\
        config = json.load(f)\
\
OPENAI_API_KEY = config["openai_api_key"]\
TELEGRAM_BOT_TOKEN = config["telegram_bot_token"]\
TELEGRAM_CHAT_ID = config["telegram_chat_id"]\
SITES = config["sites"]\
\
# 2. \uc0\u1056 \u1072 \u1073 \u1086 \u1090 \u1072  \u1089  \u1083 \u1086 \u1082 \u1072 \u1083 \u1100 \u1085 \u1099 \u1084  \u1089 \u1086 \u1089 \u1090 \u1086 \u1103 \u1085 \u1080 \u1077 \u1084  (state.json)\
\
def load_state():\
    if not os.path.exists(STATE_PATH):\
        return \{\}\
    with open(STATE_PATH, "r", encoding="utf-8") as f:\
        try:\
            return json.load(f)\
        except json.JSONDecodeError:\
            return \{\}\
\
def save_state(state):\
    with open(STATE_PATH, "w", encoding="utf-8") as f:\
        json.dump(state, f, ensure_ascii=False, indent=2)\
\
# 3. \uc0\u1059 \u1090 \u1080 \u1083 \u1080 \u1090 \u1099 \
\
def fetch_page(url: str) -> str:\
    """\
    \uc0\u1057 \u1082 \u1072 \u1095 \u1080 \u1074 \u1072 \u1077 \u1084  HTML \u1089 \u1090 \u1088 \u1072 \u1085 \u1080 \u1094 \u1099 .\
    """\
    headers = \{\
        "User-Agent": (\
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "\
            "AppleWebKit/537.36 (KHTML, like Gecko) "\
            "Chrome/122.0 Safari/537.36"\
        )\
    \}\
    resp = requests.get(url, headers=headers, timeout=30)\
    resp.raise_for_status()\
    return resp.text\
\
def calc_hash(text: str) -> str:\
    """\
    \uc0\u1061 \u1101 \u1096  \u1086 \u1090  \u1090 \u1077 \u1082 \u1089 \u1090 \u1072  \u1076 \u1083 \u1103  \u1073 \u1099 \u1089 \u1090 \u1088 \u1086 \u1075 \u1086  \u1089 \u1088 \u1072 \u1074 \u1085 \u1077 \u1085 \u1080 \u1103 .\
    """\
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()\
\
def send_telegram_message(text: str):\
    """\
    \uc0\u1054 \u1090 \u1087 \u1088 \u1072 \u1074 \u1082 \u1072  \u1089 \u1086 \u1086 \u1073 \u1097 \u1077 \u1085 \u1080 \u1103  \u1074  Telegram.\
    """\
    url = f"https://api.telegram.org/bot\{TELEGRAM_BOT_TOKEN\}/sendMessage"\
    payload = \{\
        "chat_id": TELEGRAM_CHAT_ID,\
        "text": text,\
        "parse_mode": "HTML"\
    \}\
    resp = requests.post(url, json=payload, timeout=30)\
    resp.raise_for_status()\
\
def call_openai_diff(old_html: str, new_html: str, site_name: str, site_url: str) -> str:\
    """\
    \uc0\u1054 \u1090 \u1087 \u1088 \u1072 \u1074 \u1083 \u1103 \u1077 \u1084  \u1089 \u1090 \u1072 \u1088 \u1091 \u1102  \u1080  \u1085 \u1086 \u1074 \u1091 \u1102  \u1074 \u1077 \u1088 \u1089 \u1080 \u1102  \u1089 \u1090 \u1088 \u1072 \u1085 \u1080 \u1094 \u1099  \u1074  OpenAI \u1080  \u1087 \u1086 \u1083 \u1091 \u1095 \u1072 \u1077 \u1084  \u1086 \u1087 \u1080 \u1089 \u1072 \u1085 \u1080 \u1077  \u1080 \u1079 \u1084 \u1077 \u1085 \u1077 \u1085 \u1080 \u1081 .\
    """\
    api_url = "https://api.openai.com/v1/chat/completions"\
\
    system_prompt = (\
        "\uc0\u1058 \u1099  \u1072 \u1085 \u1072 \u1083 \u1080 \u1079 \u1080 \u1088 \u1091 \u1077 \u1096 \u1100  \u1080 \u1079 \u1084 \u1077 \u1085 \u1077 \u1085 \u1080 \u1103  \u1085 \u1072  \u1074 \u1077 \u1073 -\u1089 \u1090 \u1088 \u1072 \u1085 \u1080 \u1094 \u1077 . "\
        "\uc0\u1058 \u1077 \u1073 \u1077  \u1076 \u1072 \u1102 \u1090  \u1089 \u1090 \u1072 \u1088 \u1091 \u1102  \u1080  \u1085 \u1086 \u1074 \u1091 \u1102  \u1074 \u1077 \u1088 \u1089 \u1080 \u1102  HTML. "\
        "\uc0\u1054 \u1087 \u1080 \u1096 \u1080  \u1090 \u1086 \u1083 \u1100 \u1082 \u1086  \u1074 \u1072 \u1078 \u1085 \u1099 \u1077  \u1080 \u1079 \u1084 \u1077 \u1085 \u1077 \u1085 \u1080 \u1103  \u1087 \u1088 \u1086 \u1089 \u1090 \u1099 \u1084  \u1088 \u1091 \u1089 \u1089 \u1082 \u1080 \u1084  \u1103 \u1079 \u1099 \u1082 \u1086 \u1084 , "\
        "\uc0\u1082 \u1086 \u1088 \u1086 \u1090 \u1082 \u1086  \u1080  \u1087 \u1086  \u1076 \u1077 \u1083 \u1091 . \u1048 \u1075 \u1085 \u1086 \u1088 \u1080 \u1088 \u1091 \u1081  \u1082 \u1086 \u1089 \u1084 \u1077 \u1090 \u1080 \u1095 \u1077 \u1089 \u1082 \u1080 \u1077 /\u1090 \u1077 \u1093 \u1085 \u1080 \u1095 \u1077 \u1089 \u1082 \u1080 \u1077  \u1087 \u1088 \u1072 \u1074 \u1082 \u1080 ."\
    )\
\
    user_prompt = (\
        f"\uc0\u1057 \u1072 \u1081 \u1090 : \{site_name\}\\nURL: \{site_url\}\\n\\n"\
        "\uc0\u1057 \u1090 \u1072 \u1088 \u1072 \u1103  \u1074 \u1077 \u1088 \u1089 \u1080 \u1103  HTML:\\n"\
        "-------------------\\n"\
        f"\{old_html[:15000]\}\\n\\n"\
        "\uc0\u1053 \u1086 \u1074 \u1072 \u1103  \u1074 \u1077 \u1088 \u1089 \u1080 \u1103  HTML:\\n"\
        "-------------------\\n"\
        f"\{new_html[:15000]\}\\n\\n"\
        "\uc0\u1054 \u1087 \u1080 \u1096 \u1080  \u1074 \u1072 \u1078 \u1085 \u1099 \u1077  \u1080 \u1079 \u1084 \u1077 \u1085 \u1077 \u1085 \u1080 \u1103  (\u1084 \u1072 \u1082 \u1089 \u1080 \u1084 \u1091 \u1084  10 \u1087 \u1091 \u1085 \u1082 \u1090 \u1086 \u1074 , \u1077 \u1089 \u1083 \u1080  \u1080 \u1093  \u1084 \u1085 \u1086 \u1075 \u1086 )."\
    )\
\
    headers = \{\
        "Authorization": f"Bearer \{OPENAI_API_KEY\}",\
        "Content-Type": "application/json"\
    \}\
\
    data = \{\
        "model": "gpt-4.1-mini",\
        "messages": [\
            \{"role": "system", "content": system_prompt\},\
            \{"role": "user", "content": user_prompt\}\
        ],\
        "temperature": 0.2\
    \}\
\
    resp = requests.post(api_url, headers=headers, json=data, timeout=60)\
    resp.raise_for_status()\
    result = resp.json()\
\
    try:\
        return result["choices"][0]["message"]["content"]\
    except Exception:\
        return "\uc0\u1053 \u1077  \u1091 \u1076 \u1072 \u1083 \u1086 \u1089 \u1100  \u1082 \u1086 \u1088 \u1088 \u1077 \u1082 \u1090 \u1085 \u1086  \u1088 \u1072 \u1079 \u1086 \u1073 \u1088 \u1072 \u1090 \u1100  \u1086 \u1090 \u1074 \u1077 \u1090  \u1084 \u1086 \u1076 \u1077 \u1083 \u1080 ."\
\
# 4. \uc0\u1054 \u1089 \u1085 \u1086 \u1074 \u1085 \u1072 \u1103  \u1083 \u1086 \u1075 \u1080 \u1082 \u1072  \u1087 \u1088 \u1086 \u1074 \u1077 \u1088 \u1082 \u1080  \u1089 \u1072 \u1081 \u1090 \u1086 \u1074 \
\
def check_sites_once():\
    state = load_state()\
    changes_found = []\
\
    for site in SITES:\
        name = site["name"]\
        url = site["url"]\
        key = url  # \uc0\u1082 \u1083 \u1102 \u1095  \u1074  state\
\
        print(f"[INFO] \uc0\u1055 \u1088 \u1086 \u1074 \u1077 \u1088 \u1103 \u1102  \u1089 \u1072 \u1081 \u1090 : \{name\} (\{url\})")\
\
        try:\
            new_html = fetch_page(url)\
        except Exception as e:\
            print(f"[ERROR] \uc0\u1053 \u1077  \u1091 \u1076 \u1072 \u1083 \u1086 \u1089 \u1100  \u1089 \u1082 \u1072 \u1095 \u1072 \u1090 \u1100  \{url\}: \{e\}")\
            continue\
\
        new_hash = calc_hash(new_html)\
        old_entry = state.get(key)\
\
        if old_entry is None:\
            print(f"[INFO] \uc0\u1044 \u1083 \u1103  \{url\} \u1077 \u1097 \u1105  \u1085 \u1077 \u1090  \u1089 \u1086 \u1093 \u1088 \u1072 \u1085 \u1105 \u1085 \u1085 \u1086 \u1081  \u1074 \u1077 \u1088 \u1089 \u1080 \u1080 . \u1057 \u1086 \u1093 \u1088 \u1072 \u1085 \u1103 \u1102  \u1074 \u1087 \u1077 \u1088 \u1074 \u1099 \u1077 .")\
            state[key] = \{\
                "hash": new_hash,\
                "html": new_html\
            \}\
            continue\
\
        old_hash = old_entry.get("hash")\
        old_html = old_entry.get("html", "")\
\
        if old_hash == new_hash:\
            print(f"[INFO] \uc0\u1048 \u1079 \u1084 \u1077 \u1085 \u1077 \u1085 \u1080 \u1081  \u1085 \u1072  \{url\} \u1085 \u1077  \u1085 \u1072 \u1081 \u1076 \u1077 \u1085 \u1086 .")\
            continue\
\
        print(f"[INFO] \uc0\u1054 \u1041 \u1053 \u1040 \u1056 \u1059 \u1046 \u1045 \u1053 \u1067  \u1048 \u1047 \u1052 \u1045 \u1053 \u1045 \u1053 \u1048 \u1071  \u1085 \u1072  \{url\}!")\
\
        try:\
            diff_text = call_openai_diff(old_html, new_html, name, url)\
        except Exception as e:\
            print(f"[ERROR] \uc0\u1054 \u1096 \u1080 \u1073 \u1082 \u1072  \u1087 \u1088 \u1080  \u1086 \u1073 \u1088 \u1072 \u1097 \u1077 \u1085 \u1080 \u1080  \u1082  OpenAI \u1076 \u1083 \u1103  \{url\}: \{e\}")\
            diff_text = "\uc0\u1054 \u1096 \u1080 \u1073 \u1082 \u1072  \u1087 \u1088 \u1080  \u1079 \u1072 \u1087 \u1088 \u1086 \u1089 \u1077  \u1082  OpenAI."\
\
        changes_found.append(\{\
            "name": name,\
            "url": url,\
            "diff": diff_text\
        \})\
\
        state[key] = \{\
            "hash": new_hash,\
            "html": new_html\
        \}\
\
    save_state(state)\
\
    if changes_found:\
        parts = []\
        for ch in changes_found:\
            part = (\
                f"<b>\uc0\u1048 \u1079 \u1084 \u1077 \u1085 \u1077 \u1085 \u1080 \u1103  \u1085 \u1072  \u1089 \u1072 \u1081 \u1090 \u1077 :</b> \{ch['name']\}\\n"\
                f"URL: \{ch['url']\}\\n\\n"\
                f"\{ch['diff']\}\\n"\
                f"\{'-'*40\}"\
            )\
            parts.append(part)\
\
        full_message = "\\n\\n".join(parts)\
\
        try:\
            send_telegram_message(full_message)\
            print("[INFO] \uc0\u1059 \u1074 \u1077 \u1076 \u1086 \u1084 \u1083 \u1077 \u1085 \u1080 \u1077  \u1074  Telegram \u1086 \u1090 \u1087 \u1088 \u1072 \u1074 \u1083 \u1077 \u1085 \u1086 .")\
        except Exception as e:\
            print(f"[ERROR] \uc0\u1053 \u1077  \u1091 \u1076 \u1072 \u1083 \u1086 \u1089 \u1100  \u1086 \u1090 \u1087 \u1088 \u1072 \u1074 \u1080 \u1090 \u1100  \u1089 \u1086 \u1086 \u1073 \u1097 \u1077 \u1085 \u1080 \u1077  \u1074  Telegram: \{e\}")\
    else:\
        print("[INFO] \uc0\u1048 \u1079 \u1084 \u1077 \u1085 \u1077 \u1085 \u1080 \u1081  \u1085 \u1080  \u1085 \u1072  \u1086 \u1076 \u1085 \u1086 \u1084  \u1089 \u1072 \u1081 \u1090 \u1077  \u1085 \u1077  \u1085 \u1072 \u1081 \u1076 \u1077 \u1085 \u1086 .")\
\
# 5. \uc0\u1058 \u1086 \u1095 \u1082 \u1072  \u1074 \u1093 \u1086 \u1076 \u1072 \
\
if __name__ == "__main__":\
    check_sites_once()\
}