"""Goals and the daily progress email.

A goal is "grow this bot's account from its starting amount to a target by a
deadline". The email reports where the account is against that goal, plainly,
including when the goal is out of reach.

Environment (email is off until SMTP_HOST and REPORT_EMAIL_TO are set):
  REPORT_EMAIL_TO    where the daily report goes
  SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD
  SMTP_FROM          defaults to SMTP_USER
  REPORT_HOUR_UTC    hour of day to send, 0-23 (default 8)
"""

from __future__ import annotations

import os
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import web_core


def email_settings() -> dict:
    return {
        "to": os.getenv("REPORT_EMAIL_TO", "").strip(),
        "host": os.getenv("SMTP_HOST", "").strip(),
        "port": int(os.getenv("SMTP_PORT", "587") or 587),
        "user": os.getenv("SMTP_USER", "").strip(),
        "password": os.getenv("SMTP_PASSWORD", ""),
        "sender": (os.getenv("SMTP_FROM", "") or os.getenv("SMTP_USER", "")).strip(),
        "hour": min(23, max(0, int(os.getenv("REPORT_HOUR_UTC", "8") or 8))),
    }


def email_missing() -> list[str]:
    s = email_settings()
    missing = [name for name, key in (("REPORT_EMAIL_TO", "to"), ("SMTP_HOST", "host")) if not s[key]]
    if not s["sender"]:
        missing.append("SMTP_FROM")
    return missing


def _parse(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def goal_progress(goal: dict | None, starting: float, equity: float,
                  now: datetime | None = None) -> dict | None:
    """Where the account stands against its goal.

    `needed_daily_pct` is the compound growth per day still required to reach
    the target by the deadline. It is the number that shows whether a goal is
    realistic: a few percent a day, every day, is already extraordinary.
    """
    if not goal:
        return None
    now = now or datetime.now(timezone.utc)
    target = float(goal["target_usd"])
    deadline = _parse(goal["deadline"])
    created = _parse(goal["created_at"])
    days_left = max(0.0, (deadline - now).total_seconds() / 86400.0)
    total_days = max(1e-9, (deadline - created).total_seconds() / 86400.0)
    span = target - starting
    done = (equity - starting) / span if span > 0 else 1.0
    reached = equity >= target
    needed = None
    if not reached and equity > 0 and days_left > 0:
        needed = ((target / equity) ** (1.0 / max(days_left, 1.0)) - 1.0) * 100.0
    if reached:
        status = "reached"
    elif days_left <= 0:
        status = "missed"
    elif equity <= 0:
        status = "wiped out"
    else:
        expected = (now - created).total_seconds() / 86400.0 / total_days
        status = "on pace" if done >= expected else "behind"
    return {
        "starting": starting,
        "target_usd": target,
        "equity": equity,
        "deadline": goal["deadline"],
        "days_left": days_left,
        "progress": done,
        "needed_daily_pct": needed,
        "status": status,
    }


def _money(v: float) -> str:
    return f"${v:,.2f}"


def bot_section(bot: dict, goal: dict | None, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    report = web_core.bot_report(bot["id"])
    acct, perf = report["account"], report["performance"]
    since = (now - timedelta(hours=24)).isoformat()
    recent = [t for t in report["trades"] if t["closed_at"] >= since]
    day_net = sum(t["net"] for t in recent)
    day_wins = sum(1 for t in recent if t["net"] > 0)
    state = "paused" if bot.get("paused") else ("running" if bot.get("is_running") else "stopped")
    lines = [
        f"{bot['bot_name']} ({state}, paper money)",
        f"  Account now:    {_money(acct['equity'])}  (started at {_money(acct['starting'])})",
    ]
    progress = goal_progress(goal, acct["starting"], acct["equity"], now)
    if progress:
        lines.append(f"  Goal:           {_money(progress['target_usd'])} by "
                     f"{progress['deadline'][:10]}, {progress['days_left']:.0f} days left")
        lines.append(f"  Progress:       {progress['progress'] * 100:.0f}% of the way, "
                     f"{progress['status']}")
        if progress["needed_daily_pct"] is not None:
            lines.append(f"  Still needed:   {progress['needed_daily_pct']:.1f}% growth per day, "
                         f"every day, to get there")
    lines.append(f"  Last 24 hours:  {len(recent)} trades closed, {day_wins} won, "
                 f"{day_net:+.2f} USD after fees")
    if perf["trades"]:
        lines.append(f"  All time:       {perf['trades']} trades, {perf['wins']} won "
                     f"({perf['win_rate'] * 100:.0f}%), {perf['net']:+.2f} USD after fees, "
                     f"{_money(perf['fees'])} paid in fees")
    else:
        lines.append("  All time:       no closed trades yet")
    if report["positions"]:
        held = ", ".join(f"{p['direction']} {p['symbol']}" for p in report["positions"])
        lines.append(f"  Open now:       {held} ({acct['unrealized']:+.2f} USD unrealised)")
    if bot.get("last_error"):
        lines.append(f"  Last error:     {bot['last_error']}")
    return "\n".join(lines)


def build_report(bots: list[dict], goals: dict[int, dict],
                 now: datetime | None = None) -> tuple[str, str]:
    """(subject, body) for the daily email, covering every paper bot."""
    now = now or datetime.now(timezone.utc)
    paper = [b for b in bots if (b.get("trading_mode") or "paper") == "paper"]
    if not paper:
        return ("Neko daily report: no bots yet",
                "No paper bots exist yet. Create one in the console to start a record.\n")
    sections = [bot_section(b, goals.get(b["id"]), now) for b in paper]
    total = sum(web_core.bot_report(b["id"])["account"]["equity"] for b in paper)
    subject = f"Neko daily report: {_money(total)} across {len(paper)} bot" \
              f"{'' if len(paper) == 1 else 's'}"
    body = (f"Neko daily report, {now.strftime('%d %b %Y %H:%M')} UTC\n\n"
            + "\n\n".join(sections)
            + "\n\nThis is paper trading: real prices, simulated money. "
              "Results here do not guarantee results with real money.\n")
    return subject, body


def send_email(subject: str, body: str) -> None:
    """Send through the configured SMTP server. Raises on any failure."""
    missing = email_missing()
    if missing:
        raise RuntimeError("Email is not set up. Missing: " + ", ".join(missing))
    s = email_settings()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = s["sender"]
    msg["To"] = s["to"]
    msg.set_content(body)
    context = ssl.create_default_context()
    if s["port"] == 465:
        server = smtplib.SMTP_SSL(s["host"], s["port"], timeout=30, context=context)
    else:
        server = smtplib.SMTP(s["host"], s["port"], timeout=30)
    try:
        if s["port"] != 465:
            server.starttls(context=context)
        if s["user"]:
            server.login(s["user"], s["password"])
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except smtplib.SMTPException:
            pass
