# Release 0.2.1: criteri di distribuzione

Rust, CLI e SDK Python registrano nove provider. I sei aggiunti sono descritti nella
[matrice operativa](provider-expansion.md). Le sette operazioni e i contratti
pubblici v1 rimangono compatibili; ogni configurazione ha uno schema specifico.
La ricevuta della [release 0.1.0](release-readiness-0.1.md) rimane storica e
non qualifica automaticamente i nuovi binari.

La distribuzione richiede `dist/0.2.1/release-qualification.json` con stato
`qualified_for_publication`, generato da `scripts/qualify_release.py` sul
commit definitivo e pulito. Un archivio costruito da un checkout modificato
? un candidato, anche se i test sono verdi.

## Gate richiesti

- Format, Clippy con tutti i target/feature, suite Rust Windows e Linux;
  Linux esegue anche tutti i test che richiedono le fixture.
- Sette operazioni su tutti e nove i provider per entrambi i binari finali,
  con report legati al loro SHA-256. File vuoto, oltre 8 MiB, checksum,
  paginazione, sovrascrittura, limiti e creazione concorrente senza overwrite.
- Errori CLI e pubblicazione locale, interruzioni multipart S3 e test runtime
  gi? richiesti dalla 0.1.0.
- Redirect HTTP bloccati, nomi Azure/GCS non rappresentabili rifiutati,
  token di continuazione ripetuti, propriet? WebDAV assenti, multistatus
  parziale e deadline durante commit. FTPS rifiuta certificati non fidati;
  SMB usa Samba con cifratura obbligatoria e rifiuta credenziali errate.
- Audit dipendenze, licenze e sorgenti senza eccezioni. Il controllo separato
  `audit_smb_upstream.py` cerca advisory anche sotto il nome originale `smb2`.
- Archivi Cargo verificati da un consumer esterno e CLI compilata dagli
  archivi estratti; manifest, contratti e checksum coerenti sui due target.

## Matrice e limiti

Le prove usano Windows x86_64 e Linux x86_64 / Debian Bookworm, con server
isolati sulla VM dedicata: MinIO, OpenSSH, Pure-FTPd, pyftpdlib TLS, Samba,
WsgiDAV, Azurite e fake-gcs-server. Gli emulatori cloud non verificano IAM,
rete privata cloud, rinnovo token del consumer o comportamento di account reali.
Non si estende questa evidenza ad Azure/GCS reali, Windows Server, Nextcloud
oppure ADLS con ACL complesse senza la matrice del deployment destinatario.

La [procedura operativa](release.md) descrive build, qualifica, installazione
e rollback. La CI produce candidati; il job Windows pubblico non ha accesso
alla VM privata e quindi non sigilla da solo la qualifica dei provider remoti.
Pubblicazione su registry/GitHub, tag e deployment rimangono passi separati.

La qualifica include anche la wheel Python installata, i suoi test offline e
le sette operazioni sui nove provider tramite SDK (`qualify_python.py`).
I gate di selezione provider, documentazione generata e SBOM devono passare.
