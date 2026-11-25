# YAWsP Reloaded (Yet Another Webshare Plugin)

[![Version](https://img.shields.io/badge/verze-2.0.0-blue.svg)](https://github.com/Hammanek/plugin.video.yawspreloaded)
[![License](https://img.shields.io/badge/licence-AGPL--3.0-green.svg)](LICENSE)

Moderní a vylepšený doplněk pro Kodi umožňující sledování obsahu ze služby Webshare. Tento projekt vychází z původního doplňku od "cache" a přináší řadu vylepšení pro lepší uživatelský zážitek.

## ✨ Klíčové funkce

- 🧩 **Modulární architektura 2.0.0** – Webshare, TMDB, download a Kodi UI jsou oddělené komponenty, s jednotným routerem a zachovanými akcemi pro média, frontu, historii i watchlist.
- 🗑️ **Odstraněná Backup DB** – Zastaralá JSON/textová Backup DB už nemá položku menu, instalaci ani akce `db` a `searchdb`. Staré záložky s těmito akcemi otevřou obyčejné hlavní menu bez autentizace, sítě či čtení databázových souborů. Existující uživatelské soubory `database*.json`, `database*.txt`, adresář `db` a trvalé zálohy se nemažou ani nemigrují.
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

1. Stáhněte **[plugin.video.yawspreloaded-2.0.0.zip](https://github.com/Hammanek/plugin.video.yawspreloaded/releases/download/v2.0.0/plugin.video.yawspreloaded-2.0.0.zip)** z [release v2.0.0](https://github.com/Hammanek/plugin.video.yawspreloaded/releases/tag/v2.0.0) (případně použijte [nejnovější release](https://github.com/Hammanek/plugin.video.yawspreloaded/releases/latest)).
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
| `yawsp_app/json_cache.py` | Defenzivní načítání, TTL záznamy a atomické ukládání cache. |
| `yawsp_app/watchlist_snapshot.py` | Kompletní snapshoty watchlistu oddělené účtem, médiem a jazykem. |
| `yawsp_app/download_jobs.py` | Omezená fronta stahování médií se skutečnými stavy úloh a typovanými rezervacemi. |
| `yawsp_app/tmdb.py` | TMDB HTTP, cache metadata, sdílené lety a férová fronta prefetch. |
| `yawsp_app/webshare_ui.py` | Prezentace vyhledávání, deduplikované historie, fronty a handoff stahování médií. |
| `yawsp_app/tmdb_ui.py` | Prezentace TMDB autorizace, QR, watchlistu, epizod a trailerů. |
| `yawsp_app/router.py` | Jednotné směrování akcí, adaptéry dokončení a hraniční zpracování chyb. |

Služby neimportují Kodi moduly; runtime/UI vrstvy jim vkládají potřebné adaptéry.

## ⚡ Výkon, snapshoty a úlohy na pozadí

Verze **2.0.0** dokončuje přechod z původního monolitu na modulární architekturu. Měření níže rozlišuje počty requestů, I/O a souběžných rezervací od tvrzení o latenci.

- **TMDB watchlist:** kompletní snapshot je klíčovaný podle **účtu TMDB, typu média, jazyka a verze schématu**, bez tokenů či přihlašovacích údajů. Warm kompatibilní snapshot se vykreslí lokálně s **nulovými account/watchlist HTTP požadavky**; filtrování a globální řazení stále pracují nad celým seznamem. Data mohou být **dočasně zastaralá**, protože obnova na pozadí se projeví až při **pozdějším otevření** (stale-while-revalidate). Při chybějícím, nekompatibilním nebo poškozeném snapshotu cold cesta znovu načte všechny stránky. Uloží se až po úspěchu a validaci **všech stránek**; při chybě vznikne **žádný částečný snapshot** a předchozí kompletní data zůstanou zachována. Změna identity, autentizace a pravidla watched akcí invalidují pouze příslušný rozsah.
- **TMDB metadata:** listování použije jeden průchod cache trailerů a epizod a odesílání se skládá do `post_complete` až po `endOfDirectory`. Worker ukládá atomicky po nejvýše osmi validovaných změnách (32 změn = nejvýše čtyři flush operace), zatímco on-demand akce ukládají okamžitě. Férový prefetch střídá trailery a seriály, sdílí aktivní request stejného klíče s on-demand akcí a drží nejvýše **32 background rezervací**. Jedna interaktivní položka může projít před nezahájenou frontou bez čekání na kapacitu; další souběžné interaktivní klíče čekají v jediném prioritním slotu, takže strop je 32 background + 1 interaktivní rezervace. Dokončení, chyba i retry vrací vlastnictví správné claim/frontě. Každé otevření předá metadata prvních 50 viditelných řádků, nikoli neomezenou historii.
- **Historie odstraněné Backup DB:** dřívější implementace používala atomický **sidecar** mimo datový adresář s relativním jménem, velikostí a `mtime_ns` každého souboru. Shodná teplá instalace prováděla pouze **O(F)** kontrol metadat a `load()` parsoval **pouze vybraný soubor**. Tyto výsledky popisují odstraněný historický kód, ne současnou funkci; produkce už neobsahuje `BackupDBStore`, `backup_db.py` ani `backup_db_job_key`.
- **Stahování médií:** **přijetí úlohy do fronty není dokončení vzdálené operace ani vzdálený úspěch.** Skutečné získání odkazu, metadat a uspořádání souboru proběhne až ve workeru; duplicitní cíl se odmítne před druhou vzdálenou operací. Původní DB `Container.Refresh` a cold-install event evidence zůstávají pouze historickým měřením odstraněné funkce, nikoli současným chováním médií.
- **Omezený životní cyklus:** jeden procesní vlastník drží jeden `BackgroundPrefetch` a jeden `DownloadJobManager`. Fronta médií má jednoho workera a **nejvýše čtyři** běžící i čekající úlohy; duplicitní cíl se odmítne. Každá přijatá práce používá a zavře **vlastní HTTP relaci**. `close(wait=False)` je idempotentní, odmítne novou práci a ruší čekající úlohy; rušení již blokovaného OS/socket I/O zůstává kooperativní.
- **Webshare a historie:** hlavní menu, lokální hledání/historie, zrušení klávesnice a odstranění historie nevyžadují síťovou autentizaci. Skutečné hledání a zařazení do fronty ověří token až při použití a sdílí jej v rámci jedné akce. Historie používá rekurzivní kanonický klíč celého obsahu s O(H) deduplikací, zachovává první výskyt a při odstranění bez platného ID znovu použije již staženou odpověď.
- **TMDB identity:** account ID se uloží až po validované odpovědi a za stále platné session/jazyka. Cold i watched cesty, operace i background worker sdílejí owner lock krátké lokální identity transakce. Zpožděná 401 starého účtu nesmí smazat právě dokončenou novou QR session; explicitní odhlášení a aktuální 401 nadále invalidují identitu podle původního kontraktu. HTTP a retry čekání nejsou pod tímto lockem.
- **Snapshot I/O:** jedna refresh-state transakce sdílí snapshot, generation a due rozhodnutí. Fresh warm cesta používá **1 JSON load místo 2**, stale UI + worker save **2 místo 5** a accepted complete save **1 místo 2**. Generation konflikt stále odmítne zápis a cizí účet/scope obsah nezmění.
- **Streamování médií:** média používají výchozí bloky **256 KiB**, kontrolované zápisy, dočasné soubory a teprve potom replacement. Produkce nečte celé `response.content`. Atomičnost vzdáleného VFS rename závisí na backendu. Dřívější ZIP evidence popisovala extrakci kontrolující CRC bez druhého `testzip()` průchodu a `json.load()` **jeden JSON dokument** najednou; tato archivní cesta byla spolu s Backup DB odstraněna.

### Naměřené chování a jeho hranice

Offline Windows/CPython benchmarky používaly synthetická data, testovací HTTP/Kodi/VFS dvojníky a tehdejší produkční služby. Časové výsledky uvádějí opakovaný **medián a rozsah minimum–maximum**, nikoli akceptační časový limit. Zachovaná media evidence prokazuje například warm watchlist bez account/watchlist requestů, dvě otevření s původně chybějícím account ID za celkem **3 HTTP místo 4**, čtyři místo 32 cache flushů při 32 úspěších a čtyřikrát méně write callů při 256 KiB místo 64 KiB. Jediný datový JSON read při warm DB `ensure_installed()` + výběru souboru místo 55 a cold/migrační/tamper DB cesty jsou výslovně pouze historická evidence odstraněné Backup DB, nikoli reprodukovatelné tvrzení o aktuálním zdroji. U historie je kanonická varianta při H=1000 rychlejší, ale při H=20/200 je měřeně pomalejší a potřebuje lineární pomocnou alokaci. Media event benchmarky dokazují pořadí handoff → skutečné dokončení, nikoli latenci zařízení. Historické mapování původních PERF-01…PERF-09, včetně stavu `removed` pro DB požadavky, je v `.superpowers/sdd/2026-11-25-performance-remediation/performance-coverage.json`.

Novější **333-testová offline regresní sada** doplňuje přesné bounded concurrency, publikaci identity a stale-401 scénáře cold, watched, metadata i background. Dva nezávislé opakované audity finálního runtime commitu byly uzavřené s **0 prokázanými funkčními a 0 výkonovými nálezy**.

Doplňující **live smoke v hlavním profilu Kodi 21.2** ověřil verzi 1.3.15 téhož runtime commitu před čistým povýšením metadata verze: hlavní menu s 6 položkami, 338 výsledků Webshare hledání „Matrix“, 66 filmů TMDB watchlistu včetně warm reopen a skutečné otevření/zrušení klávesnice. Nebyla provedena živá ověření na ARM, NFS/SMB či vzdáleném VFS; offline desktopové časy nejsou slibem latence či propustnosti těchto prostředí.

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
python .superpowers/sdd/2026-11-25-performance-remediation/task-5-event-benchmark.py
python .superpowers/sdd/2026-11-25-performance-remediation/task-6-dedup-benchmark.py --output .superpowers/sdd/2026-11-25-performance-remediation/task-7-history.json
python .superpowers/sdd/2026-11-25-performance-remediation/task-6-benchmark.py --output .superpowers/sdd/2026-11-25-performance-remediation/task-7-chunks-current.json
```

Historický `benchmark_backup_db.py` a jeho výstupy zůstávají v SDD evidenci, ale nejsou součástí ověření aktuálního zdroje po odstranění Backup DB.

Testy používají syntetická data a testovací dvojníky pro HTTP i Kodi. Běží offline, bez přístupu k síti a bez přihlašovacích údajů. Samy o sobě nedokazují aktuální chování vzdálených služeb ani hardware. Nebyla provedena živá ověření na ARM, NFS/SMB či vzdáleném VFS. Oddělený hlavní smoke test na Kodi 21.2 doplňuje ověření Webshare, TMDB a UI bez nové QR reautentizace; skutečné stahování filmu ani přehrávání v něm nebyly součástí testu.

## 📝 Changelog

Historie změn je v [GitHub releasesech](https://github.com/Hammanek/plugin.video.yawspreloaded/releases).

## 💬 Kontakt a podpora

Pokud narazíte na chybu nebo máte návrh na vylepšení, můžete mě kontaktovat:

- **Telegram:** [@hammanek](https://t.me/hammanek)
- **GitHub Issues:** [Zde](https://github.com/Hammanek/plugin.video.yawspreloaded/issues)

---
*Vytvořeno s ❤️ pro českou a slovenskou Kodi komunitu.*
