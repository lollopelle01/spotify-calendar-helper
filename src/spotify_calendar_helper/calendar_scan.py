"""Scansione del calendario: riconoscimento delle playlist mensili dal nome (B1) e lettura
dei loro brani con cache e filtri (B3).

L'API di Spotify non mostra le cartelle: il calendario si riconosce solo dal NOME della playlist.

Regole di riconoscimento (documentate perché sono una scelta, non un'ovvietà):

1. La data deve stare all'INIZIO del nome (spazi iniziali ignorati). "Preferiti Marzo 2025" non
   è una playlist del calendario; se il tuo schema è diverso usa `[calendar] name_regex`.
2. Dopo la data può esserci testo libero ("2025-03 Preferiti", "Marzo 2025 - estate"), ma NON
   se inizia con una parentesi: "Marzo 2025 (vecchia)" è considerata una copia, non il calendario.
   Per escludere altre playlist usa `ignore_playlist_ids`.
3. Una data completa come "2025-03-15" non è un mese: viene scartata.
4. La `name_regex` personalizzata, se presente, ha la precedenza sui formati integrati;
   se non corrisponde si provano comunque i formati integrati.

Formati integrati: "2025-03", "2025/03", "2025.03", "03/2025", "03-2025", "Marzo 2025",
"Mar 2025", "Mar. 2025", "2025 Marzo" (mesi in italiano, maiuscole/minuscole irrilevanti).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache

from .cache import CalendarCache
from .config import Settings
from .models import CalendarPlaylist, PlaylistInfo, parse_marker
from .protocols import SpotifyClient

ITALIAN_MONTHS: dict[str, int] = {
    "gennaio": 1,
    "febbraio": 2,
    "marzo": 3,
    "aprile": 4,
    "maggio": 5,
    "giugno": 6,
    "luglio": 7,
    "agosto": 8,
    "settembre": 9,
    "ottobre": 10,
    "novembre": 11,
    "dicembre": 12,
    # abbreviazioni comuni
    "gen": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "mag": 5,
    "giu": 6,
    "lug": 7,
    "ago": 8,
    "set": 9,
    "sett": 9,
    "ott": 10,
    "nov": 11,
    "dic": 12,
}

_YEAR = r"(?:19|20)\d{2}"
_NUM_MONTH = r"(?:0?[1-9]|1[0-2])"
_NAMES = "|".join(sorted((re.escape(n) for n in ITALIAN_MONTHS), key=len, reverse=True))
# Dopo la data non deve continuare una parola o un'altra cifra ("2025-031", "2025-03-15").
_END = r"(?!\w|[-/.]\d)"

_BUILTIN_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        rf"^\s*(?P<year>{_YEAR})\s*[-/.]\s*(?P<month>{_NUM_MONTH}){_END}",
        rf"^\s*(?P<month>{_NUM_MONTH})\s*[-/.]\s*(?P<year>{_YEAR}){_END}",
        rf"^\s*(?P<month>{_NAMES})\.?\s+(?P<year>{_YEAR}){_END}",
        rf"^\s*(?P<year>{_YEAR})\s+(?P<month>{_NAMES})\.?(?!\w)",
    )
)

# Testo tra parentesi subito dopo la data: la playlist è una copia/variante, non il calendario.
_NON_CALENDAR_SUFFIX = re.compile(r"^\s*[(\[]")


@lru_cache(maxsize=32)
def compile_name_regex(pattern: str) -> re.Pattern[str]:
    """Compila la `name_regex` dell'utente verificando che abbia i gruppi `year` e `month`."""
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"name_regex non valida: {exc}") from exc
    missing = {"year", "month"} - set(compiled.groupindex)
    if missing:
        names = ", ".join(f"(?P<{n}>...)" for n in sorted(missing))
        raise ValueError(f"name_regex deve contenere i gruppi nominati: manca {names}")
    return compiled


def month_number(text: str) -> int | None:
    """Converte "03", "3", "Marzo", "mar." in 3; None se non è un mese."""
    cleaned = text.strip().strip(".").casefold()
    if cleaned.isdigit():
        number = int(cleaned)
        return number if 1 <= number <= 12 else None
    return ITALIAN_MONTHS.get(cleaned)


def _from_groups(match: re.Match[str]) -> tuple[int, int] | None:
    year_text = match.group("year").strip()
    if not (year_text.isdigit() and len(year_text) == 4):
        return None
    year = int(year_text)
    month = month_number(match.group("month"))
    if month is None or not 1900 <= year <= 2100:
        return None
    return year, month


