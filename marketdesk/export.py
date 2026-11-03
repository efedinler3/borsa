"""Claude Code bridge CLI.

    py -m marketdesk.export [sabah|ogle|aksam|manual] [--zorla]   write the data pack and print the paths
    py -m marketdesk.export --import                              import Claude's output files as briefs
    py path\\to\\marketdesk\\export.py [...]                        same, from any working directory

With -m the working directory must be the project root (Python cannot find the package otherwise); running the
file directly works from anywhere. The printed SONRA commands are cwd-independent. Without a kind the current ET time
decides (sabah < 10:30, ogle until the close, else aksam; non-trading day: manual). An explicit kind that does not
match the clock prints a UYARI line; if it would also overwrite an existing CIKTI file the command refuses (HATA,
exit 3) unless --zorla is given.
Printed lines ("KEY: value", ASCII keys): TUR, ZAMAN, UYARI, GIRDI, LATEST, CIKTI, SONRA, SONRA_PS  /
ICE_AKTARILAN, YUKLU, YUKLENMEDI, DURUM, KLASOR. CIKTI is the exact file Claude must write; see CLAUDE_BRIEF.md.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

if not __package__:  # run as a file (py path\to\export.py): put the project root on sys.path, then import as a package
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "marketdesk"

from . import briefs, db
from .config import load_config
from .util import CLAUDE_IN_DIR, CLAUDE_OUT_DIR, ROOT_DIR, fmt_et, fmt_tr, make_id, now_ts

RECENT_OUTPUT_SECONDS = 2 * 3600  # --import reports on output files changed this recently


def _utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
            else:
                stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _wait_for_settle(min_age: float = 5.5, max_wait: float = 8.0) -> None:
    """import_claude_outputs skips files modified < 5 s ago; when Claude just wrote one, wait for it."""
    try:
        ages = [time.time() - p.stat().st_mtime for p in CLAUDE_OUT_DIR.glob("*.md")]
    except OSError:
        return
    young = [a for a in ages if 0 <= a < min_age]
    if young:
        time.sleep(min(max_wait, min_age - min(young)))


def _import_cmd() -> tuple[str, str]:
    """(Bash, PowerShell) commands for the import step that work from any working directory."""
    root = str(ROOT_DIR)
    return (f'cd "{root.replace(chr(92), "/")}" && py -m marketdesk.export --import',
            f'Set-Location "{root}"; py -m marketdesk.export --import')


def _report_outputs() -> None:
    """Per recent CIKTI file: YUKLU (in the app's DB, same or newer than the file) or YUKLENMEDI (+ reason).
    The running app imports this folder by itself every minute, so ICE_AKTARILAN: 0 alone is not a failure."""
    now = time.time()
    try:
        stored = {b["id"]: int(b.get("created_at") or 0) for b in db.list_briefs(limit=500, author="claude")}
    except Exception as e:  # noqa: BLE001 — report, do not crash the CLI
        print(f"DURUM: brifing listesi okunamadı ({e})")
        return
    shown = 0
    for path in sorted(CLAUDE_OUT_DIR.glob("*.md")):
        try:
            st = path.stat()
        except OSError:
            continue
        if now - st.st_mtime > RECENT_OUTPUT_SECONDS:
            continue
        shown += 1
        created = stored.get(make_id(path.name))
        if created is not None and created >= int(st.st_mtime):
            print(f"YUKLU: {path.name}")
            continue
        if st.st_size == 0:
            why = "dosya boş"
        elif now - st.st_mtime < 5:
            why = "dosya çok yeni, birkaç saniye sonra tekrar çalıştırın"
        else:
            try:
                _, body = briefs._split_title(path.read_text(encoding="utf-8-sig", errors="replace"))
            except OSError:
                body = ""
            why = "içerik çok kısa" if len(body) < 20 else "bilinmeyen neden, uygulama günlüğüne bakın"
        print(f"YUKLENMEDI: {path.name} ({why})")
    if not shown:
        print("DURUM: son 2 saatte değişen çıktı dosyası yok")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _utf8_stdout()
    if any(a in ("-h", "--help", "/?") for a in argv):
        print(__doc__.strip())
        return 0
    db.init_db()
    if "--import" in argv:
        _wait_for_settle()
        n = briefs.import_claude_outputs()
        print(f"ICE_AKTARILAN: {n}")
        _report_outputs()
        print(f"KLASOR: {CLAUDE_OUT_DIR}")
        return 0
    args = [a for a in argv if not a.startswith("-")]
    force = any(a in ("--zorla", "--force") for a in argv)
    now = now_ts()
    expected = briefs.default_kind(now)
    kind = briefs.normalize_kind(args[0]) if args else expected
    if not kind:
        print(f"HATA: bilinmeyen tür '{args[0]}' (sabah | ogle | aksam | manual)", file=sys.stderr)
        return 2
    _, out_path = briefs.claude_paths(kind, now)
    mismatch = bool(args) and kind != "manual" and kind != expected
    if mismatch and out_path.exists() and not force:
        # e.g. an 'aksam' task running on a Sunday/Monday TR night would overwrite Friday's evening brief.
        print(f"HATA: '{kind}' türü şu saate uymuyor (ET'ye göre beklenen: {expected}) ve {out_path.name} zaten "
              f"var; üzerine yazılmadı. Görev saatini kontrol edin (CLAUDE_BRIEF.md 'Zamanlama'); bilerek yeniden "
              f"yazmak için --zorla ekleyin.", file=sys.stderr)
        return 3
    in_path = briefs.export_for_claude(kind, load_config(), now)
    bash_cmd, ps_cmd = _import_cmd()
    print(f"TUR: {kind}")
    print(f"ZAMAN: {fmt_tr(now)} TR ({fmt_et(now, '%H:%M')} ET)")
    if mismatch:
        print(f"UYARI: tür saatle uyuşmuyor (ET'ye göre beklenen: {expected})")
    print(f"GIRDI: {in_path}")
    print(f"LATEST: {CLAUDE_IN_DIR / 'latest.md'}")
    print(f"CIKTI: {out_path}")
    print(f"SONRA: {bash_cmd}")
    print(f"SONRA_PS: {ps_cmd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
