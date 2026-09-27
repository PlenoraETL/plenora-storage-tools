# Perimetro di compatibilità della 1.0

Decisione del 27 settembre 2026: la prima 1.0.0 mantiene tutti i nove provider
in Rust, CLI e SDK Python, con distribuzione solo tramite GitHub Releases.
Le prove su account AWS, Azure e Google Cloud reali sono rinviate e non sono
un requisito di uscita per questa release. Il rinvio non equivale a un PASS
e non certifica il funzionamento sui servizi cloud gestiti.

## Sistemi nel perimetro di qualifica

| Provider | Sistema su cui eseguire la qualifica degli artefatti finali |
| --- | --- |
| local | Filesystem locale dei target Windows e Linux dichiarati |
| S3-compatible | MinIO, con HTTP esplicitamente autorizzato e prove TLS Linux |
| SFTP | OpenSSH con pin della host key |
| FTP | Pure-FTPd |
| FTPS esplicito | pyftpdlib con TLS obbligatorio |
| Azure Blob | Azurite |
| Google Cloud Storage | fake-gcs-server |
| SMB | Samba con cifratura SMB3 |
| WebDAV | WsgiDAV |

Questa tabella definisce il perimetro; gli esiti effettivi e i digest sono nella
ricevuta della singola release. Le versioni delle fixture sono fissate nei file
Compose e Docker del commit qualificato. Una capability nella discovery indica
un comportamento implementato, non la certificazione di ogni server che espone
quel protocollo.

AWS S3, Azure Blob gestito e Google Cloud Storage reale restano **non qualificati**.
In particolare non si estendono le prove degli emulatori a IAM/RBAC, credenziali
temporanee, scadenza e rinnovo token, policy o vincoli di rete del cloud.
Windows Server, Nextcloud, ADLS DFS/ACL, SMB Kerberos/DFS e altri server o
modalità non provati non rientrano nella compatibilità dichiarata.

## Evidenze e distribuzione

`release-manifest.json` e `release-qualification.json` riportano
`qualification_scope`: fixture previste per provider e servizi cloud reali con
stato `not_qualified`. Il gate di verifica rifiuta un perimetro differente;
i test negativi sono eseguiti nel job `product-quality`. Questo documento è
incluso anche negli archivi CLI distribuiti.

Restano obbligatori build e test dei due target, matrice dei nove provider sulle
fixture, SDK installato, API, audit dipendenze, integrità degli artefatti e prove
di affidabilità previste dal [piano](roadmap-1.0.0.md). Le prove di durata restano
sulla VM dedicata. Non vengono eliminati altri gate né riutilizzate ricevute di
binari o wheel differenti.

La qualifica dei cloud reali resta nel backlog: richiederà ambienti dedicati,
identità limitate alle risorse di prova e un budget concordato. Un futuro
aggiornamento del perimetro dovrà riferirsi a nuove evidenze riproducibili.
