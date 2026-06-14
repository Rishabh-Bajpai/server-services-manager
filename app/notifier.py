"""Pluggable notification backends for health-check transitions.

Each notifier is a small class with a single ``send(event)`` method.
Currently supported: ``ntfy``, ``webhook``, ``telegram``, ``email``.

Notifier instances are constructed from the ``notifications:`` list
in ``config.yaml``; see :func:`build_notifiers`.
"""
import json
import logging
import smtplib
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from email.message import EmailMessage
from typing import List, Optional
from urllib import request as urlrequest
from urllib.error import URLError

logger = logging.getLogger("Notifier")


@dataclass
class Event:
    """A health-check transition or other notification."""
    service: str
    kind: str          # "transition" | "info" | "error"
    state: str         # "healthy" | "unhealthy" | "unknown" | "started" | "stopped"
    detail: str = ""   # free-form text (last error, last check time, etc.)
    timestamp: float = 0.0

    def subject(self) -> str:
        return f"[{self.service}] {self.kind}: {self.state}"

    def body(self) -> str:
        return (
            f"Service: {self.service}\n"
            f"State:   {self.state}\n"
            f"Time:    {self.timestamp}\n"
            f"Detail:  {self.detail}\n"
        )


class Notifier(ABC):
    """Base class for notification backends."""
    @abstractmethod
    def send(self, event: Event) -> bool:
        """Send ``event``. Return True on success, False on failure."""


class NtfyNotifier(Notifier):
    """Push to ntfy.sh (or any self-hosted ntfy server)."""
    def __init__(self, topic: str, server: str = "https://ntfy.sh", priority: str = "default"):
        if not topic:
            raise ValueError("ntfy topic required")
        self.url = f"{server.rstrip('/')}/{topic}"
        self.priority = priority

    def send(self, event: Event) -> bool:
        try:
            req = urlrequest.Request(
                self.url,
                data=event.body().encode("utf-8"),
                method="POST",
                headers={
                    "Title": event.subject(),
                    "Priority": self.priority,
                    "Tags": "warning" if event.state == "unhealthy" else "white_check_mark",
                },
            )
            with urlrequest.urlopen(req, timeout=10) as resp:
                return 200 <= resp.status < 300
        except (URLError, OSError) as e:
            logger.warning(f"ntfy send failed: {e}")
            return False


class WebhookNotifier(Notifier):
    """Generic HTTP POST with JSON body."""
    def __init__(self, url: str, headers: Optional[dict] = None):
        if not url:
            raise ValueError("webhook url required")
        self.url = url
        self.headers = headers or {}

    def send(self, event: Event) -> bool:
        try:
            payload = json.dumps({
                "service": event.service,
                "kind": event.kind,
                "state": event.state,
                "detail": event.detail,
                "timestamp": event.timestamp,
            }).encode("utf-8")
            req = urlrequest.Request(
                self.url, data=payload, method="POST",
                headers={"Content-Type": "application/json", **self.headers},
            )
            with urlrequest.urlopen(req, timeout=10) as resp:
                return 200 <= resp.status < 300
        except (URLError, OSError) as e:
            logger.warning(f"webhook send failed: {e}")
            return False


class TelegramNotifier(Notifier):
    """Telegram Bot API message."""
    def __init__(self, bot_token: str, chat_id: str):
        if not bot_token or not chat_id:
            raise ValueError("telegram bot_token and chat_id required")
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    def send(self, event: Event) -> bool:
        try:
            text = f"*{event.subject()}*\n```\n{event.body()}```"
            payload = json.dumps({
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "Markdown",
            }).encode("utf-8")
            req = urlrequest.Request(
                self.url, data=payload, method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urlrequest.urlopen(req, timeout=10) as resp:
                return 200 <= resp.status < 300
        except (URLError, OSError) as e:
            logger.warning(f"telegram send failed: {e}")
            return False


class EmailNotifier(Notifier):
    """SMTP email. Requires a configured SMTP server (Gmail with app
    password, Mailgun, your own relay, etc.)."""
    def __init__(self, host: str, port: int, username: str, password: str,
                 from_addr: str, to_addrs: List[str],
                 use_tls: bool = True):
        if not host or not to_addrs:
            raise ValueError("email host and to_addrs required")
        self.host = host
        self.port = port or 587
        self.username = username
        self.password = password
        self.from_addr = from_addr or username
        self.to_addrs = to_addrs
        self.use_tls = use_tls

    def send(self, event: Event) -> bool:
        try:
            msg = EmailMessage()
            msg["Subject"] = event.subject()
            msg["From"] = self.from_addr
            msg["To"] = ", ".join(self.to_addrs)
            msg.set_content(event.body())

            with smtplib.SMTP(self.host, self.port, timeout=10) as s:
                if self.use_tls:
                    s.starttls()
                if self.username:
                    s.login(self.username, self.password)
                s.send_message(msg)
            return True
        except (smtplib.SMTPException, OSError) as e:
            logger.warning(f"email send failed: {e}")
            return False


def build_notifier(spec: dict) -> Optional[Notifier]:
    """Construct a notifier from a config dict. Returns None on bad config."""
    if not isinstance(spec, dict):
        return None
    kind = spec.get("type", "").lower()
    try:
        if kind == "ntfy":
            return NtfyNotifier(
                topic=spec.get("topic", ""),
                server=spec.get("server", "https://ntfy.sh"),
                priority=spec.get("priority", "default"),
            )
        if kind == "webhook":
            return WebhookNotifier(
                url=spec.get("url", ""),
                headers=spec.get("headers"),
            )
        if kind == "telegram":
            return TelegramNotifier(
                bot_token=spec.get("bot_token", ""),
                chat_id=spec.get("chat_id", ""),
            )
        if kind == "email":
            return EmailNotifier(
                host=spec.get("host", ""),
                port=spec.get("port", 587),
                username=spec.get("username", ""),
                password=spec.get("password", ""),
                from_addr=spec.get("from", spec.get("username", "")),
                to_addrs=spec.get("to", []),
                use_tls=spec.get("use_tls", True),
            )
        logger.warning(f"unknown notifier type: {kind!r}")
        return None
    except (ValueError, TypeError) as e:
        logger.warning(f"invalid notifier config {kind}: {e}")
        return None


def build_notifiers(specs) -> List[Notifier]:
    """Build a list of notifiers from the ``notifications:`` config block."""
    if not isinstance(specs, list):
        return []
    out: List[Notifier] = []
    for spec in specs:
        n = build_notifier(spec)
        if n is not None:
            out.append(n)
    return out


def fanout(notifiers: List[Notifier], event: Event) -> None:
    """Send ``event`` through all notifiers in background threads.

    Returns immediately; each notifier runs in its own thread so a
    slow SMTP server doesn't block the health-check loop.
    """
    for n in notifiers:
        t = threading.Thread(
            target=_safe_send, args=(n, event), daemon=True,
            name=f"notifier-{type(n).__name__}",
        )
        t.start()


def _safe_send(n: Notifier, event: Event) -> None:
    try:
        n.send(event)
    except Exception as e:
        logger.warning(f"notifier {type(n).__name__} failed: {e}")
