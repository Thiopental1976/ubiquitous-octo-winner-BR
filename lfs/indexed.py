#!/usr/bin/env python3
# Sombrero File Search — Copyright (C) 2026 Rodrigo Toledo
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Este programa é software livre: você pode redistribuí-lo e/ou modificá-lo sob
# os termos da GNU General Public License, versão 3 ou posterior (ver LICENSE).
# Distribuído na esperança de ser útil, mas SEM QUALQUER GARANTIA.
"""Sombrero File Search — aceleração por índice `plocate` (F9b §3.2).

Decisão de SEMÂNTICA do Fable (23/07, RESPOSTA_F9): **opt-in explícito; poda =
erro claro; nunca automático**. O SFS é "o que está no disco AGORA"; o índice é
uma escolha CONSCIENTE do usuário, com a data na cara. O que MATA o "usa se
existir" automático não é o zero — é o PARCIAL PODADO: um root indexado com uma
submontagem em PRUNEFS/PRUNEPATHS devolve resultados com um subtree faltando,
uma mentira silenciosa com cara de resposta. Por isso:

  1. Gatilho: SOMENTE explícito (`--index`). Nunca automático, nem no zero.
  2. Cobertura checada ANTES: `/etc/updatedb.conf` (PRUNEFS/PRUNEPATHS/PRUNENAMES)
     × as montagens sob o root. Qualquer parte do subtree podada => `--index`
     RECUSA com erro claro ("use a busca viva"), não degrada em silêncio.
  3. Zero candidatos com cobertura ok: confia no zero, COM a data do índice
     sempre visível.
  4. Staleness (arquivo sumiu do disco mas está no índice): verificação viva
     (`lstat`) descarta — o resultado só sai se o arquivo AINDA existe.

Índice acelera busca por NOME (o `plocate` indexa caminhos, não conteúdo). Para
CONTEÚDO, `--index` é recusado — a busca viva é o caminho. Puro e headless: sem
Qt, `_run`/`mounts`/`conf_text` injetáveis p/ teste determinístico."""
from __future__ import annotations
import os, subprocess
from typing import Optional

try:                       # funciona como pacote (-m lfs.*) e flat (cli.py)
    from . import engine
    from . import disks
except ImportError:
    import engine
    import disks

PLOCATE_DB = "/var/lib/plocate/plocate.db"
UPDATEDB_CONF = "/etc/updatedb.conf"


class IndexError_(Exception):
    """Recusa de cobertura: `--index` não pode responder honestamente por este
    root (poda no caminho). Mensagem já pronta p/ o usuário."""


# ------------------------------------------------------------ updatedb.conf
def parse_updatedb_conf(text: str) -> dict:
    """Parse das variáveis shell de `/etc/updatedb.conf`. Devolve
    {prunepaths:[abs], prunefs:{lower}, prunenames:[str], prune_bind:bool}.
    Tolerante: linha sem `=`, comentário e aspas são tratados; chave desconhecida
    ignorada."""
    out = {"prunepaths": [], "prunefs": set(), "prunenames": [], "prune_bind": True}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip().upper()
        # formato shell: KEY="a b c". As aspas AGRUPAM (shlex daria 1 token só);
        # o que queremos são as palavras DENTRO — tira aspas e divide por espaço.
        toks = val.strip().strip('"').strip("'").split()
        if key == "PRUNEPATHS":
            out["prunepaths"] = [os.path.abspath(p) for p in toks]
        elif key == "PRUNEFS":
            out["prunefs"] = {t.lower() for t in toks}
        elif key == "PRUNENAMES":
            out["prunenames"] = list(toks)
        elif key == "PRUNE_BIND_MOUNTS":
            out["prune_bind"] = (toks[:1] or ["yes"])[0].lower() in ("yes", "1", "true")
    return out


