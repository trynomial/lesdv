#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "playwright",
# ]
# ///
"""
Registra in blocco le lezioni SharePoint/Teams elencate in un file di testo.

Per ogni link apre il player in un Chrome distinto su uno schermo virtuale (Xvfb),
nasconde tutta l'interfaccia tranne il <video> e registra schermo + audio del 
browser con ffmpeg. Se non trova sessione adatta apre una finestra di Chrome 
visibile sul link e aspetta che ti autentichi:
appena il video compare la finestra si chiude da sola e la registrazione riparte.

Con -j N registra N lezioni in parallelo. Si possono lanciare anche più istanze
insieme: condividono lo stesso token di auth.

Formato del file dei link (una lezione per riga):
    # le righe che iniziano con # e quelle vuote sono ignorate
    https://ateneo.sharepoint.com/...                  → nome preso dal link
    Lezione 01 - Introduzione | https://ateneo...      → nome arbitrario

Requisiti (Linux):
  sudo apt install xvfb ffmpeg pulseaudio-utils     # pactl; va bene anche con PipeWire
  Google Chrome installato (il Chromium di Playwright non ha i codec H.264/AAC),
  oppure --browser-path /usr/bin/chromium

Uso:
  ./registra.py links.txt lezioni/
  ./registra.py links.txt lezioni/ -j 2 --crf 20
  ./registra.py --login "<un link qualsiasi>"       # forza un nuovo login
"""
import argparse
import asyncio
import fcntl
import hashlib
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from playwright.sync_api import sync_playwright

STATE_DIR = Path.home() / ".cache" / "lesdv"
STATE = STATE_DIR / "sessione.json"       # cookie di login condivisi da tutte le istanze
LOGIN_LOCK = STATE_DIR / "login.lock"     # un solo login alla volta
LOCKS = STATE_DIR / "locks"               # una lezione è registrata da un solo worker

LOGIN_HOSTS = ("login.microsoftonline.com", "login.live.com", "login.microsoft.com",
               "account.microsoft.com", "adfs.", "sso.", "idp.", "shibboleth")

# Nasconde tutto tranne il <video>, che viene steso su tutta la finestra.
CLEAN_CSS = """
html, body { background:#000 !important; overflow:hidden !important; }
body * { visibility:hidden !important; transform:none !important; filter:none !important;
         will-change:auto !important; contain:none !important; animation:none !important; }
video  { visibility:visible !important; position:fixed !important; inset:0 !important;
         width:100vw !important; height:100vh !important; max-width:none !important;
         max-height:none !important; object-fit:contain !important; background:#000 !important;
         z-index:2147483647 !important; }
* { cursor:none !important; }
"""

VIDEO = "document.querySelector('video')"

stop = threading.Event()  # Ctrl+C: tutti i worker chiudono le registrazioni in corso

# Playwright avvia il suo driver nel nostro gruppo di processi: un Ctrl+C dal terminale
# lo ucciderebbe di colpo e la chiusura del browser resterebbe appesa per sempre.
# Lo mettiamo in una sessione a parte: il Ctrl+C arriva solo a noi e chiudiamo con ordine.
_create_subprocess_exec = asyncio.create_subprocess_exec


def _detached_subprocess_exec(*a, **kw):
    kw.setdefault("start_new_session", True)
    return _create_subprocess_exec(*a, **kw)


asyncio.create_subprocess_exec = _detached_subprocess_exec


def _interrupt(signum, frame):
    raise KeyboardInterrupt


# Chiusura del terminale o `kill`: stessa uscita pulita del Ctrl+C
signal.signal(signal.SIGTERM, _interrupt)
signal.signal(signal.SIGHUP, _interrupt)


def need(*tools):
    missing = [t for t in tools if not shutil.which(t)]
    if missing:
        sys.exit(f"[x] Mancano: {', '.join(missing)}  "
                 f"(sudo apt install xvfb ffmpeg pulseaudio-utils)")


# ------------------------------------------------------------------------ output

