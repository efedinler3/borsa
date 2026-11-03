"""Launcher: background scheduler + local Flask server + desktop window.

    py -m marketdesk.main [--no-window] [--port N] [--edge | --browser] [--debug]
    py -m marketdesk.main --kisayol | --kisayol-kaldir | --kapat

Window order of preference: pywebview (Edge WebView2) → Microsoft Edge in app mode → default browser.
One instance per data folder (OS file lock on DATA_DIR/instance.lock); a second launch only opens a window
onto the running instance, whose port it reads from DATA_DIR/instance.json.
"""
from __future__ import annotations

import argparse
import base64
import errno
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import requests

from . import __version__
from .util import DATA_DIR, ROOT_DIR, setup_logging

log = logging.getLogger("marketdesk.main")

TITLE = "Piyasa Masası"
HOST = "127.0.0.1"
INSTANCE_LOCK = DATA_DIR / "instance.lock"
INSTANCE_INFO = DATA_DIR / "instance.json"
PORT_TRIES = 6          # configured port, then the next few (stable window origin = kept UI preferences), then any
STARTUP_LNK = "Piyasa Masası (arka plan).lnk"
DESKTOP_LNK = "Piyasa Masası.lnk"

_lock_fd: int | None = None       # held open for the life of the process; the OS releases it even after a crash
_shutdown = threading.Event()     # set by POST /api/shutdown


def _utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:  # redirected to a file/pipe: keep Turkish text readable
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((HOST, port)) == 0


def _listen_socket(port: int) -> socket.socket:
    """Listening socket bound exclusively (werkzeug's own bind uses SO_REUSEADDR, which on Windows lets a
    second process bind the same port while the first is still listening)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # POSIX: only TIME_WAIT reuse
        s.bind((HOST, port))
        s.listen(128)
    except OSError:
        s.close()
        raise
    return s


def _bind_listen(port: int) -> tuple[socket.socket, int]:
    """Exclusive listening socket on `port`; if that is taken or not allowed (e.g. a Hyper-V reserved port),
    one of the next few ports, then any free port. Raises OSError when nothing can be bound."""
    last: OSError | None = None
    for p in [*range(port, min(port + PORT_TRIES, 65536)), 0]:
        if p and _port_in_use(p):
            last = last or OSError(f"port {p} başka bir program tarafından kullanılıyor")
            continue
        try:
            sock = _listen_socket(p)
        except OSError as e:
            last = e
            continue
        real = sock.getsockname()[1]
        if real != port:
            log.warning("port %d is not available (%s); using %d", port, last, real)
        return sock, real
    raise last or OSError("bağlanılacak port yok")


_local = requests.Session()
_local.trust_env = False  # never route 127.0.0.1 checks through a system proxy


def _is_our_app(port: int, timeout: float = 2.0, pid: int | None = None) -> bool:
    """True when our API answers on `port` (and, with `pid`, it is that process)."""
    try:
        r = _local.get(f"http://{HOST}:{port}/api/status", timeout=timeout)
        data = r.json() if r.ok else {}
        if data.get("app") != "piyasa_masasi":
            return False
        return pid is None or (data.get("instance") or {}).get("pid") == pid
    except (requests.RequestException, ValueError, AttributeError):
        return False


# ---------------------------------------------------------------- single instance

def _acquire_instance_lock() -> bool:
    """True when this process is the only instance for DATA_DIR (lock held until exit)."""
    global _lock_fd
    try:
        fd = os.open(str(INSTANCE_LOCK), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as e:
        log.warning("instance lock file could not be opened: %s", e)
        return True  # never refuse to start because of the lock file itself
    try:
        if sys.platform == "win32":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        if e.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK, getattr(errno, "EWOULDBLOCK", errno.EAGAIN)):
            return False  # held by another instance
        log.warning("instance lock failed (%s); starting without it", e)
        return True
    _lock_fd = fd
    return True


def _write_instance_info(port: int, headless: bool) -> None:
    data = {"app": "piyasa_masasi", "pid": os.getpid(), "port": port, "url": f"http://{HOST}:{port}/",
            "headless": headless, "started_at": int(time.time()), "version": __version__}
    try:
        tmp = INSTANCE_INFO.with_name(INSTANCE_INFO.name + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, INSTANCE_INFO)
    except OSError as e:
        log.warning("instance.json could not be written: %s", e)


def _read_instance_info() -> dict:
    try:
        data = json.loads(INSTANCE_INFO.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _clear_instance_info() -> None:
    if _read_instance_info().get("pid") == os.getpid():
        try:
            INSTANCE_INFO.unlink()
        except OSError:
            pass


def _find_running(cfg_port: int, timeout: float = 45.0) -> int | None:
    """Port of the instance that holds the lock (it may still be starting up), or None."""
    end = time.time() + timeout
    while True:
        info = _read_instance_info()
        try:
            port, pid = int(info["port"]), int(info["pid"])
        except (KeyError, TypeError, ValueError):
            port, pid = None, None
        if port and _is_our_app(port, timeout=1.5, pid=pid):
            return port
        if not port and _is_our_app(cfg_port, timeout=1.5):
            return cfg_port
        if time.time() >= end:
            return None
        time.sleep(0.5)


def request_shutdown() -> None:
    """POST /api/shutdown: end the main loop (and close this process's window) so main() stops everything."""
    _shutdown.set()
    webview = sys.modules.get("webview")
    if webview is not None:
        for w in list(getattr(webview, "windows", None) or []):
            try:
                w.destroy()
            except Exception:  # noqa: BLE001
                log.debug("pywebview destroy failed", exc_info=True)


def _fatal(msg: str) -> int:
    """Turkish error for the user: console (run.bat pauses on exit code 1) or a message box under pythonw."""
    log.error(msg)
    if sys.stderr is None and sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, TITLE, 0x10)
        except Exception:  # noqa: BLE001
            pass
    return 1


