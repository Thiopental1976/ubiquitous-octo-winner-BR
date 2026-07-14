#!/usr/bin/env python3
"""Linux File Search — busca BOOLEANA de conteúdo (recurso-assinatura, F3).

Sintaxe:  (A OR B) AND C NOT D     [também: | & !, e adjacência = AND implícito]
  - termos entre "aspas" preservam espaços; termo cru vai até o próximo operador/parêntese
  - precedência:  NOT (unário) > AND > OR ;  parênteses agrupam
  - "AND NOT X" e "X NOT Y" funcionam (NOT binário vira A AND (NOT B))

Estratégia (casada com o desenho do Fable):
  1. parser -> AST
  2. cada TERMO -> conjunto de arquivos que o contêm, via `rg -l` (rápido) ou fallback Python
  3. AND=interseção, OR=união, NOT=universo−conjunto (universo só é calculado se preciso)
  4. passada final de exibição: pega as linhas dos termos POSITIVOS nos arquivos do resultado

Sem Qt aqui. O motor devolve Matches iguais aos de engine.py (a GUI/CLI reaproveitam).
"""
from __future__ import annotations
import os, re, json, subprocess, threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

try:                       # funciona como pacote (-m lfs.boolean / GUI) e flat (cli.py)
    from . import engine    # RG, Query, Match, _passes_meta, _iter_content_python
except ImportError:
    import engine


# ------------------------------------------------------------------ AST
@dataclass
class Term:  text: str
@dataclass
class Not:   node: object
@dataclass
class And:   a: object; b: object
@dataclass
class Or:    a: object; b: object


class BooleanError(ValueError):
    pass


# ------------------------------------------------------------------ tokenizer
_OPS = {"and": "AND", "or": "OR", "not": "NOT",
        "&": "AND", "&&": "AND", "|": "OR", "||": "OR", "!": "NOT"}

def tokenize(s: str):
    toks = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1; continue
        if c in "()":
            toks.append((c, c)); i += 1; continue
        if c == '"':                      # termo com aspas
            j = i + 1
            while j < n and s[j] != '"':
                j += 1
            toks.append(("TERM", s[i+1:j])); i = j + 1; continue
        if c in "&|":                     # & && | ||
            if i+1 < n and s[i+1] == c:
                toks.append((_OPS[c*2], c*2)); i += 2
            else:
                toks.append((_OPS[c], c)); i += 1
            continue
        if c == "!":
            toks.append(("NOT", "!")); i += 1; continue
        # palavra crua até espaço/operador/parêntese
        j = i
        while j < n and not s[j].isspace() and s[j] not in '()&|!"':
            j += 1
        word = s[i:j]
        low = word.lower()
        if low in _OPS:
            toks.append((_OPS[low], word))
        else:
            toks.append(("TERM", word))
        i = j
    return toks


# ------------------------------------------------------------------ parser (recursive descent)
class _P:
    def __init__(self, toks):
        self.t = toks; self.i = 0
    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)
    def eat(self):
        tok = self.peek(); self.i += 1; return tok

    def parse(self):
        if not self.t:
            raise BooleanError("expressão vazia")
        node = self.parse_or()
        if self.i != len(self.t):
            raise BooleanError(f"token inesperado: {self.peek()[1]!r}")
        return node

    def parse_or(self):
        node = self.parse_and()
        while self.peek()[0] == "OR":
            self.eat(); node = Or(node, self.parse_and())
        return node

    def parse_and(self):
        node = self.parse_not()
        while True:
            k = self.peek()[0]
            if k == "AND":
                self.eat(); node = And(node, self.parse_not())
            elif k == "NOT":                       # "A NOT B" = A AND (NOT B)
                self.eat(); node = And(node, Not(self.parse_not()))
            elif k in ("TERM", "("):               # adjacência = AND implícito
                node = And(node, self.parse_not())
            else:
                break
        return node

    def parse_not(self):
        if self.peek()[0] == "NOT":
            self.eat(); return Not(self.parse_not())
        return self.parse_atom()

    def parse_atom(self):
        k, v = self.peek()
        if k == "(":
            self.eat(); node = self.parse_or()
            if self.peek()[0] != ")":
                raise BooleanError("parêntese ')' faltando")
            self.eat(); return node
        if k == "TERM":
            self.eat(); return Term(v)
        raise BooleanError(f"esperava termo, veio {v!r}")