class Board:
    """Messaggi normali + una riga di stato in fondo con l'avanzamento di ogni worker."""

    def __init__(self):
        self.lock = threading.Lock()
        self.status = {}
        self.tty = sys.stdout.isatty()

    def log(self, msg):
        with self.lock:
            self._clear()
            print(msg)
            self._draw()

    def set(self, key, text):
        with self.lock:
            if text is None:
                self.status.pop(key, None)
            else:
                self.status[key] = text
            self._clear()
            self._draw()

    def _clear(self):
        if self.tty:
            sys.stdout.write("\r\033[K")

    def _draw(self):
        if self.tty and self.status:
            line = "  │  ".join(self.status[k] for k in sorted(self.status))
            sys.stdout.write(line[:shutil.get_terminal_size().columns - 1])
        sys.stdout.flush()


# ----------------------------------------------------------------------- link file

def read_links(path, outdir):
    """Restituisce [(url, file_di_output)] dal file di testo."""
    items, seen, videos = [], set(), {}
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, url = line.rpartition("|")
        url, name = url.strip(), name.strip() if sep else ""
        if not url.startswith("http"):
            print(f"[!] Riga {n} ignorata, non sembra un link: {line[:60]}")
            continue
        key = video_key(url)
        if key in videos:
            print(f"[!] Riga {n} ignorata: è la stessa lezione della riga {videos[key]}")
            continue
        videos[key] = n
        stem = safe_stem(name) if name else stem_from_url(url)
        stem = stem or f"lezione_{len(items) + 1:02d}"
        base, k = stem, 2
        while stem.lower() in seen:  # due link con lo stesso nome
            stem, k = f"{base} ({k})", k + 1
        seen.add(stem.lower())
        items.append((url, outdir / f"{stem}.mp4"))
    return items


def video_key(url):
    """Identifica il video, ignorando i parametri di tracciamento (es. referrerScenario)."""
    p = urlparse(url)
    qs = parse_qs(p.query)
    vid = unquote(qs["id"][0]) if "id" in qs else ""
    return p.netloc.lower(), unquote(p.path).rstrip("/"), vid


def safe_stem(s):
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", s).strip(" .")


def stem_from_url(url):
    qs = parse_qs(urlparse(url).query)
    raw = unquote(qs["id"][0]) if "id" in qs else unquote(urlparse(url).path)
    return safe_stem(Path(raw).stem)


# ------------------------------------------------------------- schermo e audio

class Screen:
    """Schermo Xvfb + uscita audio virtuale riservati a un singolo worker."""

    def __init__(self, size, tag):
        w, h = size.split("x")
        self.module = ""
        # -displayfd: Xvfb sceglie da solo un display libero, niente collisioni
        # tra worker e istanze diverse.
        r, wfd = os.pipe()
        self.xvfb = subprocess.Popen(
            ["Xvfb", "-displayfd", str(wfd), "-screen", "0", f"{w}x{h}x24", "-nolisten", "tcp"],
            pass_fds=(wfd,), stderr=subprocess.DEVNULL, start_new_session=True)
        os.close(wfd)
        with os.fdopen(r) as f:
            num = f.readline().strip()
        if not num:
            raise RuntimeError("Xvfb non è partito")
        self.display = f":{num}"
        self.sink = f"lesdv_{os.getpid()}_{tag}"
        res = subprocess.run(["pactl", "load-module", "module-null-sink",
                              f"sink_name={self.sink}",
                              f"sink_properties=device.description={self.sink}"],
                             capture_output=True, text=True)
        self.module = res.stdout.strip()
        if res.returncode or not self.module:
            self.close()
            raise RuntimeError(f"pactl non riesce a creare l'audio virtuale: {res.stderr.strip()}")

    def close(self):
        if self.module:
            subprocess.run(["pactl", "unload-module", self.module], capture_output=True)
            self.module = ""
        self.xvfb.terminate()


# ------------------------------------------------------------------------- browser

def launch_browser(p, args, screen=None):
    """Chrome senza profilo persistente; con `screen` gira invisibile su Xvfb."""
    env, extra = dict(os.environ), []
    if screen:
        env["DISPLAY"] = screen.display
        env["PULSE_SINK"] = screen.sink
        # Su Wayland Chrome ignora DISPLAY e si apre sul desktop vero:
        # togliamo Wayland dall'ambiente e forziamo X11 (→ Xvfb).
        env.pop("WAYLAND_DISPLAY", None)
        env["XDG_SESSION_TYPE"] = "x11"
        w, h = args.size.split("x")
        extra = ["--ozone-platform=x11", "--kiosk", "--window-position=0,0",
                 f"--window-size={w},{h}"]
    kw = dict(headless=False, env=env,
              ignore_default_args=["--mute-audio", "--enable-automation"],
              args=["--autoplay-policy=no-user-gesture-required",
                    "--disable-infobars", "--no-first-run", *extra])
    if args.browser_path:
        kw["executable_path"] = args.browser_path
    else:
        kw["channel"] = args.channel
    return p.chromium.launch(**kw)


