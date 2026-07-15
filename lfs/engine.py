#!/usr/bin/env python3
"""Linux File Search — motor de busca (nome + conteúdo).

Filosofia de compatibilidade (roda em QUALQUER distro):
  - Busca de CONTEÚDO: usa `ripgrep` (rg) se existir -> rapidíssimo, --json.
    Fallback: Python puro (re + leitura em blocos), mais lento mas universal.
  - Busca por NOME: usa `fd`/`fdfind` se existir. Fallback: os.walk + fnmatch/regex.
  - Nomes de binário mudam entre distros (fd vs fdfind) -> autodetecção.
  - Sem dependência dura de nada além da stdlib.

O motor NÃO depende de Qt. A GUI o consome via callbacks/geradores, num thread,
pra interface nunca travar (foi o defeito do menu do Cinnamon: busca síncrona).
"""
from __future__ import annotations
import os, re, fnmatch, shutil, subprocess, json, stat, time, tempfile
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional


# ---------------------------------------------------------------- detecção
# binários que o próprio app pode empacotar (ver F6) — procurados além do PATH
_APP_BIN = os.path.expanduser("~/.local/share/linux-file-search/bin")

def _which(*names):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
        cand = os.path.join(_APP_BIN, n)   # fallback: binário empacotado
        if os.access(cand, os.X_OK):
            return cand
    return None

RG = _which("rg")                    # ripgrep
FD = _which("fd", "fdfind")          # fd (Debian/Mint = fdfind)
RGA = _which("rga", "ripgrep-all")   # ripgrep-all: busca DENTRO de PDF/docx/epub/zip…

def engine_info():
    return {
        "ripgrep": RG or "(ausente — fallback Python)",
        "fd": FD or "(ausente — fallback Python)",
        "rga": RGA or "(ausente — sem modo documentos)",
    }


