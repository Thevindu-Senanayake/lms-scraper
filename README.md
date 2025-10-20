# lms-scraper

A small Python scraper that polls Moodle course pages and notifies a Discord channel when changes appear. It also exposes admin-only slash commands for managing courses and cookies and can forward runtime logs to a Discord channel as colorized embeds.

## Contents

- `main.py` — scraper + Discord slash-command bot
- `requirements.txt` — Python dependencies
- `course_urls.json` (optional) — monitored course URLs
- `cookies.json` (optional) — exported cookie JSON object used for authenticated requests

## Prerequisites

- Python 3.13+ (some dependencies require >= 3.13)
- pip
- A Discord application + bot (see "Discord setup")
- On the server: git, python3.13 (or Docker), systemd (optional)

## Discord setup (step-by-step)

1. Create the Discord application and bot

   - Open: https://discord.com/developers/applications
   - Open: [Discord Developer Portal](https://discord.com/developers/applications)
   - Click "New Application" → name it → create.
   - In the application menu: Bot → Add Bot.

2. Enable privileged intents

   - Under Bot settings, enable "Server Members Intent" (the code sets `intents.members = True`).

3. Copy the bot token

   - Under Bot settings: "TOKEN" → Copy. Keep this secret.

4. Invite the bot to your server

   - Go to OAuth2 → URL Generator
   - Scopes: `bot`, `applications.commands`
   - Bot permissions (minimum): View Channels, Send Messages, Embed Links, Read Message History
   - Generate URL and open it to invite the bot to your server
   - Go to OAuth2 → URL Generator
   - Scopes: `bot`, `applications.commands`
   - Bot permissions (minimum): View Channels, Send Messages, Embed Links, Read Message History
   - Generate URL and open it to invite the bot to your server

5. Get IDs

   - Enable Developer Mode (User Settings → Advanced → Developer Mode)
   - Right-click channel → Copy ID → `DISCORD_NOTIFY_CHANNEL_ID`
   - Right-click role → Copy ID (or note the role name) → `DISCORD_ADMIN_ROLE`
   - Enable Developer Mode (User Settings → Advanced → Developer Mode)
   - Right-click channel → Copy ID → `DISCORD_NOTIFY_CHANNEL_ID`
   - Right-click role → Copy ID (or note the role name) → `DISCORD_ADMIN_ROLE`

## Environment variables / `.env` (example)

Create `.env` in the project root with:

```env
DISCORD_BOT_TOKEN=YOUR_BOT_TOKEN
DISCORD_NOTIFY_CHANNEL_ID=123456789012345678
DISCORD_LOG_CHANNEL_ID=123456789012345678
DISCORD_LOG_LEVEL=WARNING
DISCORD_ADMIN_ROLE=course-admin
DISCORD_WHITELISTED_IDS=111111111111111111,222222222222222222
```

- Treat `DISCORD_BOT_TOKEN` as a secret. Use GitHub Secrets or a protected repo environment for production.
- `DISCORD_WHITELISTED_IDS` is comma-separated user IDs allowed to administer the bot.

## Files used by the project

`course_urls.json` (optional): JSON array of course URLs. Example:

```json
[
	"https://example.com/course/view.php?id=123",
	"https://example.com/course/view.php?id=456"
]
```

`cookies.json` (optional): full exported cookie JSON object. Minimal example:

```json
{
	"name": "MoodleSession",
	"value": "abcdef123456",
	"domain": "example.com"
}
```

## Local setup & run

1. Create and activate a virtual environment

   Linux / macOS:

   ```bash
   python3.13 -m venv venv
   source venv/bin/activate
   ```

   Windows (PowerShell):

   ```powershell
   python -m venv venv
   .\venv\Scripts\Activate.ps1
   ```

1. Install dependencies

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

1. Create `.env` (see example above)

1. Run the app

```bash
python main.py
```

## Slash commands (quick reference)

- `/get_courses` — list configured courses
- `/add_course <url>` — add a course URL (admin/whitelisted only)
- `/remove_course <url>` — remove a course URL (admin/whitelisted only)
- `/get_cookie` — show masked cookie info (ephemeral or DM fallback)
- `/set_cookie <cookie_json>` — set `cookies.json` (expects a JSON-object string)
- `/preview_notification` — send a sample embed to the notify channel
- `/get_log_level` — show log-forwarding level
- `/set_log_level <level>` — set runtime log-forwarding level (admin only)

## Deploy with GitHub Actions

Workflow location: `.github/workflows/deploy.yml`

- Triggers: `push` to `main` and manual runs via `workflow_dispatch`.
- What it does:
  - Sets up Python (3.13 on the runner)
  - Runs flake8
  - SSHes to remote server, writes `/root/lms-discord-bot/.env` from repo variables/secrets
  - Creates venv on server and installs requirements
  - Restarts `discord-bot.service`

Repository variables / secrets to configure

- `DROPLET_IP` (secret)
- `SSH_PRIVATE_KEY` (secret)
- `SSH_PASSPHRASE` (optional secret)
- `DISCORD_BOT_TOKEN` (secret)
- `DISCORD_NOTIFY_CHANNEL_ID` (repo variable)
- `DISCORD_LOG_CHANNEL_ID` (repo variable)
- `DISCORD_LOG_LEVEL` (repo variable)
- `DISCORD_ADMIN_ROLE` (repo variable)
- `DISCORD_WHITELISTED_IDS` (repo variable)

## Example systemd unit (server)

Create `/etc/systemd/system/discord-bot.service`:

```
[Unit]
Description=lms-scraper Discord bot
After=network.target

[Service]
User=root
WorkingDirectory=/root/lms-discord-bot
EnvironmentFile=/root/lms-discord-bot/.env
ExecStart=/root/lms-discord-bot/venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now discord-bot.service
```

## Troubleshooting

- audioop-lts / Python version mismatch

  If pip reports "Ignored the following versions that require a different python version" or "No matching distribution found for audioop-lts==0.2.2", your Python is older than 3.13.

  Install Python 3.13 on Ubuntu (deadsnakes):

  ```bash
  sudo apt-get update
  sudo apt-get install -y software-properties-common
  sudo add-apt-repository -y ppa:deadsnakes/ppa
  sudo apt-get update
  sudo apt-get install -y python3.13 python3.13-venv python3.13-dev build-essential
  ```

- flake8 failures in CI

  The Actions workflow runs flake8. Reproduce locally:

  ```bash
  pip install flake8
  flake8 .
  ```

  Fix any reported issues (syntax / unused variables / indentation). If you want, I can run a pass fixing linter errors.

## Security notes

- Keep `DISCORD_BOT_TOKEN` secret. Do not commit `.env`.
- Logs forwarded to Discord may contain sensitive data; set `DISCORD_LOG_LEVEL` conservatively in production.

## Contributing

Contributions are welcome. If you find bugs or would like a feature, please open an issue with details. To contribute code:

1. Fork the repository.
2. Create a feature branch (git checkout -b my-feature).
3. Commit your changes and push the branch to your fork.
4. Open a Pull Request describing the change and any testing performed.

Please be sure to avoid committing secrets or `.env` files. Thanks for your help!
