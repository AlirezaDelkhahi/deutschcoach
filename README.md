# DeutschCoach (`دویچ‌کوچ`) — Phase 0 (48-Hour Telegram Validation Harness)

> **HARD PHASE GATE:** This repository implements **ONLY Phase 0** (the 48-hour Telegram diagnostic validation harness). **Phase 1 remains strictly locked** until the founder records written confirmation of `PHASE_0_PASSED`.

## What Phase 0 Validates
1. **Onboarding & Attribution:** `/start [source_tag]` → `B1` / `B2` level selection → exam timeline selection → authentic Goethe Sprechen Prompt Card (`B1 Teil 2` / `B2 Teil 1`).
2. **Pre-AI Guards (Zero Wasted API Spend):**
   - Atomic `1` free evaluation per `telegram_user_id` (`AVAILABLE → RESERVED → CONSUMED`) with admin QA whitelist repeatability (`ADMIN_USER_IDS`), plus capping potentially billed failed/timed-out evaluations (`MAX_BILLED_FAILED_ATTEMPTS=2`) so attackers cannot loop Gemini retries.
   - Telegram `VOICE`-only evaluation (rejects non-voice audio/document uploads) with pre-download size (`≤ 5 MB`), MIME, and duration (`15s ≤ d ≤ 125s`) checks.
   - Strict `ffprobe` validation (`-protocol_whitelist file` / `-format_whitelist` + video-stream rejection) and decoded `ffmpeg` Opus normalization (capped at `-t 126`, verified `15s ≤ d ≤ 125s`) before calling Gemini, plus immediate temp-file cleanup.
3. **Gemini 3.8 Flash Native Audio Evaluation:** Evaluates `.ogg` voice note using `gemini-3.8-flash` (configurable back to `2.5` if needed) with strict JSON parsing, exact `Decimal` USD / integer `Toman` cost tracking (`1 USD = 273,000 Toman`), and quota release on transient API errors or unparseable responses.
4. **Founder-Beta Pre-Order Funnel:** Immediate `490,000 Toman` (vs `790,000 Toman`) offer with Shaparak VPN-off warning, manual receipt submission, admin `/approve <telegram_user_id> <tx_ref>`, `/stats`, and Hour-36 `/followup`.

---

## Non-Iran VPS Docker Quickstart (Recommended)

Deploy on a **non-Iran Linux VPS** (so Google Gemini API calls are not geo-blocked and Telegram long-polling runs continuously) with Docker Engine and Docker Compose v2 installed:

```bash
cp .env.example .env
# Edit .env and fill at minimum TELEGRAM_BOT_TOKEN, GEMINI_API_KEY,
# ADMIN_USER_IDS, ADMIN_NOTIFY_CHAT_ID, and payment/support fields
nano .env

docker compose up -d --build
docker compose logs -f bot
```

> **Container Hardening & Runtime Paths:** The Compose setup runs `bot` as non-root UID/GID `10001:10001` with a read-only root filesystem, `cap_drop: [ALL]`, `no-new-privileges:true`, a persistent `deutschcoach_data` volume at `/app/data`, and an ephemeral `tmpfs` at `/app/tmp_audio` (`DATA_DIR=/app/data` and `TMP_AUDIO_DIR=/app/tmp_audio` are pinned automatically in `docker-compose.yml`).

---

## Common VPS Operations

- **Check service status & healthcheck:**
  ```bash
  docker compose ps
  ```
- **Update & redeploy after pulling new code:**
  ```bash
  git pull && docker compose up -d --build
  ```
- **Restart or stop the bot:**
  ```bash
  docker compose restart bot
  docker compose down
  ```
- **Export append-only JSONL telemetry (`telemetry.jsonl`):**
  ```bash
  docker compose cp bot:/app/data/telemetry.jsonl ./telemetry.jsonl
  ```
- **Safe SQLite hot backup (WAL-safe via Python `sqlite3` backup API):**
  ```bash
  docker compose exec bot python -c "import sqlite3; src = sqlite3.connect('/app/data/deutschcoach_phase0.db'); dst = sqlite3.connect('/tmp/deutschcoach_phase0_backup.db'); src.backup(dst); dst.close(); src.close()"
  docker compose cp bot:/tmp/deutschcoach_phase0_backup.db ./deutschcoach_phase0_backup.db
  docker compose exec bot rm -f /tmp/deutschcoach_phase0_backup.db
  ```
- **Optional host bind-mount permissions note:** If you replace the named `deutschcoach_data` volume with a host directory bind mount (e.g., `./data:/app/data`), ensure the host directory is owned by UID/GID `10001`:
  ```bash
  mkdir -p ./data && chown -R 10001:10001 ./data
  ```

---

## Local Development Without Docker

### Prerequisites
- Python `>=3.11`
- `ffmpeg` / `ffprobe` installed on `PATH` (e.g., `/usr/bin/ffprobe`)
- Telegram Bot Token (`@BotFather`) & Google Gemini API Key

### Setup & Run
```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env
# Edit .env with your local test credentials

.venv/bin/python -m deutschcoach_phase0.bootstrap
# or via console script:
.venv/bin/deutschcoach-phase0
```

