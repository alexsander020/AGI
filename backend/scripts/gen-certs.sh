#!/usr/bin/env bash
# Gera a CA própria e o certificado do broker. Uso: ./scripts/gen-certs.sh solo.seudominio.com.br
# O ca.crt vai para o firmware (CA_CERT) e para o backend. Guarde ca.key FORA do servidor.
set -euo pipefail
DOM="${1:?uso: $0 <dominio-do-broker>}"
DIR="$(cd "$(dirname "$0")/.." && pwd)/mosquitto/certs"
mkdir -p "$DIR"; cd "$DIR"
[ -f ca.crt ] && { echo "Certificados já existem em $DIR (apague para gerar de novo)."; exit 1; }

openssl genrsa -out ca.key 4096 2>/dev/null
openssl req -x509 -new -key ca.key -sha256 -days 3650 -subj "/CN=Solo-CA" -out ca.crt

openssl genrsa -out server.key 2048 2>/dev/null
openssl req -new -key server.key -subj "/CN=$DOM" -out server.csr
printf "subjectAltName=DNS:%s,DNS:mosquitto\nextendedKeyUsage=serverAuth\n" "$DOM" > ext.cnf
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out server.crt \
  -days 730 -sha256 -extfile ext.cnf 2>/dev/null
rm -f server.csr ext.cnf ca.srl

chmod 644 ca.crt server.crt
# a chave precisa ser legível pelo usuário do Mosquitto (uid 1883) dentro do container
if command -v docker >/dev/null && docker run --rm --user root -v "$DIR":/c --entrypoint sh eclipse-mosquitto:2 \
     -c "chown 1883:1883 /c/server.key && chmod 600 /c/server.key" 2>/dev/null; then
  :
else
  chmod 644 server.key; echo "Aviso: server.key ficou 644 (sem docker para ajustar o dono). Restrinja o acesso ao host."
fi
mv ca.key ../../ca.key.GUARDE-FORA-DO-SERVIDOR
echo "OK. Cole mosquitto/certs/ca.crt em CA_CERT no firmware."
echo "Servidor válido por 730 dias: agende a renovação (rode de novo apagando server.*)."
