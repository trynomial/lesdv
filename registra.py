#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "playwright",
# ]
# ///
"""
Registra in blocco le lezioni SharePoint/Teams elencate in un file di testo.

Per ogni link apre il player in un Chrome nascosto su uno schermo virtuale (Xvfb),
nasconde tutta l'interfaccia tranne il <video> e registra schermo + audio del solo
browser con ffmpeg. Se la sessione Microsoft è scaduta (o non hai mai fatto il
login) apre una finestra di Chrome visibile sul link e aspetta che ti autentichi:
appena il video compare la finestra si chiude da sola e la registrazione riparte.

Formato del file dei link (una lezione per riga):
    # le righe che iniziano con # e quelle vuote sono ignorate
    https://ateneo.sharepoint.com/...                  → nome preso dal link
    Lezione 01 - Introduzione | https://ateneo...      → nome scelto da te

Requisiti (Linux):
  sudo apt install xvfb ffmpeg pulseaudio-utils     # pactl; va bene anche con PipeWire
  Google Chrome installato (il Chromium di Playwright non ha i codec H.264/AAC),
  oppure --browser-path /usr/bin/chromium

Uso:
  ./registra.py links.txt lezioni/
  ./registra.py links.txt lezioni/ --crf 20 --size 1280x720
  ./registra.py --login "<un link qualsiasi>"       # forza un nuovo login
"""
import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from playwright.sync_api import sync_playwright

PROFILE = Path.home() / ".cache" / "sp-record-profile"
SINK = "sprec"
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


def need(*tools):
    missing = [t for t in tools if not shutil.which(t)]
    if missing:
        sys.exit(f"[x] Mancano: {', '.join(missing)}  "
                 f"(sudo apt install xvfb ffmpeg pulseaudio-utils)")


# ----------------------------------------------------------------------- link file

def read_links(path, outdir):
    """Restituisce [(url, file_di_output)] dal file di testo."""
    items, seen = [], set()
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, url = line.rpartition("|")
        url, name = url.strip(), name.strip() if sep else ""
        if not url.startswith("http"):
            print(f"[!] Riga {n} ignorata, non sembra un link: {line[:60]}")
            continue
        stem = safe_stem(name) if name else stem_from_url(url)
        stem = stem or f"lezione_{len(items) + 1:02d}"
        base, k = stem, 2
        while stem.lower() in seen:  # due link con lo stesso nome
            stem, k = f"{base} ({k})", k + 1
        seen.add(stem.lower())
        items.append((url, outdir / f"{stem}.mp4"))
    return items


def safe_stem(s):
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", s).strip(" .")


def stem_from_url(url):
    qs = parse_qs(urlparse(url).query)
    raw = unquote(qs["id"][0]) if "id" in qs else unquote(urlparse(url).path)
    return safe_stem(Path(raw).stem)


# ------------------------------------------------------------------------- browser

def launch(p, args, display=None):
    """Chrome col profilo persistente; con `display` gira invisibile su Xvfb."""
    env, extra = dict(os.environ), []
    if display:
        env["DISPLAY"] = display
        env["PULSE_SINK"] = SINK
        # Su Wayland Chrome ignora DISPLAY e si apre sul desktop vero:
        # togliamo Wayland dall'ambiente e forziamo X11 (→ Xvfb).
        env.pop("WAYLAND_DISPLAY", None)
        env["XDG_SESSION_TYPE"] = "x11"
        w, h = args.size.split("x")
        extra = ["--ozone-platform=x11", "--kiosk", "--window-position=0,0",
                 f"--window-size={w},{h}"]
    kw = dict(user_data_dir=str(PROFILE), headless=False, no_viewport=True, env=env,
              ignore_default_args=["--mute-audio", "--enable-automation"],
              args=["--autoplay-policy=no-user-gesture-required",
                    "--disable-infobars", "--no-first-run", *extra])
    if args.browser_path:
        kw["executable_path"] = args.browser_path
    else:
        kw["channel"] = args.channel
    PROFILE.mkdir(parents=True, exist_ok=True)
    return p.chromium.launch_persistent_context(**kw)


def on_login_page(page):
    host = urlparse(page.url).netloc.lower()
    return any(h in host for h in LOGIN_HOSTS)


def find_video_frame(page, timeout):
    """Frame che contiene il <video> pronto, "login" se siamo finiti sul login, o None."""
    t0 = time.time()
    while time.time() - t0 < timeout:
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


def interactive_login(p, url, args):
    """Apre Chrome visibile sul link e aspetta che il video compaia."""
    print("\n[🔑] Serve autenticarsi: si apre una finestra di Chrome.")
    print("     Fai il login (anche MFA). Quando vedi il video la finestra si chiude da sola.")
    ctx = launch(p, args)
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url, wait_until="domcontentloaded")
        t0 = time.time()
        while time.time() - t0 < args.login_timeout:
            if page.is_closed():
                print("[x] Finestra chiusa prima del login.")
                return False
            fr = find_video_frame(page, timeout=3)
            if fr and fr != "login":
                time.sleep(3)  # lascia a Chrome il tempo di salvare i cookie
                print("[✓] Login riuscito.")
                return True
        print("[x] Tempo scaduto per il login.")
        return False
    finally:
        try:
            ctx.close()
        except Exception:
            pass


