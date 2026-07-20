"""⏱ Time-awareness — messages carry timestamps; gaps are surfaced.

A long-running session is a stitched-together set of conversations across
time. The agent has always known "what time is it now" (the date is in
``environment_info()``), but until M58 it had no concept of "how long since
you last spoke." Resuming yesterday's session looked identical to the user
typing thirty seconds ago — which sometimes made the agent's first response
feel oblivious to what the gap means.

This module provides the small machinery to fix that:

* ``stamp(message)`` — write ``created_at`` (ISO 8601 seconds-precision) into
  the message's ``additional_kwargs``. Idempotent: never overwrites an
  existing stamp, so messages keep their original creation time across
  any number of save/load cycles.
* ``timestamp_of(message)`` — extract the stamp as ``datetime``, or None.
* ``detect_gap(messages, now, threshold)`` — find the latest stamped message
  in ``messages`` and return ``now - that_time`` if it exceeds the threshold.
  Returns ``None`` when no gap is worth mentioning (or no stamps exist).
* ``gap_notice(gap, now)`` — produce a ``SystemMessage`` that the runtime
  injects before the new user turn so the model can adjust its first
  response ("welcome back — you've been away 14h, want me to recap?").
* ``format_gap(td)`` — "14h 23m", "2d 4h", "5m". Human-friendly.
* ``time_span(messages)`` — start/end span of a list, for compaction
  summaries ("These turns spanned 2 days").

Backwards compatibility: any message without a stamp is just *not* a gap
anchor. Old session JSONs load cleanly; gap detection silently no-ops
until you start producing stamped messages. Nothing crashes.

The injection is symmetric: the model sees a ``SystemMessage`` (so it
adjusts behavior) AND the human sees a dim one-line notice in the
terminal (so they see that the agent saw the gap). Defaults to a 30
minute threshold; tunable via ``settings.gap_minutes`` (0 disables).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from langchain_core.messages import BaseMessage, SystemMessage


GAP_NOTICE_PREFIX = "⏱ "  # easy to grep for in tests + transcripts


def stamp(message: BaseMessage, *, when: datetime | None = None) -> BaseMessage:
    """Stamp a message with its creation time, in place. Idempotent —
    a message that already has ``created_at`` is left alone, so the
    original creation time survives compaction/resume round-trips.

    Stored as ISO 8601 with seconds precision in
    ``additional_kwargs['created_at']``. Returns the same message for
    chaining (``msg = stamp(HumanMessage(content=...))``)."""
    if not hasattr(message, "additional_kwargs") or message.additional_kwargs is None:
        try:
            message.additional_kwargs = {}
        except AttributeError:
            return message  # immutable subclass — best effort, don't crash
    if "created_at" in message.additional_kwargs:
        return message
    ts = (when or datetime.now()).isoformat(timespec="seconds")
    message.additional_kwargs["created_at"] = ts
    return message


def stamp_all(messages: list[BaseMessage], *, when: datetime | None = None) -> int:
    """Stamp every message in a list that doesn't already have a stamp.
    Returns the count of newly-stamped messages. Useful at session load
    time to bring old sessions up to par without losing real timestamps."""
    n = 0
    for m in messages:
        before = (m.additional_kwargs or {}).get("created_at")
        stamp(m, when=when)
        if before is None and (m.additional_kwargs or {}).get("created_at"):
            n += 1
    return n


def timestamp_of(message: BaseMessage) -> datetime | None:
    """Read back the stamp as a ``datetime``. Returns None for messages
    without a stamp or with an unparseable one — never raises."""
    kw = getattr(message, "additional_kwargs", None) or {}
    ts = kw.get("created_at")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def last_active(messages: list[BaseMessage]) -> datetime | None:
    """Latest creation time across the message list. Returns None when
    no stamped messages exist."""
    latest: datetime | None = None
    for m in messages:
        ts = timestamp_of(m)
        if ts is not None and (latest is None or ts > latest):
            latest = ts
    return latest


def detect_gap(
    messages: list[BaseMessage],
    now: datetime | None = None,
    threshold_minutes: int = 30,
) -> timedelta | None:
    """If the most-recent stamped message is older than ``threshold_minutes``
    relative to ``now``, return the gap as a ``timedelta``. Otherwise
    return None. Threshold ≤ 0 disables (always returns None)."""
    if threshold_minutes is None or threshold_minutes <= 0:
        return None
    if not messages:
        return None
    now = now or datetime.now()
    latest = last_active(messages)
    if latest is None:
        return None
    gap = now - latest
    if gap.total_seconds() < threshold_minutes * 60:
        return None
    return gap


def format_gap(gap: timedelta) -> str:
    """Render a timedelta as 'Xd Yh Zm'. Drops zero components from the
    high side; always shows at least minutes (so 4-second gaps render
    as '0m', not the empty string)."""
    total = int(max(0, gap.total_seconds()))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def format_last_active(messages: list[BaseMessage],
                        now: datetime | None = None) -> str | None:
    """One-liner suitable for the resume banner: 'last active 14h ago'.
    Returns None when no stamps exist."""
    la = last_active(messages)
    if la is None:
        return None
    gap = (now or datetime.now()) - la
    if gap.total_seconds() < 60:
        return "last active just now"
    return f"last active {format_gap(gap)} ago"


def gap_notice(gap: timedelta, *, when: datetime | None = None) -> SystemMessage:
    """Build the SystemMessage injected before the user's first message
    after a long gap. The content is deliberately brief — the model just
    needs to know the gap exists; it doesn't need the agent to over-react."""
    when = when or datetime.now()
    notice = (
        f"{GAP_NOTICE_PREFIX}{format_gap(gap)} elapsed since last activity. "
        "Context may be stale — confirm with the user if 'today', 'yesterday', "
        "or 'the last thing we did' could be ambiguous now."
    )
    msg = SystemMessage(content=notice)
    msg.additional_kwargs = {
        "created_at": when.isoformat(timespec="seconds"),
        "kind": "gap_notice",
    }
    return msg


def is_gap_notice(message: BaseMessage) -> bool:
    """Detect our own gap-notice messages, e.g. to filter them out of
    user-facing transcripts."""
    if not isinstance(message, SystemMessage):
        return False
    kw = getattr(message, "additional_kwargs", None) or {}
    return kw.get("kind") == "gap_notice"


def time_span(messages: list[BaseMessage]) -> tuple[datetime, datetime] | None:
    """Return ``(earliest, latest)`` of stamped messages, or None if
    nothing is stamped. Used by the compaction summary so the digest can
    naturally mention 'these turns spanned 2 days'."""
    earliest: datetime | None = None
    latest: datetime | None = None
    for m in messages:
        ts = timestamp_of(m)
        if ts is None:
            continue
        if earliest is None or ts < earliest:
            earliest = ts
        if latest is None or ts > latest:
            latest = ts
    if earliest is None or latest is None:
        return None
    return earliest, latest


def format_span(span: tuple[datetime, datetime] | None) -> str:
    """Render a (start, end) pair as a short string for the compaction
    prompt. Empty string for None so callers can prefix without guarding."""
    if span is None:
        return ""
    start, end = span
    duration = end - start
    if duration.total_seconds() < 3600:
        # short conversation — just the dates
        return f"({start.strftime('%Y-%m-%d %H:%M')} → {end.strftime('%H:%M')})"
    return (
        f"({start.strftime('%Y-%m-%d %H:%M')} → "
        f"{end.strftime('%Y-%m-%d %H:%M')}, spanning {format_gap(duration)})"
    )
