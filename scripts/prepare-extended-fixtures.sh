#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .fixtures/extended
openssl req -x509 -newkey rsa:2048 -nodes -days 2 \
  -keyout .fixtures/extended/server.key -out .fixtures/extended/server.crt \
  -subj '/CN=ftps' -addext 'subjectAltName=DNS:ftps,IP:192.168.2.134' \
  -addext 'basicConstraints=critical,CA:FALSE' >/dev/null 2>&1
docker compose -f docker-compose.yml -f compose.extended.yml build ftps
docker compose -f docker-compose.yml -f compose.extended.yml up -d azure gcs ftps webdav smb
for attempt in $(seq 1 30); do
  if docker compose -f docker-compose.yml -f compose.extended.yml run --rm --no-deps azure-init; then break; fi
  if [ "$attempt" = 30 ]; then exit 1; fi
  sleep 1
done
docker compose -f docker-compose.yml -f compose.extended.yml run --rm --no-deps --entrypoint python azure-init -c '
import urllib.request, urllib.error, time
for attempt in range(30):
    try:
        request = urllib.request.Request("http://gcs:4443/storage/v1/b", data=b"{\"name\":\"plenora-test\"}", headers={"Content-Type": "application/json"})
        urllib.request.urlopen(request, timeout=5).read()
        break
    except urllib.error.HTTPError as error:
        if error.code == 409: break
        if attempt == 29: raise
    except OSError:
        if attempt == 29: raise
    time.sleep(1)
'
