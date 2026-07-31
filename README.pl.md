# valheim-proxmox

Jedna komenda na hoście Proxmoxa i masz serwer Valheima we własnym LXC, a do tego panel
WWW do zarządzania nim. Bez Dockera, bez kombinowania z RCON-em, bez kolejnego panelu do
utrzymywania.

🇬🇧 **[English version → README.md](README.md)**

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/PawelSzymanski89/valheim-proxmox/main/install.sh)"
```

Uruchamiasz **na hoście Proxmox VE** (jako root). Skrypt tworzy nieuprzywilejowany
kontener Debian 12, instaluje SteamCMD i serwer Valheima, zakłada usługi i timery systemd,
stawia panel i na koniec wypisuje adres, login oraz wygenerowane hasło.

Trwa kilka minut — prawie całość to pobieranie ~1,5 GB ze Steama.

## Jak to wygląda

Panel ma własną mroczną nordycką skórę — kamień, sadza i przygaszone złoto — na ekranie
logowania i w środku. Interfejs po polsku albo angielsku, przełącznik w prawym górnym rogu.

| | |
|---|---|
| ![Logowanie](docs/login.png) | ![Mody](docs/mods.png) |
| **Logowanie** — własny ekran, nie szare okienko przeglądarki | **Mody** — wklejasz kod z Thunderstore i wybierasz, co zainstalować |

![Podsumowanie](docs/summary.png)

**Podsumowanie** — adresy do wklejenia z przyciskiem kopiowania, żywe obciążenie kontenera
i testy, które mówią wprost, czego dowodzą. Adresy i sekrety na tych zrzutach maskuje sam
panel: otwierasz go z `?demo=1` i każde IP, hasło, kod dołączenia i kod profilu zamienia się
na wartość przykładową — zrzut nigdy nie wynosi sieci, w której powstał.

![Ustawienia](docs/settings.png)

**Ustawienia** — nazwa serwera, świat, porty, widoczność, crossplay, preset i modyfikatory
świata oraz logowanie do panelu.

## Co dostajesz

| | |
|---|---|
| **Serwer gry** | Valheim dedicated, systemd z czystym stopem (`SIGINT`, więc świat się zapisuje) |
| **Panel** | WWW na porcie **2460**, autoryzacja HTTP Basic, hasło losowane przy instalacji |
| **Backupy** | kopia świata co 2 h, trzyma 30, przywracanie jednym klikiem |
| **Aktualizacje** | sprawdza Steama co 2 h i restartuje **tylko** gdy jest nowy build |
| **Domyślnie** | 4 rdzenie, 6 GB RAM, 30 GB dysku, kontener wstaje z hostem |

## Panel

| Zakładka | Co daje |
|---|---|
| **Summary** | adresy do wklejenia — z LAN-u i z internetu (z kopiowaniem), żywe obciążenie / RAM / dysk kontenera oraz testy łączności, które mówią wprost, czego dowodzą, a czego nie |
| **Players** | kto gra teraz — nick, identyfikator, **licznik czasu sesji na żywo** — oraz trwała historia logowań (pierwszy raz / ostatnio / ile wejść) |
| **Access & bans** | lista adminów, lista banów, whitelista; ban prosto z listy online albo z historii. **Niepusta whitelista wpuszcza wyłącznie wpisanych** — to reguła samego Valheima, nie panelu |
| **World** | lista światów, przełączanie aktywnego, pobieranie, kasowanie, wgrywanie pary `.db` + `.fwl` |
| **Backups** | przywróć, pobierz, usuń; przełączniki timerów auto-backup i auto-update |
| **Settings** | nazwa serwera, świat, hasło, **port gry**, **port panelu**, widoczność na liście serwerów, crossplay, preset i modyfikatory świata (walka, kara za śmierć, surowce, najazdy, portale) oraz przełączniki (`nobuildcost`, `playerevents`, `passivemobs`, `nomap`) |
| **Mods** | wklejasz **kod udostępniania** z Thunderstore Mod Managera / r2modman: panel go rozwija, pokazuje zawartość i instaluje zaznaczone paczki (BepInEx w komplecie, świat najpierw do kopii). Umie też pojedyncze paczki po nazwie |
| **Log** | zdarzenia serwera, bez szumu keepalive od PlayFaba |

Plus Start / Stop / Restart / Backup teraz / Sprawdź update.

### Logowanie do panelu i odzyskiwanie dostępu

Logujesz się na własnym ekranie panelu, nie w szarym okienku przeglądarki: ciasteczko sesji
podpisane sekretem **i** skrótem aktualnego hasła, więc zmiana hasła kończy wszystkie sesje.
HTTP Basic dalej działa — dla `curl`a i skryptów.

Pierwsze logowanie jest **zawsze takie samo, celowo**: **`admin` / `valheim123`**. Żadnego
szukania wylosowanego ciągu w wyjściu instalatora. Panel pokazuje czerwony baner, dopóki
hasła nie zmienisz w **Ustawieniach → Logowanie do panelu**, i nie pozwoli ustawić
domyślnego z powrotem.

Zablokowałeś się? Nie ma żadnej procedury resetu — ustawiasz nowe hasło z hosta Proxmoxa:

```bash
pct exec <CTID> -- /opt/valheim/panel-passwd.sh 'nowe-haslo-min-8-znakow'
pct exec <CTID> -- /opt/valheim/panel-passwd.sh nowyuser 'nowe-haslo'   # razem z loginem
```

Skrypt zapisuje `/opt/valheim/panel.env` (600, root) i działa od następnego zapytania —
bez restartu. Login możesz też podać przy instalacji: `--panel-user` / `--panel-pass`.

### Porty

| Port | Co |
|---|---|
| `2456-2458/udp` | gra (Valheim zawsze zajmuje trzy kolejne porty od tego, który ustawisz) |
| `2460/tcp` | panel — celowo tuż obok portów gry, żeby się pamiętało, i z dala od zajeżdżonych 8080, 8000, 9000, 8006… |

Oba zmienisz w **Settings**. Zmiana portu panelu restartuje go przez `systemd-run`, żeby
zapytanie, które tę zmianę zleciło, zdążyło dostać odpowiedź. Panel nie pozwoli ustawić
portu, który wpadłby w trzyportowy zakres gry.

## Granie z internetu

Przekieruj na routerze **UDP 2456-2458** na kontener. Tyle wystarczy — Valheim to goły
UDP, nie idzie przez reverse proxy i nie potrzebuje certyfikatu.

### Crossplay zmienia znaczenie słowa „port"

**Zmierzone, nie założone:** przy włączonym crossplayu serwer rozmawia przez relay PlayFaba i
**w ogóle nie otwiera portu gry** — `ss -uln` pokazuje wyłącznie port zapytań. Gracze wchodzą
z listy crossplay przez kod dołączenia, a przekierowanie portów na routerze nie robi nic.

Przy wyłączonym crossplayu serwer nasłuchuje na `2456` i wchodzi się po adresie — i po to
właśnie jest forward opisany wyżej. Dlatego instalator zostawia crossplay **wyłączony**;
włączysz go w Settings, jeśli wolisz graczy z Xboxa/Game Passa zamiast wejścia po adresie.
**Crossplay wymaga `libpulse-mainloop-glib0`.** Bez tego PlayFab Party nigdy się nie
inicjalizuje, log co 30 s powtarza `begin PlayFab create and join network`, a kod dołączenia
wychodzi pusty — czyli serwer, do którego nikt nie wejdzie żadną drogą. Instalator ją dokłada;
diagnoza to `ldd libparty.so`. Przy działającym crossplayu panel wyciąga **kod dołączenia**
z logu i pokazuje go na Summary (zmienia się przy każdym restarcie).


**Panelu nie wystawiaj na świat.** Umie kasować światy i wydawać je do pobrania. Tylko
LAN albo VPN. Jeśli musisz — schowaj go za reverse proxy z własną autoryzacją.

## Mody z kodu udostępniania

Wyeksportuj profil w Thunderstore Mod Managerze albo r2modmanie (**Settings → Export profile →
as a code**) i wklej kod w zakładce **Mods**. Panel ściąga profil z Thunderstore, wypisuje
paczki z dokładnymi wersjami i instaluje te, które zostawisz zaznaczone — razem z BepInEx-em,
jeśli go jeszcze nie ma.

Po co przez kod, a nie klikając mody z listy: wersje w kodzie to dokładnie te, które mają już
Twoi gracze, a to niezgodność wersji odbija ludzi przy wejściu. Kod jest potem widoczny na
zakładce **Summary**, więc możesz go oddać każdemu, kto ma się dostroić.

Profile zawierają też mody czysto klienckie (UI, mapy, dźwięki). Na serwerze zwykle nie
przeszkadzają, ale kilka potrafi rzucić wyjątkiem, więc odznacz to, czego serwer nie
potrzebuje. Przed pierwszym modem świat trafia do kopii — mody potrafią popsuć zapis
nieodwracalnie.

Zainstalowane mody leżą w `server/BepInEx/plugins/<autor>-<Paczka>/`, a `start.sh` sam włącza
loader doorstop, gdy tylko pojawi się katalog `BepInEx/`.

## Opcje

Każdą wartość nadpiszesz zmienną środowiskową:

```bash
CTID=250 RAM=8192 CORES=6 DISK=40 GAME_PORT=2456 PANEL_PORT=2460 \
SERVER_NAME="Klans" WORLD_NAME="Midgard" SERVER_PASS="wpuscmnie42" \
bash -c "$(curl -fsSL .../install.sh)"
```

| Zmienna | Domyślnie | |
|---|---|---|
| `CTID` | pierwszy wolny | numer kontenera |
| `HOSTNAME_` | `valheim` | nazwa hosta kontenera |
| `CORES` / `RAM` / `DISK` | `4` / `6144` / `30` | rdzenie / MB / GB |
| `STORAGE` | pierwszy storage przyjmujący rootfs | gdzie ląduje dysk kontenera |
| `IP` / `GW` | `dhcp` | stały adres zamiast DHCP: `IP=192.168.89.21/24 GW=192.168.89.1` — przydaje się, gdy DNS albo forward już wskazują ten adres |
| `BRIDGE` | `vmbr0` | mostek sieciowy |
| `GAME_PORT` / `PANEL_PORT` | `2456` / `2460` | |
| `SERVER_NAME` / `WORLD_NAME` | `Valheim` / `Dedicated` | |
| `SERVER_PASS` | losowe 10 znaków | hasło do gry (min. 5 znaków i **nie może zawierać** nazwy serwera ani świata — gra to odrzuca) |

### Flagi

Wszystko powyżej działa też jako flaga — czytelniej wygląda w jednolinijkowcu, który się gdzieś zapisuje:

```bash
bash -c "$(curl -fsSL .../install.sh)" -- --ram 12288 --disk 40 --ip 192.168.89.21/24 --gw 192.168.89.1
```

`--help` wypisze listę razem z aktualnymi domyślnymi wartościami.

## Instalacja bez Proxmoxa

`setup.sh` działa samodzielnie na dowolnym Debianie 12:

```bash
curl -fsSL .../setup.sh -o setup.sh && bash setup.sh
```

## Co gdzie leży

```
/opt/valheim/
├── server/            pliki gry (SteamCMD)
├── data/              savedir: worlds_local/, adminlist.txt, bannedlist.txt, permittedlist.txt
├── backups/           world-RRRRMMDD-GGMMSS.tar.gz, trzyma 30
├── server.env         ustawienia startowe — to edytuje panel
├── panel.env          login, hasło i port panelu (600)
├── players.json       historia logowań (journal się rotuje, ten plik nie)
├── start.sh           skleja argumenty startowe z server.env
├── backup.sh          kopia świata + retencja
├── update.sh          sprawdzenie buildu na Steamie, restart tylko gdy nowy
└── panel/             app.py, index.html, .venv
```

systemd: `valheim`, `valheim-panel`, `valheim-backup.timer`, `valheim-update.timer`.

## Czego uczciwie nie da się zrobić

- **Valheim nie ma RCON-a.** Komendy w grze (kick, spawn, pogoda, tryb boga) wpisuje się
  w konsoli F5 jako gracz, którego identyfikator jest w `adminlist.txt`. Panel zarządza tą
  listą — nie napisze za Ciebie w grze. „Kick" to tutaj ban i odbanowanie.
- **Lista graczy online to heurystyka.** Linia z nickiem (`Got character ZDOID from …`)
  nie zawiera identyfikatora gracza, więc nicki dopinane są do połączeń w kolejności
  wejścia. Miarodajny licznik (`Connections N`) pada raz na ~10 minut — gdy oba się
  rozjadą, panel to pokazuje, zamiast ukrywać.
- **Panel chodzi jako root** we własnym kontenerze: woła `systemctl` i pisze po
  `/opt/valheim`. Dlatego to osobny kontener i dlatego nie ma go w internecie.

## Na czym sprawdzone

Proxmox VE 8.4, szablon `debian-12-standard`, Valheim dedicated `l-0.221.12`.
Każda akcja panelu przeszła test na prawdziwym kontenerze: propagacja ustawień aż do
argumentów działającego procesu, przełączanie/wgrywanie/kasowanie światów, przywracanie
kopii (suma kontrolna zgodna przed i po), timery, zmiana portu panelu, zmiana logowania.
Listę graczy i historię pokrywa `panel/test_parse.py`, bo wymagają realnych wejść na serwer.

## Licencja

MIT — patrz [LICENSE](LICENSE).
