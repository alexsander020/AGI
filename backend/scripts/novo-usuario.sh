#!/usr/bin/env bash
# Cria usuário no broker com senha aleatória (exibida UMA vez).
#   ./scripts/novo-usuario.sh backend
#   ./scripts/novo-usuario.sh esp32-solo-02 zona2     # só publica em solo/zona2/{leitura,status}
set -euo pipefail
U="${1:?uso: $0 <usuario> [zona]}"; ZONA="${2:-}"
[[ "$U" =~ ^[a-z0-9_-]{3,40}$ ]] || { echo "usuário inválido (a-z 0-9 _ -)"; exit 1; }
cd "$(dirname "$0")/.."
PASS="$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-24)"
[ -f mosquitto/passwd ] && FLAG="-b" || FLAG="-c -b"

docker run --rm --user root -v "$PWD/mosquitto":/c --entrypoint sh eclipse-mosquitto:2 -c \
  "mosquitto_passwd $FLAG /c/passwd '$U' '$PASS' && chown 1883:1883 /c/passwd && chmod 600 /c/passwd"

if [ "$U" != "backend" ]; then
  [ -n "$ZONA" ] || { echo "informe a zona para dispositivos"; exit 1; }
  grep -q "^user $U\$" mosquitto/acl || printf "\nuser %s\ntopic write solo/%s/leitura\ntopic write solo/%s/status\n" "$U" "$ZONA" "$ZONA" >> mosquitto/acl
fi
docker compose ps --status running mosquitto 2>/dev/null | grep -q mosquitto && docker compose kill -s HUP mosquitto || true
echo "Usuário: $U"; echo "Senha:   $PASS   (anote agora; não é possível recuperar)"
