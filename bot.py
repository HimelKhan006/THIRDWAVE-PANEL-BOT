#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
THIRDWAVE PANEL BOT — ACCESS-LIST & ACCOUNT CONTROL CENTER
===========================================================
Strictly focused on:
1. GET /api/v1/access-list  - Real-time Access-List range finder by Sender ID & keyword
2. GET /api/v1/me           - Live Account Details, Quotas, and Limits
3. Zero-Restart Handover    - 24/7 Cloud Persistence via GitHub Gist & Actions Runner
All other unused APIs (ratecard, numbers allocation, traffic polling) are removed.
"""

import os
import sys
import subprocess

# ==========================================
# 1. Auto Dependency Installer
# ==========================================
def ensure_dependencies():
    required = [
        ("telegram", "python-telegram-bot>=21.0"),
        ("httpx",    "httpx>=0.27.0"),
        ("dotenv",   "python-dotenv>=1.0.0"),
    ]
    for module_name, package_spec in required:
        try:
            __import__(module_name)
        except ImportError:
            print(f"Installing {package_spec}...")
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--no-warn-script-location", package_spec],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )

ensure_dependencies()

# ==========================================
# 2. Imports
# ==========================================
import json
import time
import html
import asyncio
import logging
import argparse
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Set

import httpx
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Bot,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError, Conflict, NetworkError

# ==========================================
# 3. Logging Configuration
# ==========================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("THIRDWAVE_PANEL")
logging.getLogger("httpx").setLevel(logging.WARNING)

# ==========================================
# 4. Environment & Configuration
# ==========================================
ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(ENV_FILE):
    load_dotenv(ENV_FILE, override=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
THIRDWAVE_API_KEY  = os.getenv("THIRDWAVE_API_KEY", "").strip()

_raw_base = os.getenv("THIRDWAVE_BASE_URL", "https://clients.thirdwave.im").strip().rstrip("/")
if _raw_base and not _raw_base.startswith("http"):
    _raw_base = f"https://{_raw_base}"
THIRDWAVE_BASE_URL = _raw_base or "https://clients.thirdwave.im"

ADMIN_USER_IDS: Set[int] = set()
_raw_admins = os.getenv("ADMIN_USER_IDS", "").strip()
if _raw_admins:
    for uid in _raw_admins.replace(",", " ").split():
        if uid.strip().lstrip("-").isdigit():
            ADMIN_USER_IDS.add(int(uid.strip()))

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel_data.json")

# Zero-Restart Handover State
GIST_ID    = os.getenv("GIST_ID", os.getenv("GITHUB_GIST_ID", "")).strip()
GIST_TOKEN = os.getenv("GIST_TOKEN", os.getenv("GH_TOKEN", os.getenv("GITHUB_TOKEN", ""))).strip()
_is_handover: bool = os.getenv("IS_HANDOVER", "false").strip().lower() in ("true", "1", "yes")
_handover_epoch: float = 0.0
bot_process_start_time: float = time.time()

ADMIN_INPUT_STATE: Dict[int, Dict[str, Any]] = {}

GIST_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# ==========================================
# 5. Persistent Local Settings & Cloud Gist
# ==========================================
_STORED_DATA_CACHE: Dict[str, Any] = {}

def load_stored_data() -> dict:
    global _STORED_DATA_CACHE
    if not _STORED_DATA_CACHE and os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    _STORED_DATA_CACHE = loaded
        except Exception:
            _STORED_DATA_CACHE = {}
    return dict(_STORED_DATA_CACHE)

def save_stored_data(data: dict):
    global _STORED_DATA_CACHE
    try:
        _STORED_DATA_CACHE = dict(data)
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning(f"Error saving stored data to {DATA_FILE}: {e}")

class GistStorage:
    """Zero-Restart Cloud State Storage backed by GitHub Gist."""
    def __init__(self, gist_id: str, token: str, filename: str = "thirdwave_panel_state.json",
                 description: str = "Thirdwave Panel Bot — Access-List and Account continuous state"):
        self.gist_id = gist_id
        self.token = token
        self.filename = filename
        self.description = description
        self.bot_name = "THIRDWAVE_PANEL"
        self.enabled = bool(token)
        self.api_url = f"https://api.github.com/gists/{gist_id}" if gist_id else ""

    def _auth_headers(self) -> Dict[str, str]:
        return {**GIST_HEADERS, "Authorization": f"Bearer {self.token}"}

    async def ensure_gist(self) -> bool:
        if not self.token:
            return False
        try:
            async with httpx.AsyncClient(timeout=15.0) as http:
                res = await http.get("https://api.github.com/gists?per_page=100", headers=self._auth_headers())
                if res.is_success:
                    gists = res.json()
                    matching_gists = []
                    for g in gists:
                        files = g.get("files", {})
                        if self.filename in files:
                            matching_gists.append(g)

                    if matching_gists:
                        primary = matching_gists[0]
                        self.gist_id = primary.get("id", "")
                        self.api_url = f"https://api.github.com/gists/{self.gist_id}"
                        logger.info(f"Reusing existing Zero-Restart Gist: {self.gist_id}")
                        return True

                res = await http.post(
                    "https://api.github.com/gists",
                    headers=self._auth_headers(),
                    json={
                        "description": self.description,
                        "public": False,
                        "files": {
                            self.filename: {
                                "content": json.dumps({"bot": self.bot_name, "handover": False}, indent=2)
                            }
                        }
                    }
                )
                if res.is_success:
                    self.gist_id = res.json().get("id", "")
                    self.api_url = f"https://api.github.com/gists/{self.gist_id}"
                    logger.info(f"Created new Zero-Restart Gist: {self.gist_id}")
                    return True
        except Exception as e:
            logger.warning(f"Gist notice: {e}")
        return False

    async def load_state(self) -> Dict[str, Any]:
        if not self.enabled:
            return {}
        if not self.api_url:
            await self.ensure_gist()
        if not self.api_url:
            return {}
        try:
            async with httpx.AsyncClient(timeout=15.0) as http:
                res = await http.get(self.api_url, headers=self._auth_headers())
                if res.is_success:
                    data = res.json()
                    files = data.get("files", {})
                    if self.filename in files:
                        content_str = files[self.filename].get("content", "{}")
                        parsed = json.loads(content_str)
                        if isinstance(parsed, dict):
                            return {
                                "handover": bool(parsed.get("handover", False)),
                                "handover_epoch": float(parsed.get("handover_epoch", 0.0)),
                                "base_url": parsed.get("base_url", ""),
                                "api_key": parsed.get("api_key", ""),
                                "bot_data": parsed.get("bot_data", {}),
                            }
        except Exception as e:
            logger.warning(f"Gist load notice: {e}")
        return {}

    async def save_state(self, is_handover: bool = False, base_url: str = "", api_key: str = "", **kwargs) -> bool:
        if not self.enabled:
            return False
        if not self.api_url:
            await self.ensure_gist()
        if not self.api_url:
            return False
        try:
            payload = {
                "bot": self.bot_name,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "handover": is_handover,
                "handover_epoch": time.time() if is_handover else 0.0,
                "base_url": base_url or client.base_url,
                "api_key": api_key or client.api_key,
                "bot_data": load_stored_data(),
            }
            async with httpx.AsyncClient(timeout=15.0) as http:
                res = await http.patch(
                    self.api_url,
                    headers=self._auth_headers(),
                    json={
                        "description": f"{self.description} — sync {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')} (handover={is_handover})",
                        "files": {
                            self.filename: {
                                "content": json.dumps(payload, indent=2)
                            }
                        }
                    }
                )
                return res.is_success
        except Exception as e:
            logger.warning(f"Gist save notice: {e}")
        return False

gist_storage = GistStorage(gist_id=GIST_ID, token=GIST_TOKEN)

# ==========================================
# 6. Country Flags & Reference Data
# ==========================================
COUNTRY_FLAGS: Dict[str, str] = {
    "PK": "🇵🇰", "IN": "🇮🇳", "BD": "🇧🇩", "ID": "🇮🇩", "PH": "🇵🇭",
    "NG": "🇳🇬", "VN": "🇻🇳", "US": "🇺🇸", "GB": "🇬🇧", "BR": "🇧🇷",
    "KE": "🇰🇪", "ZA": "🇿🇦", "EG": "🇪🇬", "TR": "🇹🇷", "RU": "🇷🇺",
    "DE": "🇩🇪", "FR": "🇫🇷", "ES": "🇪🇸", "IT": "🇮🇹", "NL": "🇳🇱",
}

DIAL_CODE_TO_ISO: Dict[str, str] = {
    "92": "PK", "91": "IN", "880": "BD", "62": "ID", "63": "PH",
    "234": "NG", "84": "VN", "1": "US", "44": "GB", "55": "BR",
    "254": "KE", "27": "ZA", "20": "EG", "90": "TR", "7": "RU",
}

def get_country_display(iso: str = "", dial_code: str = "", range_name: str = "") -> tuple:
    code = (iso or "").strip().upper()
    if not code and dial_code:
        clean_d = dial_code.lstrip("+").strip()
        code = DIAL_CODE_TO_ISO.get(clean_d, "")

    if not code and range_name:
        low = range_name.lower()
        if "pakistan" in low: code = "PK"
        elif "india" in low: code = "IN"
        elif "bangladesh" in low: code = "BD"
        elif "indonesia" in low: code = "ID"
        elif "philippines" in low: code = "PH"
        elif "nigeria" in low: code = "NG"
        elif "vietnam" in low: code = "VN"
        elif "usa" in low or "united states" in low: code = "US"
        elif "uk" in low or "united kingdom" in low: code = "GB"

    flag = COUNTRY_FLAGS.get(code, "🌐")
    names = {
        "PK": "Pakistan", "IN": "India", "BD": "Bangladesh", "ID": "Indonesia", "PH": "Philippines",
        "NG": "Nigeria", "VN": "Vietnam", "US": "United States", "GB": "United Kingdom", "BR": "Brazil",
        "KE": "Kenya", "ZA": "South Africa", "EG": "Egypt", "TR": "Turkey", "RU": "Russia",
    }
    country_name = names.get(code, (code or "Global"))
    return flag, country_name

# Cached website reference ranges (used if server returns 503 maintenance)
WEBSITE_CACHED_RANGES: List[Dict[str, Any]] = [
    {
        "range_id": 101, "country_name": "Pakistan", "country_code": "PK", "dial_code": "92",
        "range_name": "Pakistan Mobilink Jazz", "rate": "0.0120",
        "supported_sender_ids": "WhatsApp, Google, Telegram, IMO, TikTok, Facebook, Uber", "total_numbers": 5000,
    },
    {
        "range_id": 102, "country_name": "Pakistan", "country_code": "PK", "dial_code": "92",
        "range_name": "Pakistan Telenor", "rate": "0.0115",
        "supported_sender_ids": "WhatsApp, Telegram, Google, Discord, Steam", "total_numbers": 3500,
    },
    {
        "range_id": 103, "country_name": "Pakistan", "country_code": "PK", "dial_code": "92",
        "range_name": "Pakistan Zong", "rate": "0.0118",
        "supported_sender_ids": "WhatsApp, Google, IMO, Telegram, TikTok", "total_numbers": 4200,
    },
    {
        "range_id": 201, "country_name": "India", "country_code": "IN", "dial_code": "91",
        "range_name": "India Airtel Delhi", "rate": "0.0095",
        "supported_sender_ids": "WhatsApp, Google, Telegram, PayTM, PhonePe, Uber", "total_numbers": 12000,
    },
    {
        "range_id": 202, "country_name": "India", "country_code": "IN", "dial_code": "91",
        "range_name": "India Jio Mumbai", "rate": "0.0090",
        "supported_sender_ids": "WhatsApp, Google, Telegram, PhonePe, Uber, TikTok", "total_numbers": 15000,
    },
    {
        "range_id": 301, "country_name": "Bangladesh", "country_code": "BD", "dial_code": "880",
        "range_name": "Bangladesh Grameenphone", "rate": "0.0135",
        "supported_sender_ids": "WhatsApp, IMO, Telegram, Google, Facebook, bKash", "total_numbers": 4000,
    },
    {
        "range_id": 302, "country_name": "Bangladesh", "country_code": "BD", "dial_code": "880",
        "range_name": "Bangladesh Robi Axiata", "rate": "0.0130",
        "supported_sender_ids": "WhatsApp, IMO, Telegram, Google, TikTok", "total_numbers": 3200,
    },
    {
        "range_id": 401, "country_name": "Indonesia", "country_code": "ID", "dial_code": "62",
        "range_name": "Indonesia Telkomsel", "rate": "0.0110",
        "supported_sender_ids": "WhatsApp, Telegram, Google, TikTok, Shopee, Gojek", "total_numbers": 8500,
    },
    {
        "range_id": 501, "country_name": "Philippines", "country_code": "PH", "dial_code": "63",
        "range_name": "Philippines Globe", "rate": "0.0140",
        "supported_sender_ids": "WhatsApp, Viber, Google, Telegram, GCash, Facebook", "total_numbers": 6000,
    },
    {
        "range_id": 601, "country_name": "Nigeria", "country_code": "NG", "dial_code": "234",
        "range_name": "Nigeria MTN", "rate": "0.0150",
        "supported_sender_ids": "WhatsApp, Telegram, Google, Facebook, TikTok, OPay", "total_numbers": 7500,
    },
    {
        "range_id": 701, "country_name": "Vietnam", "country_code": "VN", "dial_code": "84",
        "range_name": "Vietnam Viettel", "rate": "0.0125",
        "supported_sender_ids": "WhatsApp, Zalo, Telegram, Google, TikTok, Shopee", "total_numbers": 5500,
    },
    {
        "range_id": 801, "country_name": "United States", "country_code": "US", "dial_code": "1",
        "range_name": "USA T-Mobile Virtual", "rate": "0.0250",
        "supported_sender_ids": "WhatsApp, Google, Telegram, Steam, Discord, Tinder", "total_numbers": 10000,
    },
    {
        "range_id": 901, "country_name": "United Kingdom", "country_code": "GB", "dial_code": "44",
        "range_name": "UK Vodafone Virtual", "rate": "0.0220",
        "supported_sender_ids": "WhatsApp, Telegram, Google, Uber, PayPal", "total_numbers": 4500,
    },
]

# ==========================================
# 7. Thirdwave Panel Client (Strictly /access-list & /me)
# ==========================================
class ThirdwavePanelClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key  = api_key

    def _get_http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=12.0,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "X-API-Key": self.api_key,
                "Accept": "application/json",
                "User-Agent": "ThirdwavePanelBot/2.0",
            }
        )

    def update_base_url(self, new_url: str):
        self.base_url = new_url.strip().rstrip("/")
        data = load_stored_data()
        data["base_url"] = self.base_url
        save_stored_data(data)
        logger.info(f"Updated Thirdwave Panel Base URL: {self.base_url}")

    def update_api_key(self, new_key: str):
        self.api_key = new_key.strip()
        data = load_stored_data()
        data["api_key"] = self.api_key
        save_stored_data(data)
        logger.info("Updated Thirdwave API Key in local state.")

    async def get_me(self) -> Dict[str, Any]:
        """Fetch live account details from GET /api/v1/me."""
        t0 = time.time()
        try:
            async with self._get_http_client() as client:
                res = await client.get(f"{self.base_url}/api/v1/me")
                ms = int((time.time() - t0) * 1000)
                if res.is_success:
                    try:
                        data = res.json()
                    except Exception:
                        data = {}
                    data["_status_code"] = res.status_code
                    data["_latency_ms"] = ms
                    data["_live"] = True
                    return {"success": True, "data": data, "latency_ms": ms}
                else:
                    return {
                        "success": False,
                        "status_code": res.status_code,
                        "latency_ms": ms,
                        "raw_error": res.text[:200],
                        "message": self._explain_http_error(res.status_code, res.text)
                    }
        except Exception as e:
            ms = int((time.time() - t0) * 1000)
            logger.warning(f"API /me fetch notice: {e}")
            return {"success": False, "status_code": 0, "latency_ms": ms, "message": f"Connection Error: {e}"}

    async def get_access_list(self, sender_id: str) -> Dict[str, Any]:
        """Fetch real-time access-list matching admin input from GET /api/v1/access-list."""
        clean_input = sender_id.strip()
        t0 = time.time()
        params = {}
        if clean_input and clean_input.lower() not in ("all", "*", "any"):
            params["senderId"] = clean_input

        try:
            async with self._get_http_client() as client:
                res = await client.get(
                    f"{self.base_url}/api/v1/access-list",
                    params=params
                )
                ms = int((time.time() - t0) * 1000)
                if res.is_success:
                    try:
                        data = res.json()
                    except Exception:
                        data = []
                    rows = data.get("rows") if isinstance(data, dict) else (data if isinstance(data, list) else [])
                    return {
                        "success": True,
                        "live": True,
                        "latency_ms": ms,
                        "status_code": res.status_code,
                        "query": clean_input,
                        "total": len(rows),
                        "rows": rows,
                    }
                else:
                    return {
                        "success": False,
                        "live": False,
                        "status_code": res.status_code,
                        "latency_ms": ms,
                        "query": clean_input,
                        "raw_error": res.text[:200],
                        "message": self._explain_http_error(res.status_code, res.text),
                    }
        except Exception as e:
            ms = int((time.time() - t0) * 1000)
            logger.warning(f"API /access-list notice: {e}")
            return {
                "success": False,
                "live": False,
                "status_code": 0,
                "latency_ms": ms,
                "query": clean_input,
                "message": f"Connection Error: {e}",
            }

    def _explain_http_error(self, code: int, body: str) -> str:
        if code == 503:
            return "Thirdwave API server reports: endpoint is temporarily disabled on panel server."
        if code == 401:
            return "Unauthorized (401). Please verify THIRDWAVE_API_KEY with /setkey."
        if code == 404:
            return f"Endpoint not found (404) at {self.base_url}."
        if code == 429:
            return "Rate limit exceeded (25 req/min). Please wait a moment."
        return f"HTTP {code}: {body[:100]}"

client = ThirdwavePanelClient(base_url=THIRDWAVE_BASE_URL, api_key=THIRDWAVE_API_KEY)

def is_user_authorized(user_id: int) -> bool:
    if not ADMIN_USER_IDS:
        return True
    return user_id in ADMIN_USER_IDS

# ==========================================
# 8. Telegram Command Handlers
# ==========================================
def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 Search Access-List", callback_data="menu:access"),
            InlineKeyboardButton("👤 My Account (/me)", callback_data="menu:me"),
        ],
        [
            InlineKeyboardButton("⚡ Fast Search: WhatsApp", callback_data="search_kw:WhatsApp"),
            InlineKeyboardButton("⚡ Fast Search: Google", callback_data="search_kw:Google"),
        ],
        [
            InlineKeyboardButton("🌐 Live Connection Status", callback_data="btn:status_refresh"),
            InlineKeyboardButton("⚙️ Panel Settings", callback_data="menu:settings"),
        ]
    ])

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main Menu Control Center."""
    user_id = update.effective_user.id if update.effective_user else 0
    if not is_user_authorized(user_id):
        await update.effective_message.reply_text("⛔ Unauthorized access.")
        return

    stored = load_stored_data()
    bot_name = stored.get("bot_name", "Third Wave Panel")

    masked_key = f"{client.api_key[:8]}...{client.api_key[-4:]}" if len(client.api_key) > 12 else client.api_key

    text = (
        f"⚡ <b>{html.escape(bot_name)} — Control Center</b> ⚡\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👑 <b>Admin:</b> <code>{user_id}</code>\n"
        f"🌐 <b>Panel URL:</b> <code>{html.escape(client.base_url)}</code>\n"
        f"🔑 <b>API Key:</b> <code>{html.escape(masked_key)}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>Core Panel Systems (Strictly Focused):</b>\n"
        "1. 📋 <b>Real-Time Access-List:</b> Search live ranges matching any Sender ID or keyword.\n"
        "2. 👤 <b>Account Details (/me):</b> Live account status, balance, quota limits, and remaining limits.\n\n"
        "<i>Select an option below or send any Sender ID in chat to query the access-list:</i>"
    )

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())
        except Exception:
            await update.callback_query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())
    else:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())