### Verification (`pytest` & `ruff`)
```bash
.venv/bin/pytest -v
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
```

### Optional Bare-Metal `systemd` Service Snippet
If running without Docker on a non-Iran Linux VPS:

```ini
[Unit]
Description=DeutschCoach Phase 0 Telegram Validation Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=deutschcoach
WorkingDirectory=/opt/deutschcoach
EnvironmentFile=/opt/deutschcoach/.env
ExecStart=/opt/deutschcoach/.venv/bin/deutschcoach-phase0
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

---

## Environment Variables Reference

All 20 environment variables parsed in `src/deutschcoach_phase0/config.py`:

| Variable | Required | Default | Description |
| :--- | :--- | :--- | :--- |
| `TELEGRAM_BOT_TOKEN` | Yes | — | Telegram Bot API token from `@BotFather` |
| `GEMINI_API_KEY` | Yes | — | Google Gemini API key |
| `ADMIN_USER_IDS` | No | `""` (empty) | Comma-separated numeric Telegram user IDs for admin commands & repeatable QA runs |
| `ADMIN_NOTIFY_CHAT_ID` | No | `None` | Numeric Telegram chat/user ID receiving diagnostic & payment proof notifications |
| `ZARINLINK_URL` | No | `""` | Optional ZarinLink payment URL shown in the Founder-Beta checkout message |
| `CARD_TO_CARD_NUMBER` | No | `""` | Optional Card-to-Card fallback number (automatically redacted from logs/telemetry) |
| `CARD_TO_CARD_HOLDER` | No | `""` | Optional Card-to-Card recipient holder name |
| `FOUNDER_SUPPORT_HANDLE` | No | `""` | Optional Telegram support handle (e.g., `@handle`) for payment help |
| `GEMINI_MODEL` | No | `gemini-3.8-flash` | Gemini model ID used for native audio evaluation (`gemini-2.5-flash` also supported) |
| `GEMINI_THINKING_LEVEL` | No | `low` | Thinking depth for Gemini 3.x models: `low`, `medium`, or `high` |
| `GEMINI_THINKING_BUDGET` | No | `512` | Non-negative integer thinking token budget passed to Gemini 2.5 models |
| `GEMINI_TIMEOUT_SEC` | No | `60` | Positive integer timeout in seconds for Gemini evaluation calls |
| `USD_TO_TOMAN_RATE` | No | `273000` | Positive integer FX rate used to convert USD API cost into Toman |
| `BETA_PACK_PRICE_TOMAN` | No | `490000` | Positive integer Founder-Beta 10-mock pack price in Toman |
| `REGULAR_PACK_PRICE_TOMAN` | No | `790000` | Positive integer standard launch pack price in Toman |
| `RESERVATION_TTL_SEC` | No | `180` | Positive integer quota reservation TTL in seconds (must be `>= GEMINI_TIMEOUT_SEC + 45`) |
| `MAX_BILLED_FAILED_ATTEMPTS` | No | `2` | Max potentially billed failed/timed-out evaluations before locking free quota |
| `DATA_DIR` | No | `./data` | Directory for `deutschcoach_phase0.db` and `telemetry.jsonl` (`/app/data` in Docker) |
| `TMP_AUDIO_DIR` | No | `./tmp_audio` | Temporary directory for downloaded `.ogg` voice notes (`/app/tmp_audio` in Docker) |
| `LOG_LEVEL` | No | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`) |

---

## Admin Commands
- `/stats` — Real user Phase 0 Go/Pivot/No-Go gate counters (excludes `is_qa = 1` admin runs)
- `/approve <telegram_user_id> <tx_ref>` — Idempotently verify a Founder-Beta payment (`490,000 Toman`) and notify the buyer
- `/followup preview` — Dry-run preview of eligible unpaid diagnostic finishers for the Hour-36 follow-up
- `/followup` — Send the Hour-36 polite follow-up message to eligible unpaid diagnostic finishers

---

## Quantitative Go / Pivot / No-Go Gate (PRD Section 4.6)

| Decision | Hard Quantitative Threshold | Required Action |
| :--- | :--- | :--- |
| **`GO` (Unlock Phase 1)** | **`≥ 100` completed diagnostics** AND **`≥ 10` paid pre-orders (`≥ 4,900,000 Toman` collected)** AND **`< 3,000 Toman` (`~$0.011`) avg API cost per eval** | Founder issues written `PHASE_0_PASSED` sign-off; unlock Phase 1 architecture, UI, and domestic build. |
| **`PIVOT` (Diagnose & Re-test 48h)** | `5–9` paid pre-orders OR strong diagnostic completion (`≥ 80`) with checkout drop-off (e.g., VPN errors on ZarinLink) | Diagnose drop-off (`ZarinLink` VPN blocks vs. CTA wording vs. prompt quality), switch to card-to-card/adjust offer, run one 48h re-test. |
| **`NO-GO` (Kill & Refund)** | **`< 5` paid pre-orders out of `≥ 100` completed diagnostics** OR **`< 50` free diagnostics** achievable after seeding 10+ active communities | **Immediately refund 100% of early beta payers**, archive Phase 0 learnings, and pivot founder time to **`IDEA-013` (`اپلای‌پرو`)**. |
