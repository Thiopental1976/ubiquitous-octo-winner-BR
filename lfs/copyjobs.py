#!/usr/bin/env python3
# Sombrero File Search — Copyright (C) 2026 Rodrigo Toledo
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fila de cópia que sobrevive ao fechamento (F10b #5 do desenho do Fable).

Sem Qt, sem I/O de arquivo aqui — só a (de)serialização dos jobs para dentro do
`config.json` que o app já grava. Assim o núcleo é testável headless (o teste do
kill -9 roda contra `fileops.copy_to`, não contra a GUI).

Um job é a tripla que o worker já entende: `(sources, dest, sanitize)`. Persistir
a FILA (não o progresso por arquivo) basta porque a cópia é idempotente por
desenho:

  - a escrita ATOMIC grava em `.sombrero-part` e só então faz `os.replace`, então
    nunca há meio-arquivo passando por inteiro — reexecutar um job é seguro;
  - arquivo já concluído vira CONFLITO na retomada (a política do usuário decide;
    o padrão da GUI é Pular), então retomar não duplica nem sobrescreve às cegas;
  - um `.sombrero-part` órfão é lixo reconhecível, não um arquivo válido.

Por isso o snapshot é gravado a cada TRANSIÇÃO de job (enfileirou / começou /
terminou), nunca por arquivo — barato e suficiente.
"""
from __future__ import annotations

from typing import List, Tuple

Job = Tuple[List[str], str, bool]
_KEY = "copy_queue"


def to_dict(job: Job) -> dict:
    sources, dest, sanitize = job
    return {"sources": list(sources), "dest": dest, "sanitize": bool(sanitize)}


def from_dict(d: dict):
    """Volta um dict do config para a tripla `(sources, dest, sanitize)`, ou None
    se estiver malformado — um config editado à mão não pode derrubar o arranque."""
    if not isinstance(d, dict):
        return None
    dest = d.get("dest")
    sources = d.get("sources")
    if not isinstance(dest, str) or not dest:
        return None
    if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
        return None
    sources = [s for s in sources if s]
    if not sources:
        return None
    return (sources, dest, bool(d.get("sanitize", False)))


def snapshot(cfg: dict, jobs) -> dict:
    """Grava no `cfg` (in-place, e devolve-o) a fila de jobs AINDA não concluídos —
    o em andamento primeiro. Vazio => remove a chave (nada pendente = nada a
    perguntar na próxima abertura)."""
    serial = [to_dict(j) for j in jobs]
    if serial:
        cfg[_KEY] = serial
    else:
        cfg.pop(_KEY, None)
    return cfg


def pending(cfg: dict) -> List[Job]:
    """Jobs pendentes gravados numa sessão anterior, já validados. [] se não há."""
    raw = cfg.get(_KEY)
    if not isinstance(raw, list):
        return []
    out = []
    for d in raw:
        job = from_dict(d)
        if job is not None:
            out.append(job)
    return out


def clear(cfg: dict) -> dict:
    """Descarta a fila persistida (o [Descartar] do prompt de retomada)."""
    cfg.pop(_KEY, None)
    return cfg
