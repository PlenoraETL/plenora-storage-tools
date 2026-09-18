# Sei nuovi provider in 0.2.0

Tutti espongono `test`, `list`, `stat`, `get`, `put`, `copy`, `delete` nella
libreria Rust e nella CLI. `copy` opera all'interno della stessa connessione.
Le credenziali vengono risolte dall'host per operazione, mai da campi segreti
nel file di connessione.

| ID | Configurazione | Credenziali del resolver | Pubblicazione atomica | Create-if-absent |
| --- | --- | --- | --- | --- |
| `local` | `root` assoluto, directory esistente | `credential_ref: local:process`; permessi del processo | s?, rename/hard link | s? |
| `ftps` | `host`, `port` (21), `root`, `mode`, `tls_ca_pem` opzionale | `username`, `password` | no | non supportato |
| `azure` | `endpoint`, `account`, `container` | `account_key` oppure `bearer_token` | s?, singolo blob | s? |
| `gcs` | `endpoint`, `bucket` | `bearer_token` OAuth | s?, singolo oggetto | s?, generation=0 |
| `smb` | `host`, `port` (445), `share`, `root` opzionale | `username`, `password`, `domain` opzionale | no | s?, CREATE esclusiva |
| `webdav` | `endpoint` della collection, con `/` finale | `username`/`password` oppure `bearer_token` | no | s?, If-None-Match |

`atomic-required` viene rifiutato prima della mutazione da FTPS, SMB e WebDAV.
FTPS rifiuta anche `overwrite=false`: una verifica di esistenza seguita da
STOR non fornirebbe una creazione esclusiva. Per SMB/WebDAV la creazione
esclusiva non implica che i lettori non possano osservare un upload parziale.
I server devono rispettare le condizioni del protocollo.

## Uso

Le connessioni di esempio sono in `docker/*-connection.json`. Sostituire
endpoint, namespace e riferimento credenziali con quelli del deployment.
Per esempio, una connessione GCS usa:

```json
{
  "provider": "gcs",
  "config_contract": "plenora-storage-gcs-connection-v1",
  "config": {"endpoint": "https://storage.googleapis.com", "bucket": "my-bucket"},
  "credential_ref": "env:STORAGE_CREDENTIALS"
}
```

Il valore della variabile ? materiale riservato fornito dall'host. Per GCS
contiene `bearer_token`; il consumer ? responsabile dell'ottenimento e rinnovo
del token. Non sono attive discovery di credenziali, accessi al metadata server,
ADC o login impliciti. Azure accetta una SharedKey oppure un token fornito.
Per Azure usare `https://ACCOUNT.blob.core.windows.net` come endpoint.
ADLS Gen2 ? accessibile tramite la superficie Blob: ACL, operazioni directory
DFS e rename gerarchico non fanno parte delle sette operazioni v1.

```sh
plenora-storage --format json test --connection connection.json
plenora-storage --format json put --connection connection.json --key reports/data.csv --input data.csv --overwrite false --publication-policy atomic-required
```

In Rust registrare `LocalProvider`, `AzureProvider`, `GcsProvider`, `SmbProvider`
o `WebDavProvider` dal crate `plenora-storage-providers`, costruiti con
`new(Arc<dyn CredentialResolver>)`. FTPS usa
`plenora_storage_ftp::FtpProvider::new_ftps(credentials)`. Le request e il
controllo di deadline/cancellazione sono quelli di `plenora-storage-core`.

## Garanzie e limiti

- Local, Azure, GCS, SMB e WebDAV effettuano download in streaming e bufferizzano
  put/copy fino a `max_buffered_put_bytes` (default 64 MiB), rispettando anche
  `max_transfer_bytes`. Il limite ? per operazione; il consumer limita la
  concorrenza. FTPS trasferisce in streaming e controlla anche la dimensione
  pubblicata dopo il completamento del canale dati.
