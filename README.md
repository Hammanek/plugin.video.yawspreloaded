# YAWsP Reloaded (Yet Another Webshare Plugin)

[![Version](https://img.shields.io/badge/verze-1.3.0-blue.svg)](https://github.com/Hammanek/plugin.video.yawspreloaded)
[![License](https://img.shields.io/badge/licence-AGPL--3.0-green.svg)](LICENSE)

Moderní a vylepšený doplněk pro Kodi umožňující sledování obsahu ze služby Webshare. Tento projekt vychází z původního doplňku od "cache" a přináší řadu vylepšení pro lepší uživatelský zážitek.

## ✨ Klíčové funkce

- 📺 **Integrace TMDB** – Watchlist filmů a seriálů z TheMovieDB (náhrada za zpoplatněný Trakt), české názvy, popisy a žánry přímo z TMDB.
- 🔍 **Pokročilé vyhledávání** – Rychlé a přesné vyhledávání přímo v databázi Webshare.
- 🎬 **Trailer systém** – Možnost přehrát trailery před samotným spuštěním filmu.
- ✅ **Správa watchlistu** – Označení jako zhlédnuté přímo z Kodi, položka se zároveň odstraní z TMDB watchlistu.
- 📅 **Další díl u seriálů** – U běžících seriálů se zobrazí číslo a datum příští epizody.
- 🗂️ **Řazení a filtry** – Watchlist lze řadit (abecedně, nově přidané, hodnocení, rok) a skrýt zatím nevydané filmy.
- 🛠️ **Opravy a optimalizace** – Pravidelné aktualizace pro zajištění stability a rychlosti.

## 📋 Požadavky

- Kodi **19 (Matrix) a novější**
- Účet na [Webshare](https://webshare.cz)
- Účet na [TMDB](https://www.themoviedb.org) (zdarma) – potřebný pro watchlist

## 🚀 Instalace

1. Stáhněte si repozitář jako [ZIP archiv](https://github.com/Hammanek/plugin.video.yawspreloaded/archive/refs/heads/main.zip).
2. V Kodi zvolte **Doplňky** -> **Instalovat ze souboru zip**.
3. Vyhledejte stažený soubor a potvrďte instalaci.
4. V nastavení doplňku vyplňte přihlašovací údaje k Webshare.

## ⚙️ Nastavení TMDB

1. Přihlaste se na TMDB a na [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api) si **vyžádejte API klíč** (typ *Developer*, schválení je okamžité a zdarma).
2. Na stejné stránce zkopírujte **API Read Access Token** (dlouhý token začínající `eyJ...`).
3. Vložte jej v nastavení doplňku (sekce **TMDB**) do pole **TMDB Read Access Token** – tím funguje čtení watchlistu.
4. *(Volitelné)* Pro **označení jako zhlédnuté** (odebrání z watchlistu) zvolte v menu doplňku **Připojit k TMDB...** a schvalte zobrazený odkaz v prohlížeči. Kód z odkazu najdete v případě potřeby také v `kodi.log`.

## 📝 Changelog

### 1.3.0
- Trakt.tv nahrazen TMDB (Trakt API je nově zpoplatněné).
- Watchlist filmů a seriálů z TMDB v češtině včetně sezón a epizod.
- Označení jako zhlédnuté zároveň odebere položku z TMDB watchlistu.
- Trailery se načítají z TMDB (YouTube), cachují se pro rychlé načítání.
- U seriálů se zobrazuje číslo a datum další epizody.
- Řazení watchlistu v nastavení + filtr zatím nevydaných filmů.
- Vyhledávání z watchlistu hledá název bez roku (lepší výsledky).
- API klíče přesunuty z kódu do nastavení doplňku.

### 1.2.5
- Oprava pádu Trakt watchlistu (TypeError: NoneType).
- Navýšen limit položek ve watchlistu na 1000.

### 1.2.4
- Opravy chyb v rozhraní.
- Implementace funkce trailerů.

### 1.1
- Přidáno vyhledávání v DB.

### 1.0
- Základní verze "Reloaded".
- Integrace Trakt.tv.
- Různá vylepšení stability.

## 💬 Kontakt a podpora

Pokud narazíte na chybu nebo máte návrh na vylepšení, můžete mě kontaktovat:

- **Telegram:** [@hammanek](https://t.me/hammanek)
- **GitHub Issues:** [Zde](https://github.com/Hammanek/plugin.video.yawspreloaded/issues)

---
*Vytvořeno s ❤️ pro českou a slovenskou Kodi komunitu.*