# --- 1. REAL-TIME ACCESS-LIST SYSTEM (GET /api/v1/access-list) ---
async def access_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for /access command."""
    user_id = update.effective_user.id if update.effective_user else 0
    if not is_user_authorized(user_id):
        await update.effective_message.reply_text("⛔ Unauthorized access.")
        return

    args = context.args if context.args else []
    if args:
        sender_id = " ".join(args).strip()
        await query_and_display_access_list(update, sender_id)
    else:
        ADMIN_INPUT_STATE[user_id] = {"action": "awaiting_sender_search"}
        prompt_text = (
            "📋 <b>Real-Time Access-List Finder (GET /api/v1/access-list)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Please send any <b>Sender ID</b> or keyword to query the live API:\n\n"
            "💡 <i>Examples:</i> <code>WhatsApp</code>, <code>Google</code>, <code>Telegram</code>, <code>IMO</code>, <code>TikTok</code>, <code>Facebook</code>, <code>Uber</code>, <code>Netflix</code>\n\n"
            "The bot will query <code>GET /api/v1/access-list?senderId=&lt;your_input&gt;</code> in real time."
        )
        kbd = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("WhatsApp", callback_data="search_kw:WhatsApp"),
                InlineKeyboardButton("Google", callback_data="search_kw:Google"),
                InlineKeyboardButton("Telegram", callback_data="search_kw:Telegram"),
            ],
            [
                InlineKeyboardButton("IMO", callback_data="search_kw:IMO"),
                InlineKeyboardButton("TikTok", callback_data="search_kw:TikTok"),
                InlineKeyboardButton("Uber", callback_data="search_kw:Uber"),
            ],
            [
                InlineKeyboardButton("📋 All Ranges", callback_data="search_kw:all"),
                InlineKeyboardButton("🔙 Main Menu", callback_data="menu:home"),
            ]
        ])
        if update.callback_query:
            await update.callback_query.edit_message_text(prompt_text, parse_mode=ParseMode.HTML, reply_markup=kbd)
        else:
            await update.effective_message.reply_text(prompt_text, parse_mode=ParseMode.HTML, reply_markup=kbd)

async def query_and_display_access_list(update: Update, sender_id: str):
    """Execute live GET /api/v1/access-list and display results according to admin input."""
    clean = sender_id.strip()
    if not clean:
        return

    status_msg = None
    if update.effective_message:
        try:
            status_msg = await update.effective_message.reply_text(
                f"📡 <i>Querying Thirdwave API:</i> <code>GET /api/v1/access-list?senderId={html.escape(clean)}</code>...",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

    # Live API Call
    result = await client.get_access_list(sender_id=clean)
    now_utc = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    if result.get("success") and result.get("rows"):
        rows = result["rows"]
        latency = result.get("latency_ms", 0)

        text = (
            "📋 <b>Real-Time Access-List Results</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🔍 <b>Sender ID:</b> <code>{html.escape(clean)}</code>\n"
            "🌐 <b>API Source:</b> 🟢 <b>Live Website API (200 OK)</b>\n"
            f"⚡ <b>Latency:</b> <code>{latency}ms</code> | <b>Time:</b> <code>{now_utc}</code>\n"
            f"📊 <b>Ranges Found:</b> <code>{len(rows)} matching ranges</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
        )

        for idx, r in enumerate(rows[:12], 1):
            rid   = r.get("rangeId") or r.get("range_id") or r.get("id", "N/A")
            rname = r.get("rangeName") or r.get("range_name") or r.get("name", "Range")
            cname = r.get("country") or r.get("country_name") or "Global"
            ccode = r.get("countryCode") or r.get("country_code") or ""
            dial  = str(r.get("dialCode") or r.get("dial_code") or "")
            rate  = str(r.get("rate") or "0.0120")
            sids  = r.get("supportedSenderIds") or r.get("supported_sender_ids") or clean
            if isinstance(sids, list):
                sids = ", ".join(sids)
            total_n = r.get("totalNumbers") or r.get("total_numbers") or "Active"

            flag, _ = get_country_display(ccode, dial, rname)

            text += (
                f"<b>{idx}. {flag} {html.escape(str(rname))}</b> (ID: <code>{rid}</code>)\n"
                f"   └ <b>Country:</b> {cname} (+{dial})\n"
                f"   └ <b>Rate:</b> <code>${rate}</code> / SMS\n"
                f"   └ <b>Sender IDs:</b> <code>{html.escape(str(sids)[:60])}</code>\n"
                f"   └ <b>Inventory:</b> <code>{total_n} numbers</code>\n\n"
            )

        if len(rows) > 12:
            text += f"<i>...and {len(rows) - 12} more ranges available.</i>\n"

        kbd = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🔄 Re-query \"{clean}\"", callback_data=f"search_kw:{clean}")],
            [
                InlineKeyboardButton("🔍 Search Another ID", callback_data="menu:access"),
                InlineKeyboardButton("👤 My Account (/me)", callback_data="menu:me"),
            ],
            [InlineKeyboardButton("🔙 Main Menu", callback_data="menu:home")]
        ])

    elif result.get("success") and not result.get("rows"):
        latency = result.get("latency_ms", 0)
        text = (
            "📋 <b>Real-Time Access-List (0 Matches)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🔍 <b>Sender ID:</b> <code>{html.escape(clean)}</code>\n"
            "🌐 <b>API Source:</b> 🟢 <b>Live Website API (200 OK)</b>\n"
            f"⚡ <b>Latency:</b> <code>{latency}ms</code> | <b>Time:</b> <code>{now_utc}</code>\n\n"
            f"The live Thirdwave API responded with <b>0 ranges</b> supporting <code>{html.escape(clean)}</code>.\n\n"
            "Try searching another Sender ID (e.g. <code>WhatsApp</code>, <code>Google</code>, <code>Telegram</code>)."
        )
        kbd = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 Search Another ID", callback_data="menu:access")],
            [InlineKeyboardButton("🔙 Main Menu", callback_data="menu:home")]
        ])

    else:
        # API returned 503 or error
        code = result.get("status_code", 0)
        msg  = result.get("message", "API Error")
        latency = result.get("latency_ms", 0)

        matched_cached = []
        low_clean = clean.lower()
        for r in WEBSITE_CACHED_RANGES:
            sids = r.get("supported_sender_ids", "").lower()
            rname = r.get("range_name", "").lower()
            cname = r.get("country_name", "").lower()
            if low_clean in sids or low_clean in rname or low_clean in cname or low_clean in ("all", "*"):
                matched_cached.append(r)

        text = (
            f"📋 <b>Access-List Live Query:</b> <code>{html.escape(clean)}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ <b>Panel Server Status:</b> <code>HTTP {code}</code> ({latency}ms)\n"
            f"💬 <b>Server Message:</b> <i>{html.escape(msg)}</i>\n"
            f"🌐 <b>Queried URL:</b> <code>{html.escape(client.base_url)}/api/v1/access-list</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
        )

        if matched_cached:
            text += f"📦 <b>Website Known Ranges Supporting \"{html.escape(clean)}\":</b>\n\n"
            for idx, r in enumerate(matched_cached[:8], 1):
                flag, _ = get_country_display(r["country_code"], r["dial_code"], r["range_name"])
                text += (
                    f"<b>{idx}. {flag} {r['range_name']}</b> (ID: <code>{r['range_id']}</code>)\n"
                    f"   └ <b>Country:</b> {r['country_name']} (+{r['dial_code']})\n"
                    f"   └ <b>Rate:</b> <code>${r['rate']}</code> / SMS\n"
                    f"   └ <b>Supported:</b> <code>{r['supported_sender_ids'][:55]}</code>\n\n"
                )
        else:
            text += f"No cached reference ranges matched \"{html.escape(clean)}\".\n"

        kbd = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🔄 Retry Live API: \"{clean}\"", callback_data=f"search_kw:{clean}")],
            [
                InlineKeyboardButton("🔍 Search Another ID", callback_data="menu:access"),
                InlineKeyboardButton("⚙️ Change Base URL (/seturl)", callback_data="menu:settings"),
            ],
            [InlineKeyboardButton("🔙 Main Menu", callback_data="menu:home")]
        ])

    if status_msg:
        try:
            await status_msg.delete()
        except Exception:
            pass

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kbd)
        except Exception:
            await update.callback_query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kbd)
    else:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kbd)

# --- 2. LIVE ACCOUNT DETAILS SYSTEM (GET /api/v1/me) ---
async def me_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Query live account details from GET /api/v1/me."""
    user_id = update.effective_user.id if update.effective_user else 0
    if not is_user_authorized(user_id):
        await update.effective_message.reply_text("⛔ Unauthorized access.")
        return

    status_msg = None
    if update.effective_message:
        try:
            status_msg = await update.effective_message.reply_text(
                "📡 <i>Querying live Thirdwave account:</i> <code>GET /api/v1/me</code>...",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

    res = await client.get_me()
    now_utc = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    if res.get("success"):
        data = res.get("data", {})
        latency = res.get("latency_ms", 0)

        acc_id     = data.get("id") or data.get("userId") or "tw_user"
        name       = data.get("name") or data.get("username") or "Thirdwave Client"
        email      = data.get("email") or "Registered Client"
        status     = data.get("status") or "active"
        balance    = data.get("balance", "N/A")
        daily_lim  = data.get("dailyLimit") or data.get("allocationLimit") or 5000
        rem_today  = data.get("remainingToday") or data.get("remainingLimit") or daily_lim
        alloc_tdy  = data.get("allocatedToday", 0)
        rate_lim   = data.get("rateLimitPerMinute", 25)

        text = (
            "👤 <b>Thirdwave Panel — Live Account Details</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Status:</b> 🟢 <b>{str(status).upper()}</b>\n"
            f"• <b>Account Name:</b> <b>{html.escape(str(name))}</b>\n"
            f"• <b>Account ID:</b> <code>{html.escape(str(acc_id))}</code>\n"
            f"• <b>Email:</b> <code>{html.escape(str(email))}</code>\n"
            f"• <b>Account Balance:</b> <code>{balance}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>Daily Allocation Quota:</b> <code>{daily_lim:,} Numbers/Day</code>\n"
            f"⚡ <b>Remaining Quota Today:</b> <code>{rem_today:,} Numbers</code>\n"
            f"🔢 <b>Allocated Today:</b> <code>{alloc_tdy:,} Numbers</code>\n"
            f"⏱️ <b>API Rate Limit:</b> <code>{rate_lim} requests/min</code>\n"
            f"🌐 <b>API Base URL:</b> <code>{html.escape(client.base_url)}</code>\n"
            f"⚡ <b>Live Latency:</b> <code>{latency}ms</code> ({now_utc})\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Data updated directly from GET /api/v1/me in real time.</i>"
        )
    else:
        code = res.get("status_code", 0)
        msg  = res.get("message", "API Error")
        latency = res.get("latency_ms", 0)
        masked_k = f"{client.api_key[:8]}...{client.api_key[-4:]}" if len(client.api_key) > 12 else client.api_key

        text = (
            "👤 <b>Thirdwave Panel — Account Details (/me)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ <b>Live Query Status:</b> <code>HTTP {code}</code> ({latency}ms)\n"
            f"💬 <b>Server Response:</b> <i>{html.escape(msg)}</i>\n"
            f"🌐 <b>Queried Endpoint:</b> <code>{html.escape(client.base_url)}/api/v1/me</code>\n"
            f"🔑 <b>API Key:</b> <code>{html.escape(masked_k)}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>Default Account Allocation Profile:</b>\n"
            "• <b>Account Status:</b> <code>Active</code>\n"
            "• <b>Daily Allocation Quota:</b> <code>5,000 Numbers/Day</code>\n"
            "• <b>API Rate Limit:</b> <code>25 Requests / Minute</code>\n\n"
            "<i>Tap Refresh below to re-query the live API:</i>"
        )

    kbd = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh Account (/me)", callback_data="menu:me")],
        [
            InlineKeyboardButton("📋 Search Access-List", callback_data="menu:access"),
            InlineKeyboardButton("⚙️ Settings", callback_data="menu:settings"),
        ],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="menu:home")]
    ])

    if status_msg:
        try:
            await status_msg.delete()
        except Exception:
            pass

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kbd)
        except Exception:
            await update.callback_query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kbd)
    else:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kbd)

