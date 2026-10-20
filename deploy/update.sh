#!/bin/sh
# Обновление (от root): sh /home/support/rag-support-bot/deploy/update.sh
set -e
cd /home/support/rag-support-bot
sudo -u support git pull
sudo -u support /usr/local/bin/uv sync --frozen --no-dev
systemctl restart 'support@*'   # миграции и синхронизация — в ExecStartPre
systemctl --no-pager --lines=0 status 'support@*'
