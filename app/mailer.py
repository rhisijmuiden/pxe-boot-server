"""Outgoing mail through a configurable SMTP server (STARTTLS, implicit TLS or plain, with auth)."""
from __future__ import annotations

import logging
import smtplib
import ssl
import threading
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from . import db

log = logging.getLogger("pxe.mail")


def configured() -> bool:
    s = db.get_setting("smtp")
    return bool(s.get("host") and s.get("from_addr"))


def _context(verify: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def html_wrap(title: str, body_html: str) -> str:
    """Minimal RHIS styled mail layout (inline styles for mail clients)."""
    return f"""<!doctype html><html><body style="margin:0;background:#F6F7FB;font-family:Archivo,Segoe UI,Arial,sans-serif;color:#14213D">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F6F7FB;padding:32px 12px"><tr><td align="center">
<table role="presentation" width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;background:#FFFFFF;border:1px solid #DCE1EB;border-radius:16px">
<tr><td style="background:#14213D;border-radius:16px 16px 0 0;padding:20px 28px;color:#FFFFFF;font-weight:800;font-size:18px;letter-spacing:.02em">RH<span style="color:#FF6B57">I</span>S <span style="font-weight:600;color:#9CC3FF;font-size:13px;letter-spacing:.14em;text-transform:uppercase">&nbsp;PXE Boot Server</span></td></tr>
<tr><td style="padding:28px">
<h1 style="margin:0 0 12px;font-size:20px;line-height:1.3">{title}</h1>
<div style="font-size:15px;line-height:1.6;color:#4F5B73">{body_html}</div>
</td></tr></table></td></tr></table></body></html>"""


def send(to: str | list[str], subject: str, text: str, html: str | None = None, settings: dict | None = None) -> None:
    s = settings or db.get_setting("smtp")
    if not s.get("host"):
        raise RuntimeError("SMTP is not configured")
    recipients = [to] if isinstance(to, str) else to
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((s.get("from_name") or "", s["from_addr"]))
    msg["To"] = ", ".join(recipients)
    if s.get("reply_to"):
        msg["Reply-To"] = s["reply_to"]
    msg["Message-ID"] = make_msgid(domain=s["from_addr"].split("@")[-1] or None)
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    port = int(s.get("port") or (465 if s.get("security") == "ssl" else 587))
    ctx = _context(bool(s.get("verify", True)))
    if s.get("security") == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(s["host"], port, timeout=20, context=ctx)
    else:
        server = smtplib.SMTP(s["host"], port, timeout=20)
    try:
        server.ehlo()
        if s.get("security") == "starttls":
            server.starttls(context=ctx)
            server.ehlo()
        if s.get("username"):
            server.login(s["username"], s.get("password") or "")
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except smtplib.SMTPException:
            pass


def notify(kind: str, subject: str, text: str) -> None:
    """Fire-and-forget notification to the configured address."""
    s = db.get_setting("smtp")
    if not configured() or not s.get("notify_to") or not s.get(f"notify_{kind}", True):
        return

    def _go():
        try:
            send([a.strip() for a in s["notify_to"].split(",") if a.strip()], subject, text,
                 html_wrap(subject, "<p>" + text.replace("\n", "<br>") + "</p>"))
        except Exception as exc:  # noqa: BLE001
            log.warning("notification failed: %s", exc)

    threading.Thread(target=_go, daemon=True).start()