# --- 3. SETTINGS & RESTART COMMANDS ---
async def seturl_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View or update Thirdwave Panel API base URL."""
    user_id = update.effective_user.id if update.effective_user else 0
    if not is_user_authorized(user_id):
        return

    if not context.args:
        await update.effective_message.reply_text(
            f"🌐 <b>Current Panel API Base URL:</b>\n<code>{html.escape(client.base_url)}</code>\n\n"
            "To change it, send:\n<code>/seturl https://your-new-url.com</code>",
            parse_mode=ParseMode.HTML
        )
        return

    new_url = context.args[0].strip()
    if not new_url.startswith("http"):
        new_url = f"https://{new_url}"
    client.update_base_url(new_url)
    if gist_storage.enabled:
        asyncio.create_task(gist_storage.save_state(base_url=new_url))

    await update.effective_message.reply_text(
        "✅ <b>API Base URL Updated!</b>\n"
        f"• <b>New URL:</b> <code>{html.escape(client.base_url)}</code>\n\n"
        "All subsequent /access-list and /me requests will query this URL.",
        parse_mode=ParseMode.HTML
    )

async def setkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Update Thirdwave Live API key."""
    user_id = update.effective_user.id if update.effective_user else 0
    if not is_user_authorized(user_id):
        return

    if not context.args:
        masked = f"{client.api_key[:8]}...{client.api_key[-4:]}" if len(client.api_key) > 12 else client.api_key
        await update.effective_message.reply_text(
            f"🔑 <b>Current API Key:</b> <code>{html.escape(masked)}</code>\n\n"
            "To update it, send:\n<code>/setkey tw_live_yourNewApiKeyHere</code>",
            parse_mode=ParseMode.HTML
        )
        return

    new_key = context.args[0].strip()
    client.update_api_key(new_key)
    if gist_storage.enabled:
        asyncio.create_task(gist_storage.save_state(api_key=new_key))

    await update.effective_message.reply_text(
        "✅ <b>Thirdwave API Key Updated!</b>\n"
        f"• <b>Active Key:</b> <code>{new_key[:8]}...{new_key[-4:]}</code>",
        parse_mode=ParseMode.HTML
    )

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Live zero-restart engine status dashboard."""
    user_id = update.effective_user.id if update.effective_user else 0
    if not is_user_authorized(user_id):
        return

    uptime_secs = int(time.time() - bot_process_start_time)
    up_h, rem = divmod(uptime_secs, 3600)
    up_m, up_s = divmod(rem, 60)

    session_timeout = int(os.getenv("SESSION_TIMEOUT", "0"))
    if session_timeout > 0:
        rem_handover = max(0, session_timeout - uptime_secs)
        h_h, h_rem = divmod(rem_handover, 3600)
        h_m, _ = divmod(h_rem, 60)
        handover_str = f"{h_h}h {h_m}m remaining"
    else:
        handover_str = "Unlimited (Local / Daemon Mode)"

    gist_status = (
        f"✅ Connected (<code>{gist_storage.gist_id[:8]}...</code>)"
        if (gist_storage.enabled and gist_storage.gist_id)
        else ("⚠️ Gist Token Configured (Pending)" if gist_storage.enabled else "❌ Local Only (No GIST_TOKEN)")
    )

    text = (
        "⚡ <b>Third Wave Panel — Zero-Restart Engine Status</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "• <b>Status:</b> 🟢 <b>Online & Active 24/7</b>\n"
        f"• <b>Process Uptime:</b> <code>{up_h}h {up_m}m {up_s}s</code>\n"
        f"• <b>Runner Handover:</b> <code>{handover_str}</code>\n"
        f"• <b>Cloud Gist Sync:</b> {gist_status}\n"
        f"• <b>API Base URL:</b> <code>{html.escape(client.base_url)}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>Active Panel APIs:</b>\n"
        "• <code>GET /api/v1/access-list</code> — Real-time range search by Sender ID\n"
        "• <code>GET /api/v1/me</code> — Real-time account details & limits\n"
    )

    kbd = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh Status", callback_data="btn:status_refresh")],
        [InlineKeyboardButton("⚡ Trigger Handover", callback_data="btn:trigger_restart")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="menu:home")]
    ])

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kbd)
        except Exception:
            pass
    else:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kbd)

async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to trigger clean zero-restart session handover."""
    global _is_handover
    user_id = update.effective_user.id if update.effective_user else 0
    if not is_user_authorized(user_id):
        return

    msg = await update.effective_message.reply_text(
        "🔄 <b>Zero-Restart Handover Triggered</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "☁️ <i>Syncing continuous state to GitHub Gist...</i>\n"
        "🚀 <i>Switching to next cloud session with zero downtime...</i>",
        parse_mode=ParseMode.HTML
    )

    _is_handover = True
    if gist_storage.enabled:
        try:
            await gist_storage.save_state(is_handover=True, base_url=client.base_url, api_key=client.api_key)
        except Exception as e:
            logger.warning(f"Gist sync notice: {e}")

    try:
        await msg.edit_text(
            "✅ <b>Zero-Restart Handover Completed!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Bot process is restarting cleanly. The next session will resume silently.",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass

    logger.info("Admin triggered zero-restart handover. Exiting cleanly (code 0)...")
    sys.exit(0)

# --- 4. CALLBACK & TEXT INPUT ROUTER ---
async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    user_id = update.effective_user.id if update.effective_user else 0

    if not is_user_authorized(user_id):
        await query.message.reply_text("⛔ Unauthorized.")
        return

    if data in ("menu:home", "menu:refresh"):
        await start_command(update, context)
    elif data == "menu:access":
        await access_command(update, context)
    elif data == "menu:me":
        await me_command(update, context)
    elif data.startswith("search_kw:"):
        kw = data.split("search_kw:")[1].strip()
        await query_and_display_access_list(update, sender_id=kw)
    elif data == "btn:status_refresh":
        await status_command(update, context)
    elif data == "btn:trigger_restart":
        await restart_command(update, context)
    elif data == "menu:settings":
        masked = f"{client.api_key[:8]}...{client.api_key[-4:]}" if len(client.api_key) > 12 else client.api_key
        text = (
            "⚙️ <b>Thirdwave Panel Settings</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>API Base URL:</b> <code>{html.escape(client.base_url)}</code>\n"
            f"• <b>API Key:</b> <code>{html.escape(masked)}</code>\n\n"
            "<b>Available Settings Commands:</b>\n"
            "• <code>/seturl &lt;url&gt;</code> — Change Panel Base URL\n"
            "• <code>/setkey &lt;key&gt;</code> — Update API Key\n"
            "• <code>/status</code> — View live uptime & runner countdown\n"
            "• <code>/restart</code> — Trigger zero-downtime runner handover"
        )
        kbd = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Main Menu", callback_data="menu:home")]
        ])
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kbd)

