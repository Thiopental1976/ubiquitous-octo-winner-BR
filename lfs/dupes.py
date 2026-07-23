#!/usr/bin/env python3
"""Caçador de duplicatas NATIVO do Sombrero File Search (F10c do desenho do Fable).

Código PRÓPRIO do SFS — não é dependência de nenhum outro projeto (o motor de
dedup do cedro serve só de ORÁCULO nos testes de paridade). A linha vermelha
vem antes de qualquer linha de código:

    O SFS ACHA, MOSTRA e EXPORTA duplicatas. NÃO as apaga — nem com confirmação,
    nem "só a lixeira". "Lê e exporta, jamais altera" é a identidade do produto.
    Este módulo não tem função de remover, e não deve ganhar uma: a exclusão, se
    um dia existir, é decisão de identidade do Rodrigo em desenho próprio.

Pipeline (disciplina de I/O do projeto — a mesma do resto do SFS):

  Estágio 0 — identidade física: agrupa por (st_dev, st_ino) ANTES de tudo.
      HARDLINKS SÃO O MESMO ARQUIVO, não duplicatas — reportar dois hardlinks
      como "duplicata" convidaria a apagar o que não ocupa espaço. Um grupo de
      inode vira UM candidato (que carrega todos os seus nomes). Symlinks fora.
      Tamanho 0 fora por padrão (todos são "iguais"); `include_zero` liga.
  Estágio 1 — tamanho: só tamanhos com ≥2 candidatos seguem (de graça — o stat
      do walk já deu o tamanho).
  Estágio 2 — hash de cabeça (BLAKE2b dos primeiros 64 KiB): mata a maioria sem
      ler arquivos inteiros. Em acervo de vídeo é a diferença entre horas e min.
  Estágio 3 — hash completo só nos sobreviventes. Leitura sequencial POR
      DISPOSITIVO (candidatos ordenados por st_dev — um disco de cada vez, a
      trava SMR de sempre), fadvise DONTNEED no que leu (não expulsa o cache de
      quem usa a máquina), cancel por bloco, progresso honesto em BYTES (o total
      a hashear é conhecido ao fim do estágio 2).

Resultado: grupos (mesmo conteúdo) ordenados por bytes desperdiçados. Cada grupo
sabe seu tamanho e seus membros (cada membro = um candidato, que lista os nomes
dos hardlinks quando há). O badge de disco por membro é resolvido na GUI.
"""
from __future__ import annotations

import hashlib
import os
import stat as _stat
from typing import Callable, Dict, List, Optional, Tuple

HEAD_BYTES = 64 * 1024        # estágio 2: cabeça hasheada
FULL_BLOCK = 1 << 20          # estágio 3: bloco de leitura (cancel por bloco)
_HEAD_DIGEST = 16             # 128 bits de cabeça bastam p/ triar
_FULL_DIGEST = 32             # 256 bits confirmam identidade

# Assinatura dos callbacks (todos opcionais):
CancelFn = Callable[[], bool]                 # True => abortar
ProgressFn = Callable[[int, int], None]       # (bytes_hasheados, bytes_totais)
PhaseFn = Callable[[str], None]               # rótulo do estágio corrente


def new_stats() -> Dict[str, int]:
    """Contadores do run (lidos pela GUI e pelos testes). Atualizados in-place."""
    return {"files": 0, "candidates": 0, "symlinks": 0, "denied": 0,
            "hashed_bytes": 0, "groups": 0}


class Candidate:
    """Um arquivo FÍSICO (um inode). Carrega todos os seus nomes — dois hardlinks
    viram UM candidato com dois caminhos, nunca uma "duplicata"."""
    __slots__ = ("dev", "ino", "size", "paths")

    def __init__(self, dev: int, ino: int, size: int):
        self.dev = dev
        self.ino = ino
        self.size = size
        self.paths: List[str] = []

    @property
    def path(self) -> str:
        """Caminho representativo: o mais curto (o mais "raiz") — determinístico."""
        return min(self.paths, key=lambda p: (len(p), p))

    @property
    def names(self) -> List[str]:
        return sorted(self.paths, key=lambda p: (len(p), p))


class DupGroup:
    """Um conjunto de candidatos byte-idênticos (mesmo tamanho e mesmo hash)."""
    __slots__ = ("size", "digest", "members")

    def __init__(self, size: int, digest: str, members: List[Candidate]):
        self.size = size
        self.digest = digest
        self.members = members

    @property
    def wasted(self) -> int:
        """Bytes recuperáveis: mantendo UMA cópia, o resto é redundante."""
        return self.size * (len(self.members) - 1)

    @property
    def paths(self) -> List[str]:
        return [c.path for c in self.members]