def parse_playlist_name(name: str, name_regex: str = "") -> tuple[int, int] | None:
    """Estrae (anno, mese) dal nome di una playlist, o None se non è del calendario.

    Solleva ValueError se `name_regex` non è valida (così l'errore emerge subito, non in silenzio).
    """
    text = unicodedata.normalize("NFC", name or "").strip()
    if not text:
        return None

    if name_regex:
        match = compile_name_regex(name_regex).search(text)
        if match:
            parsed = _from_groups(match)
            if parsed:
                return parsed

    for pattern in _BUILTIN_PATTERNS:
        match = pattern.match(text)
        if not match:
            continue
        if _NON_CALENDAR_SUFFIX.match(text[match.end() :]):
            return None
        parsed = _from_groups(match)
        if parsed:
            return parsed
    return None


# ---------------------------------------------------------------------------
# B3: scansione
# ---------------------------------------------------------------------------

_YEAR_MONTH = re.compile(r"^\s*(?P<year>(?:19|20)\d{2})-(?P<month>0[1-9]|1[0-2])\s*$")


def parse_year_month(text: str) -> tuple[int, int]:
    """Legge "2024-01" (valore di --from/--to). Solleva ValueError se il formato non è valido."""
    match = _YEAR_MONTH.match(text)
    if not match:
        raise ValueError(f"Data non valida: {text!r} (formato atteso AAAA-MM, es. 2024-01)")
    return int(match.group("year")), int(match.group("month"))


@dataclass(frozen=True)
class ScanFilters:
    """Selezione dei mesi (`--year`, `--from/--to`, `--all`). Senza filtri si prende tutto:
    spetta alla CLI decidere se chiedere un filtro esplicito."""

    year: int | None = None
    from_ym: tuple[int, int] | None = None  # inclusivo
    to_ym: tuple[int, int] | None = None  # inclusivo
    all: bool = False

    def __post_init__(self) -> None:
        ranged = self.from_ym is not None or self.to_ym is not None
        if self.year is not None and ranged:
            raise ValueError("--year non si combina con --from/--to")
        if self.all and (self.year is not None or ranged):
            raise ValueError("--all non si combina con --year o --from/--to")

    def matches(self, year: int, month: int) -> bool:
        if self.year is not None and year != self.year:
            return False
        if self.from_ym is not None and (year, month) < self.from_ym:
            return False
        if self.to_ym is not None and (year, month) > self.to_ym:
            return False
        return True


@dataclass
class ScanResult:
    playlists: list[CalendarPlaylist] = field(default_factory=list)  # ordinate per (anno, mese)
    ignored_ids: list[str] = field(default_factory=list)  # riconosciute ma in ignore_playlist_ids
    unrecognized: list[str] = field(default_factory=list)  # nomi che non sembrano del calendario
    not_owned: list[str] = field(default_factory=list)  # nomi di playlist del calendario non tue
    warnings: list[str] = field(default_factory=list)
    cache_hits: int = 0
    cache_misses: int = 0

    @property
    def skipped_items(self) -> int:
        """Totale di local file, episodi e item nulli saltati nelle playlist lette."""
        return sum(p.skipped_items for p in self.playlists)


