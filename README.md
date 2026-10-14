# spotify-calendar-helper

Hai playlist mensili su Spotify — "Marzo 2025", "Aprile 2025" — dove tieni i brani ascoltati in quel mese? Questo strumento le legge e genera automaticamente playlist **trasversali**: una per ogni artista, una per ogni macro-genere. Ogni mese rilanci un comando e le playlist si aggiornano senza duplicati.

```
sch sync artists --year 2025
```

> **Nota su Account Premium**: l'API Spotify per sviluppatori richiede che l'account con cui si registra l'app sia intestatario di un piano Premium (non membro di un piano Family). Se ricevi `403 Active premium subscription required`, è questo il motivo.

---

## Indice

1. [Cosa serve](#1-cosa-serve)
2. [Installazione](#2-installazione)
3. [Creare l'app Spotify](#3-creare-lapp-spotify)
4. [Configurazione](#4-configurazione)
5. [Primo run consigliato](#5-primo-run-consigliato)
6. [Uso mensile](#6-uso-mensile)
7. [Spostare le playlist nelle cartelle](#7-spostare-le-playlist-nelle-cartelle)
8. [Riferimento comandi](#8-riferimento-comandi)
9. [Risoluzione problemi](#9-risoluzione-problemi)
10. [Limiti dell'API Spotify](#10-limiti-dellapi-spotify)

---

## 1. Cosa serve

- Python ≥ 3.11
- Un account Spotify **Premium** intestatario di un'app nella dashboard sviluppatori (vedi §3)
- Connessione internet (solo durante i run, non nei test)

---

## 2. Installazione

```bash
# Clona il repository
git clone https://github.com/tuonome/spotify-calendar-helper.git
cd spotify-calendar-helper

# Installa in modalità sviluppo (con le dipendenze di test)
pip install -e ".[dev]"

# Verifica che il comando sia disponibile
sch --version
```

---

## 3. Creare l'app Spotify

L'app usa il flusso OAuth **PKCE**: non serve nessuna password né un client secret, solo il Client ID.

1. Vai su [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) con il tuo account Premium.
2. Clicca **Create app**.
   - Nome e descrizione: qualsiasi.
   - **Redirect URI**: `http://127.0.0.1:8888/callback` (esatto, con `127.0.0.1` — non `localhost`).
3. Apri l'app appena creata → **Settings** → copia il **Client ID**.
4. Sempre in Settings → **User Management**: aggiungi il tuo indirizzo email Spotify. Senza questo passaggio l'app non può accedere al tuo account.

---

## 4. Configurazione

### 4.1 File `.env`

```bash
cp .env.example .env
```

Apri `.env` e compila:

```dotenv
SPOTIFY_CLIENT_ID=il_tuo_client_id_qui
SPOTIFY_REDIRECT_URI=http://127.0.0.1:8888/callback
# Opzionale, solo se usi provider = "lastfm" in config.toml
LASTFM_API_KEY=
```

`.env` è ignorato da git: rimane solo sul tuo computer.

### 4.2 File `config.toml`

```bash
cp config.example.toml config.toml
```

Le impostazioni più importanti:

```toml
[calendar]
# Lascia vuoto se le tue playlist si chiamano "Marzo 2025", "2025-03", "03/2025" ecc.
# (questi formati sono già riconosciuti automaticamente).
# Se il tuo formato è diverso, scrivi una regex con i gruppi (?P<year>...) e (?P<month>...).
name_regex = ""

# ID di playlist da ignorare anche se il nome sembra un mese.
ignore_playlist_ids = []

[artists]
min_tracks = 4          # crea la playlist di un artista solo se ha ≥ 4 brani
primary_artist_only = true  # ogni brano va solo nella playlist del primo artista
playlist_prefix = "Artista - "

[genres]
enabled = true
provider = "musicbrainz"  # non richiede chiavi; oppure "lastfm" (più veloce)
min_tracks = 8
playlist_prefix = "Genere - "

[sync]
mode = "additive"       # non rimuove mai i brani già presenti
add_batch_size = 10     # ridurre se Spotify rifiuta le richieste di aggiunta
confirm_if_creating_more_than = 20
```

### 4.3 File `genre_map.toml`

Mappa i tag di genere granulari (come `"bedroom pop"`, `"shoegaze"`) ai macro-generi usati per le playlist. Il file è già popolato con i generi più comuni ed è pensato per essere modificato a mano nel tempo.

Se un artista ha un genere non mappato, `sch genres-report` te lo segnala con il numero di occorrenze.

---

## 5. Primo run consigliato

### Passo 1 — Login

```bash
sch auth
```

Si apre il browser sulla pagina di autorizzazione Spotify. Dopo l'ok il token viene salvato in `data/token.json` (ignorato da git, permessi `0600`).

### Passo 2 — Verifica il calendario

```bash
sch calendar
```

Mostra le playlist riconosciute come calendario, per anno. Controlla che i nomi vengano interpretati correttamente. Se vedi anni o mesi sbagliati, aggiusta `name_regex` in `config.toml` oppure aggiungi gli ID da ignorare in `ignore_playlist_ids`.

Per lavorare su un solo anno:

```bash
sch calendar --year 2024
```

### Passo 3 — Pianifica (dry-run)

Prima di creare centinaia di playlist, guarda cosa verrebbe fatto:

```bash
sch plan artists --year 2024
```

```bash
sch plan genres --year 2024
```

L'output mostra quante playlist verrebbero create o aggiornate, senza scrivere nulla su Spotify.

### Passo 4 — Sync

Se il piano ti convince:

```bash
sch sync artists --year 2024
```

Il primo run crea le playlist. Se superano la soglia `confirm_if_creating_more_than` (default 20), il comando chiede conferma. Passa `--yes` per saltarla.

Al termine viene stampata la lista **"Playlist nuove da spostare nella cartella giusta"** (vedi §7).

---

## 6. Uso mensile

Ogni mese, dopo aver aggiunto i brani alla playlist mensile:

```bash
sch sync artists
sch sync genres
```

Senza `--year` o `--all`, il comando usa i filtri impostati di default (nessuno = tutto il calendario). In genere conviene passare `--year <anno corrente>` per velocizzare.

Un secondo `sync` immediato non produce nessuna modifica: il comportamento è idempotente.

---

## 7. Spostare le playlist nelle cartelle

Le API di Spotify non permettono di creare né spostare playlist nelle cartelle: lo devi fare a mano **una volta sola**.

Al termine di ogni `sync` che crea nuove playlist, il report mostra:

```
── Playlist nuove da spostare nella cartella giusta ──
  Artista - Radiohead
  Artista - Portishead
  (gli aggiornamenti successivi le trovano anche se le sposti)
```

Trascinale nella cartella giusta nell'app Spotify desktop. L'ID della playlist non cambia, quindi i run successivi le aggiornano correttamente ovunque siano.

---

## 8. Riferimento comandi

```
sch auth
    Esegue il login PKCE o rinnova il token se scaduto.

sch calendar [--year AAAA] [--from AAAA-MM] [--to AAAA-MM] [--all]
    Elenca le playlist del calendario riconosciute: anni, mesi, brani totali,
    mesi mancanti o duplicati, playlist ignorate.

sch plan artists|genres [filtri]
    Mostra il piano (dry-run): quante playlist verrebbero create o aggiornate
    e quanti brani aggiunti. Non scrive nulla su Spotify.

sch sync artists|genres [filtri] [--dry-run] [--prune] [--yes]
    Crea e aggiorna le playlist. Con --dry-run non scrive nulla.
    Con --prune (richiede sync.mode = "mirror" in config.toml) rimuove
    i brani spariti dal calendario.
    Con --yes salta la conferma interattiva.

sch status
    Mostra le playlist derivate note (con ID Spotify e data dell'ultimo sync)
    e gli ultimi run registrati.

sch genres-report [filtri]
    Elenca i tag di genere non mappati in genre_map.toml (con il conteggio
    di quante volte compaiono) e gli artisti per cui nessun provider
    ha trovato generi.
```

**Filtri disponibili per `calendar`, `plan`, `sync`, `genres-report`:**

| Opzione | Significato |
|---|---|
| `--year 2025` | Solo il 2025 |
| `--from 2024-01` | Dal gennaio 2024 in poi |
| `--to 2024-12` | Fino al dicembre 2024 incluso |
| `--all` | Tutto il calendario senza filtri |

---

## 9. Risoluzione problemi

### Token scaduto o `invalid_grant`

```
Errore: token non valido o scaduto; esegui `sch auth` per effettuare di nuovo il login.
```

Rilancia `sch auth`. Il refresh token Spotify ha durata limitata: dopo circa sei mesi di inattività è necessario ri-autorizzare l'app.

### Errori 429 (rate limit)

L'API Spotify impone un limite di richieste al minuto. Il client rispetta l'header `Retry-After` e riprova automaticamente con backoff esponenziale. Se vedi molti rallentamenti:

- Riduci `add_batch_size` in `config.toml` (es. `5`).
- Lavora su un anno alla volta invece di `--all`.
- La cache dei `snapshot_id` evita di rileggere le playlist invariate: assicurati che `data/calendar_cache.json` sia presente (si crea al primo run).

### Generi non mappati

```bash
sch genres-report
```

Mostra i tag non ancora in `genre_map.toml`, ordinati per frequenza. Aggiungili al file nella sezione più appropriata e rilancia `sch sync genres`.

Esempio: se `sch genres-report` mostra `42  neo-soul`, aggiungi `"neo-soul"` all'array del macro-genere `"R&B / Soul"` in `genre_map.toml`.

### MusicBrainz lento

MusicBrainz impone 1 richiesta/secondo. Per qualche centinaio di artisti ci vogliono alcuni minuti. I generi vengono salvati in `data/genres_cache.json` con TTL 90 giorni: i run successivi sono molto più veloci.

Se preferisci velocità, registra una chiave gratuita su [last.fm/api](https://www.last.fm/api/account/create), aggiungila a `.env` come `LASTFM_API_KEY=...` e imposta `provider = "lastfm"` in `config.toml`.

### `sch calendar` non riconosce le mie playlist

I formati riconosciuti automaticamente sono:

```
2025-03          2025-03 Titolo libero
2025/03          03/2025
Marzo 2025       marzo 2025       MAR 2025
```

Se il tuo formato è diverso (es. `"Cal Mar 2025"`), imposta `name_regex` in `config.toml`:

```toml
[calendar]
name_regex = "^Cal (?P<month>\\w+) (?P<year>\\d{4})$"
```

I gruppi `(?P<year>...)` e `(?P<month>...)` sono obbligatori. `month` può essere un numero (1–12) o il nome del mese in italiano.

### Stato perso (`data/state.json` cancellato)

Nessun problema: il run successivo riconosce le playlist esistenti tramite il marker `[sch:artist:...]` o `[sch:genre:...]` nella loro descrizione, senza creare duplicati.

### Playlist create 403 / non tua

Le playlist del calendario di un altro utente (condivise con te) non vengono lette. Vengono segnalate nel report di `sch calendar` come "Non tue (non lette)".

---

## 10. Limiti dell'API Spotify

Alcuni comportamenti dipendono da vincoli dell'API che potrebbero cambiare:

- **Le cartelle non sono accessibili via API**: il riconoscimento del calendario avviene solo tramite il nome delle playlist.
- **`GET /artists/{id}` spesso restituisce `genres: []`**: per questo il default del provider è MusicBrainz.
- **Dimensione massima dei batch di aggiunta**: l'API documentava 100 brani per chiamata, ma fonti di terze parti riportano limiti più bassi. Il default di `add_batch_size = 10` è prudente; aumentalo sperimentando se non hai errori.
- **Contenuto playlist non tuo**: le playlist di altri utenti (anche condivise) non sono leggibili tramite le API per sviluppatori.
- **Endpoint rinominati** (aggiornamento febbraio 2026): si usa `/playlists/{id}/items` (non più `/tracks`). Il codice è già aggiornato.

---

## Struttura del progetto

```
src/spotify_calendar_helper/
  auth.py            # login PKCE, refresh token
  client.py          # chiamate HTTP a Spotify (retry, 429, paginazione)
  calendar_scan.py   # lettura e parsing delle playlist mensili
  cache.py           # cache snapshot_id per evitare riletture inutili
  grouping.py        # calendario → specifiche di playlist (artista, genere)
  genres.py          # provider di generi + mappa macro-generi
  state.py           # stato persistente (playlist_id, storico run)
  sync.py            # diff e applicazione su Spotify
  cli.py             # comando `sch`
  models.py          # dataclass condivise
  config.py          # caricamento configurazione
tests/
  fakes.py           # FakeSpotifyClient in memoria (nessuna rete)
  fixtures/          # risposte API sintetiche per i test
data/                # (ignorata da git) token, cache, stato
genre_map.toml       # mappa generi fini → macro-generi (modifica a mano)
config.example.toml  # template di configurazione
```

---

## Licenza

MIT — vedi `LICENSE`.
