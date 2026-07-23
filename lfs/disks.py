#!/usr/bin/env python3
# Sombrero File Search — Copyright (C) 2026 Rodrigo Toledo
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Este programa é software livre: você pode redistribuí-lo e/ou modificá-lo sob
# os termos da GNU General Public License, versão 3 ou posterior (ver LICENSE).
# Distribuído na esperança de ser útil, mas SEM QUALQUER GARANTIA.
"""Sombrero File Search — disk topology (F7).

Extracted from boolean.py so that both the search (which SERIALIZES scans on
rotational disks) and the copy engine (which must know what the DESTINATION
filesystem can actually hold) share one source of truth. No Qt here, and no
dependency beyond the stdlib — this module is imported by the headless tests.

Three jobs:
  1. Which device/filesystem backs a path (`/proc/mounts`, longest prefix).
  2. Should scans on it be serialized (SMR/USB seek thrash) — `path_needs_serial`.
  3. **What the destination filesystem supports** — `dest_caps`. The copy engine
     targets pendrives, external disks and media players, which are very often
     exFAT/FAT32/NTFS/MTP: no symlinks, no POSIX permissions, a 4 GiB file limit
     on FAT32 and a restricted filename charset. Copying an 8 GiB video to FAT32
     fails at byte 4294967296, not at the start, so we check BEFORE writing.
"""
from __future__ import annotations
import os, re, errno, select, threading, warnings, shutil
from dataclasses import dataclass

try:                        # pacote (GUI) e flat (cli.py/testes)
    from . import engine
except ImportError:
    import engine

# R2 (achado Fable, revisão 23/07): no Python 3.12+ o os.fork() da sonda de mount
# (mount_status) emite DeprecationWarning "process is multi-threaded, use of fork()
# may lead to deadlocks" — a GUI tem QThreads sempre vivas, então seria um aviso por
# sonda no stderr. O uso AQUI é dos seguros: o filho só faz syscalls (stat/close/
# write) + os._exit, NUNCA toca um lock herdado do pai (não há alloc, logging, nem
# import no caminho do filho). Silenciamos ESTE aviso específico. Um filtro global
# instalado uma vez é thread-safe; warnings.catch_warnings() em volta do fork NÃO é
# (mexe em estado global) — justo o que não se quer num processo multi-thread.
warnings.filterwarnings("ignore", category=DeprecationWarning,
                        message=r".*multi-threaded.*fork.*")


# ------------------------------------------------------------------ topologia
# Pontos de montagem onde discos SMR/USB do acervo costumam viver: seek concorrente
# os castiga, então buscas AQUI são SERIALIZADAS (1 processo por vez).
_MNT_PREFIXES = ("/mnt", "/media", "/run/media")


def _under_mount(ap: str) -> bool:
    return any(ap == pre or ap.startswith(pre + os.sep) for pre in _MNT_PREFIXES)


def _read_mounts(src="/proc/mounts"):
    """/proc/mounts como lista de (dev, mountpoint, fstype). `src` pode ser um
    caminho OU um iterável de linhas — é o que torna testável o casamento de
    caminho com montagens que NÃO têm nó em /dev/ (MTP, gvfs, sshfs)."""
    linhas = open(src, encoding="utf-8").readlines() if isinstance(src, str) else list(src)
    out = []
    for line in linhas:
        parts = line.split()
        if len(parts) < 3:
            continue
        # espaço no ponto de montagem vem escapado como \040 no /proc/mounts
        out.append((parts[0], parts[1].replace("\\040", " "), parts[2]))
    return out


def _mount_entry(ap: str, mounts=None):
    """(dev, mountpoint, fstype) do mount de prefixo MAIS LONGO que cobre `ap`.
    ("", "", "") se não achar.

    NÃO exige source em /dev/: um telefone via MTP/gvfs aparece como
    `gvfsd-fuse /run/user/1000/gvfs fuse.gvfsd-fuse`, sem nó de bloco. Filtrar por
    /dev/ fazia o casamento subir até `/` e classificar o celular como o disco de
    sistema ext4 — o pior erro possível para uma cópia (sem aviso, sem ritmo). O
    dev fica "" nesses casos, e is_removable/rotational lidam bem com isso."""
    best = ("", "", "")
    try:
        entradas = mounts if mounts is not None else _read_mounts()
    except OSError:
        return ("", "", "")
    for dev, mp, fstype in entradas:
        if ap == mp or mp == "/" or ap.startswith(mp.rstrip("/") + "/"):
            if len(mp) >= len(best[1]):               # prefixo mais específico vence
                best = (dev, mp, fstype)
    return best


def _dev_for_path(ap: str) -> str:
    """Nó de dispositivo (/dev/...) que sustenta `ap`. "" se não achar (então
    tratamos como desconhecido)."""
    return _mount_entry(ap)[0]


def _udev_unescape(name: str) -> str:
    r"""Nomes em /dev/disk/by-label vêm com escape udev: espaço = \x20, etc."""
    return re.sub(r"\\x([0-9a-fA-F]{2})",
                  lambda m: chr(int(m.group(1), 16)), name)


