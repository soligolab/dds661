#!/usr/bin/env bash
# Esegue le suite che girano senza hardware, contro il gateway emulato.
# tests/hardware/ resta fuori: quelle vogliono il DR302 vero.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY=python3

# test_resilienza uccide e riavvia l'emulatore: va per ultima, e la pulizia
# finale va fatta per porta, non sul PID che abbiamo lanciato noi.
SUITES=(test_devices.py test_polling.py test_poller_ha.py test_resilienza.py)

kill_gateway() {
  local pids
  pids=$(ss -ltnp 2>/dev/null | grep -E '127\.0\.0\.1:(15020|15021)' \
         | grep -oP 'pid=\K[0-9]+' | sort -u)
  [ -n "$pids" ] && kill $pids 2>/dev/null
  return 0
}
trap kill_gateway EXIT

kill_gateway; sleep 0.3
"$PY" "$HERE/fake_gateway.py" >/dev/null 2>&1 &
for _ in $(seq 40); do
  sleep 0.25
  ss -ltn 2>/dev/null | grep -q '127.0.0.1:15020' && break
done
ss -ltn 2>/dev/null | grep -q '127.0.0.1:15020' || { echo "emulatore non partito"; exit 1; }

rc=0
for s in "${SUITES[@]}"; do
  echo; echo "═══ $s ═══"
  "$PY" "$HERE/$s" || { rc=1; echo "  ↑ FALLITA"; break; }
done

echo
[ $rc -eq 0 ] && echo "TUTTE LE SUITE SUPERATE" || echo "SUITE FALLITA"
exit $rc