def _wait_ready(url: str, timeout: float = 30.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if _local.get(url + "/api/status", timeout=3).ok:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.25)
    return False


# ---------------------------------------------------------------- windows

def _webview2_ok() -> bool:
    """True when pywebview would use Edge WebView2 (not the legacy IE engine)."""
    if sys.platform != "win32":
        return True
    try:
        from webview.platforms import winforms  # loads pythonnet/.NET; cached for webview.start
        return bool(getattr(winforms, "is_chromium", False))
    except Exception as e:  # noqa: BLE001
        log.warning("pywebview/WebView2 unavailable: %s", e)
        return False


def open_pywebview(url: str) -> bool:
    """Blocks until the window is closed. False if pywebview could not show a window."""
    try:
        import webview
    except Exception as e:  # noqa: BLE001
        log.warning("pywebview import failed: %s", e)
        return False
    if not _webview2_ok():
        return False
    try:
        try:
            webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True  # target=_blank → system browser
        except Exception:  # noqa: BLE001 - older/newer pywebview without this key
            log.debug("OPEN_EXTERNAL_LINKS_IN_BROWSER not supported")
        win = webview.create_window(TITLE, url, width=1440, height=900, min_size=(1000, 650),
                                    background_color="#0d1117", text_select=True)
        storage = DATA_DIR / "webview"
        storage.mkdir(parents=True, exist_ok=True)
        gui = "edgechromium" if sys.platform == "win32" else None
        webview.start(gui=gui, private_mode=False, storage_path=str(storage))
        shown = getattr(getattr(win, "events", None), "shown", None)
        if shown is not None and not shown.is_set():
            log.warning("pywebview window was never shown")
            return False
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("pywebview failed: %s", e, exc_info=True)
        return False


def find_edge() -> str | None:
    cands = []
    for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            cands.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    cands += [Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
              Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")]
    for p in cands:
        if p.is_file():
            return str(p)
    return shutil.which("msedge")


def open_edge_app(url: str) -> bool:
    """Edge in app mode with its own profile (so the process lives as long as the window). Blocks."""
    exe = find_edge()
    if not exe:
        return False
    profile = DATA_DIR / "edge-profile"
    profile.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={profile}", "--window-size=1440,900",
                                 "--no-first-run", "--no-default-browser-check", "--disable-features=Translate"])
    except OSError as e:
        log.warning("Edge could not be started: %s", e)
        return False
    log.info("Edge app window opened (pid %s)", proc.pid)
    start = time.time()
    while proc.poll() is None:
        if _shutdown.wait(0.5):
            proc.terminate()
            return True
    if time.time() - start < 3 and not _shutdown.is_set():
        # Edge handed the window to an already running process for this profile; keep serving.
        log.info("Edge exited immediately; serving until Ctrl+C")
        _block()
    return True