def parse(expr: str):
    return _P(tokenize(expr)).parse()


def positive_terms(node) -> list[str]:
    """Termos NÃO negados (p/ a passada de exibição das linhas)."""
    out = []
    def walk(n, neg):
        if isinstance(n, Term):
            if not neg: out.append(n.text)
        elif isinstance(n, Not):    walk(n.node, not neg)
        elif isinstance(n, (And, Or)):
            walk(n.a, neg); walk(n.b, neg)
    walk(node, False)
    # únicos preservando ordem
    seen = set(); uniq = []
    for t in out:
        if t not in seen: seen.add(t); uniq.append(t)
    return uniq


# ------------------------------------------------------------------ conjuntos de arquivos por termo
def _rg_base(q: engine.Query):
    cmd = [engine.RG]
    if not q.respect_gitignore: cmd.append("--no-ignore")
    if q.include_hidden:        cmd.append("--hidden")
    if q.follow_symlinks:       cmd.append("--follow")
    if q.one_file_system:       cmd.append("--one-file-system")
    if not q.case_sensitive:    cmd.append("--ignore-case")
    if q.whole_word:            cmd.append("--word-regexp")
    if not q.recursive:         cmd += ["--max-depth", "1"]
    elif q.max_depth is not None: cmd += ["--max-depth", str(q.max_depth)]
    if q.name_patterns and not q.name_is_regex:
        if not q.case_sensitive:                 # B2: glob insensível
            cmd.append("--glob-case-insensitive")
        for p in q.name_patterns: cmd += ["--glob", p]
    return cmd


_BATCH = 400   # caminhos por invocação do rg (evita estourar ARG_MAX — B4 e opt#1)


def _files_with_term(term: str, q: engine.Query, cancel, restrict=None) -> set[str]:
    """Arquivos que CONTÊM o termo (rg -l). Fallback Python se rg ausente.

    Opt#1 (AND progressivo): se `restrict` (lista de caminhos) é dado, varre SÓ
    esses arquivos — em lotes p/ não estourar o argv — em vez da árvore inteira.
    Retorna sempre um subconjunto de `restrict` quando ele é dado.
    """
    if not engine.RG:
        res = _files_with_term_py(term, q, cancel)
        return res & set(restrict) if restrict is not None else res
    base = _rg_base(q) + ["-l"]
    if not q.content_is_regex: base.append("--fixed-strings")
    base += ["-e", term]
    if restrict is None:
        batches = [list(q.paths)]                 # varredura da árvore toda
    else:
        rl = list(restrict)
        batches = [rl[i:i + _BATCH] for i in range(0, len(rl), _BATCH)]
    out = set()
    for roots in batches:
        if cancel(): break
        if not roots: continue
        cmd = base + ["--"] + roots
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True, errors="replace")
        except OSError:
            if restrict is None:
                return _files_with_term_py(term, q, cancel)
            continue                              # lote isolado falhou; segue os outros
        try:
            for line in proc.stdout:
                if cancel(): break
                fp = line.rstrip("\n")
                if fp: out.add(os.path.abspath(fp))
        finally:
            engine._reap(proc)                    # B1: nunca deixar rg órfão
    return out


def _files_with_term_py(term: str, q: engine.Query, cancel) -> set[str]:
    sub = engine.Query(**{**q.__dict__, "content": term})
    return {os.path.abspath(m.path) for m in engine._iter_content_python(sub, cancel)}


