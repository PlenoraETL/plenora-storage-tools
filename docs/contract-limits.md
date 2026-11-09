# Limiti dell'adozione contrattuale

Il pin adottato e `3aec9f724f5a0ad21366f293895b4d0dbcd0aad1`.
Il manifest generato incorpora le deviazioni di `contracts/adoption-policy.json`.

| Registro | Regola | Ambito e comportamento | Hazard | Rientro |
| --- | --- | --- | --- | --- |
| STOR-LIMIT-001 | RT-024 | Il limite opzionale del binding conta i byte del payload JSON di successo gia materializzato. Non limita la costruzione del Value, gli envelope o i flussi artifact. Il costruttore esistente conserva l'assenza di un limite implicito. | Un'allocazione puo fallire prima dell'errore tipizzato; il trasporto deve riservare spazio per metadata ed errori. | Budget condiviso con materializzazione fallibile, limite predefinito verificato e prove sul trasporto consumer. |

La selezione della destinazione e il contesto persistente di negoziazione
(RT-026 e RT-027) appartengono al consumer: questo binding riceve invocazioni,
non instrada richieste. La verifica del payload non certifica quei comportamenti.

## STOR-LIMIT-001

Regola RT-024, superficie runtime. Il consumer configura
`RuntimeBinding::with_result_payload_limit(bytes)` e riserva spazio separato
per envelope ed errori. Il limite misura JSON UTF-8 compatto, inclusi escape;
il valore esatto e accettato, quello superiore fallisce integralmente.
La deviazione di materializzazione e annotata in `runtime_result_limit.rs`.

## STOR-LIMIT-002

Regola ERR-016, provider S3, SFTP, FTP/FTPS, tutte le superfici pubbliche.
I fallback generici delle mutazioni possono classificare una conferma persa
come `execution` invece di `io`; conservano `unknown` e `requires_recovery`.
Hazard: un consumer che instrada il recupero in base alla sola categoria puo
scegliere un percorso inappropriato. Rientro: distinguere le cause di trasporto
nei wrapper upstream, con regressioni di conferma persa e timeout per ogni
provider. Il binding preserva correttamente le categorie esplicite; questo
non dimostra la classificazione dei fallback. Le annotazioni sono in
`errors.rs` dei tre crate provider. Local, HTTP e SMB hanno mapping distinti:
questa deviazione non li qualifica ne estende automaticamente il loro ambito.

## STOR-LIMIT-003

Regola: policy dei pacchetti e DECISIONE DEL TITOLARE del 06/11/2026 sulle
guardie statiche: coprire per costruzione le forme dirette e dichiarare le
varianti come limite, senza inseguirle.

Ambito: scansione dei membri testuali ammessi in tutte le distribuzioni,
compresi i contenitori annidati, tramite `scripts/check_packages.py`.
Riconosce PEM privati e DER privato in base64 nudo; non garantisce il
riconoscimento di chiavi ricodificate in hex, base64 con prefisso o mescolato
ad altro testo, o altre codifiche. I test hex e base64 con prefisso documentano
la mancata rilevazione come comportamento dichiarato, non come successo della
guardia. Vedere la [policy dei pacchetti](package-policy.md).

Hazard: il contenuto codificato di un file testuale ammesso puo contenere una
chiave non rilevata. Un file di testo nuovo o modificato e comunque
nell'inventario esatto e passa dalla revisione della PR; il limite riguarda
solo il contenuto codificato dei file testuali ammessi.

Rientro: nuova decisione esplicita del titolare che estenda il perimetro a
codifiche nominate, con prove riproducibili e gate eseguibile per quelle forme.
Non e richiesto inseguire varianti nell'attuale perimetro.