def user_mounts(lines=None):
    """Pontos de montagem 'de usuário' — discos externos/acervo: dispositivos
    reais (/dev/*) montados sob /media, /mnt ou /run/media. São os candidatos
    da busca MULTIDISCOS na GUI ("Discos ▾"). `lines` injetável p/ teste."""
    if lines is None:
        try:
            with open("/proc/mounts", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return []
    out = set()
    for line in lines:
        parts = line.split()
        if len(parts) < 2 or not parts[0].startswith("/dev/"):
            continue
        mp = parts[1].replace("\\040", " ")     # espaço vem escapado no mounts
        if mp.startswith(("/media/", "/mnt/", "/run/media/")):
            out.add(mp)
    return sorted(out)


# ---------------------------------------------------------------- utilidades
def _reap(proc, errf=None, stats=None):
    """Encerra o subprocesso SEM deixar órfão (B1) e conta 'inacessíveis' do
    stderr capturado (B8). Idempotente e à prova de exceção."""
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(1)
            except Exception:
                proc.kill()
                try: proc.wait(1)
                except Exception: pass
    except Exception:
        pass
    if errf is not None:
        if stats is not None:
            try:
                errf.seek(0)
                d = sum(1 for L in errf if "ermission denied" in L)
                stats["denied"] = stats.get("denied", 0) + d
            except Exception:
                pass
        try: errf.close()
        except Exception: pass


def parse_size(s):
    """'10M', '1.5G', '512K', '2TB'… -> bytes. None se vazio/ inválido.
    Canônico (antes duplicado em app.py e cli.py)."""
    if not s:
        return None
    s = str(s).strip().upper().replace(" ", "")
    if not s:
        return None
    mult = 1
    for suf, m in (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10),
                   ("T", 1 << 40), ("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10), ("B", 1)):
        if s.endswith(suf):
            s = s[:-len(suf)]; mult = m; break
    try:
        return int(float(s) * mult)
    except ValueError:
        return None


_GLOB_META = frozenset("*?[")

def as_name_glob(term: str) -> str:
    """Entrada crua do usuário no campo de NOME -> glob de basename, estilo
    Agent Ransack/Windows: texto puro significa 'contém' — "rotina" vira
    "*rotina*" e acha "exames de rotina.txt", qualquer extensão. Quem digita
    metacaracteres (* ? [) está pedindo glob literal e mantém o controle."""
    t = term.strip()
    if not t or _GLOB_META & set(t):
        return t
    return f"*{t}*"


# ---------------------------------------------------------------- parâmetros
@dataclass
class Query:
    paths: list[str]                       # onde procurar
    name_patterns: list[str] = field(default_factory=list)  # globs OU 1 regex
    name_is_regex: bool = False
    content: str = ""                      # texto/regex a conter (vazio = só nome)
    content_is_regex: bool = False
    documents: bool = False                # busca DENTRO de PDF/docx/epub/zip via rga (F4)
    case_sensitive: bool = False
    whole_word: bool = False
    recursive: bool = True
    max_depth: Optional[int] = None
    include_hidden: bool = False
    follow_symlinks: bool = False
    respect_gitignore: bool = False   # False = busca TUDO (estilo Agent Ransack)
    one_file_system: bool = False     # não cruzar mounts (útil c/ USB do acervo)
    min_size: Optional[int] = None         # bytes
    max_size: Optional[int] = None
    modified_after: Optional[float] = None # epoch
    modified_before: Optional[float] = None
    max_results: int = 100000


@dataclass
class Match:
    path: str
    size: int
    mtime: float
    is_dir: bool = False
    lines: list[tuple[int, str]] = field(default_factory=list)  # (lineno, texto)
    nmatch: int = 0


# ---------------------------------------------------------------- filtros comuns
def _name_matcher(q: Query):
    """Retorna função(basename)->bool conforme padrões de nome."""
    if not q.name_patterns:
        return lambda b: True
    if q.name_is_regex:
        flags = 0 if q.case_sensitive else re.IGNORECASE
        rx = re.compile(q.name_patterns[0], flags)
        return lambda b: rx.search(b) is not None
    # globs (lista). case-insensitive por padrão como o Agent Ransack
    pats = q.name_patterns
    if q.case_sensitive:
        return lambda b: any(fnmatch.fnmatchcase(b, p) for p in pats)
    lp = [p.lower() for p in pats]
    return lambda b: any(fnmatch.fnmatchcase(b.lower(), p) for p in lp)


def _passes_meta(q: Query, st: os.stat_result) -> bool:
    if q.min_size is not None and st.st_size < q.min_size:
        return False
    if q.max_size is not None and st.st_size > q.max_size:
        return False
    if q.modified_after is not None and st.st_mtime < q.modified_after:
        return False
    if q.modified_before is not None and st.st_mtime > q.modified_before:
        return False
    return True


# ---------------------------------------------------------------- busca por NOME
def _walk_onerror(stats):
    """N2: os.walk engolia erros silenciosamente; agora conta os inacessíveis."""
    def cb(err):
        if stats is not None and isinstance(err, PermissionError):
            stats["denied"] = stats.get("denied", 0) + 1
    return cb


def _iter_names_python(q: Query, stats=None):
    """Fallback universal: os.walk com profundidade/hidden/symlink/meta/one-fs.
    N2: `stats` recebe 'denied' de diretórios sem permissão (onerror do os.walk)."""
    match_name = _name_matcher(q)
    for root in q.paths:
        root = os.path.abspath(os.path.expanduser(root))
        base_depth = root.rstrip("/").count("/")
        root_dev = None
        if q.one_file_system:                       # B9: não cruzar mounts no fallback
            try: root_dev = os.stat(root).st_dev
            except OSError: root_dev = None
        for dp, dns, fns in os.walk(root, followlinks=q.follow_symlinks,
                                    onerror=_walk_onerror(stats)):
            depth = dp.rstrip("/").count("/") - base_depth
            if not q.include_hidden:
                dns[:] = [d for d in dns if not d.startswith(".")]
            if root_dev is not None:
                keep = []
                for d in dns:
                    try:
                        if os.stat(os.path.join(dp, d)).st_dev == root_dev:
                            keep.append(d)
                    except OSError:
                        pass
                dns[:] = keep
            # pastas também casam por nome (busca só-por-nome; dir não tem conteúdo).
            # Feito com a lista JÁ podada (ocultos/one-fs), antes do corte de recursão.
            for d in dns:
                if not match_name(d):
                    continue
                dpp = os.path.join(dp, d)
                try:
                    st = os.stat(dpp)
                except OSError:
                    continue
                if not _passes_meta(q, st):
                    continue
                yield Match(dpp, st.st_size, st.st_mtime, is_dir=True)
            if not q.recursive:
                dns[:] = []
            elif q.max_depth is not None and depth >= q.max_depth:
                dns[:] = []
            for f in fns:
                if not q.include_hidden and f.startswith("."):
                    continue
                if not match_name(f):
                    continue
                fp = os.path.join(dp, f)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                if not _passes_meta(q, st):
                    continue
                yield Match(fp, st.st_size, st.st_mtime)


_MERGE_GLOBS_MIN = 4                      # opt#3: >3 globs -> funde numa regex só

def _glob_to_regex(glob: str) -> str:
    """Converte um glob de basename numa regex ANCORADA (^...$), equivalente ao
    fnmatch. `*`->`.*`, `?`->`.`, `[...]` preservado (com `!`->`^`), resto literal."""
    out = ["^"]
    i, n = 0, len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            out.append(".*")
        elif c == "?":
            out.append(".")
        elif c == "[":
            j = i + 1
            if j < n and glob[j] in "!^":
                j += 1
            if j < n and glob[j] == "]":         # ']' logo no início é literal
                j += 1
            while j < n and glob[j] != "]":
                j += 1
            if j >= n:                           # '[' sem fechamento -> literal
                out.append(r"\[")
            else:
                inner = glob[i + 1:j]
                if inner.startswith("!"):
                    inner = "^" + inner[1:]
                out.append("[" + inner + "]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    out.append("$")
    return "".join(out)


def _merge_globs(pats) -> Optional[str]:
    """Opt#3: funde vários globs de basename numa única regex alternada, p/ rodar
    UM só fd em vez de um por padrão (menos varreduras = menos I/O, bom p/ SMR).
    Só funde globs simples (sem '/'); valida a regex antes. Devolve None p/ recusar."""
    if any("/" in p for p in pats):              # glob de caminho: fd casa a path toda
        return None
    merged = "(?:" + "|".join(_glob_to_regex(p) for p in pats) + ")"
    try:
        re.compile(merged)                       # sanidade (se falhar, cai no multi-fd)
    except re.error:
        return None
    return merged


def _iter_names_fd(q: Query, cancel, stats=None):
    """fd/fdfind quando disponível (rápido). Multi-glob: >3 padrões viram UMA regex
    alternada (opt#3, 1 só fd); até 3, um fd por padrão."""
    pats = q.name_patterns or ["."]
    use_glob = bool(q.name_patterns) and not q.name_is_regex
    # opt#3: muitos globs -> funde numa regex única (uma varredura só)
    if use_glob and len(pats) >= _MERGE_GLOBS_MIN:
        merged = _merge_globs(pats)
        if merged is not None:
            pats = [merged]
            use_glob = False                      # agora é regex, não glob
    seen = set() if len(pats) > 1 else None   # dedup só faz sentido com múltiplos padrões
    for pat in pats:
        # arquivos E pastas (e symlinks): busca só-por-nome acha "Argentina/" como
        # pasta e "argentina.txt" como arquivo — dir não tem conteúdo p/ filtrar.
        cmd = [FD, "--absolute-path", "--type", "f", "--type", "d", "--type", "l"]
        if not q.respect_gitignore:
            cmd.append("--no-ignore")
        if q.include_hidden:
            cmd.append("--hidden")
        if q.follow_symlinks:
            cmd.append("--follow")
        if q.one_file_system:
            cmd.append("--one-file-system")
        if not q.recursive:
            cmd += ["--max-depth", "1"]
        elif q.max_depth is not None:
            cmd += ["--max-depth", str(q.max_depth)]
        if use_glob:
            cmd += ["--glob"]
        if q.name_patterns and not q.case_sensitive:
            cmd.append("--ignore-case")
        elif q.name_patterns and q.case_sensitive:
            cmd.append("--case-sensitive")        # N1: fd usa smart-case; força sensível
        pat_val = pat if q.name_patterns else "."
        cmd += ["--", pat_val] + q.paths          # B10: '--' encerra as opções
        errf = tempfile.TemporaryFile(mode="w+")
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf,
                                    text=True, errors="replace")
        except OSError:
            errf.close()
            yield from _iter_names_python(q); return
        try:
            for line in proc.stdout:
                if cancel():
                    return
                fp = line.rstrip("\n")
                if not fp or (seen is not None and fp in seen):
                    continue
                if seen is not None:
                    seen.add(fp)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                if not _passes_meta(q, st):
                    continue
                yield Match(fp, st.st_size, st.st_mtime, is_dir=stat.S_ISDIR(st.st_mode))
        finally:
            _reap(proc, errf, stats)              # B1/B8: mata processo + conta inacessíveis


# ---------------------------------------------------------------- busca por CONTEÚDO
def _iter_content_rg(q: Query, cancel, stats=None):
    """ripgrep --json (ou rga p/ documentos): filtra por nome (glob) E casa conteúdo, streaming.

    Em modo documentos (q.documents + rga presente) o rga extrai texto de PDF/docx/epub/zip…
    e repassa ao rg no MESMO formato --json. Caminhos dentro de containers (ex zip) podem não
    ter stat no FS — nesse caso emitimos o Match sem metadados (size/mtime 0) p/ não perder o hit.
    """
    docs = bool(q.documents and RGA)
    binary = RGA if docs else RG
    cmd = [binary, "--json"]
    if not docs:                                   # --encoding é do rg; rga extrai já em UTF-8
        cmd += ["--encoding", "auto"]
    if not q.respect_gitignore:
        cmd.append("--no-ignore")
    if q.include_hidden:
        cmd.append("--hidden")
    if q.follow_symlinks:
        cmd.append("--follow")
    if q.one_file_system:
        cmd.append("--one-file-system")
    if not q.case_sensitive:
        cmd.append("--ignore-case")
    if not q.content_is_regex:
        cmd.append("--fixed-strings")
    if q.whole_word:
        cmd.append("--word-regexp")
    if not q.recursive:
        cmd += ["--max-depth", "1"]
    elif q.max_depth is not None:
        cmd += ["--max-depth", str(q.max_depth)]
    # filtro de nome via glob (rg aplica no arquivo)
    if q.name_patterns and not q.name_is_regex:
        if not q.case_sensitive:
            cmd.append("--glob-case-insensitive")   # B2: glob insensível como o fd/Agent Ransack
        for p in q.name_patterns:
            cmd += ["--glob", p]
    cmd += ["-e", q.content, "--"]
    cmd += q.paths

    name_rx = None
    if q.name_patterns and q.name_is_regex:
        name_rx = re.compile(q.name_patterns[0], 0 if q.case_sensitive else re.IGNORECASE)

    errf = tempfile.TemporaryFile(mode="w+")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf,
                                text=True, errors="replace")
    except OSError:
        errf.close()
        yield from _iter_content_python(q, cancel); return

    cur = None
    try:
        for line in proc.stdout:
            if cancel():
                break
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            t = ev.get("type")
            if t == "begin":
                path = ev["data"]["path"].get("text")
                if path is None or (name_rx and not name_rx.search(os.path.basename(path))):
                    cur = None
                    continue
                try:
                    st = os.stat(path)
                except OSError:
                    # arquivo dentro de container (ex algo.zip/interno.pdf): sem stat no FS
                    cur = Match(path, 0, 0) if docs else None
                    continue
                if not _passes_meta(q, st):
                    cur = None; continue
                cur = Match(path, st.st_size, st.st_mtime)
            elif t == "match" and cur is not None:
                ln = ev["data"].get("line_number")
                txt = ev["data"]["lines"].get("text", "")
                cur.nmatch += len(ev["data"].get("submatches", []))
                if len(cur.lines) < 200:
                    cur.lines.append((ln or 0, txt.rstrip("\n")))
            elif t == "end" and cur is not None:
                yield cur
                cur = None
    finally:
        _reap(proc, errf, stats)                    # B1/B8


def _iter_content_python(q: Query, cancel, stats=None):
    """Fallback: varre nomes e faz grep em Python (blocos, ignora binário).
    N2: conta 'denied' de diretórios (os.walk) e de arquivos sem permissão."""
    if q.case_sensitive:
        rx = re.compile(q.content if q.content_is_regex else re.escape(q.content))
    else:
        rx = re.compile(q.content if q.content_is_regex else re.escape(q.content), re.IGNORECASE)
    if q.whole_word and not q.content_is_regex:
        rx = re.compile(r"\b" + re.escape(q.content) + r"\b",
                        0 if q.case_sensitive else re.IGNORECASE)
    for m in _iter_names_python(q, stats):
        if cancel():
            return
        try:
            with open(m.path, "r", errors="ignore") as fh:
                hit = None
                for i, line in enumerate(fh, 1):
                    if "\x00" in line:      # provável binário
                        hit = None; break
                    if rx.search(line):
                        if hit is None:
                            hit = m
                        m.nmatch += 1
                        if len(m.lines) < 200:
                            m.lines.append((i, line.rstrip("\n")))
                if hit is not None:
                    yield m
        except PermissionError:
            if stats is not None:                     # N2: arquivo sem permissão de leitura
                stats["denied"] = stats.get("denied", 0) + 1
            continue
        except (OSError, UnicodeError):
            continue


# ---------------------------------------------------------------- API pública
def search(q: Query, on_result: Callable[[Match], None],
           cancel: Callable[[], bool] = lambda: False,
           on_progress: Callable[[int], None] = lambda n: None,
           stats: Optional[dict] = None):
    """Executa a busca chamando on_result(Match) em streaming.
    Retorna (total_encontrado, segundos). Se `stats` (dict) for passado, recebe
    contadores como stats['denied'] (arquivos inacessíveis vistos no stderr)."""
    t0 = time.time()
    n = 0
    if q.content:
        if RG or (q.documents and RGA):
            it = _iter_content_rg(q, cancel, stats)
        else:
            it = _iter_content_python(q, cancel, stats)
    else:
        it = _iter_names_fd(q, cancel, stats) if FD else _iter_names_python(q, stats)
    for m in it:
        if cancel():
            break
        on_result(m)
        n += 1
        if n % 25 == 0:
            on_progress(n)
        if n >= q.max_results:
            break
    return n, time.time() - t0


if __name__ == "__main__":
    # teste rápido de linha de comando
    import sys
    q = Query(paths=[sys.argv[1] if len(sys.argv) > 1 else "."],
              name_patterns=["*.py"], content=sys.argv[2] if len(sys.argv) > 2 else "")
    print("engine:", engine_info())
    tot, dt = search(q, lambda m: print(f"{m.size:>10} {m.path}"
                                        + (f"  [{m.nmatch} matches]" if m.nmatch else "")))
    print(f"\n{tot} resultados em {dt:.2f}s")
