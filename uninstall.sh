#!/usr/bin/env bash
# Remove o painel do Raspberry Pi. Por segurança, MANTÉM a configuração (com as
# senhas) e as fotos gravadas, a não ser que você passe --tudo.
#   sudo ./uninstall.sh          # remove programa e serviço
#   sudo ./uninstall.sh --tudo   # remove também /etc/painel-cameras.env e /var/lib/painel
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Rode com sudo: sudo ./uninstall.sh [--tudo]" >&2
  exit 1
fi

systemctl disable --now painel-cameras.service 2>/dev/null || true
rm -f /etc/systemd/system/painel-cameras.service /usr/local/bin/painel-cameras.py
systemctl daemon-reload
vcgencmd display_power 1 >/dev/null 2>&1 || true

if [ "${1:-}" = "--tudo" ]; then
  rm -f /etc/painel-cameras.env
  rm -rf /var/lib/painel
  echo "Removido, inclusive a configuração e as fotos."
else
  echo "Removido. Mantidos: /etc/painel-cameras.env e /var/lib/painel (use --tudo para apagar)."
fi
