#!/usr/bin/env python3
"""Linux File Search — freedesktop integration (F7): mime type + "Open with".

No Qt, no new dependency: `mimetypes` from the stdlib, `.desktop` files parsed by
hand, and the external `xdg-mime`/`gio` tools used only when they exist (same
policy as rg/fd in engine.py — use if present, degrade gracefully if not).

Read-only by design. Nothing here writes, moves or deletes anything: it answers
"what is this file" and "which programs can open it", and launches one of them.
"""
from __future__ import annotations
import configparser, mimetypes, os, shlex, shutil, subprocess

_GIO = shutil.which("gio")
_XDG_MIME = shutil.which("xdg-mime")


def _data_dirs():
    """XDG_DATA_HOME + XDG_DATA_DIRS, na ordem de precedência freedesktop."""
    home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    return [home] + [d for d in dirs.split(":") if d]


def mime_for(path: str) -> str:
    """Tipo MIME do arquivo. `mimetypes` acerta pela extensão na esmagadora
    maioria; `xdg-mime` (que olha o conteúdo) resolve o resto quando existe."""
    if os.path.isdir(path):
        return "inode/directory"
    guess = mimetypes.guess_type(path)[0]
    if guess:
        return guess
    if _XDG_MIME:
        try:
            out = subprocess.run([_XDG_MIME, "query", "filetype", path],
                                 capture_output=True, timeout=4)
            got = out.stdout.decode("utf-8", "replace").strip()
            if got:
                return got
        except (OSError, subprocess.SubprocessError):
            pass
    return "application/octet-stream"


class DesktopApp:
    """Uma entrada .desktop lançável."""

    def __init__(self, desktop_id, path, name, exec_line, terminal=False):
        self.desktop_id = desktop_id      # "org.gnome.Nautilus.desktop"
        self.path = path                  # caminho absoluto do .desktop
        self.name = name
        self.exec_line = exec_line
        self.terminal = terminal

    def __repr__(self):
        return "DesktopApp(%r)" % self.desktop_id


def _parse_desktop(path: str):
    cp = configparser.RawConfigParser(interpolation=None, strict=False)
    cp.optionxform = str                  # chaves do .desktop são case-sensitive
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            cp.read_file(f)
        s = cp["Desktop Entry"]
    except (OSError, KeyError, configparser.Error):
        return None
    if s.get("Type", "Application") != "Application":
        return None
    if s.get("NoDisplay", "false").lower() == "true":
        return None
    if s.get("Hidden", "false").lower() == "true":
        return None
    ex = s.get("Exec", "").strip()
    if not ex:
        return None
    # binário inexistente = entrada órfã (pacote removido sem limpar o .desktop)
    try:
        argv0 = shlex.split(ex)[0]
    except ValueError:
        return None
    if not shutil.which(argv0) and not os.access(argv0, os.X_OK):
        return None
    return DesktopApp(os.path.basename(path), path,
                      s.get("Name", os.path.basename(path)), ex,
                      s.get("Terminal", "false").lower() == "true")


def _iter_desktop_files():
    """desktop_id -> caminho, com a precedência do XDG (o primeiro vence)."""
    seen = {}
    for base in _data_dirs():
        appdir = os.path.join(base, "applications")
        for root, dirs, files in os.walk(appdir):
            for fn in files:
                if not fn.endswith(".desktop"):
                    continue
                rel = os.path.relpath(os.path.join(root, fn), appdir).replace(os.sep, "-")
                seen.setdefault(rel, os.path.join(root, fn))
    return seen


def _mimeapps_order(mime: str):
    """IDs de .desktop associados a `mime` nos mimeapps.list/defaults.list, na
    ordem: Default primeiro, depois Added, depois o cache do sistema."""
    home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    cands = [os.path.join(home, "mimeapps.list")]
    for base in _data_dirs():
        cands.append(os.path.join(base, "applications", "mimeapps.list"))
        cands.append(os.path.join(base, "applications", "defaults.list"))
        cands.append(os.path.join(base, "applications", "mimeinfo.cache"))
    default, added = [], []
    for p in cands:
        cp = configparser.RawConfigParser(interpolation=None, strict=False)
        cp.optionxform = str
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                cp.read_file(f)
        except (OSError, configparser.Error):
            continue
        for sec, sink in (("Default Applications", default),
                          ("Added Associations", added),
                          ("MIME Cache", added)):
            if cp.has_section(sec) and cp.has_option(sec, mime):
                for did in cp.get(sec, mime).split(";"):
                    did = did.strip()
                    if did and did not in sink:
                        sink.append(did)
    return default + [d for d in added if d not in default]


def apps_for(path: str, limit: int = 12):
    """Aplicativos que declaram saber abrir este arquivo, o padrão primeiro.
    Lista vazia é resultado legítimo (sistema mínimo) — a GUI cai no "Outro…"."""
    mime = mime_for(path)
    by_id = _iter_desktop_files()
    out, seen = [], set()
    for did in _mimeapps_order(mime):
        p = by_id.get(did)
        if not p or did in seen:
            continue
        app = _parse_desktop(p)
        if app and not app.terminal:      # app de terminal sem terminal garantido: fora
            out.append(app); seen.add(did)
        if len(out) >= limit:
            return out
    # complemento: varre MimeType= dos .desktop (mimeapps.list pode estar vazio)
    if len(out) < limit:
        for did, p in sorted(by_id.items()):
            if did in seen:
                continue
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    head = f.read(8192)
            except OSError:
                continue
            if mime not in head:
                continue
            app = _parse_desktop(p)
            if app and not app.terminal:
                out.append(app); seen.add(did)
            if len(out) >= limit:
                break
    return out


def expand_exec(exec_line: str, paths):
    """Exec= do .desktop -> argv. Expande %f/%F/%u/%U com os caminhos e descarta
    os campos que não se aplicam (%i %c %k %d %n %v %m). %% é um '%' literal."""
    try:
        argv = shlex.split(exec_line)
    except ValueError:
        argv = exec_line.split()
    uris = ["file://" + p for p in paths]
    out, consumed = [], False
    for tok in argv:
        if tok in ("%f", "%u"):
            if paths:
                out.append(paths[0] if tok == "%f" else uris[0])
            consumed = True
        elif tok in ("%F", "%U"):
            out.extend(paths if tok == "%F" else uris)
            consumed = True
        elif tok in ("%i", "%c", "%k", "%d", "%D", "%n", "%N", "%v", "%m"):
            continue
        else:
            out.append(tok.replace("%%", "%"))
    if not consumed:                      # sem campo de arquivo: anexa no fim
        out.extend(paths)
    return out


def launch(app: DesktopApp, paths) -> bool:
    """Abre `paths` com `app`. `gio launch` quando existe (respeita o ambiente do
    desktop); senão expande o Exec= à mão. start_new_session: o filho sobrevive
    ao fechamento do LFS — quem abriu um vídeo não o perde ao fechar a busca."""
    paths = [os.path.abspath(p) for p in paths]
    if _GIO:
        argv = [_GIO, "launch", app.path] + paths
    else:
        argv = expand_exec(app.exec_line, paths)
    if not argv:
        return False
    try:
        subprocess.Popen(argv, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def launch_command(cmdline: str, paths) -> bool:
    """"Outro…": o usuário digitou um comando (ex.: `mpv --loop`). Mesmas regras
    de expansão; sem %f/%F, os caminhos vão no fim."""
    argv = expand_exec(cmdline, [os.path.abspath(p) for p in paths])
    if not argv:
        return False
    try:
        subprocess.Popen(argv, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False