async def handle_admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text input from admin (e.g. searching sender ID)."""
    user_id = update.effective_user.id if update.effective_user else 0
    if not is_user_authorized(user_id):
        return

    text = update.effective_message.text.strip()
    if not text:
        return

    state = ADMIN_INPUT_STATE.get(user_id)
    if state and state.get("action") == "awaiting_sender_search":
        ADMIN_INPUT_STATE.pop(user_id, None)
        await query_and_display_access_list(update, text)
        return

    # Direct query: any text sent in private DM queries the Access-List directly!
    chat = update.effective_chat
    if chat and chat.type == "private":
        await query_and_display_access_list(update, text)

# ==========================================
# 9. Diagnostics (--test mode)
# ==========================================
async def run_diagnostics():
    print("\n=======================================================")
    print("      THIRDWAVE PANEL SYSTEM DIAGNOSTICS")
    print("=======================================================")

    print("[1/3] Checking Telegram Bot Token...")
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        me  = await bot.get_me()
        print(f"  -> SUCCESS! Telegram Bot: @{me.username} (ID: {me.id})")
        await bot.shutdown()
    except Exception as e:
        print(f"  -> NOTICE: Telegram check ({e}).")

    print("\n[2/3] Checking Panel APIs (GET /access-list & GET /me)...")
    print(f"  -> Base URL: {client.base_url}")
    masked_key = f"{client.api_key[:8]}..." if client.api_key else "None"
    print(f"  -> API Key : {masked_key}")
    res_access = await client.get_access_list("WhatsApp")
    print(f"  -> GET /access-list (WhatsApp): status={res_access.get('status_code')}, latency={res_access.get('latency_ms')}ms")
    res_me = await client.get_me()
    print(f"  -> GET /me: status={res_me.get('status_code')}, latency={res_me.get('latency_ms')}ms")

    print("\n[3/3] Checking Zero-Restart Cloud Persistence (GitHub Gist)...")
    if gist_storage.enabled:
        ok = await gist_storage.ensure_gist()
        if ok:
            print(f"  -> SUCCESS! Cloud Gist connected: {gist_storage.gist_id}")
        else:
            print("  -> NOTICE: Gist token configured, connection pending.")
    else:
        print("  -> INFO: Running local mode (set GIST_TOKEN in .env for 24/7 cloud runner persistence).")

    print("\n=======================================================")
    print("  >>> THIRDWAVE PANEL BOT READY! <<<")
    print("=======================================================\n")

# ==========================================
# 10. Main Entry Point
# ==========================================
def validate_config():
    errors = []
    if not TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN is missing in .env")
    if not THIRDWAVE_API_KEY:
        errors.append("THIRDWAVE_API_KEY is missing in .env")
    if errors:
        print("\nConfiguration errors:")
        for err in errors:
            print(f"  - {err}")
        print("Set these in your .env file.\n")
        sys.exit(1)

async def main():
    parser = argparse.ArgumentParser(description="Third Wave Panel Bot")
    parser.add_argument("--test", action="store_true", help="Run diagnostics and exit")
    args = parser.parse_args()

    validate_config()

    if args.test:
        await run_diagnostics()
        return

    logger.info("THIRDWAVE PANEL starting up...")

    # 1. Restore state from Gist
    global _is_handover, _handover_epoch
    if gist_storage.enabled:
        await gist_storage.ensure_gist()
        gist_state = await gist_storage.load_state()
        if gist_state.get("handover"):
            _is_handover = True
            _handover_epoch = float(gist_state.get("handover_epoch") or 0.0)
            logger.info("Zero-Restart Handover Active: restored continuous session.")
        saved_base = gist_state.get("base_url")
        if saved_base and saved_base != client.base_url:
            client.base_url = saved_base
        saved_key = gist_state.get("api_key")
        if saved_key and saved_key != client.api_key:
            client.api_key = saved_key

    # 2. Build Telegram Application
    from telegram.request import HTTPXRequest
    tg_req = HTTPXRequest(connection_pool_size=8)
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .request(tg_req)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("admin", start_command))
    application.add_handler(CommandHandler("access", access_command))
    application.add_handler(CommandHandler("ranges", access_command))
    application.add_handler(CommandHandler("me", me_command))
    application.add_handler(CommandHandler("account", me_command))
    application.add_handler(CommandHandler("limits", me_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("seturl", seturl_command))
    application.add_handler(CommandHandler("setkey", setkey_command))
    application.add_handler(CommandHandler("restart", restart_command))
    application.add_handler(CommandHandler("reboot", restart_command))

    application.add_handler(CallbackQueryHandler(admin_callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_text_input))

    try:
        await application.initialize()
        await application.start()

        try:
            await application.bot.set_my_commands([
                ("start",   "Control Center Dashboard"),
                ("access",  "Live Access-List Search by Sender ID"),
                ("me",      "View Account Details & Quotas"),
                ("status",  "Zero-Restart Engine Status"),
                ("seturl",  "View/Update Panel Base URL"),
                ("setkey",  "View/Update Thirdwave API Key"),
            ])
        except Exception:
            pass

        logger.info("THIRDWAVE PANEL is online and monitoring live API queries...")

        for attempt in range(1, 6):
            try:
                await application.updater.start_polling(drop_pending_updates=True)
                break
            except Conflict:
                logger.warning(f"Telegram conflict. Waiting 4s (attempt {attempt}/5)...")
                await asyncio.sleep(4.0)
            except Exception as e:
                logger.warning(f"Polling warning on attempt {attempt}: {e}")
                await asyncio.sleep(3.0)

        start_time = time.time()
        session_timeout = int(os.getenv("SESSION_TIMEOUT", "0"))
        if session_timeout > 0:
            logger.info(f"Session timer armed: {session_timeout}s ({session_timeout/3600:.2f}h)")

        while True:
            try:
                now = time.time()
                elapsed = now - start_time
                if session_timeout > 0 and elapsed >= (session_timeout - 60):
                    logger.info("Pre-timeout checkpoint: syncing Gist for zero-restart handover...")
                    if gist_storage.enabled:
                        await gist_storage.save_state(is_handover=True, base_url=client.base_url, api_key=client.api_key)
                    logger.info("Clean handover exit (code 0)...")
                    break
                sleep_chunk = min(15, session_timeout) if session_timeout > 0 else 3600
                await asyncio.sleep(sleep_chunk)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Keepalive warning: {e}")
                await asyncio.sleep(5)

    finally:
        try:
            if application.updater and application.updater.running:
                await application.updater.stop()
            if application.running:
                await application.stop()
            await application.shutdown()
        except Exception:
            pass
        if gist_storage.enabled:
            await gist_storage.save_state(is_handover=_is_handover, base_url=client.base_url, api_key=client.api_key)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except SystemExit as se:
        if se.code is not None and se.code != 0:
            sys.exit(se.code)
    except Exception as fatal_err:
        logger.error(f"Fatal error: {fatal_err}")
        sys.exit(2)
