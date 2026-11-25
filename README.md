# YAWsP Reloaded (Yet Another Webshare Plugin)

[![Version](https://img.shields.io/badge/verze-1.3.15-blue.svg)](https://github.com/Hammanek/plugin.video.yawspreloaded)
[![License](https://img.shields.io/badge/licence-AGPL--3.0-green.svg)](LICENSE)

Moderní a vylepšený doplněk pro Kodi umožňující sledování obsahu ze služby Webshare. Tento projekt vychází z původního doplňku od "cache" a přináší řadu vylepšení pro lepší uživatelský zážitek.

## ✨ Klíčové funkce

- 📺 **Integrace TMDB** – Watchlist filmů a seriálů z TheMovieDB (náhrada za zpoplatněný Trakt), české názvy, popisy a žánry přímo z TMDB.
- 🔍 **Pokročilé vyhledávání** – Rychlé a přesné vyhledávání přímo v databázi Webshare.
- 🎬 **Trailer systém** – Možnost přehrát trailery před samotným spuštěním filmu.
- ✅ **Správa watchlistu** – Označení jako zhlédnuté přímo z Kodi, položka se zároveň odstraní z TMDB watchlistu.
- 📅 **Další díl u seriálů** – U běžících seriálů se zobrazí číslo a datum příští epizody.
- 🗂️ **Řazení a filtry** – Watchlist lze řadit (abecedně, nově přidané, hodnocení, rok) a skrýt zatím nevydané filmy.
- 🖼️ **InfoWall výchozí pohled** – Watchlist se otevře v přehledném pohledu InfoWall (vlastní volba pohledu se respektuje).
- ⚡ **Chytré vyhledávání z watchlistu** – Klik na film hledá na Webshare seřazené od největších souborů, v menu i vyhledání dle původního (anglického) názvu.
- 🛠️ **Opravy a optimalizace** – Pravidelné aktualizace pro zajištění stability a rychlosti.

## 📋 Požadavky

- Kodi **19 (Matrix) a novější**
- Účet na [Webshare](https://webshare.cz)
- Účet na [TMDB](https://www.themoviedb.org) (zdarma) – potřebný pro watchlist

## 🚀 Instalace

1. Stáhněte si instalační ZIP z [nejnovějšího release](https://github.com/Hammanek/plugin.video.yawspreloaded/releases/latest) (soubor `plugin.video.yawspreloaded-*.zip`).
2. V Kodi zvolte **Doplňky** -> **Instalovat ze souboru zip**.
3. Vyhledejte stažený soubor a potvrďte instalaci.
4. V nastavení doplňku vyplňte přihlašovací údaje k Webshare.

## ⚙️ Nastavení TMDB

1. Na [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api) si zdarma **vyžádejte API klíč** (okamžité schválení).
2. Zkopírujte **API Key** (32 znaků) do nastavení doplňku, sekce **TMDB**.
3. Klikněte **Připojit k TMDB...** a naskenujte telefonem zobrazený **QR kód** – po schválení je vše nastavené (jednorázově).

## 🧩 Architektura

| Modul | Odpovědnost |
| --- | --- |
| `main.py` | Vstupní bod doplňku; předává parametry Kodi jednotnému routeru. |
| `yawsp.py` | Tenká kompatibilní fasáda bez původní monolitické logiky. |
| `yawsp_app/runtime.py` | Kontext Kodi, sestavování URL, lokalizace a dokončení akcí. |
| `yawsp_app/webshare.py` | Webshare HTTP, XML validace, autentizace a doménové operace. |
| `yawsp_app/search.py` | Normalizace dotazů, Unicode porovnávání, stránkování a české řazení. |
| `yawsp_app/downloads.py` | Kontrolované streamování a bezpečný přechod přes dočasný soubor. |
| `yawsp_app/backup_db.py` | Streamovaná ZIP/JSON validace, atomická instalace a sidecar kontrola teplé DB. |
| `yawsp_app/json_cache.py` | Defenzivní načítání, TTL záznamy a atomické ukládání cache. |
| `yawsp_app/watchlist_snapshot.py` | Kompletní snapshoty watchlistu oddělené účtem, médiem a jazykem. |
| `yawsp_app/download_jobs.py` | Omezená fronta médií a instalací DB se skutečnými stavy úloh. |
| `yawsp_app/tmdb.py` | TMDB HTTP, cache metadata, sdílené lety a férová fronta prefetch. |
| `yawsp_app/webshare_ui.py` | Prezentace vyhledávání, lineární historie a handoff médií/Backup DB. |
| `yawsp_app/tmdb_ui.py` | Prezentace TMDB autorizace, QR, watchlistu, epizod a trailerů. |
| `yawsp_app/router.py` | Jednotné směrování akcí, adaptéry dokončení a hraniční zpracování chyb. |

Služby neimportují Kodi moduly; runtime/UI vrstvy jim vkládají potřebné adaptéry.

## ⚡ Výkon, snapshoty a úlohy na pozadí

Tato optimalizace zůstává ve vývojové větvi; nemění vydanou verzi **1.3.15** a není novým release.

- **TMDB watchlist:** kompletní snapshot je klíčovaný podle **účtu TMDB, typu média, jazyka a verze schématu**, bez tokenů či přihlašovacích údajů. Warm kompatibilní snapshot se vykreslí lokálně s **nulovými account/watchlist HTTP požadavky**; filtrování a globální řazení stále pracují nad celým seznamem. Data mohou být **dočasně zastaralá**, protože obnova na pozadí se projeví až při **pozdějším otevření** (stale-while-revalidate). Při chybějícím, nekompatibilním nebo poškozeném snapshotu cold cesta znovu načte všechny stránky. Uloží se až po úspěchu a validaci **všech stránek**; při chybě vznikne **žádný částečný snapshot** a předchozí kompletní data zůstanou zachována. Změna identity, autentizace a pravidla watched akcí invalidují pouze příslušný rozsah.
- **TMDB metadata:** listování použije jeden průchod cache trailerů a epizod a odesílání se skládá do `post_complete` až po `endOfDirectory`. Worker ukládá atomicky po nejvýše osmi validovaných změnách (32 změn = nejvýše čtyři flush operace), zatímco on-demand akce ukládají okamžitě. Férový jednovláknový prefetch střídá trailery a seriály, sdílí stejný klíč s on-demand požadavkem, drží nejvýše 32 aktivních rezervací a dokončí navázané dávky. Každé otevření předá metadata prvních 50 viditelných řádků, nikoli neomezenou historii.
- **Backup DB:** atomický **sidecar** mimo datový adresář obsahuje relativní jméno, velikost a `mtime_ns` každého souboru. Shodná teplá instalace provede pouze **O(F)** kontrol metadat bez úplného parsování; `load()` parsuje **pouze vybraný soubor**. Chybějící, navíc nebo změněný soubor a neplatný sidecar vynutí úplnou validaci či opravu, nikoli zrychlené akceptování poškozených dat.
- **Cold DB a média:** při chybějící/invalidní DB je jedna profilová instalace zařazena do `DownloadJobManager` a invocation vrátí stavovou položku. **Přijetí úlohy do fronty není dokončení vzdálené operace ani vzdálený úspěch.** Skutečná instalace/uspořádání souboru proběhne až ve workeru. `Container.Refresh` se spustí **až po úspěšné validaci a atomickém přesunu cold DB**; při chybě není žádný úspěch ani `Container.Refresh`. Teplá instalovaná DB se otevírá přímo z lokálních metadat bez fronty a Webshare revalidace.
- **Omezený životní cyklus:** jeden procesní vlastník drží jeden `BackgroundPrefetch` a jeden `DownloadJobManager`. Media/DB fronta má jednoho workera a **nejvýše čtyři** běžící i čekající úlohy; duplicitní cíl/profil se odmítne. Každá přijatá práce používá a zavře **vlastní HTTP relaci**. `close(wait=False)` je idempotentní, odmítne novou práci a ruší čekající úlohy; rušení již blokovaného OS/socket I/O zůstává kooperativní.
- **Webshare a historie:** statické hlavní menu a platná lokální DB nevyžadují autentizaci; vzdálená akce si token ověří až při použití. Historie používá rekurzivní kanonický klíč celého obsahu s O(H) deduplikací, zachovává první výskyt a při odstranění bez platného ID znovu použije již staženou odpověď.
- **Streamování:** média i ZIP používají výchozí bloky **256 KiB**, kontrolované zápisy, dočasné soubory a teprve potom replacement. Produkce nečte celé `response.content` ani nevytváří celoarchivový `BytesIO`; kompletná extrakce kontroluje CRC bez druhého `testzip()` průchodu. Strukturální validace stále načítá `json.load()` **jeden JSON dokument** najednou, takže paměť není nezávisle omezená velikostí největšího členu. Atomičnost vzdáleného VFS rename závisí na backendu.

### Naměřené chování a jeho hranice

Offline Windows/CPython benchmarky používají syntetická data, testovací HTTP/Kodi/VFS dvojníky a skutečné produkční služby. Časové výsledky uvádějí opakovaný **medián a rozsah minimum–maximum**, nikoli jediný běh ani akceptační časový limit. Prokazují například warm watchlist bez account/watchlist requestů, čtyři místo 32 cache flushů při 32 úspěších, jediný datový JSON read při warm DB `ensure_installed()` + výběru souboru místo 55 a čtyřikrát méně write callů při 256 KiB místo 64 KiB. U historie je kanonická varianta při H=1000 rychlejší, ale při H=20/200 je měřeně pomalejší a potřebuje lineární pomocnou alokaci; cold/migrační/tamper DB cesty nemají být vykazovány jako zrychlené. Event benchmarky dokazují pořadí handoff → skutečné dokončení, nikoli latenci zařízení. Podrobné mapování všech PERF-01…PERF-09 na produkční symboly, testové selektory, benchmarky a meze je v `.superpowers/sdd/2026-11-25-performance-remediation/performance-coverage.json`.

Nebyla provedena živá ověření v reálném Kodi ani na ARM, NFS/SMB či vzdáleném VFS, a offline desktopové časy nejsou slibem latence či propustnosti těchto prostředí.

## ✅ Ověření pro vývojáře

Spusťte z kořene repozitáře:

```sh
python -m unittest discover -s tests -v
```

Reprodukovatelné offline benchmarky jsou v `.superpowers/sdd/2026-11-25-performance-remediation/`. Z kořene repozitáře lze spustit (výstupy se ukládají do stejného SDD adresáře):

```sh
python .superpowers/sdd/2026-11-25-performance-remediation/task-1_operation_benchmark.py
python .superpowers/sdd/2026-11-25-performance-remediation/task-2-benchmark.py
python .superpowers/sdd/2026-11-25-performance-remediation/task-3-benchmark.py .superpowers/sdd/2026-11-25-performance-remediation/task-7-metadata-counters.json
python .superpowers/sdd/2026-11-25-performance-remediation/benchmark_backup_db.py --output .superpowers/sdd/2026-11-25-performance-remediation/task-7-db-after.json
python .superpowers/sdd/2026-11-25-performance-remediation/task-5-event-benchmark.py
python .superpowers/sdd/2026-11-25-performance-remediation/task-6-dedup-benchmark.py --output .superpowers/sdd/2026-11-25-performance-remediation/task-7-history.json
python .superpowers/sdd/2026-11-25-performance-remediation/task-6-benchmark.py --output .superpowers/sdd/2026-11-25-performance-remediation/task-7-chunks-current.json
```

Testy používají syntetická data a testovací dvojníky pro HTTP i Kodi. Běží offline, bez přístupu k síti a bez přihlašovacích údajů. Nebyla provedena živá ověření v reálném Kodi ani testy Webshare/TMDB API; tato kontrola proto nedokazuje kompatibilitu s konkrétní verzí Kodi ani aktuální chování vzdálených služeb.

## 📝 Changelog

Historie změn je v [GitHub releasesech](https://github.com/Hammanek/plugin.video.yawspreloaded/releases).

## 💬 Kontakt a podpora

Pokud narazíte na chybu nebo máte návrh na vylepšení, můžete mě kontaktovat:

- **Telegram:** [@hammanek](https://t.me/hammanek)
- **GitHub Issues:** [Zde](https://github.com/Hammanek/plugin.video.yawspreloaded/issues)

---
*Vytvořeno s ❤️ pro českou a slovenskou Kodi komunitu.*