def volume_label(mp: str, mounts=None):
    """Rótulo amigável do volume montado em `mp` (ex.: 'DiscoL'), para a UI
    preferir o nome ao mountpoint cru. Fontes, em ordem:
      1. /dev/disk/by-label/<label> cujo alvo é o MESMO dev de `mp`;
      2. basename de `mp` sob /media ou /run/media (o auto-mount costuma nomear
         a pasta pelo próprio label).
    Devolve None quando nada é melhor que o mountpoint (ex.: /mnt/dados à mão).
    Só faz syscalls locais (/dev, string) — nunca toca o conteúdo da montagem."""
    dev = _mount_entry(mp, mounts)[0]
    if dev.startswith("/dev/"):
        real_dev = os.path.realpath(dev)
        bylabel = "/dev/disk/by-label"
        try:
            for name in os.listdir(bylabel):
                if os.path.realpath(os.path.join(bylabel, name)) == real_dev:
                    return _udev_unescape(name)
        except OSError:
            pass
    for prefix in ("/media/", "/run/media/"):
        if mp.startswith(prefix):
            base = os.path.basename(mp.rstrip("/"))
            if base:
                return base
    return None


def _rotational(dev: str):
    """'1'/'0' de /sys/block/<disco>/queue/rotational p/ o disco que sustenta o nó
    `dev` (sobe da partição p/ o disco inteiro). None se desconhecido."""
    if not dev:
        return None
    name = os.path.basename(dev)                       # sdb1, nvme0n1p1...
    try:
        real = os.path.realpath("/sys/class/block/" + name)
        parent = os.path.basename(os.path.dirname(real))
        base = name if parent == "block" else parent   # disco inteiro se for partição
        with open("/sys/block/%s/queue/rotational" % base, encoding="ascii") as f:
            return f.read().strip()
    except OSError:
        return None


def path_needs_serial(ap: str) -> bool:
    """Serializa se o caminho está sob /mnt (etc.) E o disco que o sustenta é
    rotacional ou desconhecido. SSD/NVMe confirmado (rotational=0) libera o
    paralelismo mesmo sob /mnt — refinamento do parecer v3 (Fable 5)."""
    if not _under_mount(ap):
        return False
    return _rotational(_dev_for_path(ap)) != "0"       # None (desconhecido) => serializa


# ------------------------------------------------------- perfil de I/O de LEITURA (F9a)
# fstypes cuja política de BUSCA muda (não é só destino de cópia). REDE: a latência
# domina a banda — paralelismo MODERADO por montagem ajuda, mas o pool inteiro numa
# montagem só afoga a busca local, e uma montagem MORTA congelaria tudo (ver
# mount_alive). GVFS/AUTOFS exigem cautela na ENUMERAÇÃO ("buscar em /" ou "/mnt").
_NET_FSTYPES = frozenset({
    "nfs", "nfs4", "cifs", "smb3", "smbfs", "smb",
    "fuse.sshfs", "sshfs", "davfs", "fuse.davfs", "webdav",
    "9p", "virtiofs", "ncpfs", "afs",
    "glusterfs", "fuse.glusterfs", "lustre", "ceph", "fuse.cephfs", "beegfs",
})
_GVFS_FSTYPES = frozenset({"fuse.gvfsd-fuse", "gvfsd-fuse"})

# Teto de workers concorrentes POR montagem de rede: latência gosta de alguns em
# voo, mas nenhuma montagem pode sequestrar o pool inteiro (mesmo raciocínio do
# pacing de escrita — recurso global protegido, aqui aplicado a threads).
NET_WORKERS_PER_MOUNT = 4


@dataclass(frozen=True)
class IOProfile:
    """Perfil de I/O de LEITURA de um caminho, para a política de busca (F9a).

    `klass` ∈ {rotational, ssd, network, gvfs, autofs, unknown}. Função-fonte:
    `search_profile`. NÃO sonda a rede (isso é `mount_alive`) — é classificação
    pura por fstype + rotational, testável com montagens sintéticas.
    """
    klass: str
    mountpoint: str
    fstype: str
    serialize: bool           # busca serializada (1 processo por vez) — SMR/rotacional
    is_network: bool          # montagem de rede: exige watchdog + teto de workers
    max_workers: "int | None" # teto de workers NESTA montagem (None = pool global)
    enumerate_default: bool    # entra no "buscar em tudo" por padrão? (gvfs/autofs: não)