def scan(
    client: SpotifyClient,
    settings: Settings,
    filters: ScanFilters | None = None,
    cache: CalendarCache | None = None,
) -> ScanResult:
    """Trova le playlist del calendario, ne legge i brani (dalla cache se lo `snapshot_id` non
    è cambiato) e le restituisce ordinate per (anno, mese).

    - le playlist derivate (con marker `[sch:...]`) non sono mai considerate calendario;
    - le playlist non tue vengono segnalate e non lette;
    - senza `snapshot_id` (D5) la cache non si usa e si rilegge sempre;
    - con una cache, le voci di playlist sparite vengono eliminate e il file viene salvato
      se qualcosa è cambiato (un errore di scrittura diventa un avviso, non blocca la scansione).
    """
    filters = filters or ScanFilters()
    calendar_cfg = settings.section("calendar")
    name_regex = str(calendar_cfg.get("name_regex", ""))
    if name_regex:
        compile_name_regex(name_regex)  # errore subito, prima di qualsiasi chiamata
    ignore_ids = set(calendar_cfg.get("ignore_playlist_ids", []))

    result = ScanResult()
    if cache is not None and cache.warning:
        result.warnings.append(cache.warning)

    my_id = client.current_user().id
    recognized: list[tuple[PlaylistInfo, tuple[int, int]]] = []
    for info in client.list_my_playlists():
        if parse_marker(info.description) is not None:
            continue  # playlist derivata da questo strumento
        parsed = parse_playlist_name(info.name, name_regex)
        if parsed is None:
            result.unrecognized.append(info.name)
        elif info.id in ignore_ids:
            result.ignored_ids.append(info.id)
        else:
            recognized.append((info, parsed))

    months: dict[tuple[int, int], int] = {}
    for _, ym in recognized:
        months[ym] = months.get(ym, 0) + 1
    for (year, month), count in sorted(months.items()):
        if count > 1:
            result.warnings.append(
                f"{count} playlist riconosciute per {year}-{month:02d}: i brani verranno uniti."
            )

    for info, (year, month) in recognized:
        if not filters.matches(year, month):
            continue
        if info.owner_id != my_id:
            result.not_owned.append(info.name)
            result.warnings.append(f"Playlist {info.name!r} non è tua: il contenuto non è leggibile.")
            continue

        entry = cache.lookup(info.id, info.snapshot_id) if cache is not None else None
        if entry is not None:
            tracks, skipped = entry.tracks, entry.skipped_items
            result.cache_hits += 1
        else:
            items = client.get_playlist_items(info.id)
            tracks, skipped = items.tracks, items.skipped
            result.cache_misses += 1
            if cache is not None:
                cache.store(info.id, info.snapshot_id, info.name, tracks, skipped)
        result.playlists.append(
            CalendarPlaylist(
                id=info.id,
                name=info.name,
                year=year,
                month=month,
                snapshot_id=info.snapshot_id,
                tracks=tuple(tracks),
                skipped_items=skipped,
            )
        )

    result.playlists.sort(key=lambda p: (p.year, p.month, p.name.casefold(), p.id))

    if cache is not None:
        removed = cache.discard_except(info.id for info, _ in recognized)
        if result.cache_misses or removed:
            try:
                cache.save()
            except OSError as exc:
                result.warnings.append(f"Non riesco a salvare la cache del calendario: {exc}")
    return result


# ---------------------------------------------------------------------------
# B4: riepilogo per `sch calendar`
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalendarSummary:
    total_playlists: int
    total_tracks: int  # somma dei brani delle playlist mensili (un brano ripetuto conta due volte)
    months_by_year: dict[int, tuple[int, ...]]  # anno -> mesi presenti (ordinati, senza doppioni)
    tracks_by_year: dict[int, int]
    missing_months: tuple[tuple[int, int], ...]  # buchi tra il primo e l'ultimo mese trovati
    duplicate_months: tuple[tuple[int, int], ...]  # mesi con più di una playlist
    ignored_ids: tuple[str, ...]
    unrecognized: tuple[str, ...]
    not_owned: tuple[str, ...]
    skipped_items: int

    @property
    def years(self) -> tuple[int, ...]:
        return tuple(self.months_by_year)


def summarize(result: ScanResult) -> CalendarSummary:
    """Riepilogo di una scansione. I buchi si calcolano sui mesi della scansione: con un filtro
    (es. `--year`) riguardano solo l'intervallo selezionato."""
    present: dict[tuple[int, int], int] = {}
    tracks_by_year: dict[int, int] = {}
    for playlist in result.playlists:
        ym = (playlist.year, playlist.month)
        present[ym] = present.get(ym, 0) + 1
        tracks_by_year[playlist.year] = tracks_by_year.get(playlist.year, 0) + len(playlist.tracks)

    months_by_year: dict[int, list[int]] = {}
    for year, month in sorted(present):
        months_by_year.setdefault(year, []).append(month)

    missing: list[tuple[int, int]] = []
    if present:
        (y0, m0), (y1, m1) = min(present), max(present)
        year, month = y0, m0
        while (year, month) <= (y1, m1):
            if (year, month) not in present:
                missing.append((year, month))
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)

    return CalendarSummary(
        total_playlists=len(result.playlists),
        total_tracks=sum(tracks_by_year.values()),
        months_by_year={y: tuple(ms) for y, ms in months_by_year.items()},
        tracks_by_year=dict(sorted(tracks_by_year.items())),
        missing_months=tuple(missing),
        duplicate_months=tuple(sorted(ym for ym, n in present.items() if n > 1)),
        ignored_ids=tuple(result.ignored_ids),
        unrecognized=tuple(result.unrecognized),
        not_owned=tuple(result.not_owned),
        skipped_items=result.skipped_items,
    )
