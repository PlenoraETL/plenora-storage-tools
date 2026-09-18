# Regole di sviluppo

Storage Tools segue gli stessi principi di qualità di Database Tools, applicati
al dominio storage:

1. Dichiarare capability e compatibilità soltanto quando esiste una prova
   riproducibile. Un test omesso o non eseguito non vale come test superato.
2. Conservare i contratti pubblici; le incompatibilità richiedono una nuova major.
3. Derivare l'inventario da codice e discovery: `docs/STATO.md` è generato da
   `scripts/render_state.py`. Le ricevute di qualifica descrivono artefatti precisi.
4. Non inserire byte dei file, credenziali, endpoint o eccezioni dei callback nei
   messaggi pubblici. Conservare categoria, fase, effetto e retry.
5. Quando emerge un difetto, controllare gli altri provider e le altre superfici
   che condividono lo stesso comportamento.
6. Collegare ogni nuovo gate a un workflow eseguibile. Separare prove offline,
   fixture locali e compatibilità effettivamente verificata sui sistemi reali.

Leggere `docs/database-alignment.md` per il perimetro, `docs/STATO.md` per
l'inventario, `docs/release.md` per packaging e qualifica, i workflow per i gate.
Non aggiornare i documenti storici per farli sembrare evidenze della release nuova.