def search_profile(path: str, mounts=None) -> IOProfile:
    """Classifica `path` para a política de busca (F9a). PURA (`mounts` injetável
    p/ teste): olha o fstype da montagem de prefixo mais longo e o rotational do
    dev. Coerente com `path_needs_serial` no eixo local (SMR/rotacional serializa,
    SSD/NVMe libera); acrescenta os eixos de REDE que aquela função não cobre."""
    ap = os.path.abspath(path)
    dev, mp, fstype = _mount_entry(ap, mounts)
    fskey = fstype.lower()
    if fskey in _GVFS_FSTYPES:
        # gvfs = rede por FUSE; fora do "buscar em tudo" por padrão — só se o
        # usuário deu o caminho explícito (senão varreria o celular montado).
        return IOProfile("gvfs", mp, fstype, serialize=False, is_network=True,
                         max_workers=NET_WORKERS_PER_MOUNT, enumerate_default=False)
    if fskey == "autofs":
        # placeholder de automount: NÃO descer ao enumerar (acordaria todo mount
        # da casa); só se o caminho foi dado explicitamente.
        return IOProfile("autofs", mp, fstype, serialize=False, is_network=True,
                         max_workers=NET_WORKERS_PER_MOUNT, enumerate_default=False)
    if fskey in _NET_FSTYPES:
        return IOProfile("network", mp, fstype, serialize=False, is_network=True,
                         max_workers=NET_WORKERS_PER_MOUNT, enumerate_default=True)
    # local: reaproveita a lógica SMR/rotacional (None desconhecido => serializa)
    rot = _rotational(dev)
    if _under_mount(ap) and rot != "0":
        return IOProfile("rotational", mp, fstype, serialize=True, is_network=False,
                         max_workers=None, enumerate_default=True)
    return IOProfile("ssd" if rot == "0" else "unknown", mp, fstype,
                     serialize=False, is_network=False,
                     max_workers=None, enumerate_default=True)


# F2 (achado Fable, NAS FUSE real): errnos que significam "a montagem RESPONDEU,
# mas está QUEBRADA" — não é uma negação de permissão, é um cadáver que responde na
# hora. Entrar num root desses dá vazio silencioso; tratamos como MORTO (pulado com
# aviso). ENOTCONN = servidor FUSE/sshfs caiu; ESTALE = handle NFS velho; EHOSTDOWN
# = host fora; ENODEV = device sumiu. EACCES/EPERM/ENOENT ficam VIVOS (a montagem
# respondeu; o problema é permissão/ausência do alvo, não da montagem). EIO fica
# VIVO (disco com defeito ainda é uma montagem presente — escolha documentada).
_DEAD_MOUNT_ERRNOS = frozenset({
    errno.ENOTCONN,   # 107
    errno.ESTALE,     # 116
    errno.EHOSTDOWN,  # 112
    errno.ENODEV,     # 19
})

_PROBE_ALIVE = b"A"
_PROBE_DEAD = b"D"

# F1 (achado Fable): sondas abandonadas presas em D-state, reapadas oportunistamente
# quando (e se) o mount ressuscitar. Nunca bloqueia; nunca reapa filho de terceiros
# (só PIDs que ESTA função forkou), então não rouba os filhos rg/fd do subprocess.
_abandoned_lock = threading.Lock()
_abandoned_pids: list = []


def _reap_abandoned():
    """Reapa (WNOHANG) as sondas antes abandonadas que já morreram; mantém as ainda
    presas em D. Chamado a cada sonda nova — o processo longo (GUI) não acumula
    zumbis quando o mount volta; o processo curto (CLI) sai limpo e o init reapa o
    que sobrar."""
    with _abandoned_lock:
        if not _abandoned_pids:
            return
        still = []
        for p in _abandoned_pids:
            try:
                done, _ = os.waitpid(p, os.WNOHANG)
            except OSError:
                done = p                  # não é mais filho / não existe => esquece
            if done == 0:
                still.append(p)           # ainda em D-state
        _abandoned_pids[:] = still


