#!/usr/bin/env python3
"""Linux File Search — copy engine (F7b). NON-DESTRUCTIVE BY CONSTRUCTION.

The governing rule of F7: **LFS reads and exports. It never alters, moves or
removes what it found.** This module is where that rule is enforced, and it is
enforced by absence: there is no delete, no move, no rename, no chmod, no
truncate of any source. Sources are opened `"rb"` and nothing else, ever. A
function that does not exist cannot be called by mistake.

The one write is creating copies under a destination the user chose. Even there,
an existing destination file is never overwritten without an explicit decision
(see `on_conflict`).

No Qt: same philosophy as engine.py — streaming through callbacks, cancellable,
and fully testable without a display.

Beyond the original design, this module refuses to start a copy it cannot
finish. The destination is usually a pendrive, an external disk or a media
player, i.e. exFAT/FAT32/NTFS/MTP — no symlinks, no POSIX permissions, a 4 GiB
file limit on FAT32 and a restricted filename charset. `preflight()` checks all
of that up front, because failing on file 380 of 400 (or at byte 4294967296 of
an 8 GiB video) is not an acceptable way to learn the destination was FAT32.
"""
from __future__ import annotations
import errno, os, stat, time

try:                        # pacote (GUI) e flat (cli.py/testes)
    from . import disks
except ImportError:
    import disks

BLOCK = 1 << 22                      # 4 MiB: bom para vídeo, e o cancel responde rápido

# Motivos de pulo/erro — CHAVES estáveis. Este módulo não fala com o usuário;
# a GUI é que traduz (i18n mora na borda, como nos rótulos do boolean.on_phase).
SKIP_TOO_BIG = "too_big"             # não cabe no limite do FS de destino (FAT32)
SKIP_BAD_NAME = "bad_name"           # nome ilegal no destino e o usuário não adaptou
SKIP_SYMLINK = "symlink_unsupported"  # destino não tem symlink e o alvo sumiu
SKIP_CONFLICT = "conflict_skip"      # já existe e o usuário escolheu Pular
SKIP_CANCELLED = "cancelled"
SKIP_LOOP = "dest_inside_source"     # copiar uma pasta para dentro dela mesma


class CopyProgress:
    """Empurrado por callback, jamais acumulado (o acervo tem 35 mil vídeos)."""

    def __init__(self):
        self.current_path = ""
        self.done_bytes = self.total_bytes = 0
        self.done_files = self.total_files = 0
        self.speed_bps = 0.0


class Entry:
    """Uma entrada planejada: origem, caminho relativo no destino, tipo."""

    def __init__(self, src, rel, kind, size=0):
        self.src, self.rel, self.kind, self.size = src, rel, kind, size  # kind: dir|file|link


class Preflight:
    """O que a cópia vai encontrar — calculado ANTES de escrever um byte."""

    def __init__(self, dest_dir):
        self.dest_dir = dest_dir
        self.entries: list[Entry] = []
        self.total_files = 0
        self.total_bytes = 0
        self.free_bytes = 0
        self.caps = None
        self.mount_ok = True
        self.too_big: list[tuple] = []      # (src, size) — estouram o limite do FS
        self.bad_names: list[tuple] = []    # (src, motivo) — nome ilegal no destino
        self.links_degraded: list[str] = []  # symlinks que virarão cópia real
        self.links_broken: list[str] = []   # symlinks quebrados, impossíveis no destino
        self.errors: list[tuple] = []       # (src, erro) na varredura

    @property
    def fits(self) -> bool:
        # 2% de folga: metadados/cluster slack de FAT enchem mais que o soma-bytes
        return self.free_bytes >= self.total_bytes * 1.02

    @property
    def blocked(self) -> bool:
        """Impede começar? (montagem sumida ou destino só-leitura)"""
        return (not self.mount_ok) or bool(self.caps and self.caps.readonly)

    @property
    def has_warnings(self) -> bool:
        return bool(self.too_big or self.bad_names or self.links_degraded
                    or self.links_broken or not self.fits)


class CopyResult:
    def __init__(self):
        self.copied: list[str] = []         # destinos criados
        self.skipped: list[tuple] = []      # (src, motivo)
        self.failed: list[tuple] = []       # (src, mensagem de erro)
        self.bytes_copied = 0
        self.cancelled = False