def new_context(browser):
    """Contesto con l'ultima sessione salvata. Restituisce anche la sua data."""
    try:
        mtime = STATE.stat().st_mtime
        ctx = browser.new_context(storage_state=str(STATE), no_viewport=True)
    except FileNotFoundError:
        mtime = 0
        ctx = browser.new_context(no_viewport=True)
    return ctx, mtime


def save_state(ctx):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_name(f".sessione.{os.getpid()}.{threading.get_ident()}.tmp")
    ctx.storage_state(path=str(tmp))
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE)  # atomico: chi legge vede o la vecchia o la nuova


def on_login_page(page):
    host = urlparse(page.url).netloc.lower()
    return any(h in host for h in LOGIN_HOSTS)


def find_video_frame(page, timeout):
    """Frame che contiene il <video> pronto, "login" se siamo finiti sul login, o None."""
    t0 = time.time()
    while time.time() - t0 < timeout and not stop.is_set():
        if page.is_closed():
            return None
        for fr in page.frames:
            try:
                if fr.evaluate(f"() => !!{VIDEO} && {VIDEO}.readyState >= 1"):
                    return fr
            except Exception:
                pass
        if on_login_page(page) and time.time() - t0 > 5:
            return "login"
        time.sleep(1)
    return None


def login(p, url, args, seen_mtime, log):
    """Login condiviso tra worker e istanze: uno alla volta, e solo se serve davvero."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOGIN_LOCK, "w") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if stop.is_set():
                    return False
                time.sleep(1)
        if STATE.exists() and STATE.stat().st_mtime > seen_mtime:
            return True  # nel frattempo qualcun altro ha rifatto il login
        return interactive_login(p, url, args, log)


def interactive_login(p, url, args, log):
    """Apre Chrome visibile sul link e aspetta che il video compaia."""
    log("[🔑] Serve autenticarsi: si apre una finestra di Chrome.\n"
        "     Fai il login (anche MFA). Quando vedi il video la finestra si chiude da sola.")
    browser = launch_browser(p, args)
    try:
        ctx, _ = new_context(browser)
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded")
        t0 = time.time()
        while time.time() - t0 < args.login_timeout and not stop.is_set():
            if page.is_closed():
                log("[x] Finestra chiusa prima del login.")
                return False
            fr = find_video_frame(page, timeout=3)
            if fr and fr != "login":
                time.sleep(3)  # lascia finire i redirect che impostano i cookie
                save_state(ctx)
                log("[✓] Login riuscito.")
                return True
        log("[x] Tempo scaduto per il login.")
        return False
    except Exception as e:
        log(f"[x] Login non riuscito: {e}")
        return False
    finally:
        try:
            browser.close()
        except Exception:
            pass


# ---------------------------------------------------------------------- recording

def fullscreen(ctx, page):
    """Toglie barre e schede: il --kiosk vale solo per la prima finestra di Chrome."""
    cdp = ctx.new_cdp_session(page)
    win = cdp.send("Browser.getWindowForTarget")["windowId"]
    cdp.send("Browser.setWindowBounds", {"windowId": win, "bounds": {"windowState": "fullscreen"}})
    cdp.detach()
    time.sleep(1)


def record_one(ctx, url, out, args, screen, log, show):
    """Restituisce "ok", "auth" (serve login) o "error"."""
    page = ctx.new_page()
    fullscreen(ctx, page)
    page.goto(url, wait_until="domcontentloaded")
    fr = find_video_frame(page, timeout=args.load_timeout)
    if stop.is_set():
        return "error"
    if fr == "login":
        return "auth"
    if not fr:
        shot = out.with_suffix(".errore.png")
        page.screenshot(path=str(shot))
        log(f"[!] {out.name}: nessun video trovato (screenshot: {shot})")
        return "auth"  # spesso è comunque una questione di permessi/sessione

    fr.add_style_tag(content=CLEAN_CSS)
    fr.evaluate(f"() => {{ const v = {VIDEO}; v.pause(); v.currentTime = 0;"
                " v.muted = false; v.volume = 1; }")
    dur = fr.evaluate(f"() => {VIDEO}.duration") or 0
    log(f"[i] {out.name}: durata {dur / 60:.1f} min")

    # Lascia al player il tempo di salire alla qualità massima prima di partire
    fr.evaluate(f"() => {VIDEO}.play()")
    time.sleep(args.warmup)
    fr.evaluate(f"() => {{ const v = {VIDEO}; v.pause(); v.currentTime = 0; }}")
    time.sleep(2)

    # Si scrive su un .part: se il PC si spegne a metà non sembra una lezione finita
    part = out.with_name(out.stem + ".part.mp4")
    w, h = args.size.split("x")
    ff = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-thread_queue_size", "1024", "-f", "x11grab", "-draw_mouse", "0",
         "-framerate", str(args.fps), "-video_size", f"{w}x{h}", "-i", screen.display,
         "-thread_queue_size", "1024", "-f", "pulse", "-i", f"{screen.sink}.monitor",
         "-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf),
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", str(part)],
        stdin=subprocess.PIPE, start_new_session=True)  # Ctrl+C lo chiudiamo noi, pulito
    time.sleep(1)

    finished = False
    try:
        fr.evaluate(f"() => {VIDEO}.play()")
        last_t, stalled_since = -1.0, None
        while not stop.is_set():
            time.sleep(2)
            st = fr.evaluate(f"() => {{ const v = {VIDEO};"
                             " return {t: v.currentTime, ended: v.ended, paused: v.paused}; }")
            if st["ended"] or (dur and st["t"] >= dur - 0.3):
                finished = True
                break
            if st["paused"]:
                fr.evaluate(f"() => {VIDEO}.play()")
            if st["t"] <= last_t:
                stalled_since = stalled_since or time.time()
                if time.time() - stalled_since > 120:
                    log(f"[!] {out.name}: riproduzione ferma da 2 minuti, interrompo.")
                    break
            else:
                stalled_since = None
            last_t = st["t"]
            pct = 100 * st["t"] / dur if dur else 0
            show(f"{out.stem[:24]} {st['t'] / 60:.1f}/{dur / 60:.1f} min {pct:.0f}%")
    finally:
        time.sleep(1.5)
        ff.communicate(b"q")  # chiusura pulita del file mp4
    if not finished:
        log(f"[x] {out.name}: registrazione incompleta, lasciata in {part.name}")
        return "error"
    part.replace(out)
    out.with_suffix(".errore.png").unlink(missing_ok=True)
    log(f"[✓] Salvato {out}")
    return "ok"


def claim(out):
    """Lock sulla lezione: None se un altro worker/istanza la sta già registrando."""
    LOCKS.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(str(out.resolve()).encode()).hexdigest()[:16]
    f = open(LOCKS / f"{key}.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except BlockingIOError:
        f.close()
        return None


def process(p, browser, url, out, args, screen, log, show):
    lock = claim(out)
    if lock is None:
        log(f"[=] {out.name}: la sta già registrando un'altra istanza, salto")
        return "skip"
    try:
        if out.exists() and not args.overwrite:
            return "skip"  # finita nel frattempo da un'altra istanza
        log(f"[▶] {out.name}")
        result = "error"
        for attempt in range(2):
            ctx, seen = new_context(browser)
            try:
                result = record_one(ctx, url, out, args, screen, log, show)
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
            if result != "auth" or attempt == 1 or stop.is_set():
                break
            if not login(p, url, args, seen, log):
                break
        return result
    finally:
        lock.close()


def worker(n, todo, args, board, results, finished):
    try:
        _worker(n, todo, args, board, results)
    finally:
        finished.set()


def _worker(n, todo, args, board, results):
    tag = f"[{n}] " if args.jobs > 1 else ""

    def log(msg):
        board.log(tag + msg)

    def show(text):
        board.set(n, tag + text)

    screen = None
    try:
        screen = Screen(args.size, n)
        with sync_playwright() as p:
            browser = None
            while not stop.is_set():
                try:
                    url, out = todo.get_nowait()
                except queue.Empty:
                    break
                try:
                    if not browser or not browser.is_connected():
                        browser = launch_browser(p, args, screen)
                    results[out.name] = process(p, browser, url, out, args, screen, log, show)
                except Exception as e:
                    if not stop.is_set():
                        log(f"[x] {out.name}: errore imprevisto: {e}")
                    results[out.name] = "error"
                finally:
                    board.set(n, None)
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        log(f"[x] Il worker {n} si è fermato: {e}")
    finally:
        board.set(n, None)
        if screen:
            screen.close()


def run(args):
    need("Xvfb", "ffmpeg", "pactl")
    outdir = Path(args.dest).expanduser()
    outdir.mkdir(parents=True, exist_ok=True)
    items = read_links(args.links, outdir)
    if not items:
        sys.exit(f"[x] Nessun link trovato in {args.links}")

    pending = [(u, o) for u, o in items if args.overwrite or not o.exists()]
    jobs = max(1, min(args.jobs, len(pending)))
    print(f"[i] {len(items)} lezioni nel file, {len(items) - len(pending)} già registrate, "
          f"{len(pending)} da fare → {outdir}"
          + (f"  ({jobs} in parallelo)" if jobs > 1 else ""))
    if not pending:
        return

    todo = queue.Queue()
    for item in pending:
        todo.put(item)
    board, results = Board(), {}
    # Ogni worker segnala da sé quando ha finito di fare pulizia: con i greenlet di
    # Playwright Thread.is_alive() può dire "finito" quando il thread lavora ancora.
    finished = []
    try:
        for n in range(1, jobs + 1):
            finished.append(threading.Event())
            threading.Thread(target=worker, name=f"worker-{n}",
                             args=(n, todo, args, board, results, finished[-1])).start()
            time.sleep(1)  # partenze scaglionate: meno picchi di CPU e di richieste
        while not all(f.wait(0.5) for f in finished):
            pass
    except KeyboardInterrupt:
        stop.set()
        board.log("\n[!] Interrotto: chiudo le registrazioni in corso…")
        while not all(f.is_set() for f in finished):
            try:
                time.sleep(0.5)
            except KeyboardInterrupt:
                pass

    done = [k for k, v in results.items() if v == "ok"]
    failed = [k for k, v in results.items() if v in ("error", "auth")]
    print(f"\n[i] Registrate: {len(done)}   Fallite: {len(failed)}")
    for name in failed:
        print(f"    ✗ {name}")
    if failed or stop.is_set():
        print("    Rilancia lo stesso comando: le lezioni già salvate vengono saltate.")


def main():
    ap = argparse.ArgumentParser(
        description="Registra le lezioni SharePoint/Teams elencate in un file di testo.")
    ap.add_argument("links", nargs="?", help="file di testo con un link per riga")
    ap.add_argument("dest", nargs="?", help="cartella di destinazione")
    ap.add_argument("-j", "--jobs", type=int, default=1,
                    help="registrazioni in parallelo (default 1)")
    ap.add_argument("--login", metavar="URL", help="apre solo il browser per fare il login")
    ap.add_argument("--channel", default="chrome", help="chrome | chrome-beta | msedge")
    ap.add_argument("--browser-path", help="eseguibile Chromium con codec H.264")
    ap.add_argument("--size", default="1920x1080", help="risoluzione (default 1920x1080)")
    ap.add_argument("--fps", type=int, default=25)
    ap.add_argument("--crf", type=int, default=23, help="qualità x264, più basso = meglio")
    ap.add_argument("--preset", default="veryfast")
    ap.add_argument("--warmup", type=int, default=15,
                    help="secondi di riproduzione iniziale per far salire la qualità")
    ap.add_argument("--load-timeout", type=int, default=90,
                    help="secondi di attesa perché il video compaia")
    ap.add_argument("--login-timeout", type=int, default=600,
                    help="secondi concessi per fare il login")
    ap.add_argument("--overwrite", action="store_true", help="riregistra anche se esiste già")
    args = ap.parse_args()

    if args.jobs < 1:
        ap.error("--jobs deve essere almeno 1")
    if args.login:
        with sync_playwright() as p:
            ok = interactive_login(p, args.login, args, print)
        sys.exit(0 if ok else 1)
    if not args.links or not args.dest:
        ap.error("servono il file dei link e la cartella di destinazione")
    if not Path(args.links).is_file():
        ap.error(f"file non trovato: {args.links}")
    run(args)


if __name__ == "__main__":
    main()
