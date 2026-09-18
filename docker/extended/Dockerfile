FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends samba openssl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pyftpdlib==2.1.0 pyOpenSSL==25.1.0 WsgiDAV==4.3.3 cheroot==11.0.0 azure-storage-blob==12.27.0
COPY server.py /server.py
COPY smb.conf /etc/samba/smb.conf
RUN useradd -M plenora && mkdir -p /data && chown plenora /data \
    && printf 'plenora-fixture-secret\nplenora-fixture-secret\n' | smbpasswd -a -s plenora
ENTRYPOINT ["python", "/server.py"]
