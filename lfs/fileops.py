#!/usr/bin/env python3
# Sombrero File Search — Copyright (C) 2026 Rodrigo Toledo
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Este programa é software livre: você pode redistribuí-lo e/ou modificá-lo sob
# os termos da GNU General Public License, versão 3 ou posterior (ver LICENSE).
# Distribuído na esperança de ser útil, mas SEM QUALQUER GARANTIA.
"""Sombrero File Search — copy engine (F7b). NON-DESTRUCTIVE BY CONSTRUCTION.

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
import errno, os, shutil, stat, subprocess, time
from urllib.parse import quote

try:                        # pacote (GUI) e flat (cli.py/testes)
    from . import disks
except ImportError:
    import disks

BLOCK = 1 << 22                      # 4 MiB: bom para vídeo, e o cancel responde rápido

# Ritmo de escrita em dispositivo removível — o análogo, para pendrive, do que a
# serialização de varredura faz pelo SMR. Só que aqui o problema não é seek, é
# CACHE: escrevendo à toda, o kernel aceita os dados em memória a velocidade de
# RAM e vai drenando para o pendrive depois. As páginas sujas são um recurso
# GLOBAL — quando enchem, TODO processo que tentar escrever qualquer coisa
# bloqueia até o pendrive drenar. Foi assim que o desktop travou em 19/06 com o
# SMR, e no pendrive é pior: medido neste SanDisk Cruzer Fit em USB 2.0, a
# escrita é de 11,8 MB/s, então os 512 MiB de dirty desta máquina são 46 s de
# travamento — e num sistema com o padrão (20% da RAM) seriam minutos.
#
# A cada PACE bytes fazemos fdatasync + fadvise(DONTNEED) na faixa já escrita:
#   - a janela suja fica limitada a PACE, não ao tamanho do arquivo;
#   - o page cache não é envenenado com dados de uso único (o usuário não vai
#     reler o vídeo que acabou de copiar; mas ele PERDE o que estava em cache);
#   - o progresso passa a ser honesto: "90%" significa 90% no dispositivo, e não
#     90% na RAM — o que importa muito para quem vai arrancar o pendrive.
# 16 MiB: ~1,4 s de escrita no pior caso medido; grande o bastante para não
# perder vazão (medido: sem diferença) e pequeno o bastante para não travar nada.
PACE = 1 << 24

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
        self.write_probe = None             # WriteProbe: dá pra gravar aqui? (§1.1)
        self.strategy = ""                  # ATOMIC|GUARDED|GIO|BLOCKED (decidido no preflight)

    @property
    def fits(self) -> bool:
        # 2% de folga: metadados/cluster slack de FAT enchem mais que o soma-bytes
        return self.free_bytes >= self.total_bytes * 1.02

    @property
    def blocked(self) -> bool:
        """Impede começar? Montagem sumida, destino só-leitura, ou a sonda provou
        que não dá pra gravar por nenhuma rota (STRAT_BLOCKED) — decidido ANTES do
        primeiro byte, nunca por Errno cru no meio do lote."""
        return ((not self.mount_ok)
                or bool(self.caps and self.caps.readonly)
                or self.strategy == STRAT_BLOCKED)

    @property
    def has_warnings(self) -> bool:
        return bool(self.too_big or self.bad_names or self.links_degraded
                    or self.links_broken or not self.fits)


# ---- estratégias de escrita (decididas UMA vez no preflight, nunca por exceção)
STRAT_ATOMIC = "ATOMIC"       # .sombrero-part + fsync + os.replace (POSIX/FAT/NTFS/exFAT/sftp)
STRAT_GUARDED = "GUARDED"     # jmtpfs: part + fsync -> remove antigo -> rename simples
STRAT_GIO = "GIO"             # gvfs-MTP: `gio copy` por arquivo (a rota do Nemo)
STRAT_BLOCKED = "BLOCKED"     # sonda falhou e não há rota: barra no preflight

# Sufixo do temporário atômico. Órfão inequívoco se cair energia no meio.
PART_SUFFIX = ".sombrero-part"


class WriteProbe:
    """Resultado de CRIAR+ESCREVER+APAGAR um arquivo-sonda no destino. Metadado
    mente (o gvfs-MTP aceita statvfs e mkdir e recusa open('wb') com ENOTSUP): a
    única resposta honesta a 'dá pra gravar aqui?' é TENTAR. De brinde, pega
    diretório sem permissão, atributo imutável, inode esgotado, FUSE morto — tudo
    ANTES do lote, nunca um Errno cru no arquivo 40 de 400."""

    def __init__(self, ok, errno_=0, kind="", detail=""):
        self.ok = bool(ok)
        self.errno = errno_ or 0
        self.kind = kind            # ""|"notsup"|"perm"|"readonly"|"nospace"|"other"
        self.detail = detail


def _classify_errno(num) -> str:
    """errno da sonda -> chave estável (a GUI traduz; i18n mora na borda)."""
    if num in (errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)):
        return "notsup"             # gvfs-MTP: a ponte FUSE não abre p/ escrita
    if num in (errno.EACCES, errno.EPERM):
        return "perm"
    if num == errno.EROFS:
        return "readonly"
    if num in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)):
        return "nospace"
    return "other"


def _nearest_existing(path: str) -> str:
    """Ancestral existente mais próximo — o sistema de arquivos onde de fato se vai
    escrever quando o subdiretório de destino ainda não existe."""
    p = os.path.abspath(path)
    while p != "/" and not os.path.exists(p):
        p = os.path.dirname(p)
    return p


def probe_write(dest_dir, _opener=open) -> WriteProbe:
    """Grava ~16 bytes num arquivo-sonda no destino, fsync, apaga. O try/finally
    remove a sonda mesmo em falha parcial — nunca deixa lixo. `_opener` é injetável
    para os testes simularem ENOTSUP/EACCES/EROFS sem hardware. Escreve APENAS no
    diretório de destino (compatível com o §0 do F7 por construção)."""
    target = _nearest_existing(dest_dir)
    probe = os.path.join(target, ".sombrero-probe-%d-%s"
                         % (os.getpid(), os.urandom(4).hex()))
    try:
        f = _opener(probe, "wb")
        try:
            f.write(b"sombrero-probe\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except (OSError, AttributeError, ValueError):
                pass                # fsync inócuo em alguns FUSE; não é falha de escrita
        finally:
            f.close()
        return WriteProbe(True)
    except OSError as ex:
        return WriteProbe(False, ex.errno, _classify_errno(ex.errno), str(ex))
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass


_GIO_BIN = []                       # cache: [] não sondado, [path] ou [None] depois


def _has_gio() -> bool:
    """`gio` presente? (padrão do rg/fd: usa se existir). É o utilitário que grava
    no gvfs-MTP por dentro — a mesma rota que o Nemo usa para copiar pro celular."""
    if not _GIO_BIN:
        _GIO_BIN.append(shutil.which("gio"))
    return _GIO_BIN[0] is not None


def decide_strategy(caps, probe, has_gio: bool) -> str:
    """A máquina de decisão do §3.5, pura e testável (caps + sonda + gio injetados):
        sonda OK  e perfil COM replace   -> ATOMIC
        sonda OK  e perfil SEM replace    -> GUARDED  (jmtpfs/_MTP não-gvfs)
        sonda FALHOU e via_gvfs e tem gio -> GIO      (a rota Nemo por `gio copy`)
        senão                             -> BLOCKED  (barra ANTES do primeiro byte)
    """
    via_gvfs = bool(getattr(caps, "via_gvfs", False))
    mtp_like = bool(caps and caps.label == "MTP")
    if probe is not None and probe.ok:
        return STRAT_GUARDED if (mtp_like and not via_gvfs) else STRAT_ATOMIC
    if via_gvfs and has_gio:
        return STRAT_GIO
    return STRAT_BLOCKED


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

    # Sonda de escrita (§1.1) — SÓ se a montagem existe (não furar o guard de
    # mount_ok escrevendo a sonda no disco de sistema sob um mountpoint vazio) e o
    # FS não se anuncia só-leitura. Daí a estratégia de escrita é decidida aqui,
    # uma vez, por taxonomia + sonda (§3.5) — nunca por exceção no meio do lote.
    if pf.mount_ok and not (pf.caps and pf.caps.readonly):
        pf.write_probe = probe_write(dest_abs)
        pf.strategy = decide_strategy(pf.caps, pf.write_probe, _has_gio())

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


def _drain(fo, desde, ate):
    """Manda ao disco o que já foi escrito e tira do cache. Melhor esforço: em
    sistema de arquivos FUSE (ntfs-3g, MTP) o fadvise pode não fazer nada, e
    isso é aceitável — nunca é motivo para falhar uma cópia."""
    try:
        fo.flush()
        os.fdatasync(fo.fileno())
        if hasattr(os, "posix_fadvise"):
            os.posix_fadvise(fo.fileno(), desde, ate - desde, os.POSIX_FADV_DONTNEED)
    except OSError:
        pass


def _copy_stream(src, dst, cancel, tick, prog, pace=0):
    """Copia um arquivo em blocos. Cancelar no meio REMOVE o destino parcial —
    nunca deixar meio-vídeo no pendrive parecendo um arquivo bom.

    `pace` > 0 (destino removível): drena a cada `pace` bytes, para não sequestrar
    o cache de páginas sujas do sistema inteiro. Ver o comentário de PACE."""
    done = 0
    drenado = 0
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
                if pace and done - drenado >= pace:
                    _drain(fo, drenado, done)
                    # a origem também: um vídeo de 5 GiB lido uma única vez não
                    # pode expulsar do cache o que o usuário estava usando.
                    try:
                        if hasattr(os, "posix_fadvise"):
                            os.posix_fadvise(fi.fileno(), drenado, done - drenado,
                                             os.POSIX_FADV_DONTNEED)
                    except OSError:
                        pass
                    drenado = done
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


def _part_path(dst: str, caps) -> str:
    """dst + '.sombrero-part'. Encurta o RADICAL do nome se o temporário estourar o
    limite de bytes/caracteres do destino (ele tem que caber tanto quanto o arquivo
    final). Órfão inequívoco se cair a energia no meio da promoção."""
    d, base = os.path.split(dst)
    nmax = getattr(caps, "namemax", 255) or 255
    cmax = getattr(caps, "maxchars", None)

    def cabe(nome):
        if len(os.fsencode(nome)) > nmax:
            return False
        return not (cmax and len(nome) > cmax)

    nome = base + PART_SUFFIX
    while base and not cabe(nome):
        base = base[:-1]
        nome = base + PART_SUFFIX
    return os.path.join(d, nome or ("x" + PART_SUFFIX))


def _mtp_uri(dst_path: str, caps) -> str:
    """Caminho FUSE do gvfs -> URI que o `gio` entende:
    '/run/user/1000/gvfs/mtp:host=HOST/Storage/f.mp4' -> 'mtp://HOST/Storage/f.mp4'.
    O host já vem percent-encoded no nome do diretório gvfs; os componentes do
    caminho (nomes reais) são encodados por BYTE, como o path_to_uri da GUI —
    preserva nome não-UTF-8 sem descartar byte."""
    mp = getattr(caps, "mountpoint", "") or "/run/user/%d/gvfs" % os.getuid()
    parts = [p for p in os.path.relpath(os.path.abspath(dst_path), mp).split(os.sep)
             if p and p != "."]
    comp = parts[0] if parts else ""                 # 'mtp:host=HOST'
    scheme, _, spec = comp.partition(":")
    host = spec.split("host=", 1)[-1] if "host=" in spec else spec
    rest = "/".join(quote(os.fsencode(p), safe="") for p in parts[1:])
    return f"{scheme}://{host}/{rest}"


def _run_gio(argv, cancel):
    """Roda um `gio` cancelável. Devolve (returncode, stderr); returncode None =
    cancelado (subprocesso terminado). Isolado para os testes injetarem um fake."""
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    while True:
        try:
            proc.wait(timeout=0.1)
            break
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return (None, "cancelado")
    err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
    return (proc.returncode, err)


def _gio_copy(src, dst, caps, cancel, tick, prog, overwrite, _runner=None):
    """Estratégia GIO (§3.3): grava no gvfs-MTP por DENTRO (a mesma rota do Nemo),
    porque a ponte FUSE recusa open('wb'). Transferência por arquivo INTEIRO
    (semântica nativa do MTP); progresso com granularidade de arquivo. Cancelar
    termina o subprocesso e aborta o objeto parcial."""
    runner = _runner or _run_gio
    uri = _mtp_uri(dst, caps)
    if overwrite:                        # gio copy não sobrescreve sem flag: remove antes
        runner(["gio", "remove", "--", uri], None)
    rc, err = runner(["gio", "copy", "--", src, uri], cancel)
    if rc is None:                       # cancelado: aborta o parcial no aparelho
        runner(["gio", "remove", "--", uri], None)
        return None
    if rc != 0:
        raise OSError(errno.EIO, f"gio copy falhou ({rc}): {(err or '').strip()[:200]}")
    try:
        size = os.stat(src).st_size
    except OSError:
        size = 0
    prog.done_bytes += size              # granularidade por arquivo (bytes indisponíveis)
    tick()
    return size


def _write_file(src, dst, strategy, caps, cancel, tick, prog, pace, overwrite,
                _gio_runner=None):
    """Escreve UM arquivo pela estratégia decidida no preflight. Devolve bytes
    copiados, ou None se cancelado (destino parcial já removido). NUNCA toca a
    origem — o temporário e a promoção são todos no DESTINO."""
    if strategy == STRAT_GIO:
        return _gio_copy(src, dst, caps, cancel, tick, prog, overwrite, _gio_runner)
    # ATOMIC / GUARDED: fluxo em bloco para um temporário + promoção sobre o alvo.
    # O original do destino só some quando o novo está íntegro e no disco (fsync).
    part = _part_path(dst, caps)
    n = _copy_stream(src, part, cancel, tick, prog, pace)
    if n is None:
        return None                      # cancelado: _copy_stream já removeu o part
    try:
        if strategy == STRAT_GUARDED:    # jmtpfs: sem os.replace atômico
            if os.path.lexists(dst):
                os.unlink(dst)           # só DEPOIS do part pronto (janela mínima)
            os.rename(part, dst)         # rename simples, sem alvo (libmtp aceita)
        else:                            # ATOMIC
            os.replace(part, dst)        # troca atômica sobre o alvo
    except OSError as ex:
        # o conteúdo novo está íntegro no part; NÃO apagar — reportar os dois nomes
        raise OSError(ex.errno, f"{ex.strerror or ex}; conteúdo novo íntegro em "
                                f"{os.path.basename(part)}")
    return n


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

    # Removível: escreve em ritmo, para não sequestrar o cache do sistema (PACE).
    # Em disco interno o kernel já administra bem e o fsync a cada 16 MiB só
    # atrapalharia — a política existe para pendrive, não para NVMe.
    pace = PACE if getattr(caps, "removable", False) else 0

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
    strategy = pf.strategy or STRAT_ATOMIC     # decidida no preflight (§3.5)

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
        overwrite = False
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
            elif ans == "overwrite":
                overwrite = True          # a estratégia promove o novo por cima do velho

        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
        except OSError as ex:
            res.failed.append((e.src, str(ex)))
            continue

        prog.current_path = e.src
        try:
            if e.kind == "link" and caps.symlinks:
                target = os.readlink(e.src)
                if os.path.lexists(dst):
                    _rm_partial(dst)              # só o destino, decidido acima
                os.symlink(target, dst)
            elif e.kind == "link" and not os.path.exists(e.src):
                res.skipped.append((e.src, SKIP_SYMLINK))
                continue
            else:
                # arquivo, ou symlink degradado (destino sem symlink): copia CONTEÚDO
                # pela estratégia decidida (ATOMIC/GUARDED/GIO), nunca in-place.
                n = _write_file(e.src, dst, strategy, caps, cancel, tick, prog, pace,
                                overwrite)
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