# ------------------------------------------------------------------ planejamento
def _walk_entries(src: str, rel_base: str, out: list, errors: list, seen_dirs: set,
                  dest_abs: str):
    """Enumera `src` recursivamente. Guarda (st_dev, st_ino) contra ciclo de
    symlink de diretório, e nunca desce para dentro do PRÓPRIO destino (copiar
    uma pasta para dentro dela mesma seria recursão infinita)."""
    try:
        st = os.lstat(src)
    except OSError as e:
        errors.append((src, str(e)))
        return
    if stat.S_ISLNK(st.st_mode):
        out.append(Entry(src, rel_base, "link"))
        return
    if stat.S_ISDIR(st.st_mode):
        key = (st.st_dev, st.st_ino)
        if key in seen_dirs:                       # ciclo: já estivemos aqui
            return
        seen_dirs.add(key)
        out.append(Entry(src, rel_base, "dir"))
        try:
            with os.scandir(src) as it:
                kids = sorted(it, key=lambda e: e.name)
        except OSError as e:
            errors.append((src, str(e)))
            return
        for e in kids:
            child = e.path
            if os.path.abspath(child) == dest_abs:  # não copia o destino p/ dentro dele
                continue
            _walk_entries(child, os.path.join(rel_base, e.name), out, errors,
                          seen_dirs, dest_abs)
        return
    if stat.S_ISREG(st.st_mode):
        out.append(Entry(src, rel_base, "file", st.st_size))
    # socket/fifo/device: não são acervo, ficam de fora em silêncio


def preflight(sources, dest_dir) -> Preflight:
    """Varre a origem e confronta com o que o destino aceita. Nada é escrito."""
    dest_abs = os.path.abspath(dest_dir)
    pf = Preflight(dest_abs)
    pf.mount_ok = disks.mount_ok(dest_abs)
    pf.caps = disks.dest_caps(dest_abs)
    pf.free_bytes = disks.free_bytes(dest_abs)

    seen_dirs = set()
    for s in sources:
        s = os.path.abspath(s)
        if dest_abs == s or dest_abs.startswith(s + os.sep):
            pf.errors.append((s, SKIP_LOOP))
            continue
        _walk_entries(s, os.path.basename(s.rstrip(os.sep)) or s,
                      pf.entries, pf.errors, seen_dirs, dest_abs)

    caps = pf.caps
    for e in pf.entries:
        if e.kind == "file":
            pf.total_files += 1
            pf.total_bytes += e.size
            if caps.max_file is not None and e.size > caps.max_file:
                pf.too_big.append((e.src, e.size))
        elif e.kind == "link" and not caps.symlinks:
            # sem symlink no destino: vira cópia real do alvo, ou é impossível
            if os.path.exists(e.src):
                pf.links_degraded.append(e.src)
                try:
                    pf.total_bytes += os.stat(e.src).st_size
                    pf.total_files += 1
                except OSError:
                    pass
            else:
                pf.links_broken.append(e.src)
        # nome ilegal no destino? checa CADA componente do caminho relativo
        for part in e.rel.split(os.sep):
            why = caps.name_problem(part) if part else None
            if why:
                pf.bad_names.append((e.src, why))
                break
    return pf


# ------------------------------------------------------------------ cópia
def _unique(dst: str) -> str:
    """`nome (1).ext`, `nome (2).ext`… no estilo Nemo, pulando os que já existem."""
    d, base = os.path.split(dst)
    stem, ext = os.path.splitext(base)
    n = 1
    while True:
        cand = os.path.join(d, f"{stem} ({n}){ext}")
        if not os.path.exists(cand) and not os.path.islink(cand):
            return cand
        n += 1


def _copy_stream(src, dst, cancel, tick, prog):
    """Copia um arquivo em blocos. Cancelar no meio REMOVE o destino parcial —
    nunca deixar meio-vídeo no pendrive parecendo um arquivo bom."""
    done = 0
    try:
        with open(src, "rb") as fi, open(dst, "wb") as fo:
            while True:
                if cancel is not None and cancel.is_set():
                    fo.close()
                    _rm_partial(dst)
                    return None
                buf = fi.read(BLOCK)
                if not buf:
                    break
                fo.write(buf)
                done += len(buf)
                prog.done_bytes += len(buf)
                tick()
            fo.flush()
            # destino típico é USB: "cópia concluída" tem que significar "no disco",
            # não "no cache de página do kernel, some se arrancarem o pendrive"
            try:
                os.fsync(fo.fileno())
            except OSError:
                pass
    except OSError:
        _rm_partial(dst)
        raise
    return done


def _rm_partial(dst):
    """Remove o parcial que NÓS acabamos de criar no destino. É a única remoção
    do módulo, e só toca um caminho que este mesmo processo abriu para escrita —
    jamais a origem."""
    try:
        os.unlink(dst)
    except OSError:
        pass


def _apply_meta(src, dst, caps):
    """mtime/permissões, no que o destino suportar. exFAT/FAT não têm modo POSIX
    e o MTP não tem mtime confiável: tentar e falhar em silêncio é o certo aqui —
    não é erro de cópia, é limite do sistema de arquivos."""
    try:
        st = os.stat(src)
    except OSError:
        return
    if caps.times:
        try:
            os.utime(dst, (st.st_atime, st.st_mtime))
        except OSError:
            pass
    if caps.perms:
        try:
            os.chmod(dst, stat.S_IMODE(st.st_mode))
        except OSError:
            pass