# ---------------------------------------------------------------------- recording

def record_one(ctx, url, out, args):
    """Restituisce "ok", "auth" (serve login) o "error"."""
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded")
        fr = find_video_frame(page, timeout=args.load_timeout)
        if fr == "login":
            return "auth"
        if not fr:
            shot = out.with_suffix(".errore.png")
            page.screenshot(path=str(shot))
            print(f"[!] Nessun video trovato (screenshot: {shot})")
            return "auth"  # spesso è comunque una questione di permessi/sessione

        fr.add_style_tag(content=CLEAN_CSS)
        fr.evaluate(f"() => {{ const v = {VIDEO}; v.pause(); v.currentTime = 0;"
                    " v.muted = false; v.volume = 1; }")
        dur = fr.evaluate(f"() => {VIDEO}.duration") or 0
        print(f"[i] Durata: {dur / 60:.1f} min")

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
             "-framerate", str(args.fps), "-video_size", f"{w}x{h}", "-i", args.display,
             "-thread_queue_size", "1024", "-f", "pulse", "-i", f"{SINK}.monitor",
             "-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf),
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", str(part)],
            stdin=subprocess.PIPE)
        time.sleep(1)
        fr.evaluate(f"() => {VIDEO}.play()")

        finished, last_t, stalled_since = False, -1.0, None
        try:
            while True:
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
                        print("\n[!] Riproduzione ferma da 2 minuti, interrompo.")
                        break
                else:
                    stalled_since = None
                last_t = st["t"]
                pct = 100 * st["t"] / dur if dur else 0
                print(f"\r[●] {st['t'] / 60:6.1f} / {dur / 60:.1f} min  ({pct:3.0f}%)",
                      end="", flush=True)
        finally:
            time.sleep(1.5)
            ff.communicate(b"q")  # chiusura pulita del file mp4
            print()
        if not finished:
            print(f"[x] Registrazione incompleta, lasciata in {part}")
            return "error"
        part.replace(out)
        out.with_suffix(".errore.png").unlink(missing_ok=True)
        print(f"[✓] Salvato {out}")
        return "ok"
    finally:
        if not page.is_closed():
            page.close()


def free_display():
    for n in range(99, 200):
        if not Path(f"/tmp/.X{n}-lock").exists():
            return f":{n}"
    sys.exit("[x] Nessun display libero per Xvfb")


def run(args):
    need("Xvfb", "ffmpeg", "pactl")
    outdir = Path(args.dest).expanduser()
    outdir.mkdir(parents=True, exist_ok=True)
    items = read_links(args.links, outdir)
    if not items:
        sys.exit(f"[x] Nessun link trovato in {args.links}")

    todo = [(u, o) for u, o in items if args.overwrite or not o.exists()]
    print(f"[i] {len(items)} lezioni nel file, {len(items) - len(todo)} già registrate, "
          f"{len(todo)} da fare → {outdir}")
    if not todo:
        return

    args.display = free_display()
    w, h = args.size.split("x")
    xvfb = subprocess.Popen(["Xvfb", args.display, "-screen", "0", f"{w}x{h}x24",
                             "-nolisten", "tcp"], stderr=subprocess.DEVNULL)
    mod = subprocess.run(["pactl", "load-module", "module-null-sink", f"sink_name={SINK}",
                          f"sink_properties=device.description={SINK}"],
                         capture_output=True, text=True).stdout.strip()
    time.sleep(1)
    done, failed = [], []
    try:
        with sync_playwright() as p:
            ctx = None
            for i, (url, out) in enumerate(todo, 1):
                print(f"\n=== [{i}/{len(todo)}] {out.name}")
                result = "error"
                for attempt in range(2):
                    ctx = ctx or launch(p, args, display=args.display)
                    result = record_one(ctx, url, out, args)
                    if result != "auth" or attempt == 1:
                        break
                    # Il profilo è uno solo: chiudo il Chrome nascosto per aprirne uno visibile
                    ctx.close()
                    ctx = None
                    if not interactive_login(p, url, args):
                        break
                (done if result == "ok" else failed).append(out.name)
            if ctx:
                ctx.close()
    except KeyboardInterrupt:
        print("\n[!] Interrotto dall'utente.")
    finally:
        if mod:
            subprocess.run(["pactl", "unload-module", mod])
        xvfb.send_signal(signal.SIGTERM)

    print(f"\n[i] Registrate: {len(done)}   Fallite: {len(failed)}")
    for name in failed:
        print(f"    ✗ {name}")
    if failed:
        print("    Rilancia lo stesso comando: le lezioni già salvate vengono saltate.")


def main():
    ap = argparse.ArgumentParser(
        description="Registra le lezioni SharePoint/Teams elencate in un file di testo.")
    ap.add_argument("links", nargs="?", help="file di testo con un link per riga")
    ap.add_argument("dest", nargs="?", help="cartella di destinazione")
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

    if args.login:
        with sync_playwright() as p:
            sys.exit(0 if interactive_login(p, args.login, args) else 1)
    if not args.links or not args.dest:
        ap.error("servono il file dei link e la cartella di destinazione")
    if not Path(args.links).is_file():
        ap.error(f"file non trovato: {args.links}")
    run(args)


if __name__ == "__main__":
    main()
