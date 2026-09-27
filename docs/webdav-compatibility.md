# WebDAV: configurazione qualificabile

La fixture della 1.0 usa **WsgiDAV 4.3.5 e Cheroot 11.1.2 con un solo worker**
(`numthreads=1`, `max=1`). I client possono inviare operazioni concorrenti;
il server le serve in sequenza. Questa configurazione non certifica WsgiDAV
con più worker, né altri server WebDAV.

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

La prova HTTP indipendente, ripetuta su un'istanza separata con un solo worker,
ha rilevato **zero round con più scrittori riusciti su 30**. Questo risultato
motiva la configurazione della nuova fixture; non promuove gli artefatti o
i report della precedente campagna fallita. Il candidato deve essere ricostruito
e qualificato sul nuovo commit. Le ricevute precedenti restano storiche.

## Gate riproducibile

`scripts/check_webdav_fixture.py` esegue 30 round con otto client HTTP concorrenti:
ogni round richiede una risposta 201, sette risposte 412 e il contenuto integro
del vincitore. Le chiavi sono uniche e vengono eliminate dopo la prova.
Il report omette endpoint, credenziali e contenuti dei file.

Nella verifica del nuovo gate, la fixture con più worker ha fallito in 13 round
su 30; l'istanza a un worker ha superato tutti i round, inclusa la verifica
del contenuto. Sono campagne distinte dalla riproduzione iniziale sopra.

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