def _universe(q: engine.Query, cancel) -> set[str]:
    """Todos os arquivos candidatos (p/ resolver NOT). rg --files ou fd/os.walk."""
    if engine.RG:
        cmd = _rg_base(q) + ["--files", "--"] + q.paths
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True, errors="replace")
        except OSError:
            proc = None
        if proc:
            out = set()
            try:
                for line in proc.stdout:
                    if cancel(): break
                    fp = line.rstrip("\n")
                    if fp: out.add(os.path.abspath(fp))
            finally:
                engine._reap(proc)                # B1
            return out
    return {os.path.abspath(m.path) for m in engine._iter_names_python(q)}


# ------------------------------------------------------------------ concorrência (opt#2)
_cache_lock = threading.Lock()               # protege cache/universo entre threads
_WORKERS = int(os.environ.get("LFS_WORKERS", "3") or "3")   # afinável por env
# Pontos de montagem onde discos SMR/USB do acervo costumam viver: seek concorrente
# os castiga, então buscas AQUI são SERIALIZADAS (1 processo por vez).
_MNT_PREFIXES = ("/mnt", "/media", "/run/media")


def _under_mount(ap: str) -> bool:
    return any(ap == pre or ap.startswith(pre + os.sep) for pre in _MNT_PREFIXES)


def _max_workers(q: engine.Query) -> int:
    """Opt#2: paraleliza scans independentes (OR) no i7, MAS serializa em /mnt —
    os SMR/USB do acervo odeiam seek concorrente."""
    if _WORKERS <= 1:
        return 1
    if any(_under_mount(os.path.abspath(p)) for p in q.paths):
        return 1                             # trava SMR: uma varredura por vez
    return _WORKERS


# ------------------------------------------------------------------ progresso por fase (opt#4)
class _Phase:
    """Relata a etapa atual da busca booleana ('passo 2/4: termo "paciente"').
    Cada termo DISTINTO conta como um passo; a extração de linhas é o passo final.
    Thread-safe: a numeração fica consistente mesmo com OR avaliado em paralelo
    (opt#2) — o `_lock` serializa a contagem, o I/O é que roda concorrente."""
    def __init__(self, on_phase, total, has_display):
        self._on = on_phase or (lambda d, t, label: None)
        self.total = total
        self._done = 0
        self._seen = set()
        self._lock = threading.Lock()
        self.has_display = has_display

    def term(self, term):
        """Anuncia (só uma vez por termo) que ele começou a ser varrido do disco."""
        with self._lock:
            if term in self._seen:
                return
            self._seen.add(term)
            self._done += 1
            d = self._done
        self._safe(d, f"termo “{term}”")

    def note(self, label):
        """Passo informativo (ex.: listar universo p/ NOT) sem consumir numeração."""
        with self._lock:
            d = self._done
        self._safe(d, label)

    def finish_display(self):
        self._safe(self.total, "extraindo linhas")

    def _safe(self, d, label):
        try:
            self._on(d, self.total, label)
        except Exception:
            pass


def _all_terms(node):
    """Todos os termos do AST (positivos E negados) — cada scan distinto é um passo."""
    if isinstance(node, Term):
        return [node.text]
    if isinstance(node, Not):
        return _all_terms(node.node)
    if isinstance(node, (And, Or)):
        return _all_terms(node.a) + _all_terms(node.b)
    return []


# ------------------------------------------------------------------ avaliação do AST
def _universe_cached(q, cancel, universe_box, phase=None):
    with _cache_lock:
        if universe_box[0] is not None:
            return universe_box[0]
    if phase is not None:
        phase.note("listando arquivos (NOT)")
    u = _universe(q, cancel)                 # I/O fora do lock
    with _cache_lock:
        if universe_box[0] is None:
            universe_box[0] = u
        return universe_box[0]


