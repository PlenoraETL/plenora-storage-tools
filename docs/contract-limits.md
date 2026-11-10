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

Regola: DECISIONE DEL TITOLARE del 10/11/2026. Ogni membro sorgente,
testuale o binario, deve essere identico ai byte del file tracciato alla
revisione di rilascio, dopo la conversione di checkout Git sulla piattaforma
di build secondo `.gitattributes`. Il confronto usa la riconduzione unica
normalizzata; inventari o digest aggiunti non autorizzano file non tracciati.

Ambito: sorgente tar.gz, crate, file Python delle wheel, documenti e licenze
CLI, sorgenti nel bundle di qualifica e nei pacchetti annidati. Lo sdist
Python resta non distribuito e rifiutato. Le eccezioni generate sono un elenco
chiuso per distribuzione in `scripts/package_generated.py`, con obbligo di
testo UTF-8 e scansione; contenitori e nativi mantengono controlli distinti.
Vedere la [policy dei pacchetti](package-policy.md) per elenco e controlli.

Hazard residuo sui sorgenti: il repository puo contenere una chiave codificata
non riconosciuta dallo scanner (hex, base64 con prefisso o mescolato ad altro
testo, altre codifiche). Questo contenuto passa dalla revisione della PR.
Una modifica introdotta soltanto nel pacchetto viene rifiutata anche quando
lo scanner non riconosce la codifica. I test di non-rilevazione dello scanner
non sono prove di successo della guardia; i test di sostituzione verificano
separatamente il vincolo di identita con Git.

I 144 generati della qualifica e i cinque documenti di pubblicazione restano
in STOR-LIMIT-003: la provenienza e garantita dalla ricevuta e dal legame
HEAD/ricevuta/tag della pubblicazione, non dal contenuto. La sottostringa di
successo in `deny.log` non garantisce il resto del log. Anche la SBOM generata
della wheel resta soggetta ai limiti di codifica dello scanner: RECORD ne
vincola i byte, non ne prova il contenuto. Il limite non e ridotto al solo
repository. Cargo.toml e Cargo.lock sono confrontati con la generazione offline
da Git; il JSON VCS e chiuso su revisione e percorso. WHEEL, RECORD e METADATA
hanno controlli strutturali derivati da pyproject, README e membri della wheel.
Rientro del residuo sui sorgenti: revisione del contenuto del repository oppure
nuova decisione del titolare che estenda le codifiche riconosciute, con prove
riproducibili e gate eseguibile. Le evidenze storiche non vengono riscritte.


## STOR-LIMIT-004

Regola: policy dei pacchetti, limite dichiarato sugli output di build opachi.

Ambito: esclusivamente i membri nativi elencati nella tabella in codice
`NATIVE_DISTRIBUTIONS` di `scripts/check_packages.py`, nella distribuzione
indicata dalla stessa tabella (tag piattaforma/ABI incluso per le wheel).
La policy non puo dichiarare ruoli `build` o aggiungere esenzioni. La guardia verifica
nome, ruolo, target, ABI e formato; il classificatore non scansiona il membro
nativo come membro singolo. La scansione grezza del contenitore espanso copre
ogni byte (intestazioni, metadati, padding, directory centrale e dati dei membri),
tranne gli intervalli esatti dei dati dei nativi della tabella per quella
distribuzione: nello ZIP i byte compressi o memorizzati della voce, nel tar i
byte dati del membro, escluso il padding dei blocchi. Sono gli stessi intervalli
calcolati dal controllo che vieta byte non contabilizzati. Le intestazioni dei
nativi e i descrittori ZIP restano scansionati. Tutti gli altri membri, inclusi
sorgenti testuali e binari versionati, contenitori dichiarati e metadati delle
wheel, restano scansionati. Il classificatore del singolo nativo non apre un
payload accodato o incorporato, per esempio uno ZIP dopo un'intestazione ELF/PE.
I test ELF/PE per CLI ed estensioni documentano l'esenzione come limite
dichiarato, non come successo della guardia. Vedere la [policy dei pacchetti](package-policy.md).

Hazard: un binario prodotto da una build compromessa puo contenere payload
non rilevati. Mitigazione: gli output di build sono prodotti dalla CI dai
sorgenti, che sono sotto inventario esatto, digest e scansione. I nomi interni sono ricondotti ai percorsi del repository; i confronti usano
separatori `/`, Unicode NFC e casefold, rifiutando percorsi ambigui e collisioni.
Nessun membro della tabella puo riclassificare un percorso tracciato da Git
o appartenente all'inventario sorgente; il limite riguarda il contenuto del binario generato.

Rientro: verifica di riproducibilita o attestazione della build, con prove
riproducibili e gate eseguibile.