def _under(child: str, parent: str) -> bool:
    """child == parent OU child está estritamente dentro de parent."""
    child = child.rstrip("/") or "/"
    parent = parent.rstrip("/") or "/"
    if child == parent:
        return True
    return child.startswith(parent + "/") if parent != "/" else True


def index_coverage(root: str, conf: dict, mounts=None, include_hidden: bool = False) -> list:
    """Lista dos BURACOS do índice sob `root` (vazio = cobertura íntegra). Cada
    item: {'path': <subtree podado>, 'reason': 'prunepath'|'prunefs:<fs>'|'prunename'}.

    Cobre quatro fontes de poda: (a) o próprio root cai sob um PRUNEPATH; (b) o
    fstype da montagem do root está em PRUNEFS; (c) qualquer PRUNEPATH ou
    montagem-filha (fstype em PRUNEFS) que mora DENTRO do root — o subtree some
    do índice; (d) PRUNENAMES (poda POR NOME em qualquer profundidade — ver R3).
    É o que transforma o `--index` de otimista em honesto."""
    root = os.path.abspath(root)
    try:
        ents = mounts if mounts is not None else disks._read_mounts()
    except OSError:
        ents = []
    holes = []
    # (a) root inteiro sob um PRUNEPATH
    for p in conf.get("prunepaths", []):
        if _under(root, p):
            holes.append({"path": p, "reason": "prunepath"})
    # (b) fstype da montagem do próprio root
    _dev, mp, fstype = disks._mount_entry(root, ents)
    if fstype.lower() in conf.get("prunefs", set()):
        holes.append({"path": mp, "reason": f"prunefs:{fstype}"})
    # (c) PRUNEPATHs dentro do root
    for p in conf.get("prunepaths", []):
        if p != root and _under(p, root):
            holes.append({"path": p, "reason": "prunepath"})
    # (c') montagens-filhas com fstype podado
    for m in disks.mounts_under(root, ents):
        _d, _mp, mfs = disks._mount_entry(m, ents)
        if mfs.lower() in conf.get("prunefs", set()):
            holes.append({"path": m, "reason": f"prunefs:{mfs}"})
    # (d) PRUNENAMES (achado R3 do Fable): podam subárvores POR NOME em QUALQUER
    # profundidade (o clássico é o admin somar 'node_modules'/'.git'). O índice
    # omitiria esses galhos em silêncio — o mesmo "parcial podado" que a própria
    # objeção do Fable matou, voltando pela porta dos fundos. Regra SEM walk, com
    # paridade: se TODOS os prunenames começam com '.' E a busca não inclui ocultos,
    # a busca VIVA também os pularia (hidden) → cobertura íntegra, sem furo. Qualquer
    # prunename NÃO-oculto, ou ocultos com --hidden (aí a busca viva DESCERIA neles),
    # deixa buraco → recusa listando os nomes.
    pnames = conf.get("prunenames", [])
    if pnames and not (not include_hidden and all(n.startswith(".") for n in pnames)):
        for n in pnames:
            holes.append({"path": n, "reason": "prunename"})
    # dedup preservando ordem
    seen = set(); uniq = []
    for h in holes:
        k = (h["path"], h["reason"])
        if k not in seen:
            seen.add(k); uniq.append(h)
    return uniq


def index_date(db_path: str = PLOCATE_DB, _stat=os.stat) -> Optional[float]:
    """mtime (epoch) do banco do plocate, ou None se não há índice. É a 'data na
    cara' que o §4.A exige sempre visível no modo índice."""
    try:
        return _stat(db_path).st_mtime
    except OSError:
        return None


# ------------------------------------------------------------ busca indexada
def _plocate_run(args) -> bytes:
    """Executa `plocate` e devolve stdout (bytes, NUL-delimitado). Isolado p/ ser
    injetável no teste (sem depender do índice real da máquina)."""
    exe = engine._which("plocate") or "plocate"
    proc = subprocess.run([exe, *args], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL)
    return proc.stdout or b""