def _term_set(term, q, cancel, cache, restrict, phase=None):
    """Conjunto de arquivos que contêm o termo.
    Sem restrição: usa/preenche o cache com o conjunto CHEIO (reuso entre nós).
    Com restrição (opt#1): intersecta o cache se já houver, senão varre SÓ os
    arquivos de `restrict` — o resultado é subconjunto e NÃO polui o cache.
    Thread-safe (opt#2): o I/O roda fora do lock; numa corrida, o pior caso é
    recalcular o mesmo conjunto (idempotente) e `setdefault` mantém um só.
    Opt#4: anuncia a fase só quando VAI varrer o disco (cache hit é instantâneo)."""
    if restrict is None:
        with _cache_lock:
            hit = cache.get(term)
        if hit is None:
            if phase is not None: phase.term(term)
            hit = _files_with_term(term, q, cancel)
            with _cache_lock:
                cache.setdefault(term, hit)
                hit = cache[term]
        return hit
    with _cache_lock:
        hit = cache.get(term)
    if hit is not None:
        return hit & restrict
    if phase is not None: phase.term(term)
    return _files_with_term(term, q, cancel, restrict=restrict)


def _or_operands(node):
    """Achata uma cadeia de OR em operandos independentes (p/ avaliar em paralelo)."""
    if isinstance(node, Or):
        return _or_operands(node.a) + _or_operands(node.b)
    return [node]


def _eval(node, q, cancel, cache, universe_box, restrict=None, pool=None, phase=None):
    """Avalia o AST -> conjunto de arquivos.

    Opt#1 (AND com restrição progressiva): o lado esquerdo de um AND vira o
    `restrict` do lado direito, que passa a varrer só esses arquivos em vez da
    árvore inteira. É correto para AND/OR/NOT porque a interseção distribui:
    (X∘Y)∩R = (X∩R)∘(Y∩R). O termo mais à esquerda é a única varredura cheia.

    Opt#2 (termos independentes em paralelo): os operandos de um OR são
    independentes e rodam concorrentes num ThreadPool — EXCETO em /mnt, onde o
    `pool` chega None (serializado). Subtarefas submetidas recebem pool=None:
    só o nível de OR alcançado pela recursão na thread principal paraleliza,
    o que evita fome de workers (deadlock de pool aninhado)."""
    if isinstance(node, Term):
        return _term_set(node.text, q, cancel, cache, restrict, phase)
    if isinstance(node, And):
        sa = _eval(node.a, q, cancel, cache, universe_box, restrict, pool, phase)
        if not sa:
            return set()                     # curto-circuito: nada satisfaz o AND
        return _eval(node.b, q, cancel, cache, universe_box, restrict=sa, pool=pool, phase=phase)
    if isinstance(node, Or):
        ops = _or_operands(node)
        if pool is not None and len(ops) > 1 and not cancel():
            futs = [pool.submit(_eval, op, q, cancel, cache, universe_box, restrict, None, phase)
                    for op in ops]
            out = set()
            for f in futs:
                out |= f.result()            # aguarda todas (cada uma respeita cancel/_reap)
            return out
        out = set()
        for op in ops:
            if cancel(): break
            out |= _eval(op, q, cancel, cache, universe_box, restrict, pool, phase)
        return out
    if isinstance(node, Not):
        if restrict is None:                 # NOT no topo: universo − termo (varredura cheia)
            univ = _universe_cached(q, cancel, universe_box, phase)
            return univ - _eval(node.node, q, cancel, cache, universe_box, restrict=None, pool=pool, phase=phase)
        # NOT dentro de um AND: já restrito ao acumulado, subtrai o que casa nele
        return restrict - _eval(node.node, q, cancel, cache, universe_box, restrict=restrict, pool=pool, phase=phase)
    raise BooleanError("nó desconhecido")


