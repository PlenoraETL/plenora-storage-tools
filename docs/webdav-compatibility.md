# WebDAV: configurazione qualificabile

La fixture della 1.0 usa **WsgiDAV 4.3.5 e Cheroot 11.1.2 con richieste WSGI
serializzate da un lock in un solo processo**. I 32 worker HTTP gestiscono le
connessioni; una sola richiesta applicativa alla volta valuta le precondizioni
e accede al filesystem. Il lock resta acquisito fino alla chiusura della risposta.
La coda di ascolto è 64 e il limite di connessioni persistenti è 256.

Questo è un adattamento esplicito del laboratorio, non una certificazione di
WsgiDAV predefinito, di più processi che condividono il filesystem, né di altri
server WebDAV. Codice e configurazione sono in `docker/extended/server.py`.

## Difetto osservato durante RC.2

Il 27 settembre 2026 la qualifica Windows degli artefatti del commit `1c7c10e`
ha rilevato due upload riusciti sulla stessa chiave con `overwrite=false`.
La qualifica si è fermata. Una riproduzione indipendente con Python `http.client`,
senza Storage Tools, ha rilevato più risposte di successo in **6 round su 30**,
con otto scrittori concorrenti e `If-None-Match: *`, sulla fixture WsgiDAV 4.3.5
con i dieci worker predefiniti di Cheroot.

Nel server la verifica della precondizione precede la creazione del file:
due worker possono osservare entrambi una destinazione assente. L'adapter
Storage invia già la precondizione prevista da
[RFC 9110, sezione 13.1.2](https://www.rfc-editor.org/rfc/rfc9110.html#section-13.1.2).
Un controllo di esistenza nel client non eliminerebbe questa corsa tra processi.

Una prima prova con un solo worker ha rilevato zero round con più scrittori
riusciti su 30. Il benchmark successivo ha però mostrato attese di 10–20 secondi
nella gestione delle connessioni. Inoltre il probe remoto da Windows ha
rilevato reset con i limiti predefiniti delle connessioni. Per questo la
configurazione finale serializza le richieste WSGI mantenendo i worker HTTP.

Il prototipo finale ha superato il probe Windows completo e le sette operazioni
della CLI, inclusa la creazione concorrente. Una campagna Linux di sviluppo con
quattro client e cinque round WebDAV ha misurato 140 operazioni, con massimo
42 ms; non è un SLO né il confronto finale della release. Le vecchie campagne
fallite o interrotte restano conservate. Il candidato deve essere ricostruito
e qualificato sul nuovo commit; queste prove non promuovono altri artefatti.

## Gate riproducibile

`scripts/check_webdav_fixture.py` esegue 30 round con otto client HTTP concorrenti:
ogni round richiede una risposta 201, sette risposte 412 e il contenuto integro
del vincitore. Le chiavi sono uniche e vengono eliminate dopo la prova.
Il report omette endpoint, credenziali e contenuti dei file.

Nella verifica del nuovo gate, la fixture con più worker ha fallito in 13 round
su 30; l'istanza a un worker ha superato tutti i round, inclusa la verifica
del contenuto. Anche il successivo prototipo con serializzazione WSGI ha
superato tutti i round da Windows. Sono campagne distinte dalla riproduzione
iniziale sopra.

Il controllo è eseguito da `scripts/verify.sh` nei workflow CI e release-candidate,
e da `scripts/qualify_target.py` sulla fixture usata per ciascun target finale.
`qualify_release.py` richiede entrambi i report `webdav-fixture.json`, con commit
pulito e tutti i round completi. Il manifest dichiara il numero di worker in
`qualification_scope.fixture_configuration`; la ricevuta conserva i digest.

Nel laboratorio Windows `PLENORA_WEBDAV_PORT` può selezionare la porta di
un'istanza separata senza riavviare una campagna già attiva. Le altre identità
restano quelle pubbliche delle fixture. L'hash dell'ambiente delle prestazioni
comprende anche il codice e la configurazione dei server in `docker/`.

Le prestazioni misurate con questo server seriale descrivono solo questa
configurazione. Un deployment diverso deve dimostrare il rispetto delle
precondizioni prima di usare la creazione esclusiva sotto concorrenza.