def _block() -> None:
    """Wait for Ctrl+C or a shutdown request."""
    try:
        while not _shutdown.wait(1):
            pass
    except KeyboardInterrupt:
        pass


def open_window(url: str, mode: str = "auto") -> None:
    """mode: auto (pywebview → Edge → browser) | edge (Edge app → browser) | browser."""
    if mode == "auto":
        if open_pywebview(url):
            return
        log.info("falling back to Edge app mode")
    if mode in ("auto", "edge") and open_edge_app(url):
        return
    log.info("falling back to the default browser")
    webbrowser.open(url)
    print(f"{TITLE} tarayıcıda açıldı: {url}\nKapatmak için bu pencereyi kapatın veya Ctrl+C'ye basın.", flush=True)
    _block()


# ---------------------------------------------------------------- shortcuts (opt-in background mode)

def _pythonw() -> str:
    """Interpreter without a console window for the shortcuts."""
    exe = Path(sys.executable)
    cand = exe.with_name("pythonw.exe")
    if cand.is_file():
        return str(cand)
    return shutil.which("pyw") or shutil.which("pythonw") or str(exe)


def _shell_folder(csidl: int, create: bool = False) -> Path | None:
    """Windows shell folder (follows OneDrive/folder redirection): 0x07 Startup, 0x10 Desktop. None when it
    does not exist (and `create` is False) or cannot be resolved."""
    import ctypes
    buf = ctypes.create_unicode_buffer(1024)
    flags = 0x8000 if create else 0  # CSIDL_FLAG_CREATE
    if ctypes.windll.shell32.SHGetFolderPathW(None, csidl | flags, None, 0, buf) == 0 and buf.value:
        return Path(buf.value)
    return None


_PS_SHORTCUT = """
$ErrorActionPreference = 'Stop'
$l = (New-Object -ComObject WScript.Shell).CreateShortcut($env:MD_LNK)
$l.TargetPath = $env:MD_TARGET
$l.Arguments = $env:MD_ARGS
$l.WorkingDirectory = $env:MD_WD
$l.Description = $env:MD_DESC
if ($env:MD_ICON) { $l.IconLocation = $env:MD_ICON + ',0' }
$l.Save()
"""


ICON_PATH = ROOT_DIR / "web" / "app.ico"


def make_shortcut(lnk: Path, target: str, arguments: str, workdir: str, description: str) -> None:
    """Create/overwrite a .lnk through WScript.Shell (PowerShell; no extra Python package). Values travel in
    environment variables, so spaces and Turkish characters need no quoting."""
    env = dict(os.environ, MD_LNK=str(lnk), MD_TARGET=target, MD_ARGS=arguments, MD_WD=workdir, MD_DESC=description,
               MD_ICON=str(ICON_PATH) if ICON_PATH.is_file() else "")
    cmd = base64.b64encode(_PS_SHORTCUT.encode("utf-16-le")).decode("ascii")
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", cmd], env=env,
                       capture_output=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0 or not lnk.is_file():
        err = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise OSError(f"kısayol oluşturulamadı: {lnk} ({err[-1] if err else 'PowerShell hatası'})")