# ------------------------------------------------------------------ API pública
def search_boolean(q: engine.Query, expr: str, on_result, cancel=lambda: False,
                   on_progress=lambda n: None, on_phase=None):
    """Resolve a expressão booleana -> arquivos, então emite Matches com linhas
    dos termos positivos. Retorna (total, segundos).

    Opt#4: `on_phase(done, total, label)` relata a etapa atual — 'passo 2/4:
    termo "paciente"' e, por fim, 'passo 4/4: extraindo linhas'. Cada termo
    DISTINTO é um passo; a extração de linhas dos positivos é o passo final."""
    import time
    t0 = time.time()
    ast = parse(expr)
    cache: dict = {}
    universe_box = [None]
    pos = positive_terms(ast)
    # opt#4: passos = termos distintos (positivos e negados) + 1 (extração de linhas)
    n_terms = len(dict.fromkeys(_all_terms(ast)))
    phase = _Phase(on_phase, n_terms + (1 if pos else 0), bool(pos))
    workers = _max_workers(q)                # opt#2: paraleliza OR fora de /mnt
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            files = _eval(ast, q, cancel, cache, universe_box, pool=pool, phase=phase)
    else:
        files = _eval(ast, q, cancel, cache, universe_box, phase=phase)
    # B3: filtro de nome por REGEX (o glob já vai pro rg; regex é pós-filtro no basename)
    if q.name_is_regex and q.name_patterns:
        nrx = re.compile(q.name_patterns[0], 0 if q.case_sensitive else re.IGNORECASE)
        files = {f for f in files if nrx.search(os.path.basename(f))}

    # passada de exibição: linhas dos termos positivos, só nos arquivos do resultado
    n = 0
    files_sorted = sorted(files)
    if pos and not cancel():
        phase.finish_display()               # opt#4: último passo
    lines_by_file = _display_lines(pos, files_sorted, q, cancel) if pos else {}
    for fp in files_sorted:
        if cancel(): break
        try:
            st = os.stat(fp)
        except OSError:
            continue
        if not engine._passes_meta(q, st):
            continue
        m = engine.Match(fp, st.st_size, st.st_mtime)
        for ln, txt in lines_by_file.get(fp, []):
            m.lines.append((ln, txt)); m.nmatch += 1
        on_result(m)
        n += 1
        if n % 25 == 0: on_progress(n)
        if n >= q.max_results: break
    return n, time.time() - t0


def _display_lines(pos_terms, files, q: engine.Query, cancel) -> dict:
    """Para os arquivos-resultado, extrai linhas que casam QUALQUER termo positivo.
    B4: processa em lotes p/ não estourar o argv (60k caminhos matariam o exec)."""
    if not files or not engine.RG:
        return {}
    base = _rg_base(q) + ["--json"]
    if not q.content_is_regex: base.append("--fixed-strings")
    for t in pos_terms: base += ["-e", t]
    res: dict = {}
    for i in range(0, len(files), _BATCH):
        if cancel(): break
        cmd = base + ["--"] + files[i:i + _BATCH]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True, errors="replace")
        except OSError:
            continue
        try:
            for line in proc.stdout:
                if cancel(): break
                try: ev = json.loads(line)
                except ValueError: continue
                if ev.get("type") == "match":
                    path = ev["data"]["path"].get("text")
                    if path is None: continue
                    path = os.path.abspath(path)
                    lst = res.setdefault(path, [])
                    if len(lst) < 200:
                        ln = ev["data"].get("line_number") or 0
                        txt = ev["data"]["lines"].get("text", "").rstrip("\n")
                        lst.append((ln, txt))
        finally:
            engine._reap(proc)                    # B1: nunca deixar rg órfão
    return res


if __name__ == "__main__":
    import sys
    expr = sys.argv[2] if len(sys.argv) > 2 else '(def OR class) AND import NOT test'
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    q = engine.Query(paths=[root], name_patterns=["*.py"])
    print("AST:", parse(expr))
    print("positivos:", positive_terms(parse(expr)))
    tot, dt = search_boolean(q, expr,
        lambda m: print(f"{m.nmatch:>3} linhas  {m.path}"))
    print(f"\n{tot} arquivos em {dt:.3f}s")