def mount_status(mp: str, timeout: float = 3.0, _stat=os.stat) -> str:
    """Sonda de vida de uma montagem (F9a §2.2 + F1/F2). Devolve
    'alive' | 'no_response' | 'broken_mount'.

    Por que um PROCESSO (fork) e não uma thread (achado F1 do Fable, NAS FUSE
    real): um NFS/FUSE morto trava o `stat()` em D-state ININTERRUPTÍVEL — nem
    SIGKILL alcança. Uma THREAD presa vira órfã que IMPEDE o `exit_group`: a main
    termina (vira zumbi Z) mas o processo não morre enquanto a sonda estiver em D.
    Num processo CURTO (CLI/cron) isso pendura o cadáver e trava pipelines. Um
    PROCESSO filho preso em D é reparentado ao init quando o pai sai; o pai lê o
    resultado por um pipe com timeout e ABANDONA o filho, saindo limpo. Vale para
    a GUI também (uniforme e mais seguro); custo de um fork por root de rede,
    desprezível.

    'no_response' = travou (não respondeu no prazo). 'broken_mount' = respondeu na
    hora com um errno de montagem morta (F2). 'alive' = respondeu OK, ou negou com
    um errno que não é da montagem (EACCES/EPERM/ENOENT/EIO). `_stat` é injetável
    p/ teste determinístico (sem NAS/sshfs real)."""
    _reap_abandoned()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        # FILHO: só stat + 1 byte + _exit. Nada de finally/atexit do pai (os._exit).
        #
        # R1 (achado Fable, revisão 23/07): o "hang só na primeira vez" NÃO era
        # cold-start do FUSE — e ESTE filho segurando as fds herdadas do pai. Preso
        # em D-state (stat de mount morto, ininterruptível), ele mantém o stdout /
        # stderr do pai ABERTOS; um leitor do `--json` por pipe (o subprocess do
        # teste, um xargs, o Fable no ambiente dele) só recebe EOF quando TODAS as
        # pontas de escrita fecham — e o pai já saiu, mas este filho-cadáver não.
        # Resultado: a CLI "some" mas o pipeline pendura > timeout. Cache quente = o
        # stat volta rapido, o filho fecha a fd, EOF chega (por isso 7/7 limpas);
        # frio = trava em D segurando o stdout. A cura e fechar TODA fd herdada menos
        # o `w` do pipe — o unico canal legitimo deste filho. (Tambem protege a GUI:
        # não segura pipes de subprocessos rg/fd nem o socket do X11.)
        try:
            os.close(r)
        except OSError:
            pass
        try:                              # /proc/self/fd: barato e exato (como o subprocess)
            _inherited = [int(e) for e in os.listdir("/proc/self/fd")]
        except OSError:
            _inherited = list(range(0, 256))
        for _fd in _inherited:
            if _fd == w:
                continue
            try:
                os.close(_fd)
            except OSError:
                pass
        code = _PROBE_ALIVE
        try:
            _stat(mp)
        except OSError as e:
            if e.errno in _DEAD_MOUNT_ERRNOS:
                code = _PROBE_DEAD        # respondeu, mas a montagem está QUEBRADA
            # demais errnos => respondeu = VIVA
        except BaseException:
            pass                          # qualquer outra falha => trate como viva
        try:
            os.write(w, code)
        except OSError:
            pass
        os._exit(0)
    # PAI
    os.close(w)
    result = "no_response"
    try:
        rlist, _, _ = select.select([r], [], [], timeout)
        if rlist:
            data = os.read(r, 1)
            if data == _PROBE_DEAD:
                result = "broken_mount"
            elif data == _PROBE_ALIVE:
                result = "alive"
            # pipe fechou sem byte (filho morreu sem responder) => no_response
    except OSError:
        pass
    finally:
        try:
            os.close(r)
        except OSError:
            pass
    # reapa sem NUNCA bloquear; se ainda preso em D, abandona ao init / ao sweep
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
    except OSError:
        done = pid
    if done == 0:
        with _abandoned_lock:
            _abandoned_pids.append(pid)
    return result


def mount_alive(mp: str, timeout: float = 3.0, _stat=os.stat) -> bool:
    """Contrato bool (F9a): True só se a montagem está VIVA e OK. 'no_response'
    (travou) e 'broken_mount' (respondeu quebrada, F2) contam como MORTA. Para o
    aviso distinguir o motivo, use `mount_status` diretamente."""
    return mount_status(mp, timeout=timeout, _stat=_stat) == "alive"


def mounts_under(root: str, mounts=None):
    """Pontos de montagem ESTRITAMENTE dentro de `root` (não o próprio root).
    `mounts` injetável. Serve ao §2.3: um 'buscar em /' ou '/mnt' precisa listar
    ANTES quais montagens serão tocadas — servidor com 40 montagens agradece."""
    ap = os.path.abspath(root).rstrip("/") or "/"
    pre = ap + "/" if ap != "/" else "/"
    try:
        entradas = mounts if mounts is not None else _read_mounts()
    except OSError:
        return []
    out = []
    for _dev, mp, _fs in entradas:
        if mp != ap and mp.startswith(pre):
            out.append(mp)
    return sorted(set(out))


def list_search_targets(paths, probe_timeout=3.0, mounts=None,
                        _profile=None, _alive=None):
    """§2.3 — VISIBILIDADE DE FRONTEIRA. Dado os roots de uma busca, diz quais
    montagens serão tocadas e de que classe (disco/ssd/rotational/network/gvfs/
    autofs), e p/ rede, se está VIVA. Alimenta os badges de chip e o preview
    'buscar em / vai tocar N montagens' SEM tocar na thread da GUI (é chamável de
    um worker). PURA e injetável (`_profile`/`_alive`/`mounts` p/ teste).

    Cada root vira uma entrada; se um root contém montagens (ex.: '/', '/mnt'),
    elas entram TAMBÉM (o usuário vê o NAS que mora sob o caminho pedido). Dedup
    por ponto de montagem. `alive` só é sondado p/ rede (custo do watchdog); em
    montagem local fica None (não faz sentido)."""
    prof = _profile or search_profile
    alive = _alive or mount_alive
    out, seen = [], set()
    def add(path):
        ap = os.path.abspath(os.path.expanduser(path))
        p = prof(ap, mounts) if mounts is not None else prof(ap)
        key = p.mountpoint or ap
        if key in seen:
            return
        seen.add(key)
        live = alive(p.mountpoint or ap, timeout=probe_timeout) if p.is_network else None
        out.append({"path": ap, "mountpoint": p.mountpoint, "klass": p.klass,
                    "fstype": p.fstype, "is_network": p.is_network,
                    "serialize": p.serialize, "enumerate_default": p.enumerate_default,
                    "alive": live})
    for root in paths:
        add(root)
        for mp in mounts_under(root, mounts):
            add(mp)
    return out


