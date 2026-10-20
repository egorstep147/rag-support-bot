# Деплой на VPS (Ubuntu/Debian): systemd + Caddy

Один код, по экземпляру службы `support@<компания>` со своим `.env`, базой и портом.

```sh
# от root
apt update && apt install -y git curl postgresql postgresql-18-pgvector caddy   # pgvector под вашу версию Postgres
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
useradd --create-home --shell /bin/bash support
sudo -u postgres createuser support
sudo -u postgres createdb -O support provider
sudo -u postgres psql provider -c "CREATE EXTENSION IF NOT EXISTS vector"   # нужен суперпользователь
sudo -u support git clone https://github.com/egorstep147/rag-support-bot.git /home/support/rag-support-bot
cd /home/support/rag-support-bot && sudo -u support /usr/local/bin/uv sync --frozen --no-dev
sudo -u support mkdir -p /home/support/envs
```

`/home/support/envs/provider.env` (права 600; комментарии — только отдельной строкой):
```
BOT_TOKEN=...
COMPANY=provider
DATABASE_URL=postgresql:///provider?host=/var/run/postgresql
PORT=8086
ADMIN_IDS=...
OPERATOR_CHAT_ID=-100...
GEMINI_API_KEY=...
PUBLIC_URL=https://support.130-17-30-251.sslip.io:8443
WEBHOOK_URL=https://support.130-17-30-251.sslip.io:8443
WEBHOOK_SECRET=...
```

```sh
cp deploy/support@.service /etc/systemd/system/ && systemctl daemon-reload
systemctl enable --now support@provider          # миграции и синхронизация знаний — в ExecStartPre
journalctl -u support@provider -f
```

Caddy (`/etc/caddy/Caddyfile`; вебхуки Telegram принимают порты 443/80/88/8443):
```
support.130-17-30-251.sslip.io:8443 {
	reverse_proxy 127.0.0.1:8086
}
```

Виджет на сайт клиента: `<script src="https://support.130-17-30-251.sslip.io:8443/widget.js" async></script>`,
адрес сайта — в `WIDGET_ORIGINS`.

Обновление: `sh /home/support/rag-support-bot/deploy/update.sh` (от root).
Бэкап: `0 4 * * * pg_dump provider > /home/support/backup-$(date +\%a).sql` в crontab пользователя support.
