# registra.py

C'era un tempo un tale che s'era messo in testa di poter cambiare il destino.
Per farlo però, doveva tra le altre cose, ottenere una laurea.
Per farlo però, doveva studiare le lezioni. 
Ma doveva anche lavorare, e quindi seguirle era complicato.
Alcuni professori di buon cuore però caricavano le lezioni sulla piattaforma d'ateneo.
Ma ne disabilitavano il download, e doversi connettere a un sito peraltro non leggerissimo ogni volta che si vuol rivedere qualcosa, e vivere nell'incertezza di quando quel qualcosa verrà rimosso, generava del disagio in quel tale.
Perciò è nato lesdv, l'ennesimo scaricatore di video.


Registra le lezioni accademiche pubblicate su SharePoint/Teams (Stream) che puoi
guardare ma non scaricare, partendo da un file di testo con i link.

Per ogni lezione apre il player in un Chrome nascosto su uno schermo virtuale,
nasconde tutta l'interfaccia tranne il video e registra immagine e audio in un
file `.mp4`. Mentre registra puoi usare il PC normalmente: non vedi finestre e
l'audio non esce dalle casse.

> La registrazione avviene in tempo reale: una lezione da 1 ora richiede 1 ora.

## Requisiti

Solo Linux.

- **uv**: <https://docs.astral.sh/uv/>
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- **Programmi di sistema**
  ```bash
  sudo apt install xvfb ffmpeg pulseaudio-utils
  ```
  (`pulseaudio-utils` fornisce `pactl`; funziona anche con PipeWire.)
- **Google Chrome**: serve il Chrome vero, perché il Chromium di Playwright non
  ha i codec H.264/AAC usati da SharePoint. In alternativa puoi indicare un
  Chromium con i codec tramite `--browser-path`.

Python e le librerie (`playwright`) non vanno installati a mano: al primo avvio
`uv` legge le dipendenze dichiarate in cima allo script e prepara da solo un
ambiente isolato (scaricando anche Python, se sul sistema non ce n'è uno ≥ 3.10).

## Uso rapido

1. Crea un file di testo con i link, ad esempio `links.txt`:
   ```
   # Analisi 1
   https://ateneo.sharepoint.com/sites/corso/_layouts/15/stream.aspx?id=...
   Lezione 02 - Limiti | https://ateneo.sharepoint.com/sites/corso/_layouts/15/stream.aspx?id=...
   ```
2. Lancia lo script indicando il file e la cartella di destinazione:
   ```bash
   ./registra.py links.txt ~/Lezioni/Analisi1/
   ```
3. Se serve autenticarsi si apre una finestra di Chrome: fai il login
   (anche con MFA) e, appena vedi il video, la finestra si chiude da sola e la
   registrazione parte.

Se lo script non è eseguibile, usa `chmod +x registra.py` oppure
`uv run registra.py links.txt ~/Lezioni/Analisi1/`.

## Il file dei link

- Una lezione per riga.
- Righe vuote e righe che iniziano con `#` vengono ignorate.
- Formato `link` → il nome del file viene ricavato dal link.
- Formato `Nome | link` → il file si chiamerà `Nome.mp4`.
- Se due lezioni finiscono con lo stesso nome, alla seconda viene aggiunto un
  numero, es. `Lezione (2).mp4`.

Vedi [`links.example.txt`](links.example.txt).

## Login

La sessione Microsoft viene salvata in un profilo di Chrome dedicato
(`~/.cache/sp-record-profile`), separato dal tuo Chrome normale.

- Al primo avvio, o quando la sessione scade, lo script se ne accorge da solo e
  apre una finestra visibile per il login.
- Hai 10 minuti per completarlo (`--login-timeout` per cambiarli).
- Per forzare un nuovo login senza registrare nulla:
  ```bash
  ./registra.py --login "<un link qualsiasi di una lezione>"
  ```
- Per ripartire da zero (es. cambiare account) cancella la cartella
  `~/.cache/sp-record-profile`.

## Interruzioni e ripresa

- Ogni lezione viene scritta prima come `Nome.part.mp4` e rinominata in
  `Nome.mp4` solo quando è completa.
- Le lezioni già presenti nella cartella vengono saltate: se interrompi
  (Ctrl+C) o qualcosa va storto, basta rilanciare lo stesso comando.
- I file `.part.mp4` rimasti sono registrazioni incomplete e si possono
  cancellare.
- Se un video non si carica, accanto viene salvato uno screenshot
  `Nome.errore.png` per capire cosa è apparso a schermo.
- Alla fine viene stampato un riepilogo delle lezioni riuscite e fallite.

## Opzioni

| Opzione | Default | Descrizione |
|---|---|---|
| `--size` | `1920x1080` | Risoluzione della registrazione |
| `--fps` | `25` | Fotogrammi al secondo |
| `--crf` | `23` | Qualità x264: più basso = migliore e file più grande (18–28 ragionevole) |
| `--preset` | `veryfast` | Preset x264: più lento = file più piccolo, più CPU |
| `--warmup` | `15` | Secondi di riproduzione iniziale per far salire la qualità dello stream |
| `--load-timeout` | `90` | Secondi di attesa perché il video compaia |
| `--login-timeout` | `600` | Secondi concessi per fare il login |
| `--overwrite` | — | Riregistra anche le lezioni già presenti |
| `--channel` | `chrome` | Browser da usare: `chrome`, `chrome-beta`, `msedge` |
| `--browser-path` | — | Percorso di un eseguibile Chromium con codec H.264 |
| `--login URL` | — | Apre solo il browser per il login |

Esempio con file più leggeri:

```bash
./registra.py links.txt lezioni/ --size 1280x720 --crf 26
```

## Problemi comuni

- **`Mancano: Xvfb, ffmpeg…`**: installa i programmi di sistema (vedi Requisiti).
- **Video nero o senza audio**: verifica di usare Google Chrome e non il
  Chromium di Playwright.
- **Si apre sempre la finestra di login anche dopo averlo fatto**: il link
  potrebbe non essere accessibile con il tuo account; guarda lo screenshot
  `.errore.png`.
- **"Riproduzione ferma da 2 minuti"**: problema di rete o del player; la
  lezione resta come `.part.mp4`, rilancia il comando per riprovarla.

## Nota

Usa lo script solo per contenuti a cui hai legittimamente accesso e nel
rispetto delle regole del tuo ateneo sulla registrazione delle lezioni.
