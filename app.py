import os
import glob
import json
import time
import threading
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

# Directory containing Claude Code's session transcripts (*.jsonl), one
# subfolder per project. Mount the host's ~/.claude/projects here read-only.
LOGS_DIR = os.environ.get("CLAUDE_LOGS_DIR", "/claude-logs")

_cache = {}
_cache_lock = threading.Lock()
CACHE_TTL = 60
# The live session moves fast enough that a full-minute cache feels stale.
SESSION_CACHE_TTL = 15

# Claude Code meters usage in a rolling window anchored to your first message;
# when it expires the next message opens a fresh one.
SESSION_WINDOW_HOURS = float(os.environ.get("SESSION_WINDOW_HOURS", "5"))
# There is no local record of your actual plan allowance, so "percent used" is
# measured against this self-set budget. Override it to match your own ceiling.
SESSION_TOKEN_BUDGET = int(os.environ.get("SESSION_TOKEN_BUDGET", "100000000"))

# Approximate list price in USD per 1M tokens: (input, output).
# Source: Anthropic's published pricing. Not tied to any billing account —
# this is a local estimate only (no discounts, batch pricing, etc. applied).
PRICING = {
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-sonnet-4-0": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
DEFAULT_PRICING = (3.0, 15.0)  # Sonnet-tier fallback for models not in the table above

CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0
CACHE_READ_MULTIPLIER = 0.1


def _price_for(model: str):
    return PRICING.get(model, DEFAULT_PRICING)


def _cost_for_usage(model, input_tokens, output_tokens, cache_read, cache_5m, cache_1h):
    in_price, out_price = _price_for(model)
    cost = (
        input_tokens * in_price
        + output_tokens * out_price
        + cache_read * in_price * CACHE_READ_MULTIPLIER
        + cache_5m * in_price * CACHE_WRITE_5M_MULTIPLIER
        + cache_1h * in_price * CACHE_WRITE_1H_MULTIPLIER
    ) / 1_000_000.0
    return cost


def _empty_bucket():
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "cost_usd": 0.0,
    }


def _log_files():
    pattern = os.path.join(LOGS_DIR, "**", "*.jsonl")
    return glob.glob(pattern, recursive=True)


def _iter_usage_events(paths=None):
    for path in (_log_files() if paths is None else paths):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or '"usage"' not in line or '"assistant"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") != "assistant":
                        continue
                    message = obj.get("message") or {}
                    usage = message.get("usage")
                    timestamp = obj.get("timestamp")
                    if not usage or not timestamp:
                        continue
                    yield {
                        "timestamp": timestamp,
                        "model": message.get("model") or "unknown",
                        "input_tokens": usage.get("input_tokens", 0) or 0,
                        "output_tokens": usage.get("output_tokens", 0) or 0,
                        "cache_read_tokens": usage.get("cache_read_input_tokens", 0) or 0,
                        "cache_creation": usage.get("cache_creation") or {},
                    }
        except OSError:
            continue


def _cache_split(event):
    cc = event["cache_creation"]
    cache_5m = cc.get("ephemeral_5m_input_tokens", 0) or 0
    cache_1h = cc.get("ephemeral_1h_input_tokens", 0) or 0
    return cache_5m, cache_1h, cache_5m + cache_1h


def _is_synthetic(event, cache_creation_total):
    """Claude Code emits zero-usage 'synthetic' entries for interrupted/
    cancelled turns that never hit the API."""
    return (
        event["input_tokens"] == 0
        and event["output_tokens"] == 0
        and event["cache_read_tokens"] == 0
        and cache_creation_total == 0
    )


