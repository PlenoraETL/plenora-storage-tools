# Confronto di qualità con Database Tools

Analisi del 27 settembre 2026. Database Tools è il riferimento richiesto per
la qualità di Storage, con adattamenti al dominio storage. Il confronto riguarda
codice, contratti, documentazione e controlli eseguibili; non assegna un punteggio
di maturità in base al numero di funzionalità, righe o test.

Revisioni esaminate:

- Database Tools: `850723d86be9d0cb5d8b0643da6cae971c7ec068`, versione 6.0.0;
  copia locale pulita e HEAD remoto coincidente al momento della verifica.
- Storage Tools: `bfbffd385df755e8e3ec97bb964fcb53dc9e638a`, versione Rust
  `1.0.0-rc.1`, versione Python `1.0.0rc1`.

Le osservazioni su Database derivano dalla lettura della revisione indicata:
questa analisi non riesegue né certifica la sua suite. Le modifiche documentali
successive non cambiano l'identità degli artefatti RC già costruiti.

## Valutazione

Storage ha una buona base architetturale e controlli operativi sostanziali, ma
non raggiunge ancora la maturità complessiva del riferimento. Il divario riguarda
soprattutto il contratto Python per chi sviluppa applicazioni, la manutenzione
automatica di sorgenti e documenti e la chiusura della catena di distribuzione.
Non emerge la necessità di aggiungere altri provider per ottenere questo risultato.

| Area | Database Tools | Storage Tools | Intervento |
| --- | --- | --- | --- |
| Architettura | Core, engine, provider, CLI, binding Python e testkit distinti | Core, factory engine, adapter e superfici pubbliche già separati | Conservare questa struttura; estrarre helper condivisi dei test dove esiste duplicazione |
| Organizzazione Rust | Test separati dai file di prodotto e gate sul layout; budget della dimensione del prodotto | Diversi moduli includono implementazione e test; manca un controllo equivalente | Separare prima i test, poi suddividere per responsabilità |
| CLI | Moduli per contesto, formato, sicurezza e gruppi di comandi | Contratto JSON e opzioni verificati; `main.rs` concentra parsing, esecuzione, segnali e test | Estrarre responsabilità senza cambiare flag, JSON, exit code o redazione |
| Python | Moduli e stub pubblici, consumer controllato con mypy strict | Engine sync/async, resolver e cancellazione presenti; risultati `dict[str, Any]` e controlli `**controls: Any` | Tipi dei risultati e dei controlli, esempi eseguibili e gate sul consumer installato |
| Documenti | Indice corrente, inventario generato, controlli su link, ancore, comandi ed esempi Python | Inventari generati e controllo versioni/link; verifica limitata a README, AGENTS e Markdown al primo livello di docs | Ampliare il gate allo SDK e agli esempi; distinguere guide correnti da resoconti storici |
| Commenti e rustdoc | Controllo dei commenti e limitazione di note provvisorie prive di riferimento | Commenti utili su sicurezza e cancellazione; documentazione pubblica disomogenea, `missing_errors_doc` consentito nel workspace | Documentare errori, invarianti, ownership e limiti; eccezioni lint locali motivate |
| Dipendenze | Pin esatti per le dipendenze workspace condivise; controlli di filiera e SBOM esteso | Versioni numeriche condivise allineate, Cargo.lock e build locked; audit anche per fuzz e strumenti | Esplicitare policy di aggiornamento e ambito SBOM per prodotto, build e qualifica |
| Coverage | Workflow su push/PR, superfici Rust prodotto, bridge e Python separate | Soglie per crate e wrapper Python; coverage Rust manuale e comprendente test inline | Misurare il solo prodotto e rendere automatico un controllo sostenibile sulle PR |
| Performance | Budget di regressione per campagne live; microbenchmark offline anche solo informativi | Trasferimenti, checksum, concorrenza e limite RSS; tempi registrati senza baseline di regressione | Baseline ripetibile con ambiente, campioni, mediana/p95 e soglie misurate |
| Release | Workflow che raccoglie materiali, attesta e allega gli asset alla release | Build e qualifica candidati; ricevuta locale e artefatti Actions | Aggregazione completa delle evidenze e pubblicazione GitHub verificata |

## Evidenze e limiti del confronto

### Struttura, CLI e commenti

