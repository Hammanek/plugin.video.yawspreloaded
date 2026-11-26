# YAWsP Reloaded (Yet Another Webshare Plugin)

[![Version](https://img.shields.io/badge/verze-2.0.0-blue.svg)](https://github.com/Hammanek/plugin.video.yawspreloaded)
[![License](https://img.shields.io/badge/licence-AGPL--3.0-green.svg)](LICENSE)

Moderní a vylepšený doplněk pro Kodi umožňující sledování obsahu ze služby Webshare. Tento projekt vychází z původního doplňku od "cache" a přináší řadu vylepšení pro lepší uživatelský zážitek.

## ✨ Klíčové funkce

- 🧩 **Modulární architektura 2.0.0** – Webshare, TMDB, download a Kodi UI jsou oddělené komponenty, s jednotným routerem a zachovanými akcemi pro média, frontu, historii i watchlist.
- 🧭 **Objevit** – Statický výběr čtyř veřejných TMDB kategorií a běžné nativní řádky InfoWall; výběr pokračuje do hledání Webshare.
- 🏷️ **Nativní metadata Kodi** – Adaptér `yawsp_app/metadata.py` používá `InfoTagVideo` z `getVideoInfoTag()` a dedikované settery včetně `setFirstAired`. Kodi 19 nemá všechny dedikované settery z Kodi 20, proto adaptér v takovém případě jednou předá celý vstup přes `setInfo("video", info)`. Nešlo o živé ověření běhu na Kodi 19.
- 🔄 **Ruční obnovení watchlistu** – Viditelný řádek **Obnovit** používá explicitní `refresh=1`; kategorie s hodnotou **Místní mezipaměť** označuje data vykreslená z lokálního snapshotu. Po chybě ručního načtení se použije poslední platný snapshot, pokud existuje a stále odpovídá aktuální identitě; po ručním pokusu nevzniká duplicitní okamžitý background watchlist požadavek.
- 🔎 **Srozumitelné prázdné hledání** – Pokud po kontrole platnosti a porovnání názvů nezůstane žádný zobrazitelný soubor, Webshare ukáže řádek **Žádné výsledky**. Ovládání stránkování se stále odvozuje od nefiltrovaného `raw_count` serveru, takže tato zpráva nepotvrzuje prázdnou celkovou odpověď ani nemaže další stránky.
- 🗑️ **Odstraněná Backup DB** – Zastaralá JSON/textová Backup DB už nemá položku menu, instalaci ani akce `db` a `searchdb`. Staré záložky s těmito akcemi otevřou obyčejné hlavní menu bez autentizace, sítě či čtení databázových souborů. Existující uživatelské soubory `database*.json`, `database*.txt`, adresář `db` a trvalé zálohy se nemažou ani nemigrují. Skutečné stahování médií a jeho historie zůstávají zachované; `DownloadJobKey` přijímá pouze rodinu `media`.
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

### Trvalé zálohy profilu

Dva schválené archivy `yawsp-main-profile-pre35b4e4a.zip` a `yawsp-main-profile-pre2.0.0-manifest.zip` jsou zachované ve Windows Store profilu v trvalém úložišti `Kodi/userdata/backup/plugin.video.yawspreloaded`. Přesné názvy a hodnoty **SHA-256** cílů odpovídají originálům; originály v **Temp** zůstávají odděleně a beze změny. Nevypisuje se obsah archivů, přihlašovací údaje ani jiné tajné hodnoty.

## ⚙️ Nastavení TMDB

1. Na [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api) si zdarma **vyžádejte API klíč** (okamžité schválení).
2. Zkopírujte **API Key** (32 znaků) do nastavení doplňku, sekce **TMDB**.
3. Klikněte **Připojit k TMDB...** a naskenujte telefonem zobrazený **QR kód** – po schválení je vše nastavené (jednorázově).

## 🧭 Objevit a pohledy

Hlavní menu má v této větvi přesné pořadí **Objevit → Vyhledávání → Chci si stáhnout → Historie stahování → Nastavení → TMDB Watchlist**. Objevit otevře obyčejný nativní seznam čtyř složek: populární filmy (`popular_movies` → `/movie/popular`), populární seriály (`popular_shows` → `/tv/popular`), nejlépe hodnocené filmy (`top_rated_movies` → `/movie/top_rated`) a nejlépe hodnocené seriály (`top_rated_shows` → `/tv/top_rated`). Hlavní menu i výběr kategorií jsou statické, tedy bez HTTP požadavků, kontrol nastavení či práce na pozadí; výběr kategorií nespouští žádná zpětná volání pohledu. Čtyři složky se zobrazí pouze při chybějícím klíči `source`; explicitně prázdný, neřetězcový nebo neznámý zdroj skončí místní chybou bez otevření nastavení či sítě. Teprve platná kategorie načte nejvýše jednu stránku a při neprázdném seznamu nejvýše jeden sdílený požadavek na žánry; nepokračuje automaticky dalšími stránkami.

**Veřejný klient** používá skutečnou základní adresu `https://api.themoviedb.org/3`; požadavky zachovávají `api_key` a lokalizaci, ale nikdy neposílají `session_id`. TMDB API klíč zůstává povinný, zatímco osobní účet, session ani watchlist snapshot nejsou potřeba. Veřejná odpověď 401 zachová osobní TMDB identitu. Řádky s artwork, lokalizovaným názvem, rokem, žánry, hodnocením a popisem procházejí stávajícím pomocníkem `apply_video_metadata` a nativním `InfoTag`; prázdná veřejná odpověď přeskočí žánry a použije lokalizaci `30440`. Výsledek otevře běžné hledání Webshare kanonickou URL `plugin://plugin.video.yawspreloaded/?action=search&what=<kódovaný lokalizovaný název>&sort=largest`, bez autoplay, watched změn, account/watchlist požadavků, dávkového dotažování metadat či background prefetch. Volitelná nativní kontextová akce původního názvu používá `Container.Update` na stejnou Webshare trasu, nikoli skript.

Po výběru skutečné kategorie se její výsledek vykreslí jako běžné nativní řádky v pohledu **InfoWall**. Po úspěšném dokončení vybrané kategorie v **Estuary** stávající zpětné volání nastaví výchozí pohled pouze pokud `tmdb_view_defaulted` ještě není `true`; potom provede `Container.SetViewMode(54)` a uloží `true`. Statický výběr kategorií nespouští nastavení pohledu a opakované otevření po nastavení příznaku už nevynucuje pohled 54; pohled ani styl watchlistu se nemění. V případném schváleném nasazení nativní prezentace nevyžaduje reinstalaci kvůli přepnutí způsobu zobrazení.

Experimentální vlastní prezentace byla po izolovaných důkazech nepodporovaného chování trvale vyřazena; spolu s ní bylo odstraněno nastavení `discovery_native_wall` a lokalizace `30444`. Historické uložené hodnoty tohoto příznaku jsou neaktivní a nemění nativní chování. Objevit používá stávající běžný pomocník veřejných metadat, nepoužívá `WindowXML` a v balíčku nemá `yawsp_app/discovery_window.py`, `yawsp_app/discovery_script.py` ani `DiscoveryWindow.xml`; neobsahuje žádný samostatný skript.

**Sekvenční veřejné žánry** používají soubor `tmdb_genres.json` v profilu doplňku pouze jako úzké úložiště veřejného mapování ID → lokalizovaný název, nikoli jako mezipaměť seznamů kategorií. Dokument má přesně kořen `{"schema":1,"genres":{...}}` a povolených šest skupin `movie/en-US`, `movie/cs-CZ`, `movie/sk-SK`, `tv/en-US`, `tv/cs-CZ`, `tv/sk-SK`; JSON klíče ID jsou textové kladné celočíselné hodnoty a hodnoty jsou pouze aktuální veřejné názvy žánrů. Neukládá URL, session, účet, token, snapshot ani záznamy médií. Skutečné sekvenční commit semantics jsou `response.close` → validace → save/fsync/close → `os.replace` → nezávislý strict `reload`, a teprve potom pozdější invoker čte mapu; pozdější otevření média/jazyka bez uložené skupiny provede první příslušný genre GET. Platná přítomná prázdná mapa je platný cache hit a formát nemá žádné TTL ani časovou expiraci. Platí, že neznámý, poškozený ani nepodporovaný vstup není přepsán a není považován za důvěryhodný; jiné jazyky zůstávají v původním procesním režimu. Při nedostupném zápisu aktuální invocation použije původní paměťový fallback a pozdější nový owner znovu načte veřejná data. Běžná otevření kategorií dál provádějí běžné síťové volání kategorizovaného seznamu, protože tento sidecar není list cache. Toto úložiště není cross-process transakce ani důkaz OS zámku; neřeší aktivní background shutdown HANG.

## 🧩 Architektura

| Modul | Odpovědnost |
| --- | --- |
| `main.py` | Vstupní bod doplňku; předává parametry Kodi jednotnému routeru. |
| `service.py` | Oficiální nativní `xbmc.service` vstup; sestavuje úložiště a spouští jediného sériového TMDB konzumenta přímo ve službě, bez aplikačního vlákna. |
| `yawsp.py` | Tenká kompatibilní fasáda bez původní monolitické logiky. |
| `yawsp_app/runtime.py` | Kontext Kodi, sestavování URL, lokalizace a dokončení akcí. |
| `yawsp_app/webshare.py` | Webshare HTTP, XML validace, autentizace a doménové operace. |
| `yawsp_app/search.py` | Normalizace dotazů, Unicode porovnávání, stránkování a české řazení. |
| `yawsp_app/downloads.py` | Kontrolované streamování a bezpečný přechod přes dočasný soubor. |
| `yawsp_app/json_cache.py` | Defenzivní načítání, TTL záznamy a atomické ukládání cache. |
| `yawsp_app/genre_file_cache.py` | Striktní veřejný reader a sekvenční repozitář schématu 1 pro ID/názvy žánrů; nezmění neznámý vstup a nepřidává TTL. |
| `yawsp_app/watchlist_snapshot.py` | Kompletní snapshoty watchlistu oddělené účtem, médiem a jazykem. |
| `yawsp_app/download_jobs.py` | Omezená fronta stahování médií se skutečnými stavy úloh a typovanými rezervacemi; `DownloadJobKey` je pouze pro `media`. |
| `yawsp_app/metadata.py` | Jednotný adaptér `InfoTagVideo`, validace klíčů a celovstupový `setInfo` fallback pro starší Kodi. |
| `yawsp_app/discovery_ui.py` | Statický čtyřkategoriální výběr, čisté mapování typovaných výsledků katalogů, nativní řádky InfoWall a Webshare handoff Objevit. |
| `yawsp_app/tmdb.py` | TMDB transport a doménové operace, sessionless veřejný klient a cache adaptéry; služba je jejich jediný zapisovatel metadata, rodičovské adaptéry pouze čtou. |
| `yawsp_app/tmdb_service_protocol.py` | Uzavřené typované deskriptory, deduplikační klíče a striktní sanitizované výsledkové dokumenty bez přenosu callbacků či citlivých hodnot; transport zůstává v TMDB klientu. |
| `yawsp_app/tmdb_service_store.py` | Profilově globální OS transakce, nájem služby, rezervace, fronty a waiters, epochové ohrazení, durable continuations a bounded GC výsledků. |
| `yawsp_app/tmdb_service_client.py` | Neblokující rodičovský submit a abort-aware join/read/ACK; žádný parent HTTP fallback ani cache/snapshot zápis pro práci spravovanou službou. |
| `yawsp_app/tmdb_service_worker.py` | Sériový service-owned runner a doménové engines pro metadata, watchlist, identitu, katalogy a detaily; jedna relace na drain cyklus. |
| `yawsp_app/tmdb_youtube.py` | Plně ohraničená validace 11znakového YouTube ID a bezpečná `urlencode` konstrukce `PlayMedia` bez interpolace builtin syntaxe. |
| `yawsp_app/webshare_ui.py` | Prezentace vyhledávání, deduplikované historie, fronty a handoff stahování médií. |
| `yawsp_app/tmdb_ui.py` | Prezentace TMDB autorizace, QR, watchlistu, epizod a trailerů. |
| `yawsp_app/router.py` | Jednotné směrování akcí, adaptéry dokončení a hraniční zpracování chyb. |

`service.py` je jediná Kodi kompozice nativního konzumenta; protokol, store, client a worker neimportují `xbmc*`. **Veškeré mutace TMDB metadata cache, watchlist snapshotů a veřejných žánrů včetně migrací a deferred flushů vykonává pouze tento jeden service writer.** Rodičovské invocation jen čte kompatibilní cache/snapshoty a čeká na typované výsledky; výjimky z tohoto read-only pravidla jsou pouze explicitní TMDB QR/session autentizace a nezávislé Webshare/download operace, nikoli TMDB metadata. Neprázdné legacy trailer ID zůstane čitelné pro okamžité zobrazení, ale read-only parent mu nevytváří falešný svěží timestamp: vždy jej předá service k ověření a až service zapíše migraci. Legacy `None` se nemaže jako negativní cache hit.

## ⚡ Výkon, snapshoty a úlohy na pozadí

Verze **2.0.0** dokončuje přechod z původního monolitu na modulární architekturu. Měření níže rozlišuje počty requestů, I/O a souběžných rezervací od tvrzení o latenci.

- **TMDB watchlist:** kompletní snapshot je klíčovaný podle **účtu TMDB, typu média, jazyka a verze schématu**, bez tokenů či přihlašovacích údajů. Warm kompatibilní snapshot se vykreslí lokálně s **nulovými account/watchlist HTTP požadavky**; filtrování a globální řazení stále pracují nad celým seznamem. Data mohou být **dočasně zastaralá**, protože obnova na pozadí se projeví až při **pozdějším otevření** (stale-while-revalidate). Při chybějícím, nekompatibilním nebo poškozeném snapshotu cold cesta znovu načte všechny stránky. Uloží se až po úspěchu a validaci **všech stránek**; při chybě vznikne **žádný částečný snapshot** a předchozí kompletní data zůstanou zachována. Změna identity, autentizace a pravidla watched akcí invalidují pouze příslušný rozsah.
- **Ruční refresh a lokální cache:** řádek **Obnovit** je viditelný i u prázdného watchlistu a otevírá stejnou kategorii s explicitním `refresh=1`. Manuální cesta načte a validuje všechny stránky před uložením. Kategorie **TMDB Watchlist / Místní mezipaměť** vysvětluje, že se právě ukazuje lokální snapshot; po chybě se opět použije poslední platný snapshot, pokud existuje a odpovídá aktuální identitě, a ohlásí se chyba načtení, nikoli částečná nová data. Dokončení manuální cesty nezařazuje duplicitní okamžitý watchlist refresh na pozadí. Běžné teplé otevření si ponechává dříve popsané stale-while-revalidate chování.
- **TMDB metadata:** listování použije jeden read-only průchod cache trailerů a epizod a odesílání se skládá do `post_complete` až po `endOfDirectory`. Nativní service worker ukládá atomicky po nejvýše osmi validovaných změnách (32 změn = nejvýše čtyři flush operace) a úspěšné on-demand výsledky jsou součástí stejné service-owned cesty; chyby se neukládají jako negativní cache. Sériový konzument střídá trailery a seriály, jeden transientní neúspěch dostane jedno krátké deferred retry ve stejné aktivní claim a všechny accepted same-key cally sdílejí jeden job ID, request a výsledek. Profilově globální strop je **32 background + 1 nezávislá interaktivní rezervace**, doplněný bounded backlogy 64/8. Interaktivní join kolizní background práce nepřidává rezervaci a označí ji prioritní; nezávislá interaktivní práce má přednost před dosud nezahájeným backgroundem. Každé otevření předá metadata prvních 50 viditelných řádků, nikoli neomezenou historii.
- **Historie odstraněné Backup DB:** dřívější implementace používala atomický **sidecar** mimo datový adresář s relativním jménem, velikostí a `mtime_ns` každého souboru. Shodná teplá instalace prováděla pouze **O(F)** kontrol metadat a `load()` parsoval **pouze vybraný soubor**. Tyto výsledky popisují odstraněný historický kód, ne současnou funkci; produkce už neobsahuje `BackupDBStore`, `backup_db.py` ani `backup_db_job_key`.
- **Stahování médií:** **přijetí úlohy do fronty není dokončení vzdálené operace ani vzdálený úspěch.** Skutečné získání odkazu, metadat a uspořádání souboru proběhne až ve workeru; duplicitní cíl se odmítne před druhou vzdálenou operací. Původní DB `Container.Refresh` a cold-install event evidence zůstávají pouze historickým měřením odstraněné funkce, nikoli současným chováním médií.
- **Omezený životní cyklus:** produkce už neobsahuje `BackgroundPrefetch` ani TMDB `ThreadPoolExecutor`. `service.py` drží OS nájem a vykonává nejvýše jednu TMDB operaci najednou přímo ve své nativní invocation; parent worker ani executor pro TMDB nevzniká. Service manifest je omezen na 128 jobů a 128 waiterů, 256 KiB manifest, 1 MiB na výsledek a 16 MiB agregátních terminálních výsledků; nepřečtené waiteren-pinované výsledky se GC nevyhazují a 32 nejnovějších unwaited výsledků/tombstonů zůstává bounded. Při stopping se nová práce odmítne a nezahájená práce dostane typovaný abort bez HTTP. Selhání publikace znovu načte autoritativní active+fence záznam: pokud terminál skutečně commitnutý je, nestane se druhý reject; jinak bounded `BACKPRESSURED` uvolní rezervaci i při pinned saturated results. Přetrvávající I/O chyba owner zastaví pro fresh recovery, nikoli nekonečný spin. No-op claim/drain logicky nezměněný manifest nezapisuje. Čekání na chybějící výsledek používá pouze bezpečný lstat probe cesty a teprve jeho přítomnost spustí autoritativní locked validaci; 0.05s abort slicing zůstává. Parent `close()` pouze odpojí vlastní waiter a zavře invocation-local auth/Webshare zdroje, ne service. Aktivní socket se násilně neruší: dokončení je kooperativní, response/session se zavřou při běžném unwindingu a nájem se uvolní ve `finally`. Nezávislý `DownloadJobManager` ponechává jednoho media workera a **nejvýše čtyři** běžící i čekající úlohy; každá přijatá media práce používá a zavře **vlastní HTTP relaci** a jeho `close(wait=False)` zůstává idempotentní.
- **Webshare a historie:** hlavní menu, lokální hledání/historie, zrušení klávesnice a odstranění historie nevyžadují síťovou autentizaci. Skutečné hledání a zařazení do fronty ověří token až při použití a sdílí jej v rámci jedné akce. Historie používá rekurzivní kanonický klíč celého obsahu s O(H) deduplikací, zachovává první výskyt a při odstranění bez platného ID znovu použije již staženou odpověď.
- **TMDB identity:** account ID se uloží až po validované odpovědi a za stále platné session/jazyka. Cold i watched cesty, operace i background worker sdílejí owner lock krátké lokální identity transakce. Zpožděná 401 starého účtu nesmí smazat právě dokončenou novou QR session; explicitní odhlášení a aktuální 401 nadále invalidují identitu podle původního kontraktu. HTTP a retry čekání nejsou pod tímto lockem.
- **Bounded transport a soukromí:** každý TMDB GET/QR/watched response používá `stream=True`, 64 KiB bloky a skutečný strop **1,048,576 bajtů**. Oversized `Content-Length` se odmítne před čtením, chybějící hlavička neochrání překročení přes chunks a UTF-8/JSON dekódování stále končí typed error a `response.close()`. Account-scoped routa se v textu i nested traceback chyb nahrazuje fixním `{redacted}`; veřejné necitlivé cesty zůstávají čitelné. Trailer hit akceptuje pouze přesně 11 znaků `[A-Za-z0-9_-]`, jinak nevznikne negativní cache ani `PlayMedia`. Batch submit používá reversible per-key delta, jeden whole-document candidate copy a jednu finální `_fits()` strukturální kontrolu před autoritativním zápisem; přijaté kolizní siblings a bounded backpressure se nemění.
- **Snapshot I/O:** jedna refresh-state transakce sdílí snapshot, generation a due rozhodnutí. Fresh warm cesta používá **1 JSON load místo 2** a accepted complete save **1 místo 2**; dřívější měření stale UI + worker save **2 místo 5** je historická měřicí evidence před přesunem writeru do služby. Aktuální service drží identity compare-and-set, generation-checked snapshot mutation a manifest continuation v jednom krátkém OS identity kritickém úseku, ale HTTP ani stránkování pod tímto lockem neběží. Generation konflikt odmítne starý zápis a vytvoří/coalescuje forced continuation s nejnovější generation; cizí účet/scope obsah nezmění.
- **Streamování médií:** média používají výchozí bloky **256 KiB**, kontrolované zápisy, dočasné soubory a teprve potom replacement. Produkce nečte celé `response.content`. Atomičnost vzdáleného VFS rename závisí na backendu. Dřívější ZIP evidence popisovala extrakci kontrolující CRC bez druhého `testzip()` průchodu a `json.load()` **jeden JSON dokument** najednou; tato archivní cesta byla spolu s Backup DB odstraněna.

### Naměřené chování a jeho hranice

Offline Windows/CPython benchmarky používaly synthetická data, testovací HTTP/Kodi/VFS dvojníky a tehdejší produkční služby. Časové výsledky uvádějí opakovaný **medián a rozsah minimum–maximum**, nikoli akceptační časový limit. Zachovaná media evidence prokazuje například warm watchlist bez account/watchlist requestů, dvě otevření s původně chybějícím account ID za celkem **3 HTTP místo 4**, čtyři místo 32 cache flushů při 32 úspěších a čtyřikrát méně write callů při 256 KiB místo 64 KiB. Jediný datový JSON read při warm DB `ensure_installed()` + výběru souboru místo 55 a cold/migrační/tamper DB cesty jsou výslovně pouze historická evidence odstraněné Backup DB, nikoli reprodukovatelné tvrzení o aktuálním zdroji. U historie je kanonická varianta při H=1000 rychlejší, ale při H=20/200 je měřeně pomalejší a potřebuje lineární pomocnou alokaci. Media event benchmarky dokazují pořadí handoff → skutečné dokončení, nikoli latenci zařízení. Historické mapování původních PERF-01…PERF-09, včetně stavu `removed` pro DB požadavky, je v `.superpowers/sdd/2026-11-25-performance-remediation/performance-coverage.json`.

Aktuální offline regresní sada pokrývá OS-fenced admission, globální limity, recovery, atomické publikace, cross-instance cache/snapshot zápisy, identitu a stale-401, watched/refresh continuations, skutečné `SystemExit` i same-key deferred retry s nezávislým interaktivním jobem. Negativní mutační kontroly znovu zavádějí historické chyby v dočasné kopii; nejde o důkaz finálního nativního běhu. Na funkční/cache, security/privacy a performance nezávislé verdikty pro tuto větev se stále čeká.

Historický **live smoke v hlavním profilu Kodi 21.2** ověřil verzi 1.3.15 tehdejšího runtime commitu před vydáním 2.0.0: hlavní menu s 6 položkami, 338 výsledků Webshare hledání „Matrix“, 66 filmů TMDB watchlistu včetně warm reopen a skutečné otevření/zrušení klávesnice. Jediné zaznamenané živé prostředí je **Kodi 21.2 (Omega), Windows Store x64**. Tato evidence se netýká aktuální service implementace ani nového Objevit této větve. Samostatná historická matice Objevit pro přesný commit `338b70f` proběhla v čerstvém **izolovaném profilu Kodi 21.2** přes **ActionRunner** a nativní Input: šest položek root, čtyři složky, **4 běžné list requesty** a **1 movie + 1 TV genre GET**, filmové i seriálové `getVideoInfoTag()` readback, Webshare handoff a **Application.Quit** s **exit 0**. Ani tato Input/metadata evidence není externí uživatelsky interaktivní nebo vizuální akceptace.

Externí disposable **Gate 0** prototyp ověřil proveditelnost topologie před produkční změnou: dva současně aktivní nativní waiters sdíleli jeden job/GET/vlastníka, druhý lease contender nevytvořil consumer, 40 různých background klíčů proběhlo jednou při maximu 32+1 rezervací, owner abort uvolnil zdroje a tři čerstvé aktivní 12sekundové shutdown běhy skončily `0` bez `CPythonInvoker ... waiting on thread`. Při těchto exit bězích Kodi vyvolalo `SystemExit` před vrácením response objektu, proto response close nebyl aplikovatelný; session/finalization/lease cleanup byl povinný a zaznamenaný. **Gate 0 není finální integrovaná akceptace aktuálního zdroje.** Finální pět native gates, tři nové aktivní shutdown běhy finálního archivu v nových izolovaných profilech a nezávislá vizuální kontrola UI stále čekají na provedení.

Kompatibilita **Python 3.8** je kontrolovaná pouze staticky pomocí **AST**/syntaxe a lokální testy běží také na **Python 3.12**; ani jedno nedokazuje spuštění na **Kodi 19**. POSIX adapter má pouze statické testy s doubles: Linux/POSIX flock, NFS/SMB a remote VFS runtime nejsou prokázány. Nebylo provedeno skutečné přehrávání na Kodi 19, ARM ani SMB/NFS a žádné z těchto prostředí se neprezentuje jako úspěšně otestované. Nativní TMDB služba nebyla vydána. Webshare handoff, čtyři Objevit katalogy, InfoWall/view pravidla, warm/manual/failed watchlist a jedna watched post-action refresh jsou offline UI kontrakty, nikoli dokončená vizuální akceptace.

Starší text zde vyžadoval **Task 4 před sloučením** pro předchozí Objevit přezkoušení; aktuální service větev má vlastní výše popsané finální native a vizuální gates. Historický aktivní legacy background shutdown **HANG**, původně označený jako nezávislý a nevyřešený, byl v Gate 0 reprodukován pro odstraněný `BackgroundPrefetch`; tato věta není tvrzením, že stejný legacy vlastník v produkci zůstal. Ani Gate 0, offline testy ani historické výsledky nejsou slibem okamžitého rušení blokovaného socket I/O nebo release readiness. Tato větev nebyla sloučena, vydána jako nová verze ani nasazena do hlavního profilu; hlavní profil a jeho trvalé zálohy zůstávají neměnné. Schválení změny profilu, merge a release jsou samostatná pozdější rozhodnutí.

## ✅ Ověření pro vývojáře

Spusťte z kořene repozitáře:

```sh
python -m unittest discover -s tests -v
```

Workflow `.github/workflows/tests.yml` spouští při `push` a `pull_request` pouze čtení repozitáře (`contents: read`) a matici Python **3.8/3.12**. Akce jsou připnuté na `actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683` a `actions/setup-python@e797f83bcb11b83ae66e0230d6156d7c80228e7c`; nepoužívají tajné údaje ani release/write/upload operace. CI příkazy jsou:

```sh
python -B -m unittest discover -s tests -v
python -B scripts/verify_release.py
```

Verifier lze spustit také lokálně. Dynamicky vybírá runtime soubory sledované v git **HEAD**, takže nepoužívá zastaralý pevně zapsaný počet souborů. Ověřuje verzi a instalační odkazy README, přesný seznam a kořen ZIP, CRC i shodu každého bajtu se zdrojem. Výstup CLI obsahuje pouze `version=`, `runtime_files=` a `sha256=`. Bez explicitního testovacího výstupního adresáře vytvoří dočasný archiv a po kontrole jej odstraní; **neukládá trvalý release archiv ani artefakt**. Výsledky v této dokumentaci pocházejí z lokálních offline příkazů, nikoli z důkazu úspěšného běhu hosted GitHub Actions.

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