def copy_to(sources, dest_dir, on_progress=None, on_conflict=None, cancel=None,
            sanitize_names=False, plan: Preflight | None = None) -> CopyResult:
    """Copia `sources` para `dest_dir`. A ORIGEM É ABERTA SÓ PARA LEITURA.

    on_progress(CopyProgress) — chamado no máximo ~10x/s.
    on_conflict(src, dst) -> "skip" | "rename" | "overwrite" | "cancel".
        Sem callback, o padrão é PULAR: nunca sobrescrever por omissão.
    cancel — threading.Event.
    sanitize_names — adapta nomes ilegais no destino (só se o usuário escolher).
    plan — Preflight já calculado (evita varrer o SMR duas vezes).
    """
    res = CopyResult()
    dest_abs = os.path.abspath(dest_dir)
    pf = plan if plan is not None else preflight(sources, dest_abs)
    caps = pf.caps

    if pf.blocked:
        for e in pf.entries:
            res.failed.append((e.src, "destination not writable"))
        return res

    prog = CopyProgress()
    prog.total_files, prog.total_bytes = pf.total_files, pf.total_bytes
    t0 = time.time()
    last = [0.0]

    def tick(force=False):
        if on_progress is None:
            return
        now = time.time()
        if force or now - last[0] > 0.1:
            dt = max(1e-6, now - t0)
            prog.speed_bps = prog.done_bytes / dt
            last[0] = now
            on_progress(prog)

    conflict_all = [None]                 # "aplicar a todos" desta operação

    def resolve_conflict(src, dst):
        if conflict_all[0]:
            return conflict_all[0]
        if on_conflict is None:
            return "skip"                 # padrão seguro: nunca sobrescreve sozinho
        ans = on_conflict(src, dst)
        if isinstance(ans, tuple):        # ("overwrite", True) = aplicar a todos
            ans, to_all = ans
            if to_all:
                conflict_all[0] = ans
        return ans or "skip"

    too_big = {p for p, _ in pf.too_big}
    bad = {p for p, _ in pf.bad_names}

    for e in pf.entries:
        if cancel is not None and cancel.is_set():
            res.cancelled = True
            break

        # nome ilegal no destino: adapta (se autorizado) ou pula com motivo claro
        rel = e.rel
        if sanitize_names:
            rel = os.sep.join(caps.sanitize(p) for p in rel.split(os.sep) if p)
        elif e.src in bad:
            res.skipped.append((e.src, SKIP_BAD_NAME))
            continue
        dst = os.path.join(dest_abs, rel)

        if e.kind == "dir":
            try:
                os.makedirs(dst, exist_ok=True)
            except OSError as ex:
                res.failed.append((e.src, str(ex)))
            continue

        if e.src in too_big:
            res.skipped.append((e.src, SKIP_TOO_BIG))
            prog.done_files += 1
            continue

        # conflito: só sobrescreve por escolha EXPLÍCITA do usuário
        if os.path.lexists(dst):
            ans = resolve_conflict(e.src, dst)
            if ans == "cancel":
                res.cancelled = True
                break
            if ans == "skip":
                res.skipped.append((e.src, SKIP_CONFLICT))
                prog.done_files += 1
                continue
            if ans == "rename":
                dst = _unique(dst)
            # "overwrite": segue; o open("wb") trunca o DESTINO (nunca a origem)

        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
        except OSError as ex:
            res.failed.append((e.src, str(ex)))
            continue

        prog.current_path = e.src
        try:
            if e.kind == "link":
                if caps.symlinks:
                    target = os.readlink(e.src)
                    if os.path.lexists(dst):
                        _rm_partial(dst)          # só o destino, decidido acima
                    os.symlink(target, dst)
                elif os.path.exists(e.src):
                    # destino sem symlink (exFAT/FAT/NTFS/MTP): copia o CONTEÚDO
                    n = _copy_stream(e.src, dst, cancel, tick, prog)
                    if n is None:
                        res.cancelled = True
                        break
                    res.bytes_copied += n
                    _apply_meta(e.src, dst, caps)
                else:
                    res.skipped.append((e.src, SKIP_SYMLINK))
                    continue
            else:
                n = _copy_stream(e.src, dst, cancel, tick, prog)
                if n is None:
                    res.cancelled = True
                    break
                res.bytes_copied += n
                _apply_meta(e.src, dst, caps)
            res.copied.append(dst)
        except OSError as ex:
            msg = str(ex)
            if ex.errno == errno.EFBIG:           # FS recusou o tamanho
                res.skipped.append((e.src, SKIP_TOO_BIG))
            elif ex.errno == errno.ENOSPC:
                res.failed.append((e.src, msg))
                res.cancelled = True              # encheu: parar já, não errar 300x
                break
            else:
                res.failed.append((e.src, msg))
        prog.done_files += 1
        tick()

    tick(force=True)
    return res