def mount_ok(path: str) -> bool:
    """O destino de uma cópia está numa montagem REAL? Sob /mnt|/media|/run/media,
    um ponto de montagem desmontado continua existindo como diretório vazio no
    disco de sistema: copiar 300 GB para lá encheria o NVMe silenciosamente.
    Fora desses prefixos (home, /tmp) não há o que checar."""
    ap = os.path.abspath(path)
    if not _under_mount(ap):
        return True
    mp = _mount_entry(ap)[1]
    if not mp or not _under_mount(mp):
        return False                     # coberto só por / (ou nada): não montado
    return mp in engine.user_mounts()


# ------------------------------------------------------------------ capacidades do destino
# O que cada família de sistema de arquivos aceita. Só listamos os que RESTRINGEM;
# o padrão (ext4/xfs/btrfs/zfs/f2fs/nfs...) aceita tudo que o Linux aceita.
#
#   max_file  — maior arquivo, em bytes (None = sem limite prático)
#   symlinks  — suporta link simbólico
#   perms     — suporta modo/uid/gid POSIX
#   times     — suporta ajustar mtime (utime)
#   charset   — caracteres PROIBIDOS no nome
#   reserved  — nomes reservados do DOS (CON, PRN, LPT1…) são inválidos
#   utf8_only — o nome precisa ser UTF-8 VÁLIDO. O vfat/exfat/ntfs guardam nomes
#               em UTF-16 e o kernel converte na hora de escrever; um nome com
#               byte inválido (foto de câmera, arquivo vindo de outro sistema)
#               volta EINVAL. Não é teoria: o pendrive de verdade recusou
#               'camera_\xff\xfe.jpg' que a imagem FAT32 em loop tinha aceitado
#               — a diferença é o iocharset com que o udisks monta o removível.
#   maxchars  — limite de nome em CARACTERES (não bytes). FAT/exFAT/NTFS contam
#               255 unidades UTF-16, mas o statvfs do vfat responde f_namemax
#               =1530 (255x6, o pior caso do UTF-8): confiar nele fazia a
#               pré-checagem aprovar um nome de 300 caracteres que o kernel
#               recusa com ENAMETOOLONG na hora de escrever. Medido em FAT32
#               real: 254 caracteres passam, 259 não.
_DOS_BAD = '"*:<>?\\|'
_FAT = dict(max_file=(1 << 32) - 1, symlinks=False, perms=False, times=True,
            charset=_DOS_BAD, reserved=True, label="FAT32", maxchars=255, utf8_only=True)
_EXFAT = dict(max_file=None, symlinks=False, perms=False, times=True,
              charset=_DOS_BAD, reserved=False, label="exFAT", maxchars=255, utf8_only=True)
_NTFS = dict(max_file=None, symlinks=False, perms=False, times=True,
             charset=_DOS_BAD, reserved=True, label="NTFS", maxchars=255, utf8_only=True)
_MTP = dict(max_file=None, symlinks=False, perms=False, times=False,
            charset=_DOS_BAD, reserved=False, label="MTP", maxchars=255, utf8_only=True)

# Destinos de REDE por gvfs. A montagem gvfs é UMA só (fuse.gvfsd-fuse) para todos
# os backends; quem distingue mtp:/sftp:/smb-share:/dav: é o ESQUEMA no primeiro
# componente do caminho, lido em dest_caps() — nunca o fstype (ver §3.1 do achado
# de campo). `net=True` marca "gargalo é a rede": sem pacing USB e statvfs não
# confiável para "cabe?".
_NET_POSIX = dict(max_file=None, symlinks=True, perms=True, times=True,
                  charset="", reserved=False, label="SFTP", net=True)
_NET_SMB = dict(max_file=None, symlinks=False, perms=False, times=True,
                charset=_DOS_BAD, reserved=False, label="SMB", net=True)
_NET_LIMITED = dict(max_file=None, symlinks=False, perms=False, times=False,
                    charset="", reserved=False, label="WebDAV", net=True)
# Esquema gvfs desconhecido ou raiz do gvfs sem componente: conservador. NÃO
# degrada nomes sem evidência (charset livre) — a SONDA decide a gravabilidade.
_NET_CONSERVATIVE = dict(max_file=None, symlinks=False, perms=False, times=False,
                         charset="", reserved=False, label="rede", net=True)
# Montagem NFS do KERNEL (não-gvfs): POSIX pleno (symlink/perms/times ok), mas
# net=True — o gargalo é a rede e o f_bavail do statvfs sobre NFS é palpite. 9p e
# virtiofs (VM) idem. Nota honesta: fsync sobre NFS é caro (close-to-open), mas
# "copiado = está lá" vale mais no NAS que a economia — o pacing de rede cuida disso.
_NET_NFS = dict(max_file=None, symlinks=True, perms=True, times=True,
                charset="", reserved=False, label="NFS", net=True)

