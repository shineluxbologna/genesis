# Tag automatico per l'early access

Ogni giorno questo programma legge i clienti Shopify creati in una finestra di date e, solo se arrivano dal modulo early access, aggiunge il tag `early access targa`. Quel tag è quello che tiene aggiornato il segmento clienti: lo script non crea il segmento e non toglie tag.

Non usa il computer di casa se lo lanci da GitHub Actions. Il portatile può restare spento.

## Cosa distingue il modulo dagli acquisti normali

Lo script non indovina chi ha compilato il modulo. Il modulo (Shopify Forms, un Flow, o l'app con cui raccogli le iscrizioni) deve già mettere un tag sul cliente, oppure il cliente deve corrispondere a un filtro di ricerca che imposti tu.

| | Valore di default | A cosa serve |
| --- | --- | --- |
| Tag del modulo | `form-early-access` | Già presente sul cliente. Chi non ce l'ha viene saltato, compresi gli acquirenti normali. |
| Tag del segmento | `early access targa` | Lo aggiunge lo script, se manca. È il tag del segmento in Shopify. |

I due tag non sono la stessa cosa. Il primo dice «questa persona viene dal modulo». Il secondo dice «questa persona entra nel segmento early access».

Se il modulo mette un tag diverso, cambia `EARLY_ACCESS_SOURCE_TAG` e scrivi quel tag **identico**, maiuscole e spazi compresi. Se non usi un tag e vuoi filtrare in un altro modo, imposta `EARLY_ACCESS_SOURCE_TAG` a `-` e compila `CUSTOMER_QUERY_EXTRA` con un filtro di ricerca clienti, per esempio `tag:'lista-vip'`, `email:*@tuodominio.com` o `country:IT`. Senza almeno uno dei due filtri lo script si ferma: non tagga tutto il negozio.

La ricerca clienti di questa versione API non elenca un filtro per le note. Se l'iscrizione finisce solo in una nota, fai mettere il tag `form-early-access` dal modulo o da un Flow.

In Shopify Admin crea il segmento a mano, una volta sola: **Clienti → Segmenti →** condizione sul tag cliente uguale a `early access targa`. Da quel momento il segmento si aggiorna perché ogni giorno arrivano clienti con quel tag.

## Cosa serve su Shopify

Un'app nel Dev Dashboard, installata sul negozio (SHINELUX), con questi scope e nient'altro:

- `read_customers` per leggere i clienti
- `write_customers` per aggiungere il tag

Le app del Dev Dashboard non consegnano un token permanente. A ogni esecuzione lo script chiede un token nuovo con Client ID e Client secret. Il token dura 24 ore (`expires_in` 86399): per un giro al giorno ne basta uno. Non va copiato da nessuna parte.

Nel modulo di creazione dell'app, campo per campo:

1. Apri il [Dev Dashboard](https://dev.shopify.com/dashboard) e crea un'app.
2. Nome app: `Early access tagger`.
3. Nome versione: lascialo vuoto.
4. App URL: `https://shopify.dev/apps/default-app-home`.
5. Lascia deselezionato **Incorpora l'app**.
6. URL di reindirizzamento: lasciali vuoti.
7. Lascia deselezionata l'installazione legacy.
8. Scope: esattamente `read_customers,write_customers`.
9. Crea l'app, poi installala sul negozio SHINELUX.
10. Apri **Settings** dell'app e copia **Client ID** e **Client secret**.

Quei due valori vanno nei secret di GitHub, oppure nel file `.env` se lanci lo script sul computer. Lo script li scambia da solo con un token all'inizio di ogni esecuzione.

`SHOPIFY_SHOP` è il dominio completo, senza `https://`. Per SHINELUX è `shinelux.myshopify.com`. Non scrivere solo `shinelux` e non aggiungere `.myshopify.com` un'altra volta: lo script usa il testo così com'è.

Se hai già un token permanente `shpat_` di una custom app vecchia, puoi lasciarlo in `SHOPIFY_ADMIN_ACCESS_TOKEN` e non compilare Client ID e Client secret. Con entrambi i percorsi compilati, lo script usa Client ID e Client secret.

## Esecuzione sul tuo computer

Ti serve Python 3.11 o successivo. Sul Mac si controlla con Terminale, comando `python3 --version`.

Nella cartella del progetto:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Apri `.env` e sostituisci:

- `SHOPIFY_SHOP` con il dominio completo, per SHINELUX `shinelux.myshopify.com` (senza `https://`)
- `SHOPIFY_CLIENT_ID` e `SHOPIFY_CLIENT_SECRET` copiati da **Settings** dell'app nel Dev Dashboard

`SHOPIFY_ADMIN_ACCESS_TOKEN` resta vuoto, a meno che tu non stia usando un token permanente al posto della coppia Client ID / Client secret.

Le date e i tag hanno già un default sensato (dal 1 gennaio 2026 al 31 dicembre 2027, tag `form-early-access` e `early access targa`). Cambiali se la campagna è un'altra. Le date sono in UTC, formato ISO 8601. La finestra è inclusiva: entra chi è stato creato esattamente all'inizio o alla fine. Se scrivi solo il giorno, per esempio `2026-03-31`, conta l'intera giornata UTC.

Poi:

```bash
python -m early_access_tagger
```

Nel terminale, in italiano, compaiono tra le altre queste righe:

- `Trovati X nuovi clienti` — clienti del modulo dentro la finestra
- `Tag aggiunto a Y clienti` — a quanti è stato messo `early access targa` in questa esecuzione
- i salti (tag già presente, oppure non è il modulo)
- un errore per ogni cliente che Shopify ha rifiutato
- un riepilogo finale

Se mancano le credenziali o Shopify rifiuta l'accesso, il programma si chiude con un errore e un codice diverso da zero. Se invece l'esecuzione arriva in fondo e solo qualche cliente fallisce, il codice è zero: quegli id restano nel riepilogo, alla riga `errori`. Nei log non compaiono né il Client secret né il token.

Per controllare: apri un cliente appena taggato in Shopify Admin e verifica che tra i tag ci sia `early access targa`.

I test, senza negozio e senza token:

```bash
python -m unittest
```

## Ogni giorno con GitHub Actions

È la strada da usare. GitHub esegue lo script una volta al giorno anche se il computer è spento. Non serve un server.

Lo schedule parte solo dopo che questo file è su `main`. Il workflow si chiama **Tag early access giornaliero** e sta in `.github/workflows/daily-early-access-tag.yml`. Parte alle 06:00 UTC (le 8:00 in Italia con l'ora legale, le 7:00 in inverno) e si può lanciare anche a mano. GitHub a volte ritarda di qualche minuto gli orari programmati: è normale.

### Segreti e variabili

1. Apri il repository su GitHub.
2. In alto, **Settings**.
3. A sinistra, **Secrets and variables**, poi **Actions**.
4. Scheda **Secrets**, pulsante **New repository secret**. Creane uno alla volta:
   - Nome `SHOPIFY_SHOP`, valore `shinelux.myshopify.com`
   - Nome `SHOPIFY_CLIENT_ID`, valore il Client ID copiato da **Settings**
   - Nome `SHOPIFY_CLIENT_SECRET`, valore il Client secret copiato da **Settings**
   - `SHOPIFY_ADMIN_ACCESS_TOKEN` è facoltativo. Crearlo solo se usi un token permanente e non la coppia Client ID / Client secret. Lo script, se trova Client ID e Client secret, chiede il token da solo.
5. Scheda **Variables**, pulsante **New repository variable**. Non sono segreti: sono la finestra e i tag. Se non le crei, valgono i default del programma.
   - `START_DATE` = `2026-01-01T00:00:00Z`
   - `END_DATE` = `2027-12-31T23:59:59Z`
   - `EARLY_ACCESS_SOURCE_TAG` = `form-early-access`
   - `TARGET_TAG` = `early access targa`
   - opzionale `CUSTOMER_QUERY_EXTRA`, vuoto oppure un filtro come `country:IT`
   - opzionale `SHOPIFY_API_VERSION` = `2026-10`

`TARGET_TAG` va scritto con gli spazi, esattamente `early access targa`, se il segmento cerca quel testo.

### Prova manuale e log

1. Dopo il merge su `main`, apri la scheda **Actions** del repository.
2. A sinistra clicca **Tag early access giornaliero**.
3. A destra, **Run workflow**. Lascia il branch `main` e conferma **Run workflow**.
4. Compare una riga nuova in alto. Cliccala.
5. Apri il job, poi lo step **Aggiunge il tag ai clienti del modulo**.
6. Nel testo cerca `Trovati` e `Tag aggiunto`. Se l'accesso a Shopify è sbagliato, lo step diventa rosso e il messaggio dice se manca il token o se l'autenticazione è stata rifiutata.

Lo step precedente lancia i test. Se quelli falliscono, lo script non parte e nessuno viene taggato.

### Cambiare l'orario

Apri `.github/workflows/daily-early-access-tag.yml` e modifica solo la riga del cron:

```yaml
- cron: "0 6 * * *"
```

I cinque campi sono minuto, ora, giorno, mese, giorno della settimana, in UTC. `0 6 * * *` significa ogni giorno alle 06:00 UTC. Per le 07:00 UTC scrivi `0 7 * * *`. Salva, fai commit e merge su `main`: finché la modifica non è sul branch predefinito, GitHub continua a usare l'orario vecchio.

## Alternativa: cron su un Linux sempre acceso

Usa questa strada solo se hai già una macchina accesa tutta la notte. GitHub Actions non ha bisogno di quella macchina.

Sul server, nella cartella del progetto, crea il venv e il file `.env` come nella sezione «Esecuzione sul tuo computer». Poi `crontab -e` e aggiungi una riga con percorsi assoluti (qui è un esempio: metti i tuoi):

```cron
0 6 * * * cd /home/sami/genesis && /home/sami/genesis/.venv/bin/python -m early_access_tagger >> /home/sami/genesis/early-access-tagger.log 2>&1
```

Anche questo è alle 06:00 UTC, se il fuso del server è UTC. Il log cresce nel file `early-access-tagger.log`. Controllalo con `tail -n 50 /home/sami/genesis/early-access-tagger.log` e cerca le stesse frasi `Trovati` e `Tag aggiunto`.

## Versione API e limiti di richieste

La versione di default è **2026-10**, la stabile Admin di ottobre 2026. Si cambia con `SHOPIFY_API_VERSION` senza toccare il codice. Shopify pubblica una versione nuova ogni trimestre: quando 2026-10 si avvicina alla scadenza, aggiorna quel valore.

La data di creazione, in Admin GraphQL, si filtra con `customer_date` (è il campo `createdAt` del cliente, la stessa informazione di `created_at`). Lo script usa gli estremi inclusivi `>=` e `<=`, poi ricontrolla la data e il tag del modulo su ogni cliente prima di scrivere.

Shopify misura le chiamate a punti (leaky bucket), non a numero di richieste. Un piano Standard reintegra circa 100 punti al secondo: questo lavoro giornaliero sta largamente dentro il limite. Se arriva un HTTP 429, un errore GraphQL `THROTTLED`, un timeout o un errore 5xx temporaneo, lo script aspetta e riprova. La pausa parte da 1 secondo e raddoppia (2, 4, …), per al massimo 5 tentativi, e si allunga se Shopify indica un costo residuo basso o un header `Retry-After`. Ogni richiesta ha un timeout di 30 secondi. I clienti si leggono a pagine da 50, con il cursore, fino all'ultima pagina.

Un cliente che fallisce dopo i tentativi viene contato e l'esecuzione continua. Se invece non si riesce proprio a parlare con l'API (token, negozio, rete), l'esecuzione si interrompe e il codice di uscita è diverso da zero, così GitHub Actions segnala il job in rosso.