Nel riferimento, [rust-ci.yml][db-ci] esegue controlli distinti su
[layout dei test][db-layout], [commenti][db-comments] e dimensione del prodotto.
Separare i test in moduli figli esterni conserva l'accesso agli elementi privati:
non richiede di allargare l'API Rust.

In Storage, [SFTP](../crates/plenora-storage-sftp/src/lib.rs),
[FTP](../crates/plenora-storage-ftp/src/lib.rs),
[S3](../crates/plenora-storage-s3/src/lib.rs) e
[CLI](../crates/plenora-storage-cli/src/main.rs) sono esempi di file che
mescolano responsabilità e test. La dimensione da sola non è un difetto:
anche il `main.rs` del CLI Database è ampio. Il criterio utile è poter cambiare
parsing, pubblicazione dei file o gestione degli errori senza coinvolgere tutto
il comando e senza duplicare le stesse regole tra superfici.

Entrambi i workspace vietano unsafe nel codice soggetto ai lint e attivano
Clippy severo. Il [manifest Storage](../Cargo.toml) consente però a livello
workspace anche `missing_errors_doc`, `too_many_lines`, `needless_pass_by_value`
e `missing_const_for_fn`. Le eccezioni non dimostrano un bug: vanno rivalutate
per responsabilità. La priorità dei commenti è spiegare invarianti, effetti di
una cancellazione e garanzie di pubblicazione, evitando di parafrasare il codice.

### SDK e documentazione

Il riferimento verifica un [consumer Python tipizzato][db-typing] nella CI.
Storage distribuisce `py.typed` e lo stub nativo, ma il
[wrapper pubblico](../crates/plenora-storage-py/python/plenora_storage/__init__.py)
restituisce dizionari generici. Il marker PEP 561 non prova che tutte le chiavi
dei risultati o tutte le opzioni siano verificabili da un type checker.

Introdurre `TypedDict` per i risultati e tipi precisi per le policy permette
di conservare i dizionari a runtime. Le annotazioni fanno parte della superficie
pubblica censita: le variazioni devono passare dal gate di compatibilità e da
una revisione della baseline. Non sostituire silenziosamente i risultati con
oggetti dal comportamento diverso.

L'implementazione asyncio di Storage delega al motore Rust mediante thread e
gestisce l'esito della cancellazione: questa scelta non è di per sé un difetto.
Servono esempi installabili di sync, async, resolver, timeout e riconciliazione
degli esiti ambigui, oltre ai test già presenti sulla wheel.

Il [controllo documentale Database][db-docs] è più ampio del
[controllo Storage](../scripts/check_docs.py), che non visita neppure il README
del crate Python e non controlla le ancore. I documenti storici Storage devono
rimanere attribuiti alle loro versioni; l'indice corrente deve evitare che una
guida alla 0.2.1 appaia come procedura definitiva della 1.0.

### Dipendenze, misure e qualifica

Le nove dipendenze condivise nei manifest workspace hanno le stesse versioni
numeriche: bytes, futures-util, rustls, serde, serde_json, sha2, thiserror, tokio
e tokio-util. Entrambi i binding usano PyO3 `=0.29.2` e ABI3 da Python 3.10.
Database usa pin esatti per quelle dipendenze workspace; Storage intervalli
compatibili e lockfile. Non è una prova che Storage usi librerie obsolete.
La MSRV 1.92 di Storage è un impegno di compatibilità: portarla alla 1.98 del
riferimento richiede una ragione e una verifica, non un allineamento meccanico.

Storage ha già gate su contratti Rust/CLI/Python, parser fuzz, errori di commit,
ENOSPC/EACCES e trasferimenti. La [coverage documentata](reliability.md) include
test Rust inline; non va confrontata direttamente con la percentuale del solo
prodotto Database. Anche le soglie di [coverage Database][db-coverage] sono
limiti minimi, non risultati misurati. Una percentuale più alta non dimostra
automaticamente una qualità maggiore.

Il [budget live Database][db-perf] confronta campioni e ambiente prima di
valutare regressioni. Storage impone un limite RSS e verifica checksum e
rifiuti senza effetti; non dispone ancora di un equivalente confronto delle
prestazioni. I test da 1 GiB non sono un successo di upload per tutti i provider:
i percorsi bufferizzati verificano il rifiuto previsto dal limite.