def _fetch_report(days: int):
    cache_key = f"report:{days}"
    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached and (time.time() - cached["ts"]) < CACHE_TTL:
            return cached["data"]

    cutoff = time.time() - days * 86400

    daily = {}
    by_model = {}

    for event in _iter_usage_events():
        try:
            ts = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.timestamp() < cutoff:
            continue

        model = event["model"]
        cache_5m, cache_1h, cache_creation_total = _cache_split(event)
        if _is_synthetic(event, cache_creation_total):
            continue

        date = ts.strftime("%Y-%m-%d")
        cost = _cost_for_usage(
            model,
            event["input_tokens"],
            event["output_tokens"],
            event["cache_read_tokens"],
            cache_5m,
            cache_1h,
        )

        day = daily.setdefault(date, _empty_bucket())
        day["input_tokens"] += event["input_tokens"]
        day["output_tokens"] += event["output_tokens"]
        day["cache_read_tokens"] += event["cache_read_tokens"]
        day["cache_creation_tokens"] += cache_creation_total
        day["cost_usd"] += cost

        m = by_model.setdefault(model, _empty_bucket())
        m["input_tokens"] += event["input_tokens"]
        m["output_tokens"] += event["output_tokens"]
        m["cache_read_tokens"] += event["cache_read_tokens"]
        m["cache_creation_tokens"] += cache_creation_total
        m["cost_usd"] += cost

    dates = sorted(daily.keys())
    models_sorted = sorted(by_model.items(), key=lambda kv: kv[1]["cost_usd"], reverse=True)

    data = {
        "dates": dates,
        "daily": [daily[d] for d in dates],
        "by_model": [{"model": k, **v} for k, v in models_sorted],
        "totals": {
            "input_tokens": sum(d["input_tokens"] for d in daily.values()),
            "output_tokens": sum(d["output_tokens"] for d in daily.values()),
            "cache_read_tokens": sum(d["cache_read_tokens"] for d in daily.values()),
            "cache_creation_tokens": sum(
                d["cache_creation_tokens"] for d in daily.values()
            ),
            "cost_usd": sum(d["cost_usd"] for d in daily.values()),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    with _cache_lock:
        _cache[cache_key] = {"ts": time.time(), "data": data}

    return data


def _current_session_report():
    """Usage inside the live session window.

    The window is anchored to your first-ever message and rolls forward: each
    window spans [anchor, anchor + N hours), and the first message at or after
    that boundary becomes the next anchor. Usage is summed across every project,
    since the allowance is account-wide rather than per-transcript.
    """
    cache_key = "current_session"
    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached and (time.time() - cached["ts"]) < SESSION_CACHE_TTL:
            return cached["data"]

    events = []
    for event in _iter_usage_events():
        cache_5m, cache_1h, cache_creation_total = _cache_split(event)
        if _is_synthetic(event, cache_creation_total):
            continue
        try:
            ts = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
        except ValueError:
            continue
        events.append((ts, event, cache_5m, cache_1h, cache_creation_total))

    if not events:
        return None

    events.sort(key=lambda e: e[0])
    window = timedelta(hours=SESSION_WINDOW_HOURS)

    anchor = events[0][0]
    for ts, *_ in events:
        if ts >= anchor + window:
            anchor = ts

    totals = _empty_bucket()
    models = set()
    turns = 0
    last_ts = None

    for ts, event, cache_5m, cache_1h, cache_creation_total in events:
        if ts < anchor:
            continue
        turns += 1
        last_ts = ts
        models.add(event["model"])
        totals["input_tokens"] += event["input_tokens"]
        totals["output_tokens"] += event["output_tokens"]
        totals["cache_read_tokens"] += event["cache_read_tokens"]
        totals["cache_creation_tokens"] += cache_creation_total
        totals["cost_usd"] += _cost_for_usage(
            event["model"],
            event["input_tokens"],
            event["output_tokens"],
            event["cache_read_tokens"],
            cache_5m,
            cache_1h,
        )

    total_tokens = (
        totals["input_tokens"]
        + totals["output_tokens"]
        + totals["cache_read_tokens"]
        + totals["cache_creation_tokens"]
    )

    data = {
        "window_start": anchor.isoformat(),
        "window_end": (anchor + window).isoformat(),
        "window_hours": SESSION_WINDOW_HOURS,
        "last_activity": last_ts.isoformat() if last_ts else None,
        "turns": turns,
        "models": sorted(models),
        "totals": totals,
        "total_tokens": total_tokens,
        "budget_tokens": SESSION_TOKEN_BUDGET,
    }

    with _cache_lock:
        _cache[cache_key] = {"ts": time.time(), "data": data}

    return data


def _logs_available():
    return os.path.isdir(LOGS_DIR) and len(_log_files()) > 0


@app.route("/")
def index():
    return render_template("index.html", logs_available=_logs_available())


@app.route("/api/data")
def api_data():
    if not _logs_available():
        return (
            jsonify(
                {
                    "error": f"No Claude Code session logs found under {LOGS_DIR}. "
                    "The host's ~/.claude/projects folder needs to be mounted "
                    "into this container (read-only)."
                }
            ),
            400,
        )

    days = request.args.get("days", default=30, type=int)
    days = max(1, min(days, 3650))

    try:
        data = _fetch_report(days)
        # Not filtered by `days` — the live session is always shown in full.
        data = dict(data, current_session=_current_session_report())
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": f"Failed to read local logs: {e}"}), 500


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
