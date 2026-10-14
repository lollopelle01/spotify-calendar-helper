# Piano di implementazione: spotify-calendar-helper

Ultimo aggiornamento: 9 ottobre 2026

## 1. Obiettivo

Hai playlist mensili ("Marzo 2025", ecc.) organizzate per anno nella cartella `calendario`, con dentro i brani messi nei preferiti in quel mese. Il progetto legge queste playlist e genera/aggiorna in automatico playlist "trasversali":

- una playlist per **artista** (tutti i suoi brani presenti nel calendario);
- una playlist per **genere**;
- eventualmente altri tagli in futuro (anno di uscita, decennio...), con la stessa architettura.

L'elaborazione si rilancia ogni mese e **aggiorna** le playlist già create senza duplicare nulla.

## 2. Cosa abbiamo già nel repo

- `pyproject.toml` (Python >= 3.11, `requests`, `python-dotenv`, comando `sch`)
- `.env.example` (solo `SPOTIFY_CLIENT_ID` e `SPOTIFY_REDIRECT_URI`) e `.gitignore` che esclude `.env`, `config.toml`, `data/`, `*.token.json`
- `config.example.toml` con `[calendar] name_regex` e `[artists] min_tracks / primary_artist_only / playlist_prefix`
- `src/spotify_calendar_helper/config.py` con `Settings` e i path `data/token.json`, `data/calendar_cache.json`, `data/state.json`
- `tests/` vuota, `README.md` vuoto

Il lavoro è quindi: tutto il resto (auth, client, scansione, raggruppamento, sync, CLI).

## 3. Vincoli reali delle API Spotify (verificati il 9/10/2026)

Dal changelog ufficiale di febbraio 2026 e da fonti di terze parti. **Questi vincoli guidano le scelte di design.**

1. **Nessuna password.** L'app usa OAuth con PKCE: serve solo il Client ID (già previsto in `.env`). La tua password Spotify non viene mai usata né salvata. L'app va creata nella dashboard sviluppatori da un account Premium (il tuo), e il tuo account va aggiunto in *User Management* dell'app.
2. **Le cartelle non esistono nell'API.** Non si possono leggere né creare né spostare playlist dentro cartelle. Conseguenze:
   - il calendario si riconosce **dal nome** delle playlist (regex su mese/anno), non dalla cartella;
   - le playlist create finiscono nella root; le sposti a mano **una volta sola**: l'ID della playlist non cambia, quindi gli aggiornamenti successivi finiscono comunque nel posto giusto.