def index_available(db_path: str = PLOCATE_DB) -> bool:
    return bool(engine._which("plocate")) and os.path.exists(db_path)


def search_indexed(q: engine.Query, conf=None, mounts=None,
                   db_path: str = PLOCATE_DB, _run=_plocate_run,
                   _lstat=os.lstat, _conf_text=None):
    """Gera `engine.Match` (só nome) a partir do índice `plocate`, RECUSANDO com
    IndexError_ se a cobertura do root estiver furada (§4.A regra 2). Conteúdo não
    é indexável => recusa. Cada candidato é verificado VIVO por `lstat` (staleness,
    regra 4); os filtros de nome/profundidade/meta são os MESMOS da busca viva
    (`_name_matcher`/`_passes_meta`), então o resultado é idêntico ao da busca por
    nome menos o que sumiu do disco."""
    if q.content:
        raise IndexError_("o índice acelera busca por NOME; para CONTEÚDO use a "
                          "busca viva (sem --index).")
    if conf is None:
        if _conf_text is None:
            try:
                with open(UPDATEDB_CONF, encoding="utf-8") as f:
                    _conf_text = f.read()
            except OSError:
                _conf_text = ""
        conf = parse_updatedb_conf(_conf_text)

    match_name = engine._name_matcher(q)
    for root in q.paths:
        given = os.path.abspath(os.path.expanduser(root))
        # R4 (achado Fable): o plocate indexa e devolve caminhos REAIS (resolvidos).
        # Se o root É ou CONTÉM symlink (ex.: ~/repositorio -> /srv/dados), casar os
        # candidatos contra a forma dada os jogaria TODOS fora de _under → zero
        # resultados apresentado como "zero confiável", uma mentira. Casamos contra o
        # realpath; a cobertura também roda no real (senão o _mount_entry sobre o
        # symlink não veria o fstype/poda do alvo verdadeiro). Depois traduzimos o
        # prefixo de volta p/ `given`, pra UI não trocar o caminho do usuário.
        real = os.path.realpath(given)
        holes = index_coverage(real, conf, mounts, include_hidden=q.include_hidden)
        if holes:
            det = ", ".join(f"{h['path']} ({h['reason']})" for h in holes)
            raise IndexError_(
                f"'{given}' contém partes fora do índice: {det}. O resultado "
                f"indexado omitiria esse subtree em silêncio — use a busca viva.")
        base_depth = real.rstrip("/").count("/")
        eff_max = 1 if not q.recursive else q.max_depth
        translate = real != given            # root (ou ancestral) é symlink
        # plocate: casa o root como substring (delimita o subtree); -0 NUL p/ nome
        # com \n; sem -i pra não alargar (o casamento fino é do _name_matcher).
        out = _run(["-0", "--", real])
        for chunk in out.split(b"\x00"):
            if not chunk:
                continue
            path = os.fsdecode(chunk)          # bytes -> str (surrogateescape)
            # plocate casa em qualquer lugar do caminho; fixa no subtree do root real
            if not _under(path, real):
                continue
            base = os.path.basename(path)
            if not q.include_hidden and base.startswith("."):
                continue
            if not match_name(base):
                continue
            # profundidade no MESMO sentido do fd/os.walk: filho direto = 1. O
            # próprio root (depth 0) nunca é resultado (fd/walk não o retornam).
            depth = path.rstrip("/").count("/") - base_depth
            if depth < 1 or (eff_max is not None and depth > eff_max):
                continue
            try:
                st = _lstat(path)              # staleness: só sai se AINDA existe
            except OSError:
                continue                       # sumiu do disco desde o updatedb
            if not engine._passes_meta(q, st):
                continue
            # R4: devolve na forma que o usuário deu (prefixo real -> given)
            shown = given + path[len(real):] if translate else path
            yield engine.Match(path=shown, size=st.st_size, mtime=st.st_mtime,
                               is_dir=os.path.isdir(path))
