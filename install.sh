#!/usr/bin/env bash
# Instala o painel de câmeras no Raspberry Pi. Rode NO Pi, a partir desta pasta:
#   sudo ./install.sh
set -euo pipefail
cd "$(dirname "$0")"

if [ "$(id -u)" -ne 0 ]; then
  echo "Rode com sudo: sudo ./install.sh" >&2
  exit 1
fi

echo "==> Instalando dependências (ffmpeg e Pillow)"
if ! apt-get install -y --no-install-recommends ffmpeg python3-pil; then
  echo "Falhou. Tente 'sudo apt-get update' e rode de novo." >&2
  exit 1
fi

echo "==> Instalando o programa e o serviço"
install -m 755 painel-cameras.py /usr/local/bin/painel-cameras.py
install -m 644 painel-cameras.service /etc/systemd/system/painel-cameras.service
systemctl daemon-reload

if [ -e /etc/painel-cameras.env ]; then
  echo "==> /etc/painel-cameras.env já existe: mantido como está"
  chmod 600 /etc/painel-cameras.env
  systemctl enable painel-cameras.service
  systemctl restart painel-cameras.service
  echo "Pronto. Painel (re)iniciado. Veja: journalctl -u painel-cameras -f"
else
  install -m 600 painel-cameras.env.example /etc/painel-cameras.env
  systemctl enable painel-cameras.service
  cat <<'MSG'

==> Falta configurar as câmeras. Edite o arquivo (é só root que lê):

      sudo nano /etc/painel-cameras.env

    Depois teste e inicie:

      sudo python3 /usr/local/bin/painel-cameras.py --verificar
      sudo systemctl start painel-cameras

MSG
fi
