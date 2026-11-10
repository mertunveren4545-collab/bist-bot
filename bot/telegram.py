"""Telegram Bot API istemcisi (ek kütüphane gerektirmez).

Güvenlik: sadece .env'deki TELEGRAM_CHAT_ID'den gelen mesajlara cevap verir.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import requests

log = logging.getLogger(__name__)


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = str(chat_id)
        self.base = f"https://api.telegram.org/bot{token}"
        self.enabled = bool(token and chat_id)
        self.commands: dict[str, Callable[[list[str]], str | None]] = {}
        self.callbacks: dict[str, Callable[[str, int], str | None]] = {}
        self._offset = 0
        self._stop = threading.Event()

    # ---------------- gönderme ----------------
    def _post(self, method: str, payload: dict) -> dict:
        if not self.enabled:
            log.info("[TELEGRAM KAPALI] %s: %s", method, payload.get("text", ""))
            return {}
        for attempt in range(3):
            try:
                r = requests.post(f"{self.base}/{method}", json=payload, timeout=20)
                data = r.json()
                if not data.get("ok"):
                    log.warning("Telegram hata: %s", data)
                return data
            except Exception as e:
                log.warning("Telegram bağlantı hatası (%s): %s", attempt + 1, e)
                time.sleep(2 * (attempt + 1))
        return {}

    def send(self, text: str, buttons: list[list[tuple[str, str]]] | None = None,
             silent: bool = False) -> int | None:
        """buttons: [[("Etiket", "callback_data"), ...], ...]  silent: sessiz bildirim"""
        chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]
        msg_id = None
        for idx, chunk in enumerate(chunks):
            payload = {"chat_id": self.chat_id, "text": chunk, "parse_mode": "HTML",
                       "disable_web_page_preview": True, "disable_notification": silent}
            if buttons and idx == len(chunks) - 1:
                payload["reply_markup"] = {"inline_keyboard": [
                    [{"text": t, "callback_data": d} for t, d in row] for row in buttons]}
            res = self._post("sendMessage", payload)
            msg_id = (res.get("result") or {}).get("message_id", msg_id)
        return msg_id

    def edit(self, message_id: int, text: str) -> None:
        self._post("editMessageText", {"chat_id": self.chat_id, "message_id": message_id,
                                       "text": text, "parse_mode": "HTML"})

    def answer(self, callback_id: str, text: str = "") -> None:
        self._post("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    # ---------------- alma ----------------
    def command(self, name: str):
        def deco(fn):
            self.commands[name] = fn
            return fn
        return deco

    def on_callback(self, prefix: str):
        def deco(fn):
            self.callbacks[prefix] = fn
            return fn
        return deco

    def handle_update(self, upd: dict) -> None:
        if "callback_query" in upd:
            cq = upd["callback_query"]
            if str(cq.get("message", {}).get("chat", {}).get("id")) != self.chat_id:
                return
            data = cq.get("data", "")
            prefix, _, arg = data.partition(":")
            fn = self.callbacks.get(prefix)
            reply = fn(arg, cq["message"]["message_id"]) if fn else "Bilinmeyen buton"
            self.answer(cq["id"], (reply or "")[:190])
            return
        msg = upd.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != self.chat_id:
            return
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return
        parts = text.split()
        name = parts[0][1:].split("@")[0].lower()
        fn = self.commands.get(name) or self.commands.get("yardim")
        try:
            reply = fn(parts[1:]) if fn else None
        except Exception as e:  # komut hatası botu düşürmesin
            log.exception("Komut hatası")
            reply = f"⚠️ Hata: {e}"
        if reply:
            self.send(reply)

    def claim_chat(self, upd: dict) -> str | None:
        """Chat id ayarlı değilse, bota ilk /start yazan kişiyi sahip yap."""
        msg = upd.get("message") or {}
        if (msg.get("text") or "").startswith("/start") and msg.get("chat", {}).get("id"):
            self.chat_id = str(msg["chat"]["id"])
            self.enabled = bool(self.token)
            return self.chat_id
        return None

    def poll_once(self, offset: int) -> int:
        """Bekleyen mesaj/butonları bir kere işler, yeni offset'i döner (GitHub Actions için)."""
        if not self.token:
            return offset
        try:
            r = requests.get(f"{self.base}/getUpdates", params={"offset": offset, "timeout": 0}, timeout=30)
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                try:
                    self.handle_update(upd)
                except Exception:
                    log.exception("Güncelleme işlenemedi")
        except Exception as e:
            log.warning("Telegram okuma hatası: %s", e)
        return offset

    def poll_forever(self) -> None:
        if not self.enabled:
            return
        while not self._stop.is_set():
            try:
                r = requests.get(f"{self.base}/getUpdates",
                                 params={"offset": self._offset, "timeout": 30}, timeout=40)
                for upd in r.json().get("result", []):
                    self._offset = upd["update_id"] + 1
                    try:
                        self.handle_update(upd)
                    except Exception:
                        log.exception("Güncelleme işlenemedi")
            except Exception as e:
                log.warning("Telegram dinleme hatası: %s", e)
                time.sleep(5)

    def start_polling(self) -> threading.Thread:
        t = threading.Thread(target=self.poll_forever, daemon=True, name="telegram")
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()
