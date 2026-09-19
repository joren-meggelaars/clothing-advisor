"""Budget/health alerts: Home Assistant push notification and/or SMTP mail. Failures never raise."""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from .config import Config
from .db import Database
from .ha import HaError, ha_configured, ha_request

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db

    def ha_service(self) -> str:
        svc = self.db.get_setting("notify_service") or self.cfg.ha_notify_service
        return svc.removeprefix("notify.").strip()

    def channels(self) -> list[str]:
        out = []
        if ha_configured(self.cfg) and self.ha_service():
            out.append("home-assistant")
        if self.cfg.smtp_host and self.cfg.smtp_to and self.cfg.smtp_from:
            out.append("email")
        return out

    def send(self, title: str, message: str) -> tuple[list[str], list[str]]:
        """Returns (channels that succeeded, error messages)."""
        ok, errors = [], []
        if "home-assistant" in self.channels():
            try:
                ha_request(self.cfg, "POST", f"/api/services/notify/{self.ha_service()}",
                           {"title": title, "message": message})
                ok.append("home-assistant")
            except HaError as e:
                errors.append(f"home-assistant: {e}")
        if "email" in self.channels():
            try:
                self._smtp(title, message)
                ok.append("email")
            except (OSError, smtplib.SMTPException) as e:
                errors.append(f"email: {e}")
        for e in errors:
            log.warning("notification failed: %s", e)
        return ok, errors

    def _smtp(self, title: str, message: str) -> None:
        cfg = self.cfg
        msg = EmailMessage()
        msg["Subject"] = title
        msg["From"] = cfg.smtp_from
        msg["To"] = cfg.smtp_to
        msg.set_content(message)
        if cfg.smtp_security == "ssl":
            server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=15, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15)
        with server:
            if cfg.smtp_security == "starttls":
                server.starttls(context=ssl.create_default_context())
            if cfg.smtp_user:
                server.login(cfg.smtp_user, cfg.smtp_password)
            server.send_message(msg)