# ------------------------------------------------------------------ walk
def _walk(roots, min_size, include_zero, follow_symlinks, cancel, stats):
    """Estágio 0: percorre as raízes e colapsa cada inode num único candidato."""
    seen: Dict[Tuple[int, int], Candidate] = {}
    order: List[Candidate] = []

    def on_err(_e: OSError):
        stats["denied"] += 1        # pasta ilegível (EACCES): contada, não fatal

    for root in roots:
        if cancel():
            break
        for dirpath, _dirs, files in os.walk(root, onerror=on_err,
                                             followlinks=follow_symlinks):
            if cancel():
                break
            for fn in files:
                p = os.path.join(dirpath, fn)
                try:
                    st = os.lstat(p)
                except OSError:
                    stats["denied"] += 1
                    continue
                mode = st.st_mode
                if _stat.S_ISLNK(mode):
                    stats["symlinks"] += 1
                    continue
                if not _stat.S_ISREG(mode):
                    continue
                sz = st.st_size
                if sz == 0 and not include_zero:
                    continue
                if sz < min_size:
                    continue
                key = (st.st_dev, st.st_ino)
                cand = seen.get(key)
                if cand is None:
                    cand = Candidate(st.st_dev, st.st_ino, sz)
                    seen[key] = cand
                    order.append(cand)
                cand.paths.append(p)
                stats["files"] += 1
    stats["candidates"] = len(order)
    return order


# ------------------------------------------------------------------ hashing
def _fadvise_dontneed(fd: int):
    """Não expulsar o cache de quem usa a máquina: solta o que acabamos de ler."""
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    except (AttributeError, OSError):
        pass                        # plataforma sem fadvise: só não otimiza


def _head_digest(path: str) -> Optional[str]:
    try:
        with open(path, "rb", buffering=0) as f:
            data = f.read(HEAD_BYTES)
            _fadvise_dontneed(f.fileno())
    except OSError:
        return None
    return hashlib.blake2b(data, digest_size=_HEAD_DIGEST).hexdigest()


def _full_digest(path: str, cancel: CancelFn,
                 on_chunk: Callable[[int], None]) -> Optional[str]:
    h = hashlib.blake2b(digest_size=_FULL_DIGEST)
    try:
        with open(path, "rb", buffering=0) as f:
            while True:
                if cancel():
                    return None
                blk = f.read(FULL_BLOCK)
                if not blk:
                    break
                h.update(blk)
                on_chunk(len(blk))
            _fadvise_dontneed(f.fileno())
    except OSError:
        return None
    return h.hexdigest()


def _by(attr_fn, items):
    out: Dict = {}
    for it in items:
        out.setdefault(attr_fn(it), []).append(it)
    return out


# ------------------------------------------------------------------ público
def find_duplicates(roots, *, min_size: int = 0, include_zero: bool = False,
                    follow_symlinks: bool = False,
                    cancel: CancelFn = lambda: False,
                    on_progress: ProgressFn = lambda done, total: None,
                    on_phase: PhaseFn = lambda name: None,
                    stats: Optional[dict] = None) -> List[DupGroup]:
    """Acha grupos de arquivos byte-idênticos sob `roots`. Nunca altera nada.

    `min_size`  — ignora arquivos menores (bytes). `include_zero` — inclui os de
    tamanho 0. Retorna [] se cancelado no meio (sem estado pendente). Os grupos
    saem ordenados por bytes desperdiçados (maior primeiro)."""
    if stats is None:
        stats = new_stats()
    if isinstance(roots, str):
        roots = [roots]

    # Estágio 0 + 1 -----------------------------------------------------------
    on_phase("scan")
    cands = _walk(roots, min_size, include_zero, follow_symlinks, cancel, stats)
    if cancel():
        return []
    size_groups = [g for g in _by(lambda c: c.size, cands).values() if len(g) > 1]

    # Estágio 2 — cabeça ------------------------------------------------------
    on_phase("head")
    survivors: List[Candidate] = []      # candidatos que passam p/ o hash completo
    head_of: Dict[int, str] = {}         # id(cand) -> head digest (agrupa com o tamanho)
    for g in size_groups:
        if cancel():
            return []
        heads: Dict[str, List[Candidate]] = {}
        for c in g:
            d = _head_digest(c.path)
            if d is None:
                stats["denied"] += 1
                continue
            heads.setdefault(d, []).append(c)
            head_of[id(c)] = d
        for sub in heads.values():
            if len(sub) > 1:
                survivors.extend(sub)

    # Estágio 3 — completo (sequencial por dispositivo, progresso em bytes) ----
    on_phase("full")
    total_bytes = sum(c.size for c in survivors)
    stats["hashed_bytes"] = 0

    def on_chunk(n: int):
        stats["hashed_bytes"] += n
        on_progress(stats["hashed_bytes"], total_bytes)

    # Um disco de cada vez: ordenar por (dev, caminho) dá leitura sequencial e
    # evita fazer dois rotacionais/SMR arfarem ao mesmo tempo.
    survivors.sort(key=lambda c: (c.dev, c.path))
    full_buckets: Dict[Tuple[int, str, str], List[Candidate]] = {}
    for c in survivors:
        if cancel():
            return []
        d = _full_digest(c.path, cancel, on_chunk)
        if d is None:
            if cancel():
                return []
            stats["denied"] += 1
            continue
        # a chave inclui tamanho + cabeça: só compara quem já era comparável
        full_buckets.setdefault((c.size, head_of.get(id(c), ""), d), []).append(c)

    groups = [DupGroup(size, digest, members)
              for (size, _head, digest), members in full_buckets.items()
              if len(members) > 1]
    groups.sort(key=lambda gr: gr.wasted, reverse=True)
    stats["groups"] = len(groups)
    return groups


def summary(groups: List[DupGroup]) -> Tuple[int, int]:
    """(nº de grupos, bytes recuperáveis) — para o cabeçalho "N grupos · X GB"."""
    return len(groups), sum(g.wasted for g in groups)