def _shortcut_paths(create: bool = False) -> tuple[Path | None, Path | None]:
    """(startup .lnk, desktop .lnk); with `create` missing folders are created (else None for a missing one)."""
    startup = _shell_folder(0x07, create)
    if startup is None and create:
        startup = (Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
                   / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup")
        startup.mkdir(parents=True, exist_ok=True)
    desktop = _shell_folder(0x10, create)
    return (startup / STARTUP_LNK if startup else None), (desktop / DESKTOP_LNK if desktop else None)


def install_shortcuts(startup_too: bool = True) -> int:
    """Desktop shortcut (opens the window, no console) and, unless `startup_too` is False, a Startup-folder shortcut
    that runs the app in the background at every Windows sign-in."""
    if sys.platform != "win32":
        print("Kısayollar yalnızca Windows'ta oluşturulabilir.")
        return 1
    try:
        startup, desktop = _shortcut_paths(create=True)
        if desktop is None or (startup_too and startup is None):
            raise OSError("Başlangıç veya Masaüstü klasörü bulunamadı")
        target, wd = _pythonw(), str(ROOT_DIR)
        make_shortcut(desktop, target, "-m marketdesk.main", wd, f"{TITLE} penceresini açar")
        if not startup_too:
            print(f"Masaüstü kısayolu oluşturuldu: {desktop}\nÇift tıklayınca pencere açılır (konsol penceresi açılmaz).",
                  flush=True)
            return 0
        make_shortcut(startup, target, "-m marketdesk.main --no-window", wd,
                      f"{TITLE}: Windows açılışında arka planda veri toplar (pencere açmaz)")
    except (OSError, subprocess.SubprocessError) as e:
        return _fatal(f"Kısayollar oluşturulamadı: {e}")
    print(f"Kısayollar oluşturuldu:\n  Başlangıç (Windows açılışında arka planda çalışır): {startup}\n"
          f"  Masaüstü (pencereyi açar): {desktop}\n"
          f"Arka plandaki uygulama bir sonraki oturum açılışında başlar. Masaüstü kısayolu çalışan uygulamaya "
          f"pencere açar; pencereyi kapatmak veri toplamayı durdurmaz.\n"
          f"Tamamen kapatmak için: Ayarlar'daki kapatma düğmesi ya da  py -m marketdesk.main --kapat\n"
          f"Kaldırmak için:  py -m marketdesk.main --kisayol-kaldir", flush=True)
    return 0


def remove_shortcuts() -> int:
    if sys.platform != "win32":
        return 0
    try:
        paths = _shortcut_paths()
    except OSError as e:
        return _fatal(f"Kısayol klasörleri bulunamadı: {e}")
    for lnk in paths:
        if lnk is None:
            continue
        try:
            lnk.unlink()
            print(f"Kaldırıldı: {lnk}", flush=True)
        except FileNotFoundError:
            print(f"Zaten yok: {lnk}", flush=True)
        except OSError as e:
            return _fatal(f"Kısayol kaldırılamadı: {lnk} ({e})")
    return 0


def stop_running(cfg_port: int) -> int:
    """--kapat: ask the running instance of this data folder to shut down."""
    port = _find_running(cfg_port, timeout=3)
    if port is None:
        print(f"{TITLE} çalışmıyor.", flush=True)
        return 0
    try:
        r = _local.post(f"http://{HOST}:{port}/api/shutdown", json={}, timeout=5)
        r.raise_for_status()
    except requests.RequestException as e:
        return _fatal(f"{TITLE} kapatılamadı ({e}). Görev Yöneticisi'nden 'pythonw' / 'python' işlemini sonlandırın.")
    print(f"{TITLE} kapatılıyor (port {port}).", flush=True)
    return 0


# ---------------------------------------------------------------- main

def _attach(port: int, no_window: bool, mode: str) -> int:
    """Another instance serves this data folder: only open a window onto it."""
    url = f"http://{HOST}:{port}/"
    log.info("%s is already running at %s", TITLE, url)
    if no_window:
        print(f"{TITLE} zaten çalışıyor: {url}", flush=True)
        return 0
    open_window(url, mode)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="marketdesk", description=f"{TITLE} {__version__}")
    ap.add_argument("--no-window", action="store_true", help="yalnızca sunucuyu çalıştır (Ctrl+C ile kapat)")
    ap.add_argument("--port", type=int, default=None, help="HTTP portu (varsayılan: ayarlardaki port)")
    ap.add_argument("--edge", action="store_true", help="pywebview yerine Microsoft Edge uygulama penceresi kullan")
    ap.add_argument("--browser", action="store_true", help="varsayılan tarayıcıda aç")
    ap.add_argument("--debug", action="store_true", help="ayrıntılı günlük")
    ap.add_argument("--kisayol", action="store_true",
                    help="masaüstü kısayolu + Windows açılışında arka planda çalıştıran başlangıç kısayolu oluştur")
    ap.add_argument("--kisayol-kaldir", action="store_true", help="bu iki kısayolu kaldır")
    ap.add_argument("--masaustu-kisayol", action="store_true",
                    help="yalnızca masaüstü kısayolu oluştur (Windows açılışında otomatik başlatma olmadan)")
    ap.add_argument("--kapat", action="store_true", help="çalışan (arka plandaki) uygulamayı kapat")
    args = ap.parse_args(argv)

    _utf8_console()
    level = logging.DEBUG if args.debug else logging.INFO
    if args.kisayol or args.kisayol_kaldir or args.masaustu_kisayol:
        setup_logging(level, file=False)
        if args.kisayol_kaldir:
            return remove_shortcuts()
        return install_shortcuts(startup_too=bool(args.kisayol))

    from .config import load_config
    cfg = load_config()
    port = int(args.port or cfg.get("port") or 8765)
    mode = "browser" if args.browser else "edge" if args.edge else "auto"

    owner = False if args.kapat else _acquire_instance_lock()
    # Only the instance that owns the data folder writes marketdesk.log (a window-only helper process holding
    # the file open would break log rotation on Windows).
    setup_logging(level, file=owner)
    for noisy in ("werkzeug", "urllib3", "pywebview", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING if noisy == "pywebview" else logging.ERROR)
    if args.kapat:
        return stop_running(port)

    if not owner:
        running = _find_running(port)
        if running is None:
            return _fatal(f"{TITLE} bu veri klasörü için zaten çalışıyor ama yanıt vermiyor ({DATA_DIR}). "
                          f"Birkaç saniye sonra yeniden deneyin; düzelmezse Görev Yöneticisi'nden eski "
                          f"'python' / 'pythonw' işlemini sonlandırın.")
        return _attach(running, args.no_window, mode)

    if _port_in_use(port) and _is_our_app(port):
        # An older version without the instance lock (or another data folder) already serves this port.
        return _attach(port, args.no_window, mode)

    try:
        sock, port = _bind_listen(port)
    except OSError as e:
        return _fatal(f"{TITLE} başlatılamadı: yerel sunucu için boş port bulunamadı ({e}). "
                      f"Ayarlar'daki portu (config.json \"port\") değiştirip yeniden deneyin.")
    _write_instance_info(port, args.no_window)

    from werkzeug.serving import make_server

    from . import db
    from .scheduler import Scheduler
    from .server import create_app

    try:
        db.init_db()
        sched = Scheduler()
        app = create_app(sched, shutdown=request_shutdown, headless=args.no_window)
        srv = make_server(HOST, port, app, threaded=True, fd=sock.fileno())
    except Exception as e:  # noqa: BLE001
        log.exception("startup failed")
        _clear_instance_info()
        return _fatal(f"{TITLE} başlatılamadı: {type(e).__name__}: {e}")
    finally:
        sock.close()  # werkzeug serves on its own duplicate of the socket
    sched.start()
    threading.Thread(target=srv.serve_forever, name="http", daemon=True).start()
    url = f"http://{HOST}:{port}"
    if not _wait_ready(url):
        log.error("server did not answer on %s", url)
    log.info("%s %s ready at %s (data: %s)", TITLE, __version__, url, DATA_DIR)

    try:
        if args.no_window:
            print(f"{TITLE} çalışıyor: {url}/  (kapatmak için Ctrl+C)", flush=True)
            _block()
        else:
            open_window(url + "/", mode)
    finally:
        log.info("shutting down")
        sched.stop()
        try:
            srv.shutdown()
        except Exception:  # noqa: BLE001
            pass
        _clear_instance_info()
    return 0


if __name__ == "__main__":
    code = main()
    logging.shutdown()
    # Don't wait for in-flight collector/Ollama threads (SQLite transactions roll back safely).
    os._exit(code)
