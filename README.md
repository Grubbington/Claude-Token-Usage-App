# Token-Usage

A self-hosted dashboard for Claude Code token usage. It reads Claude Code's own
local session transcripts — **no API key of any kind is required**, and nothing
leaves your machine.

![The Token-Usage dashboard](docs/dashboard.png)

<sub>Screenshots use generated sample data, not real usage.</sub>

## What it shows

- **Current session** — when the rolling usage window started, when it resets,
  how much of the window has elapsed, and what percentage of your token budget
  you've spent.
- **Daily token usage** — stacked input / output / cache-read / cache-write.
- **Daily cost** — estimated, see the caveat below.
- **By model** — per-model totals for the selected range (7d / 30d / 90d / all).

![The current-session panel](docs/session.png)

The session window is anchored to your first message and rolls forward in fixed
spans, so "resets at" is the point where the next message opens a new window.

## How it works

Claude Code writes one JSONL transcript per session to
`~/.claude/projects/<project>/<session-id>.jsonl`. Every assistant turn carries a
`message.usage` block with the token counts. The app globs those files, sums the
usage, and skips the zero-usage `<synthetic>` entries Claude Code emits for
interrupted turns.

The container reads that folder through a **read-only bind mount** — it never
writes to it.

## Running it

```bash
docker build -t token-usage-app .

docker run -d --name Token-Usage -p 5000:5000 \
  -v "$HOME/.claude/projects:/claude-logs:ro" \
  -v "$HOME/.token-usage:/rates:ro" \
  token-usage-app
```

On Windows the mounts are `-v "C:\Users\<you>\.claude\projects:/claude-logs:ro"`
and `-v "C:\Users\<you>\.token-usage:/rates:ro"`.

The second mount is optional — without it the app falls back to the built-in
exchange rates. See [Exchange rates](#exchange-rates).

Then open <http://localhost:5000>.

## Exchange rates

Costs are calculated in USD and converted in the browser, with a currency
picker (USD / AUD / GBP) beside the date range.

The container makes **no outbound network calls**, so it cannot look rates up
itself. Instead `scripts/update-rates.ps1` runs on the host, fetches current
rates, and writes them to `%USERPROFILE%\.token-usage\rates.json` — which the
container reads through the read-only `/rates` mount and re-reads every five
minutes. No rebuild or restart needed.

Run it by hand any time:

```powershell
.\scripts\update-rates.ps1
```

Or weekly, via Task Scheduler:

```powershell
$script  = "$PWD\scripts\update-rates.ps1"
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
             -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At "08:53"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries
Register-ScheduledTask -TaskName "Token-Usage FX rates" `
  -Action $action -Trigger $trigger -Settings $settings -Force
```

`-StartWhenAvailable` matters: if the machine is off on Monday morning, the
refresh runs at the next opportunity rather than being skipped.

The rates file deliberately lives outside the repo, so weekly updates never
show up as uncommitted changes. If it is missing or unreadable the app logs a
warning and uses `FX_RATE_AUD` / `FX_RATE_GBP`; the dashboard always prints the
rate and its date, so a stale figure is visible rather than silent.

## Configuration

All optional, set with `-e NAME=value` on `docker run`:

| Variable | Default | Purpose |
| --- | --- | --- |
| `CLAUDE_LOGS_DIR` | `/claude-logs` | Where the transcripts are mounted. |
| `SESSION_WINDOW_HOURS` | `5` | Length of the rolling usage window. |
| `SESSION_TOKEN_BUDGET` | `100000000` | Denominator for the "used this session" percentage. |
| `DISPLAY_CURRENCY` | `AUD` | Currency selected on first load (USD / AUD / GBP). |
| `FX_RATES_FILE` | `/rates/rates.json` | Rates file to read; ignored if absent. |
| `FX_RATE_AUD` | `1.52` | Fallback USD → AUD rate, used when no rates file is mounted. |
| `FX_RATE_GBP` | `0.79` | Fallback USD → GBP rate, used when no rates file is mounted. |

## Caveats

- **Cost is a local estimate, not a bill.** There is no billing API involved.
  Costs are computed from a hardcoded list-price table (`PRICING` in `app.py`)
  and the standard cache multipliers — 0.1x input for cache reads, 1.25x for 5m
  cache writes, 2.0x for 1h. Discounts, subscription plans, and batch pricing are
  not reflected. Update `PRICING` when rates change.
- **Exchange rates come from a weekly snapshot, not a live feed.** Anthropic
  lists prices in USD, so `PRICING` and the whole API stay in USD and the
  browser converts for display. Rates are refreshed on the host by
  `scripts/update-rates.ps1` — see [Exchange rates](#exchange-rates). The
  dashboard shows the rate and the date it was fetched.
- **Session budget is self-set.** Nothing in the local logs records your actual
  plan allowance, so `SESSION_TOKEN_BUDGET` is a number you choose.
- **Claude Code usage only.** Anything you send to the API outside Claude Code
  has no local transcript and so does not appear here.