3. **Endpoint playlist rinominati**: si usa `/playlists/{id}/items` (non più `/tracks`), `POST /me/playlists` per creare, e nelle risposte il campo `tracks` diventa `items` e `track` diventa `item`. Il contenuto è leggibile solo per le playlist **tue**.
4. **Rimossi** `GET /artists` (batch), `GET /tracks` (batch), `popularity`, `followers`, `available_markets`, `linked_from`. Si può usare solo `GET /artists/{id}` singolo.
5. **Generi: non disponibili.** `GET /artists/{id}` non restituisce più il campo `genres` per le app in Development Mode (confermato febbraio 2026 su Rick Astley e Radiohead). I comandi `plan genres`, `sync genres` e `genres-report` sono disabilitati e restituiscono un messaggio esplicativo. Il codice di grouping e genres resta nel progetto per una futura reintroduzione tramite Last.fm o MusicBrainz.
6. **Audio features, recommendations, related artists**: non disponibili da novembre 2024. Non ci contiamo.
7. **Limiti da verificare nello spike**: dimensione massima del batch di `POST /playlists/{id}/items` (storicamente 100, un client terzo riporta 10), limite di ricerca a 10, scadenza del refresh token (una fonte dice 6 mesi dall'autorizzazione originale: va previsto un re-login guidato).

## 4. Decisioni di design

| Tema | Decisione | Motivo |
|---|---|---|
| Identificazione calendario | Regex sul nome (config esistente) + lista `ignore_playlist_ids` | Le cartelle non sono visibili |
| Identificazione playlist derivate | **Marker nella descrizione** (`[sch:artist:<spotify_artist_id>]`) + `state.json` come cache | Se perdi `state.json` si ricostruisce da Spotify; rinominare la playlist non rompe il collegamento |
| Chiave artista | ID Spotify, mai il nome | Nomi che cambiano o con caratteri strani |
| Aggiornamento | **Additivo** di default (aggiunge i brani mancanti, non rimuove mai) | Il calendario è append-only; sicuro. Modalità `mirror` opzionale con `--prune` |
| Sicurezza | `--dry-run` disponibile ovunque e **primo run consigliato**; conferma interattiva se il piano crea più di N playlist | Il primo run può creare centinaia di playlist |
| Visibilità | Playlist create **private** di default | Non esporre il tuo ascolto per sbaglio |
| Duplicati | Dedup per ID brano; dedup opzionale per (titolo normalizzato + artista) | Stesso brano in più mesi o in versioni diverse |
| Ordine nella playlist | Cronologico (mese del calendario, poi `added_at`); i nuovi brani si accodano | Mantiene il "filo del tempo" |
| Cache | `data/calendar_cache.json` con `snapshot_id` per playlist: si rileggono solo le playlist cambiate | Meno chiamate, meno rate limit |
| Brani non gestibili | Local file, episodi e item nulli/non disponibili vengono saltati e riportati nel report | Non hanno URI utilizzabile |

## 5. Architettura

```
src/spotify_calendar_helper/
  models.py          # dataclass condivise (contratto tra gli agenti)  [Fase 0]
  config.py          # esistente, da estendere
  auth.py            # PKCE login, refresh token, salvataggio data/token.json
  client.py          # wrapper HTTP: retry, 429/Retry-After, paginazione, endpoint /items
  calendar_scan.py   # trova le playlist del calendario, legge i brani, usa la cache
  cache.py           # lettura/scrittura calendar_cache.json (snapshot_id)
  grouping.py        # calendario -> specifiche di playlist (per artista, per genere)
  genres.py          # provider di generi + mappa generi fini -> macro-generi
  state.py           # state.json: chiave -> playlist_id, storico run
  sync.py            # confronta "desiderato" vs "esistente" e applica (crea/aggiunge)
  cli.py             # comando `sch`
tests/
  fakes.py           # FakeSpotifyClient in memoria  [Fase 0]
  fixtures/          # JSON di risposte API (anonimizzate)
```

### 5.1 Contratto (`models.py`, scritto in Fase 0)

- `ArtistRef(id, name)`
- `Track(id, uri, name, artists: list[ArtistRef], added_at, album_name)`
- `CalendarPlaylist(id, name, year, month, snapshot_id, tracks: list[Track])`
- `PlaylistSpec(kind: "artist"|"genre", key, name, description, track_uris: list[str])`: la playlist **desiderata**
- `SyncReport(created, updated, unchanged, skipped_tracks, warnings)`

Interfaccia `SpotifyClient` (Protocol): `current_user()`, `list_my_playlists()`, `get_playlist_items(id)`, `create_playlist(name, description, public=False)`, `add_items(id, uris)`, `remove_items(id, uris)`, `update_details(id, name=None, description=None)`, `get_artist(id)`.

Interfaccia `GenreProvider` (Protocol): `genres_for_artist(artist_id, artist_name) -> list[str]`.

### 5.2 Flusso di un run

1. `auth`: token valido (refresh automatico, altrimenti login nel browser)
2. `calendar_scan`: elenca le tue playlist, filtra col regex, applica la selezione (`--year`, `--from/--to`, `--all`), rilegge solo quelle con `snapshot_id` cambiato
3. `grouping`: costruisce le `PlaylistSpec` desiderate
4. `sync`: legge lo stato attuale delle playlist derivate (via `state.json` + marker), calcola il diff, applica (o stampa il piano con `--dry-run`)
5. Report finale, inclusa la lista **"playlist nuove da spostare nella cartella giusta"**

### 5.3 Generi (punto delicato)

Spike iniziale: chiamare `GET /artists/{id}` su 5-10 artisti noti e vedere se `genres` è popolato.

- **Se popolato**: `SpotifyGenreProvider` (una chiamata per artista, cache su disco con TTL ~90 giorni; si chiamano solo gli artisti che superano `min_tracks`).
- **Se vuoto**: provider alternativo a scelta in config: **Last.fm** (`artist.getTopTags`, serve una API key gratuita in `.env` come `LASTFM_API_KEY`) oppure **MusicBrainz** (senza chiave, 1 richiesta/secondo, più lento ma ok per qualche centinaio di artisti). Il provider è dietro l'interfaccia `GenreProvider`, quindi il resto del codice non cambia.

In entrambi i casi serve una **mappa generi fini → macro-generi** (es. "bedroom pop", "indie pop" → "Indie/Pop"), in un file `genre_map.toml` modificabile da te. Senza questa mappa si avrebbero centinaia di micro-generi con 1-2 brani ciascuno. I generi non mappati vanno in un report per aggiornare la mappa a mano. Un brano con più artisti/generi può finire in più playlist di genere (configurabile).

## 6. Configurazione (estensioni a `config.example.toml`)

```toml
[calendar]
name_regex = ""
ignore_playlist_ids = []

[artists]            # esistente
min_tracks = 4
primary_artist_only = true
playlist_prefix = "Artista - "

[genres]
enabled = true
provider = "spotify"          # "spotify" | "lastfm" | "musicbrainz"
min_tracks = 8
playlist_prefix = "Genere - "
map_file = "genre_map.toml"
multi_genre_per_track = true

[sync]
mode = "additive"             # "additive" | "mirror"
public = false
add_batch_size = 100          # ridurre se Spotify rifiuta batch grandi
confirm_if_creating_more_than = 20
dedupe_same_song = false
```

`.env`: si aggiunge solo `LASTFM_API_KEY=` (opzionale).

## 7. Comandi CLI (`sch`)

```
sch auth                          # login (o ri-login se il refresh token è scaduto)
sch calendar                      # elenca le playlist riconosciute come calendario, per anno
sch plan  artists|genres [filtri] # mostra cosa verrebbe creato/aggiornato (= dry-run)
sch sync  artists|genres [filtri] # applica
sch status                        # playlist derivate note, ultime esecuzioni, playlist nuove da spostare
sch genres-report                 # generi non mappati e artisti senza generi

Filtri: --year 2025   --from 2024-01 --to 2025-06   --all   --dry-run   --prune
```

## 8. Fasi di lavoro e suddivisione tra 4 agenti

### Fase 0: contratto e spike (sequenziale, prima di tutto)

Un solo agente (o tu con me) prima di lanciare gli altri:

1. Creare l'app nella dashboard Spotify, compilare `.env`, aggiungere il tuo account in User Management.
2. Spike manuale su 4 punti: `genres` popolato?; forma esatta della risposta di `GET /playlists/{id}/items` (`item` vs `track`); dimensione massima batch di `POST .../items`; presenza di `snapshot_id` in `GET /me/playlists`.
3. Salvare 3-4 risposte reali **anonimizzate** in `tests/fixtures/`.
4. Scrivere `models.py` (§5.1), i Protocol, `tests/fakes.py` e le estensioni di `config.py` / `config.example.toml`.
5. Decidere il provider di generi in base all'esito dello spike.

**Uscita**: i 4 agenti possono lavorare in parallelo senza toccare gli stessi file.

### Fase 1: sviluppo in parallelo

| Agente | File di sua proprietà | Compito | Test |
|---|---|---|---|
| **A, Spotify I/O** | `auth.py`, `client.py`, `tests/test_auth.py`, `tests/test_client.py` | PKCE con server locale su `127.0.0.1:8888`, refresh automatico, retry con backoff e rispetto di `Retry-After` sui 429, paginazione, batch configurabile, tutte le chiamate dell'interfaccia `SpotifyClient` | HTTP mockato con le fixture; nessuna chiamata di rete |
| **B, Calendario** | `calendar_scan.py`, `cache.py`, `tests/test_calendar_scan.py` | Parsing nome → (anno, mese) con i formati integrati ("2025-03", "03/2025", "Marzo 2025", mesi in italiano) più regex custom; filtri anno/intervallo; lettura brani saltando local/episodi/null; cache per `snapshot_id`; avviso se una playlist non è leggibile (non tua) | Tabella di nomi validi/non validi; cache hit/miss |
| **C, Raggruppamento e generi** | `grouping.py`, `genres.py`, `genre_map.toml`, `tests/test_grouping.py`, `tests/test_genres.py` | Raggruppamento per artista (rispetta `min_tracks`, `primary_artist_only`), dedup, ordinamento cronologico; provider di generi (secondo lo spike) con cache su disco e rate limit; mappa macro-generi; report generi non mappati; costruzione di nome e descrizione con marker `[sch:...]` | Casi: brano con 2 artisti, artista sotto soglia, stesso brano in 2 mesi, genere non mappato |
| **D, Sync, stato, CLI, docs** | `state.py`, `sync.py`, `cli.py`, `README.md`, `tests/test_sync.py`, `tests/test_cli.py` | Diff desiderato/esistente, creazione playlist, aggiunta in batch dei soli brani mancanti, `--prune`, `--dry-run`, conferma sopra soglia, recupero dello stato dal marker se `state.json` manca, report con "playlist nuove da spostare", comandi CLI (§7), README con setup passo-passo | Usa `FakeSpotifyClient`: idempotenza (secondo run = nessuna modifica), playlist rinominata a mano, stato perso e ricostruito |

Regola di coordinamento: ogni agente modifica **solo i suoi file**. Se serve cambiare `models.py` o le interfacce, lo chiede e si fa in un commit separato, prima di proseguire.

### Fase 2: integrazione (sequenziale)

1. Unire i 4 rami, far girare l'intera suite.
2. Test end-to-end con `FakeSpotifyClient` su un calendario sintetico di 36 mesi (3 anni): primo run, poi aggiunta di un mese, poi secondo run.
3. **Prova reale in sicurezza**: `sch plan artists --year <ultimo anno>`, controllo a mano, poi `sch sync` su un solo anno, verifica in Spotify, spostamento delle playlist nelle cartelle, nuovo `sync` e verifica che aggiorni le stesse playlist.
4. Rifinitura di README e `genre_map.toml` con i generi non mappati usciti dal run reale.

## 9. Criteri di accettazione

- Un secondo `sync` immediatamente dopo il primo non produce **nessuna** modifica.
- Aggiungere una playlist mensile al calendario e rilanciare aggiunge solo i brani nuovi alle playlist giuste e crea le sole playlist nuove.
- Spostare una playlist derivata in una cartella, rinominarla o cambiarne l'immagine non ne impedisce l'aggiornamento.
- Cancellando `data/state.json` il run successivo ricollega le playlist esistenti tramite marker, senza crearne di duplicate.
- Nessun segreto o dato personale finisce nel repo (`.env`, `data/`, token restano ignorati).
- La suite di test gira senza rete.

## 10. Rischi

| Rischio | Mitigazione |
|---|---|
| Spotify cambia di nuovo l'API (è successo più volte nel 2024-2026) | Tutto passa da `client.py`; fixture e test fanno emergere subito le rotture |
| Generi non disponibili da Spotify | Provider alternativo (Last.fm/MusicBrainz) già previsto |
| Rate limit con molte playlist da creare | Backoff su 429, cache, `snapshot_id`, creazione a lotti con pausa |
| Primo run crea troppe playlist | `min_tracks`, `--dry-run`, conferma sopra soglia, partenza per anno |
| Refresh token scaduto | `sch auth` guida il ri-login; il messaggio d'errore lo indica chiaramente |
| Playlist del calendario con nome non standard | `sch calendar` mostra cosa è stato riconosciuto; regex personalizzabile |
| Cap sugli utenti autorizzati / un solo Client ID per sviluppatore | Uso personale: basta il tuo account, nessun problema |

## 11. Domande aperte (da decidere prima o durante la Fase 0)

1. Soglie: ti vanno bene `min_tracks = 4` per gli artisti e 8 per i generi, o preferisci altri valori?
2. Un brano con più artisti deve finire nella playlist di tutti (`primary_artist_only = false`) o solo del primo? Il default attuale è solo del primo.
3. Vuoi playlist con prefisso fisso ("Artista - Nome", "Genere - Nome") o nomi liberi? Il prefisso aiuta a ritrovarle e a riconoscerle.
4. Altri tagli oltre ad artista e genere (anno di uscita, decennio, "i più ascoltati ogni anno")? L'architettura li supporta, ma li metterei dopo la v1.
5. Se rimuovi un brano da una playlist mensile, deve sparire anche dalle derivate (`mirror`) o resta (`additive`)? Il default è che resta.