_FS_CAPS = {
    "vfat": _FAT, "fat": _FAT, "msdos": _FAT, "umsdos": _FAT,
    "exfat": _EXFAT, "fuse.exfat": _EXFAT, "exfat-fuse": _EXFAT,
    "ntfs": _NTFS, "ntfs3": _NTFS, "fuseblk": _NTFS, "fuse.ntfs-3g": _NTFS,
    # celular/câmera por FUSE REAL (jmtpfs/mtpfs): grava com open('wb'), estratégia
    # GUARDED. O gvfs NÃO entra aqui — 'fuse.gvfsd-fuse' é resolvido por esquema em
    # dest_caps(), porque o mesmo fstype serve sftp/smb/dav (que são POSIX/rede).
    "fuse.jmtpfs": _MTP, "fuse.simple-mtpfs": _MTP, "fuse.go-mtpfs": _MTP,
    "mtpfs": _MTP,
    # ISO/UDF montados são somente-leitura; tratados como erro na pré-checagem
    "iso9660": dict(max_file=None, symlinks=True, perms=False, times=False,
                    charset="", reserved=False, label="ISO9660", readonly=True),
    # F9c — montagens de REDE do KERNEL (não passam pelo gvfs; o SO já montou).
    # Sem elas, um destino CIFS caía em _DEFAULT_CAPS (POSIX otimista) e a
    # pré-checagem LIBERAVA ':' '?' '*' num nome que o SMB recusa. Agora o charset
    # segue o PROTOCOLO. Todas net=True → dispara o ritmo de escrita de rede (§4.2).
    "nfs": _NET_NFS, "nfs4": _NET_NFS, "9p": _NET_NFS, "virtiofs": _NET_NFS,
    "cifs": _NET_SMB, "smb3": _NET_SMB, "smbfs": _NET_SMB, "smb": _NET_SMB,
    "fuse.sshfs": _NET_POSIX, "sshfs": _NET_POSIX,   # rename atômico ok → ATOMIC
}

# esquema gvfs (antes do ':' no primeiro componente do caminho) -> perfil de caps.
# mtp/gphoto2/afc transferem OBJETOS inteiros: mesmas restrições e rota gvfs (GIO).
_GVFS_SCHEMES = {
    "mtp": _MTP, "gphoto2": _MTP, "afc": _MTP,
    "sftp": _NET_POSIX, "ssh": _NET_POSIX,
    "smb-share": _NET_SMB, "smb": _NET_SMB, "cifs": _NET_SMB,
    "dav": _NET_LIMITED, "davs": _NET_LIMITED,
}


def _gvfs_scheme(path: str, mountpoint: str) -> str:
    """Esquema do backend gvfs lido no primeiro componente do caminho abaixo da
    raiz do mount: '/run/user/1000/gvfs/mtp:host=Philips/...' -> 'mtp'. Devolve ''
    para o próprio ponto de montagem (sem componente) ou caminho fora dele."""
    rel = os.path.relpath(os.path.abspath(path), mountpoint)
    if rel in (".", "") or rel.startswith(".."):
        return ""
    comp = rel.split(os.sep, 1)[0]           # 'mtp:host=Philips_...'
    return comp.split(":", 1)[0].lower()     # 'mtp'


def _caps_for(fstype: str, path: str, mountpoint: str):
    """(dict de capacidades, via_gvfs) a partir do fstype e — para o gvfs — do
    ESQUEMA lido no caminho. Função PURA (não toca no disco), para ser testável
    com caminhos sintéticos e com o contraexemplo sftp (§2 do desenho A2R)."""
    fskey = fstype.lower()
    if fskey in ("fuse.gvfsd-fuse", "gvfsd-fuse"):
        base = _GVFS_SCHEMES.get(_gvfs_scheme(path, mountpoint), _NET_CONSERVATIVE)
        return dict(base), (base is _MTP)    # gvfs-MTP liga a estratégia GIO (§3.3)
    return dict(_FS_CAPS.get(fskey, _DEFAULT_CAPS)), False

_DEFAULT_CAPS = dict(max_file=None, symlinks=True, perms=True, times=True,
                     charset="", reserved=False, label="POSIX")

def _has_broken_bytes(name: str) -> bool:
    """O nome carrega bytes que não formam UTF-8 válido? São os substitutos do
    surrogateescape, que o Python usa para representar bytes indecodificáveis."""
    return any(0xDC80 <= ord(c) <= 0xDCFF for c in name)


def _fix_broken_bytes(name: str) -> str:
    """Troca cada byte indecodificável por '%XX' — o valor original fica legível
    no nome, então dá para saber de que arquivo veio sem consultar a origem."""
    return "".join("%%%02X" % (ord(c) - 0xDC00) if 0xDC80 <= ord(c) <= 0xDCFF else c
                   for c in name)


_RESERVED = ({"CON", "PRN", "AUX", "NUL"} |
             {"COM%d" % i for i in range(1, 10)} |
             {"LPT%d" % i for i in range(1, 10)})