Il [workflow Database][db-release] comprende raccolta SBOM, attestazioni e
allegati alla release. In Storage,
[release-candidate](../.github/workflows/release-candidate.yml) produce asset
Actions e impone già API, fuzz, audit e matrice Python. Tuttavia
[qualify_release.py](../scripts/qualify_release.py) può emettere
`qualified_for_publication` senza acquisire tutte le evidenze M3: non richiede
i report API, fuzz, coverage, matrice CPython completa, trasferimenti, soak e
benchmark come prerequisiti della ricevuta. La presenza di alcuni gate in CI
non chiude questa lacuna dello strumento finale.

I report di due wheel ricompilate dallo stesso commit non sono intercambiabili
quando attestano digest differenti. La pubblicazione deve scegliere gli asset
esatti da qualificare e conservare le prove pertinenti. La SBOM Storage censisce
il Cargo.lock principale; gli audit dei lockfile ausiliari non ne estendono
automaticamente il contenuto alle dipendenze native o Python degli strumenti.

## Priorità e criteri di completamento

Questa lista precisa il [piano 1.0](roadmap-1.0.0.md); non dichiara nuove prove
superate e non aggiunge automaticamente altre campagne di 24 ore.

| Ordine | Intervento | Criterio verificabile |
| --- | --- | --- |
| 1 — prima della pubblicazione | Completare la ricevuta e il workflow GitHub Release | Rifiuto di report assenti, falliti, incompleti o appartenenti ad altri artefatti; tutti i gate richiesti acquisiti con identità verificabile; asset pubblicati con checksum/provenienza e smoke test dopo download |
| 2 — prima del congelamento Python | Precisare risultati, policy e controlli dello SDK | Consumer mypy strict dalla wheel installata su versioni supportate; esempi sync/async eseguiti; runtime e contratti esistenti preservati |
| 3 — prima della pubblicazione | Chiudere misure e documentazione del supporto promesso | Baseline prestazioni ripetibile, revisione dei percorsi critici non coperti, evidenze finali del candidato e istruzioni GitHub provate su entrambi i target |
| 4 — consolidamento incrementale | Separare test e responsabilità nei moduli più esposti | Gate sul layout in CI, coverage del prodotto distinta, test invariati negli esiti e nessuna modifica involontaria alle API |
| 5 — consolidamento incrementale | Rafforzare documenti, commenti e filiera | Gate su README dei crate, ancore ed esempi; rustdoc degli errori pubblici; SBOM con ambiti espliciti e policy dipendenze documentata |

Ogni nuovo gate deve essere collegato a un workflow eseguibile. Il refactoring
interno procede a passi piccoli e verificati; non richiede di importare ORM,
SQL, Arrow o altre funzionalità specifiche del riferimento.

Le prove con account AWS/Azure/GCS restano rinviate per decisione esplicita e
non bloccano la prima 1.0. Il [perimetro dichiarato](compatibility-1.0.md)
continua a distinguere fixture verificate e servizi reali non qualificati.
Una campagna in corso non vale come prova superata; questo confronto non è
una ricevuta di qualifica né autorizza a riattribuire evidenze storiche.

[db-ci]: https://github.com/PlenoraETL/plenora-database-tools/blob/850723d86be9d0cb5d8b0643da6cae971c7ec068/.github/workflows/rust-ci.yml
[db-layout]: https://github.com/PlenoraETL/plenora-database-tools/blob/850723d86be9d0cb5d8b0643da6cae971c7ec068/scripts/check_test_layout.py
[db-comments]: https://github.com/PlenoraETL/plenora-database-tools/blob/850723d86be9d0cb5d8b0643da6cae971c7ec068/scripts/check_comments.py
[db-typing]: https://github.com/PlenoraETL/plenora-database-tools/blob/850723d86be9d0cb5d8b0643da6cae971c7ec068/crates/plenora-database-py/typing/sdk_v2_contract.py
[db-docs]: https://github.com/PlenoraETL/plenora-database-tools/blob/850723d86be9d0cb5d8b0643da6cae971c7ec068/scripts/check_docs.py
[db-coverage]: https://github.com/PlenoraETL/plenora-database-tools/blob/850723d86be9d0cb5d8b0643da6cae971c7ec068/scripts/coverage_budget.json
[db-perf]: https://github.com/PlenoraETL/plenora-database-tools/blob/850723d86be9d0cb5d8b0643da6cae971c7ec068/benchmarks/baseline/postgres-performance-budget.json
[db-release]: https://github.com/PlenoraETL/plenora-database-tools/blob/850723d86be9d0cb5d8b0643da6cae971c7ec068/.github/workflows/python-wheel.yml
