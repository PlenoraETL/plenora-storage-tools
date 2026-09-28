# Controlli di qualità della serie 2.0

La [roadmap](roadmap-2.0.0.md) definisce il perimetro. Questo documento descrive
i controlli implementati; gli esiti di sviluppo non qualificano gli artefatti
della release finale.

## Baseline e responsabilità

La [misura iniziale](code-size-baseline-1.0.0.json) è stata raccolta sui sorgenti
1.0.0 del commit `67668f47a071e35d64169f3790634692fc5fbd12`, prima del refactoring:
10.043 righe di codice proprio, 11.129 righe fisiche. Il fork SMB è riportato
separatamente. Le metriche sono definite sotto; non rappresentano coverage o
una percentuale di maturità.

Il confronto locale delle API Rust sul target Windows dopo la separazione ha
conservato tutte le firme della baseline 1.0 senza rigenerarla. Le API Linux,
CLI e della wheel finale devono superare i rispettivi gate sul candidato.

## Dimensioni del prodotto

`python scripts/code_size.py --check` misura le sorgenti Rust del workspace e
il pacchetto Python, inclusi gli stub. Esclude moduli Rust `cfg(test)` inline
e file figli di test identificati dalle dichiarazioni dei moduli, anche nel fork;
test di integrazione,
esempi, script di qualifica, output di build ed evidenze sono fuori dalle radici
di prodotto. I moduli pubblici opzionali del fork SMB, anche quelli della sua
feature `testing`, fanno parte del sorgente distribuito del fork e rimangono
nel suo conteggio separato.

Le righe fisiche includono documentazione e spazi; quelle di codice contano
righe con token, escludendo commenti e contenuto dei letterali Rust. In Python
si contano le righe di inizio token, escludendo commenti e docstring. Il conteggio
non è confrontabile direttamente con LOC di strumenti che usano altre regole.
Gli attributi e gli helper `cfg(test)` isolati fuori da moduli di test rimangono
nel conteggio del file: il controllo non simula la compilazione condizionale Rust.
Non esiste codice di prodotto generato nelle radici attuali: introdurlo richiede
una classificazione esplicita prima di aggiornare il budget.

Il budget versionato in `scripts/code-size-budget.json` ha limiti per componente
e per file. La misura dopo la separazione concede il 10% per il codice e il 20%
per le righe fisiche, arrotondati a 50 righe. La maggiore tolleranza fisica evita
di penalizzare rustdoc. Nuovi componenti o sforamenti richiedono una revisione
esplicita: il controllo non aggiorna da solo i limiti. La crescita degli import
e dei moduli durante una separazione non viene presentata come riduzione del codice.

## Documentazione Rust e commenti

Il codice proprio nega le omissioni di documentazione pubblica e conserva
Clippy `all`, `pedantic` e `nursery` senza le quattro deroghe globali della 1.0.
Le eccezioni locali spiegano il motivo: firme pubbliche da mantenere, consumo
degli errori al confine di traduzione, sequenze di pubblicazione da leggere
insieme o matrici di test da applicare allo stesso ciclo di vita.

`python scripts/check_rust_docs.py` compila rustdoc con warning rifiutati ed
esegue i doctest dei crate propri. Il fork SMB conserva la disciplina upstream
e il suo inventario pubblico continua a essere verificato separatamente.

`python scripts/check_comments.py` controlla Rust, Python, stub, shell,
PowerShell, YAML e TOML posseduti dal progetto. Sono inclusi tool, test e
workflow; sono esclusi fork upstream, contratti importati, documenti storici,
fixture generate e output di build. Il debito richiede un riferimento a issue;
i marcatori espliciti di cronologia dello sviluppo non sostituiscono spiegazioni
di invarianti e limiti. I riferimenti a versioni vulnerabili restano utili.

Il controllo distingue commenti e stringhe. Here-document, here-string e
scalari YAML multilinea sono dati, non programmi interpretati dal checker:
gli script incorporati richiedono anche revisione o controlli del loro linguaggio.
La prosa non viene certificata da una ricerca di parole chiave.

## Dipendenze e inventari

`python scripts/check_dependencies.py` richiede pin esatti per dipendenze dirette
private e strumenti Cargo. `scripts/dependency-policy.json` elenca le eccezioni
compatibili per le dipendenze sul confine Rust pubblico; il lockfile fissa
comunque la risoluzione usata per build e qualifica. I vincoli upstream del fork
SMB sono conservati e verificati tramite lock, audit, deny e audit del nome
upstream. I pin non equivalgono alla promessa di essere sempre all'ultima versione.

Ogni aggiornamento deve registrare versioni effettivamente risolte e advisory,
rieseguire i consumer degli archivi e la matrice delle feature e valutare tipi
pubblici e MSRV. Le verifiche di sicurezza sono ripetute sul candidato finale;
un precedente audit non certifica nuove risoluzioni. Non si aggiorna la baseline
API soltanto per nascondere una differenza introdotta da una dipendenza.

La serie 2.0 conserva tre ambiti distinti:

| Inventario | Fonte e significato | Limiti |
| --- | --- | --- |
| `storage-sbom.cdx.json` | Grafo completo del lockfile di prodotto e digest degli artefatti | Include dipendenze opzionali/dev e di tutti i target; non prova che siano tutte collegate |
| `qualification-sbom.cdx.json` | Lockfile dei tool API/fuzz e pin Python dichiarati | Non è l'inventario dell'ambiente installato o delle transitive Python |
| `native-components/<target>/` | CLI e wheel estratte e scansionate con Syft 1.52.0; output originale, CycloneDX, formati e import nativi | Il rilevamento può non identificare pacchetti incorporati; i nomi importati non provano versioni o presenza sul sistema del consumer |

`scripts/scan_artifacts.py` verifica i digest del manifest, rifiuta estrazioni
non confinate, registra hash e dimensioni dei file nativi e controlla che lo
scanner li abbia esaminati. Conserva configurazione, versione, conteggi e
risultati originali. Timestamp e percorsi temporanei dello scanner possono
variare: l'output rilevato non è dichiarato deterministico come quello dei lockfile.
`--development` produce un report non accettabile per la qualifica.

Il job `native-components` di `release-candidate.yml` scansiona entrambi i target.
Il bundle finale 2.0 deve contenerne i report sotto `native-components/`; il gate
di pubblicazione rifiuta scansioni mancanti, modificate o riferite ad altri
artefatti. La ricevuta conserva i digest dei report e dei dati originali.

## Workflow

I controlli di dimensione, commenti, dipendenze e rustdoc sono eseguiti dal job
`product-quality` della [CI](../.github/workflows/ci.yml). Le prove dei casi
rifiutati fanno parte della suite `scripts/tests` nello stesso job. Le scansioni
sono raccolte da [release-candidate](../.github/workflows/release-candidate.yml)
e nuovamente validate durante la [pubblicazione](../.github/workflows/release.yml).
API, fuzz, coverage, fixture, trasferimenti, prestazioni e soak di due ore
restano obbligatori per i byte finali secondo il perimetro approvato.