class DestCaps:
    """O que o sistema de arquivos de destino aceita. `fstype` vazio = não
    identificado -> assumimos POSIX (otimista), mas `namemax` do statvfs ainda
    vale, então nomes longos demais continuam sendo pegos."""

    def __init__(self, fstype="", mountpoint="", namemax=255, readonly=False,
                 via_gvfs=False, **caps):
        self.fstype = fstype
        self.mountpoint = mountpoint
        self.namemax = namemax or 255
        self.readonly = readonly
        # via_gvfs: destino é backend gvfs cujo open('wb') NÃO grava (ponte FUSE
        # devolve ENOTSUP) — a escrita vai pela estratégia GIO (`gio copy`).
        self.via_gvfs = bool(via_gvfs)
        # net: destino de rede (sftp/smb/dav). Sem pacing USB; f_bavail do statvfs
        # é inventado, então "cabe?" é palpite, não garantia.
        self.net = bool(caps.get("net"))
        self.max_file = caps.get("max_file")
        self.symlinks = caps.get("symlinks", True)
        self.perms = caps.get("perms", True)
        self.times = caps.get("times", True)
        self.charset = caps.get("charset", "")
        self.maxchars = caps.get("maxchars")             # limite em CARACTERES (UTF-16)
        self.utf8_only = bool(caps.get("utf8_only"))     # nome precisa ser UTF-8 válido
        # Removível: pendrive/cartão/gaveta USB. Não muda o QUE pode ser escrito
        # (isso é o resto da tabela) — muda o RITMO com que se escreve.
        self.removable = bool(caps.get("removable"))
        # Velocidade negociada do link USB (Mbit/s), quando aplicável. Explica
        # sozinha a maior parte das cópias "lentas demais".
        self.link_mbits = caps.get("link_mbits")
        self.reserved = caps.get("reserved", False)
        self.label = caps.get("label", "POSIX")

    @property
    def restrictive(self) -> bool:
        """Precisa avisar o usuário antes de copiar?"""
        return bool(self.charset or self.max_file or not self.symlinks
                    or self.namemax < 255)

    def name_problem(self, name: str):
        """Por que `name` não pode existir no destino? None se pode.
        Devolve chave estável ('charset'|'length'|'reserved'|'trailing'), que a
        GUI traduz — o módulo não fala com o usuário (i18n mora na borda)."""
        if self.charset and any(c in self.charset for c in name):
            return "charset"
        if self.charset and any(ord(c) < 32 for c in name):
            return "charset"             # \n, \t: ilegais em FAT/exFAT/NTFS
        if self.utf8_only and _has_broken_bytes(name):
            return "encoding"            # nome que não é UTF-8: EINVAL no vfat
        if len(os.fsencode(name)) > self.namemax:
            return "length"
        if self.maxchars and len(name) > self.maxchars:
            return "length"                  # FAT/exFAT/NTFS: 255 unidades UTF-16
        if self.reserved and os.path.splitext(name)[0].upper() in _RESERVED:
            return "reserved"
        if self.charset and (name.endswith(" ") or name.endswith(".")):
            return "trailing"            # FAT/NTFS descartam espaço/ponto final
        return None

    def sanitize(self, name: str) -> str:
        """Nome adaptado ao destino, preservando a extensão. Só é usado quando o
        usuário escolhe 'adaptar nomes' — nunca automaticamente."""
        if self.utf8_only:
            name = _fix_broken_bytes(name)
        out = "".join("_" if (c in self.charset or ord(c) < 32) else c for c in name)
        if self.reserved and os.path.splitext(out)[0].upper() in _RESERVED:
            stem, ext = os.path.splitext(out)
            out = stem + "_" + ext
        out = out.rstrip(" .") or "_"
        # corta o RADICAL preservando a extensão. O limite é em BYTES (não chars):
        # fsencode/fsdecode com surrogateescape roundtripa nome não-UTF-8 sem perder.
        stem, ext = os.path.splitext(out)
        eb = os.fsencode(ext)
        if len(eb) >= self.namemax:                   # extensão absurda: corta tudo
            return os.fsdecode(os.fsencode(out)[:self.namemax])
        room = self.namemax - len(eb)
        sb = os.fsencode(stem)
        if len(sb) > room:
            stem = os.fsdecode(sb[:room]) or "_"
        if self.maxchars:                             # e o limite em CARACTERES
            stem = stem[:max(1, self.maxchars - len(ext))]
        return (stem + ext).rstrip(" .") or "_"


def is_removable(dev: str) -> bool:
    """O dispositivo é removível (pendrive, cartão, HD USB)?

    Lê /sys/block/<disco>/removable, e trata USB como removível mesmo quando a
    flag é 0 — gaveta USB com disco comum responde 0, e o que nos interessa aqui
    não é "pode arrancar", é "escrever nisso é lento e o cache de página do
    kernel vira uma bomba-relógio"."""
    disco = _sys_disk(dev)
    if not disco:
        return False
    d = "/sys/block/%s" % disco
    try:
        with open(d + "/removable") as f:
            if f.read().strip() == "1":
                return True
    except OSError:
        return False
    try:                                   # barramento USB: caminho tem /usb
        return "/usb" in os.path.realpath(d + "/device")
    except OSError:
        return False