- TLS di FTPS ? esplicito (AUTH TLS), su controllo e dati; nome host e catena
  vengono verificati. `tls_ca_pem` aggiunge certificati pubblici di fiducia,
  non disabilita la verifica. FTPS implicito sulla porta 990 non ? supportato.
- SMB richiede sessioni autenticate con cifratura SMB3; niente guest, SMB1,
  Kerberos, DFS referral automatici o mount del sistema operativo. La piccola
  correzione alla dipendenza ? tracciata in
  [PROVENANCE](../crates/plenora-smb2/PROVENANCE.md).
- HTTP cloud/WebDAV richiede HTTPS per default. Indirizzi risolti e validati
  vengono fissati; proxy e redirect sono disabilitati. HTTP e reti private
  richiedono opt-in separati. Non si inoltrano credenziali a destinazioni di
  redirect.
- Local usa accessi relativi a una directory-capability; non segue symlink
  fuori dal root. La lista rifiuta symlink e nomi non rappresentabili; nomi
  riservati Windows e staging interno vengono protetti. Il filesystem deve
  supportare hard link per la creazione atomica senza overwrite. Non ? una
  promessa di persistenza dopo perdita di alimentazione.
- I root remoti richiedono isolamento e permessi lato server; non sostituiscono
  chroot o sandbox contro link creati da altri utenti.
- WebDAV richiede PROPFIND Depth 0/1, MKCOL, GET, PUT e DELETE. La cancellazione
  usa un ETag forte e If-Match per non eliminare una risorsa sostituita dopo
  il controllo; server senza ETag forte ricevono `unsupported` su delete.
  Lock DAV, sync-token, ACL e MOVE non sono implementati.
- Azure/GCS persistono content type e metadata custom su put. Local, SMB,
  WebDAV e FTPS rifiutano metadata che non possono conservare. Copy garantisce
  i byte; non promette la conservazione di metadata custom.
- Nomi cloud non rappresentabili nel contratto (slash iniziale/finale, segmenti
  vuoti o `..`) producono errore, senza alias silenziosi. La paginazione ?
  lessicografica e limitata; non ? uno snapshot durante modifiche concorrenti.
- Timeout durante una mutazione conserva `unknown`/`requires_recovery`.
  Non eseguire retry distruttivi o eliminare destinazioni finali senza verificare
  lo stato. Un task filesystem bloccante pu? terminare dopo la deadline.

## Riproduzione e ambito della qualifica

```sh
bash scripts/prepare-fixtures.sh
bash scripts/prepare-extended-fixtures.sh
docker compose -f docker-compose.yml -f compose.extended.yml run --rm --no-deps storage-rust
```

Le fixture estese usano Azurite 3.35.0, fake-gcs-server 1.54.0, pyftpdlib 2.1.0
con TLS obbligatorio, Samba (Debian Bookworm) con cifratura obbligatoria e
WsgiDAV 4.3.3. Servono esclusivamente ai test; le credenziali sono pubbliche
fixture, i certificati sono effimeri e non entrano negli artefatti distribuiti.
Sulla VM dedicata il progetto Compose ? `storage-extended`.

`qualify_extended.py` produce il report delle sette operazioni, file vuoto,
oltre 8 MiB, hash, pagina di un elemento, limiti, overwrite, conflitti e
concorrenza. `qualify_extended_faults.py` verifica i casi protocollo avversi.
Per Windows contro la VM impostare `PLENORA_FIXTURE_HOST` e `PLENORA_FTPS_CA`;
per entrambi i sistemi `PLENORA_CLI_BIN` seleziona il binario da qualificare.
Il filtro `PLENORA_QUALIFY_PROVIDERS` serve al debug: il gate finale rifiuta
report che non coprano tutti e sei i nuovi provider.

Le prove su emulatori non qualificano account Azure/GCS reali, IAM, ACL ADLS,
Windows Server o Nextcloud. Eseguire la matrice sul deployment destinatario
prima di estendere il claim. La ricevuta e i gate della release sono descritti
in [release-readiness](release-readiness.md).