def _sys_disk(dev: str) -> str:
    """Nome em /sys/block do DISCO inteiro que sustenta o nó `dev` (sobe de
    partição para disco e de dm-N para o disco físico). "" se não der."""
    base = os.path.basename(os.path.realpath(dev or ""))
    if not base:
        return ""
    for _ in range(4):
        if not base.startswith("dm-"):
            break
        try:
            base = sorted(os.listdir("/sys/block/%s/slaves" % base))[0]
        except (OSError, IndexError):
            return ""
    # A4.4: se `base` JÁ é um disco inteiro (mmcblk0, nvme0n1, sda sem partição),
    # ele é a resposta — cortar dígitos aqui comeria o "0"/"1" do nome do disco.
    if os.path.isdir("/sys/block/%s" % base):
        return base
    disco = re.sub(r"(p?\d+)$", "", base) if not base.startswith("sd") else base.rstrip("0123456789")
    return disco if os.path.isdir("/sys/block/%s" % disco) else ""


def link_speed(dev: str):
    """Velocidade NEGOCIADA do barramento, em Mbit/s, ou None se não for USB.

    Vale a pena mostrar porque explica a maior parte das decepções com pendrive:
    o mesmo SanDisk que faz 100 MB/s numa porta USB 3 faz 30 numa USB 2, e o
    usuário não tem como saber em qual porta o filho espetou. 480 = USB 2.0,
    5000 = USB 3.0, 10000 = 3.1 Gen2, 20000 = 3.2 Gen2x2.

    É o teto do LINK, não do dispositivo: um pendrive lento em porta rápida
    continua lento. Serve para dizer "não adianta trocar de porta" ou o
    contrário."""
    disco = _sys_disk(dev)
    if not disco:
        return None
    caminho = os.path.realpath("/sys/block/%s/device" % disco)
    # sobe a árvore até achar o nó USB que carrega 'speed'
    for _ in range(8):
        alvo = os.path.join(caminho, "speed")
        if os.path.isfile(alvo):
            try:
                with open(alvo) as f:
                    return float(f.read().strip())
            except (OSError, ValueError):
                return None
        pai = os.path.dirname(caminho)
        if pai == caminho or pai == "/sys":
            return None
        caminho = pai
    return None


def link_label(mbits) -> str:
    """'USB 2.0 (480 Mb/s)' — o nome que o usuário reconhece, com o número."""
    if not mbits:
        return ""
    nome = {480: "USB 2.0", 5000: "USB 3.0", 10000: "USB 3.1", 20000: "USB 3.2",
            12: "USB 1.1", 1.5: "USB 1.0"}.get(mbits, "USB")
    return f"{nome} ({mbits:g} Mb/s)"


def dest_caps(path: str) -> DestCaps:
    """Capacidades do sistema de arquivos que sustenta `path` (ou o ancestral
    existente mais próximo, se o diretório ainda vai ser criado)."""
    ap = os.path.abspath(path)
    probe = ap
    while probe != "/" and not os.path.exists(probe):
        probe = os.path.dirname(probe)
    dev, mp, fstype = _mount_entry(probe)
    # Esquema gvfs lido do caminho ORIGINAL (ap), não do probe: se o subdiretório
    # ainda não existe, probe subiu, mas o componente 'mtp:host=' já está em ap.
    caps, via_gvfs = _caps_for(fstype, ap, mp)
    readonly = bool(caps.pop("readonly", False))
    namemax = 255
    try:
        st = os.statvfs(probe)
        namemax = int(st.f_namemax) or 255
        # ST_RDONLY = 1; montagem só-leitura vira erro claro na pré-checagem
        readonly = readonly or bool(getattr(st, "f_flag", 0) & 1)
    except OSError:
        pass
    return DestCaps(fstype=fstype, mountpoint=mp, namemax=namemax,
                    readonly=readonly, via_gvfs=via_gvfs, removable=is_removable(dev),
                    link_mbits=link_speed(dev), **caps)


def removable_dest(path: str, mounts=None):
    """(removível?, mountpoint, dev) do volume que contém `path` — o que o
    pós-cópia precisa para oferecer "seguro remover" + Ejetar (F10b #4). Sem
    sonda de escrita: só classifica pela topologia. PURA (`mounts` injetável)."""
    dev, mp, _fs = _mount_entry(os.path.abspath(path), mounts)
    return is_removable(dev), mp, dev


def eject_command(mountpoint: str, dev: str = "", *, which=None):
    """Comando (argv) para ejetar com segurança o volume em `mountpoint`, ou None
    quando nenhuma ferramenta existe — aí o botão simplesmente não aparece (sem
    dependência nova; F10b #4).

    Prefere `gio mount -e` (desmonta pelo gvfs/udisks e faz o flush certo, é o que
    o Nemo faz), cai para `udisksctl power-off -b <dev>` (desliga o barramento — o
    "pode arrancar o pendrive" de verdade). `which` injetável para os testes."""
    which = which or shutil.which
    if which("gio"):
        return ["gio", "mount", "-e", mountpoint]
    if dev and which("udisksctl"):
        return ["udisksctl", "power-off", "-b", dev]
    return None


def free_bytes(path: str) -> int:
    """Bytes livres no destino (0 se não der para saber)."""
    probe = os.path.abspath(path)
    while probe != "/" and not os.path.exists(probe):
        probe = os.path.dirname(probe)
    try:
        st = os.statvfs(probe)
        return st.f_bavail * st.f_frsize
    except OSError:
        return 0
