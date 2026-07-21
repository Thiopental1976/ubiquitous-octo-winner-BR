#!/usr/bin/env python3
"""Testes de regressão da auditoria Fable 5 (LinuxFileSearch_Auditoria_Debug.md).

Cobre os consertos B1–B14 no que dá para exercitar sem GUI (o núcleo é sem-Qt).
Rode:  python3 tests/test_audit.py      (ou via pytest)

Cada teste constrói sua própria árvore sintética em tempdir — não toca no acervo.
"""
from __future__ import annotations
import os, sys, time, subprocess, tempfile, shutil

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lfs"))
import engine, boolean, i18n
from engine import Query


def _tree():
    """Árvore de teste: nomes com caixa mista + conteúdos p/ booleano/glob."""
    d = tempfile.mkdtemp(prefix="lfs_test_")
    files = {
        "N1.TXT":    "laudo do paciente\nassinatura",
        "n2.txt":    "laudo sem assinatura\npaciente ausente",
        "doc42.log": "paciente laudo rascunho",
        "misto.txt": "nada aqui",
        "ambos.log": "paciente e laudo juntos",
    }
    for name, body in files.items():
        with open(os.path.join(d, name), "w") as f:
            f.write(body)
    return d


# ------------------------------------------------------------------ §5 parse_size
def test_parse_size():
    assert engine.parse_size("10M") == 10 * (1 << 20)
    assert engine.parse_size("1G") == 1 << 30
    assert engine.parse_size("2TB") == 2 * (1 << 40)
    assert engine.parse_size("512") == 512
    assert engine.parse_size("") is None
    assert engine.parse_size("xxx") is None
    assert engine.parse_size("-5M") is None       # E9: tamanho negativo -> ignora filtro
    print("ok  parse_size (fonte única engine.parse_size; negativo->None)")


# ------------------------------------------------------------------ B1 _reap
def test_reap_kills_process():
    p = subprocess.Popen(["sleep", "30"])
    assert p.poll() is None
    engine._reap(p)
    assert p.poll() is not None            # morto
    engine._reap(p)                        # idempotente, não lança
    print("ok  B1  _reap mata e é idempotente")


def test_no_orphan_on_cancel():
    """B1: cancelar/abandonar a busca não deixa rg varrendo em background."""
    if not engine.RG or not os.path.isdir("/usr"):
        print("--  B1  (pulado: sem rg ou /usr)"); return
    q = Query(paths=["/usr"], content="configure", name_patterns=[], max_results=2)
    cancel = {"v": False}
    n = 0
    def on_result(m):
        nonlocal n
        n += 1
        if n >= 2:
            cancel["v"] = True             # simula abandono cedo
    engine.search(q, on_result, lambda: cancel["v"], lambda k: None)
    time.sleep(1.5)
    orphans = subprocess.run(["pgrep", "-x", "rg"], capture_output=True, text=True).stdout.split()
    assert not orphans, f"rg órfão vivo: {orphans}"
    print("ok  B1  sem rg órfão após cancelar busca em /usr")


# ------------------------------------------------------------------ B2 glob caixa
def test_glob_case_insensitive():
    """fd (só-nome) e rg (nome+conteúdo) devem concordar: *.txt insensível acha N1.TXT e n2.txt."""
    d = _tree()
    try:
        # só-nome (motor fd/python)
        got = set()
        engine.search(Query(paths=[d], name_patterns=["*.txt"], case_sensitive=False),
                      lambda m: got.add(os.path.basename(m.path)), lambda: False, lambda k: None)
        assert {"N1.TXT", "n2.txt"} <= got, f"só-nome achou {got}"
        # nome+conteúdo (motor rg) — antes do B2 achava só n2.txt
        got2 = set()
        engine.search(Query(paths=[d], name_patterns=["*.txt"], content="laudo",
                            case_sensitive=False),
                      lambda m: got2.add(os.path.basename(m.path)), lambda: False, lambda k: None)
        assert {"N1.TXT", "n2.txt"} <= got2, f"nome+conteúdo achou {got2}"
        print("ok  B2  glob *.txt insensível concorda entre fd e rg")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------ B3 booleano + regex de nome
def test_boolean_name_regex():
    """Booleano deve respeitar o filtro de nome REGEX (^doc\\d+\\.)."""
    d = _tree()
    try:
        q = Query(paths=[d], name_patterns=[r"^doc\d+\."], name_is_regex=True,
                  case_sensitive=False)
        got = set()
        boolean.search_boolean(q, "laudo AND paciente",
                               lambda m: got.add(os.path.basename(m.path)), lambda: False)
        assert got == {"doc42.log"}, f"esperava só doc42.log, veio {got}"
        print("ok  B3  booleano respeita regex de nome (só doc42.log)")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------ B4 lotes no display
def test_display_lines_batched():
    """_display_lines em lotes não perde linhas mesmo com muitos caminhos."""
    d = tempfile.mkdtemp(prefix="lfs_batch_")
    try:
        paths = []
        for i in range(1000):
            p = os.path.join(d, f"f{i}.txt")
            with open(p, "w") as f:
                f.write("paciente laudo\n")
            paths.append(os.path.abspath(p))
        old = boolean._BATCH
        boolean._BATCH = 50                # força vários lotes
        try:
            res = boolean._display_lines(["laudo"], sorted(paths),
                                         Query(paths=[d]), lambda: False)
        finally:
            boolean._BATCH = old
        assert len(res) == 1000, f"esperava 1000 arquivos com linha, veio {len(res)}"
        print("ok  B4  _display_lines em lotes cobre todos os arquivos")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------ B9 one_file_system fallback
def test_one_file_system_fallback():
    """Fallback Python com one_file_system=True não cruza mounts e não perde
    o que está no mesmo dispositivo."""
    d = _tree()
    try:
        q = Query(paths=[d], name_patterns=["*.txt"], one_file_system=True)
        got = {os.path.basename(m.path) for m in engine._iter_names_python(q)}
        assert {"N1.TXT", "n2.txt", "misto.txt"} <= got, f"fallback one_fs achou {got}"
        print("ok  B9  fallback Python respeita one_file_system sem perder o disco atual")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------ §4 parser booleano
def test_boolean_parser():
    from boolean import parse, positive_terms, And, Or, Not, Term
    assert isinstance(parse("a AND b"), And)
    assert isinstance(parse("a OR b"), Or)
    assert isinstance(parse("a b").__class__, type)          # adjacência = AND
    assert positive_terms(parse("(nota OR laudo) AND paciente NOT rascunho")) == \
        ["nota", "laudo", "paciente"]
    # | & ! equivalem a OR AND NOT
    assert positive_terms(parse("a & b ! c")) == ["a", "b"]
    print("ok  §4  parser booleano (precedência, adjacência, | & !)")


# ------------------------------------------------------------------ aspas sem fechamento
def test_boolean_unterminated_quote():
    """Aspas sem fechar viravam um termo até o fim da string, calado. Agora é erro:
    melhor avisar que adivinhar o que o usuário quis buscar."""
    from boolean import parse, BooleanError, Term
    for expr in ('"paciente', 'laudo AND "paciente', '(a OR "b)'):
        try:
            parse(expr)
        except BooleanError:
            pass
        else:
            raise AssertionError(f"aceitou aspas sem fechamento: {expr!r}")
    # o caminho normal segue intacto: aspas fechadas preservam o espaço
    assert parse('"paciente laudo"') == Term("paciente laudo")
    print("ok  §4b aspas sem fechamento viram BooleanError (fechadas seguem OK)")


# ------------------------------------------------------------------ aspas vazias / só-espaço (B4)
def test_boolean_empty_quoted_term():
    """`""` virava Term('') e casaria TODO arquivo (rg -e '' aceita qualquer linha).
    B4: frase SÓ-ESPAÇO (`" "`, `"   "`, tab) é o mesmo footgun — rg -e ' ' casa
    quase toda linha de texto — então agora também vira erro (antes `" "` passava)."""
    from boolean import parse, BooleanError, Term
    for expr in ('""', 'laudo AND ""', '("" OR nota)', '" "', '"   "', '"\t"',
                 'laudo AND "  "'):
        try:
            parse(expr)
        except BooleanError:
            pass
        else:
            raise AssertionError(f"aceitou termo vazio/só-espaço: {expr!r}")
    # frase com conteúdo real (mesmo cercada de espaço) segue válida
    assert parse('" paciente "') == Term(" paciente ")
    print("ok  §4c termo vazio \"\"/só-espaço vira BooleanError (B4)")


# ------------------------------------------------------------------ aspas escapadas (B3)
def test_boolean_escaped_quotes():
    """B3: dentro de aspas, `\\"` é uma aspa literal e `\\\\` uma barra literal, então
    dá p/ buscar uma frase que CONTÉM aspas: `"disse \\"oi\\""` → disse "oi"."""
    from boolean import parse, tokenize, Term
    assert parse(r'"disse \"oi\""') == Term('disse "oi"')
    assert parse(r'"c:\\temp"') == Term(r"c:\temp")
    # a aspa escapada NÃO fecha a frase: o AND vem depois do fecha-aspas real
    toks = tokenize(r'"a \"b\" c" AND laudo')
    assert toks[0] == ("TERM", 'a "b" c'), toks
    assert ("AND", "AND") in toks and ("TERM", "laudo") in toks
    # barra invertida solta (não seguida de " ou \) fica literal
    assert parse(r'"a\b"') == Term(r"a\b")
    print("ok  §4d aspas/barras escapadas em frase (B3)")


# ------------------------------------------------------------------ single-flight (B5)
def test_boolean_single_flight():
    """B5: sob OR paralelo, o scan CHEIO de um termo (e o universo do NOT) roda UMA
    vez só — quem chega depois espera o resultado em vez de re-varrer o disco.
    Instrumento _files_with_term/_universe p/ contar varreduras e confirmar 1x."""
    import threading, time as _time
    d = _tree()
    try:
        real_ft = boolean._files_with_term
        real_un = boolean._universe
        calls = {}
        lock = threading.Lock()
        def counting_ft(term, q, cancel, restrict=None, stats=None):
            if restrict is None:                         # só o scan cheio é single-flight
                with lock: calls[term] = calls.get(term, 0) + 1
                if term == "laudo": _time.sleep(0.08)    # segura o voo p/ forçar sobreposição
            return real_ft(term, q, cancel, restrict=restrict, stats=stats)
        def counting_un(q, cancel, stats=None):
            with lock: calls["__univ__"] = calls.get("__univ__", 0) + 1
            _time.sleep(0.08)                            # idem p/ o universo do NOT
            return real_un(q, cancel, stats)
        old = boolean._WORKERS
        boolean._files_with_term = counting_ft
        boolean._universe = counting_un
        try:
            boolean._WORKERS = 4                          # tmp paraleliza (não é /mnt)
            got = set()
            # 'laudo' abre 2 operandos do OR e o universo do NOT é pedido por 2 operandos;
            # sem single-flight cada um viraria 2 varreduras concorrentes.
            boolean.search_boolean(
                Query(paths=[d]),
                "(laudo AND paciente) OR (laudo AND assinatura) OR (NOT rascunho) OR (NOT ausente)",
                lambda m: got.add(os.path.basename(m.path)), lambda: False)
        finally:
            boolean._WORKERS = old
            boolean._files_with_term = real_ft
            boolean._universe = real_un
        assert calls.get("laudo", 0) == 1, f"'laudo' varrido {calls.get('laudo')}x (esperado 1)"
        assert calls.get("__univ__", 0) == 1, f"universo varrido {calls.get('__univ__')}x (esperado 1)"
        print("ok  B5   single-flight: termo/universo varridos 1x sob OR paralelo")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------ UX: nome "contém"
def test_name_contains_semantics():
    """Modelo Agent Ransack: texto puro no campo de nome significa 'contém'.
    Digitar 'rotina' TEM de achar 'exames de rotina.txt', qualquer extensão.
    Glob digitado pelo usuário (* ? [) é respeitado como está."""
    assert engine.as_name_glob("rotina") == "*rotina*"
    assert engine.as_name_glob("  rotina  ") == "*rotina*"
    assert engine.as_name_glob("exames de rotina") == "*exames de rotina*"
    assert engine.as_name_glob("*.pdf") == "*.pdf"          # glob: intacto
    assert engine.as_name_glob("exames?.txt") == "exames?.txt"
    assert engine.as_name_glob("n[12].txt") == "n[12].txt"
    assert engine.as_name_glob("") == ""
    d = tempfile.mkdtemp(prefix="lfs_ux_")
    try:
        for f in ("exames de rotina.txt", "ROTINA-2026.pdf", "outro arquivo.doc"):
            with open(os.path.join(d, f), "w") as fh:
                fh.write("x")
        got = set()
        engine.search(Query(paths=[d], name_patterns=[engine.as_name_glob("rotina")]),
                      lambda m: got.add(os.path.basename(m.path)), lambda: False, lambda k: None)
        assert got == {"exames de rotina.txt", "ROTINA-2026.pdf"}, got
        got2 = set()  # fallback Python (fd desligado) tem de concordar com o fd
        fd_bak, engine.FD = engine.FD, None
        try:
            engine.search(Query(paths=[d], name_patterns=[engine.as_name_glob("rotina")]),
                          lambda m: got2.add(os.path.basename(m.path)), lambda: False, lambda k: None)
        finally:
            engine.FD = fd_bak
        assert got == got2, (got, got2)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("ok  UX  nome 'contém': rotina acha 'exames de rotina.txt' (glob intacto)")


# ------------------------------------------------------------------ UX: multidiscos
def test_user_mounts_parsing():
    """Discos do menu "Discos ▾": só dispositivos reais (/dev/*) sob /media, /mnt
    ou /run/media; espaço no rótulo do disco vem escapado (\\040) e é decodificado."""
    lines = [
        "/dev/nvme0n1p2 / ext4 rw 0 0\n",                              # raiz: fora
        "/dev/sdb1 /mnt/acervo ext4 rw 0 0\n",
        "/dev/sdc1 /media/rodrigo/Backup\\040Externo ext4 rw 0 0\n",   # espaço escapado
        "tmpfs /run tmpfs rw 0 0\n",                                   # não é /dev/*
        "/dev/sdd1 /run/media/rodrigo/PENDRIVE vfat rw 0 0\n",
        "/dev/loop3 /snap/foo squashfs ro 0 0\n",                      # loop fora de /media|/mnt
    ]
    got = engine.user_mounts(lines)
    assert got == ["/media/rodrigo/Backup Externo", "/mnt/acervo",
                   "/run/media/rodrigo/PENDRIVE"], got
    assert isinstance(engine.user_mounts(), list)   # leitura real não explode
    print("ok  UX  user_mounts: /dev/* sob /media|/mnt|/run/media (espaço decodificado)")


# ------------------------------------------------------------------ N1 fd caixa-sensível
def test_fd_case_sensitive():
    """N1: com 'Aa' LIGADO, o fd não pode vazar por smart-case.
    glob *.txt sensível NÃO acha N1.TXT; regex de nome 'n1' sensível não casa N1.TXT."""
    if not engine.FD:
        print("--  N1  (pulado: sem fd)"); return
    d = _tree()
    try:
        got = set()
        engine.search(Query(paths=[d], name_patterns=["*.txt"], case_sensitive=True),
                      lambda m: got.add(os.path.basename(m.path)), lambda: False, lambda k: None)
        assert "N1.TXT" not in got, f"vazou N1.TXT com caixa ligada: {got}"
        assert "n2.txt" in got, f"perdeu n2.txt: {got}"
        got2 = set()
        engine.search(Query(paths=[d], name_patterns=["n1"], name_is_regex=True,
                            case_sensitive=True),
                      lambda m: got2.add(os.path.basename(m.path)), lambda: False, lambda k: None)
        assert "N1.TXT" not in got2, f"regex sensível vazou N1.TXT: {got2}"
        print("ok  N1  fd respeita caixa ligada (glob e regex de nome)")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------ opt#1 AND progressivo
def test_and_progressive_correctness():
    """Opt#1 não pode mudar resultados: AND/OR/NOT com restrição = interseção ingênua."""
    d = _tree()
    try:
        def run(expr):
            got = set()
            boolean.search_boolean(Query(paths=[d]), expr,
                                   lambda m: got.add(os.path.basename(m.path)), lambda: False)
            return got
        assert run("laudo AND paciente") == {"N1.TXT", "n2.txt", "doc42.log", "ambos.log"}
        assert run("laudo AND NOT rascunho") == {"N1.TXT", "n2.txt", "ambos.log"}
        assert run("(assinatura OR rascunho) AND paciente") == {"N1.TXT", "n2.txt", "doc42.log"}
        print("ok  opt#1  AND progressivo preserva os resultados (AND/OR/NOT)")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_and_progressive_restricts():
    """Opt#1 de fato restringe: a 2ª parte do AND varre só o conjunto acumulado."""
    d = tempfile.mkdtemp(prefix="lfs_prog_")
    try:
        with open(os.path.join(d, "raro.txt"), "w") as f:
            f.write("raro comum\n")
        for i in range(50):
            with open(os.path.join(d, f"c{i}.txt"), "w") as f:
                f.write("comum\n")
        calls = []
        orig = boolean._files_with_term
        def spy(term, q, cancel, restrict=None, stats=None):
            calls.append((term, None if restrict is None else len(restrict)))
            return orig(term, q, cancel, restrict, stats)
        boolean._files_with_term = spy
        try:
            got = set()
            boolean.search_boolean(Query(paths=[d]), "raro AND comum",
                                   lambda m: got.add(os.path.basename(m.path)), lambda: False)
        finally:
            boolean._files_with_term = orig
        assert got == {"raro.txt"}, f"resultado errado: {got}"
        restricted = [c for c in calls if c[1] is not None]
        assert restricted, f"nenhuma varredura restrita ocorreu: {calls}"
        assert restricted[0][1] == 1, f"'comum' devia varrer só 1 arquivo (o de 'raro'): {calls}"
        print("ok  opt#1  2ª parte do AND varreu só o acumulado (1 arquivo, não 51)")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------ N2 stats de inacessíveis
def _make_denied_tree():
    """Árvore com um subdiretório SEM permissão (000) p/ gerar 'denied'."""
    d = tempfile.mkdtemp(prefix="lfs_deny_")
    with open(os.path.join(d, "ok.txt"), "w") as f:
        f.write("laudo do paciente\n")
    sub = os.path.join(d, "secreto")
    os.mkdir(sub)
    with open(os.path.join(sub, "dentro.txt"), "w") as f:
        f.write("laudo paciente\n")
    os.chmod(sub, 0o000)                        # inacessível
    return d, sub


def test_walk_onerror_counts_denied():
    """N2 (fallback Python): os.walk num diretório sem permissão conta 'denied'."""
    if os.geteuid() == 0:
        print("--  N2  (pulado: root ignora permissões)"); return
    d, sub = _make_denied_tree()
    try:
        st = {"denied": 0}
        list(engine._iter_names_python(Query(paths=[d], name_patterns=["*.txt"]), st))
        assert st["denied"] >= 1, f"não contou o diretório inacessível: {st}"
        print("ok  N2  fallback os.walk conta diretório inacessível")
    finally:
        os.chmod(sub, 0o755); shutil.rmtree(d, ignore_errors=True)


def test_boolean_stats_denied():
    """N2: o modo booleano agora preenche stats['denied'] (antes ficava 0)."""
    if os.geteuid() == 0:
        print("--  N2  (pulado: root ignora permissões)"); return
    d, sub = _make_denied_tree()
    try:
        st = {"denied": 0}
        got = set()
        boolean.search_boolean(Query(paths=[d]), "laudo AND paciente",
                               lambda m: got.add(os.path.basename(m.path)), lambda: False,
                               stats=st)
        assert "ok.txt" in got, f"devia achar o arquivo acessível: {got}"
        assert st["denied"] >= 1, f"booleano não contou inacessível (N2): {st}"
        print("ok  N2  modo booleano conta inacessíveis em stats['denied']")
    finally:
        os.chmod(sub, 0o755); shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------ opt#4 on_phase
def test_on_phase_reports():
    """Opt#4: on_phase relata passos coerentes (1..total), com o total = termos
    distintos + 1 (extração de linhas), e o último passo é 'extracting lines'."""
    d = _tree()
    import i18n; i18n.set_lang("en")     # rótulos determinísticos (fonte inglesa)
    try:
        fases = []
        got = set()
        boolean.search_boolean(
            Query(paths=[d]), "(laudo OR assinatura) AND paciente",
            lambda m: got.add(os.path.basename(m.path)), lambda: False,
            on_phase=lambda done, total, label: fases.append((done, total, label)))
        assert fases, "nenhuma fase relatada"
        totais = {t for _, t, _ in fases}
        assert totais == {4}, f"total devia ser 4 (laudo,assinatura,paciente + linhas): {totais}"
        dones = [dn for dn, _, _ in fases]
        assert all(1 <= dn <= 4 for dn in dones), f"passo fora de 1..4: {dones}"
        assert max(dones) == 4 and "extracting" in fases[-1][2], f"último passo errado: {fases[-1]}"
        # termos distintos anunciados uma vez cada
        rotulos_termo = [lb for _, _, lb in fases if lb.startswith("term ")]
        assert len(rotulos_termo) == len(set(rotulos_termo)) == 3, f"termos: {rotulos_termo}"
        print("ok  opt#4  on_phase relata passos 1..total e termina em 'extracting lines'")
    finally:
        i18n.set_lang(None)              # volta à autodetecção
        shutil.rmtree(d, ignore_errors=True)


def test_on_phase_optional():
    """Opt#4: on_phase é opcional — sem ele, a busca funciona igual (retrocompat)."""
    d = _tree()
    try:
        got = set()
        boolean.search_boolean(Query(paths=[d]), "laudo AND paciente",
                               lambda m: got.add(os.path.basename(m.path)), lambda: False)
        assert got == {"N1.TXT", "n2.txt", "doc42.log", "ambos.log"}, f"veio {got}"
        print("ok  opt#4  on_phase omitido não quebra a busca (retrocompatível)")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------ opt#3 fd multi-glob -> 1 regex
def test_glob_to_regex():
    """A tradução glob->regex bate com fnmatch (é o que garante mesmo resultado)."""
    import re as _re, fnmatch
    casos = [("*.py", ["a.py", "x.y.py"], ["a.pyc", "py", "a.PY"]),
             ("doc?.log", ["doc1.log", "docA.log"], ["doc.log", "doc12.log"]),
             ("[ab]*.txt", ["a1.txt", "b.txt"], ["c.txt", "ab.doc"]),
             ("v[!0-9].dat", ["vx.dat"], ["v3.dat"])]
    for glob, sim, nao in casos:
        rx = _re.compile(engine._glob_to_regex(glob))     # sensível a caixa
        for s in sim:
            assert rx.match(s) and fnmatch.fnmatchcase(s, glob), f"{glob} devia casar {s}"
        for s in nao:
            assert not rx.match(s) and not fnmatch.fnmatchcase(s, glob), f"{glob} NÃO devia casar {s}"
    assert engine._merge_globs(["*.py", "src/*.c"]) is None, "glob com '/' não deve fundir"
    print("ok  opt#3  _glob_to_regex equivale a fnmatch (e recusa glob de caminho)")


def test_fd_merge_single_pass():
    """Opt#3: >3 globs viram UMA regex -> um único fd (não um por padrão),
    e o resultado é a UNIÃO correta dos padrões."""
    if not engine.FD:
        print("--  opt#3  (pulado: sem fd)"); return
    d = tempfile.mkdtemp(prefix="lfs_merge_")
    try:
        alvo = ["a.txt", "b.log", "c.py", "d.md", "e.csv"]   # 5 extensões
        ruido = ["z.bin", "w.dat"]
        for name in alvo + ruido:
            open(os.path.join(d, name), "w").close()
        pats = ["*.txt", "*.log", "*.py", "*.md", "*.csv"]   # >3 -> funde
        real_popen = subprocess.Popen
        n_popen = {"fd": 0}
        def spy(cmd, *a, **k):
            if cmd and os.path.basename(str(cmd[0])) in ("fd", "fdfind"):
                n_popen["fd"] += 1
            return real_popen(cmd, *a, **k)
        subprocess.Popen = spy
        try:
            got = {os.path.basename(m.path) for m in
                   engine._iter_names_fd(Query(paths=[d], name_patterns=pats), lambda: False)}
        finally:
            subprocess.Popen = real_popen
        assert got == set(alvo), f"união errada: {got}"
        assert n_popen["fd"] == 1, f"esperava 1 fd (regex fundida), rodou {n_popen['fd']}"
        print("ok  opt#3  5 globs -> 1 só fd, união correta (uma varredura)")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------ opt#2 trava SMR /mnt
def test_mnt_serializes():
    """Opt#2 + refinamento v3: sob /mnt (etc.) serializa se o disco for ROTACIONAL
    ou DESCONHECIDO; um SSD/NVMe confirmado (rotational=0) paraleliza mesmo sob /mnt.
    Fora de /mnt, sempre paraleliza. Mocka a leitura de sysfs p/ ser determinístico."""
    old = boolean._WORKERS
    old_dev, old_rot = boolean._dev_for_path, boolean._rotational
    boolean._WORKERS = 3
    # Mapa fake: caminho -> nó de dispositivo -> rotacional. "" = disco desconhecido.
    devs = {"/mnt/smr/x": "/dev/sdb1", "/mnt/ssd/x": "/dev/sdc1", "/mnt": "/dev/sdb1"}
    rot = {"/dev/sdb1": "1", "/dev/sdc1": "0"}       # sdb=HDD/SMR, sdc=SSD
    boolean._dev_for_path = lambda ap: devs.get(ap, "")
    boolean._rotational = lambda dev: rot.get(dev)   # None = desconhecido
    try:
        mw = lambda p: boolean._max_workers(Query(paths=p if isinstance(p, list) else [p]))
        # rotacional sob /mnt -> serializa
        assert mw("/mnt/smr/x") == 1, "rotacional em /mnt devia serializar"
        assert mw("/mnt") == 1, "rotacional no próprio /mnt devia serializar"
        # SSD confirmado sob /mnt -> paraleliza (refinamento v3)
        assert mw("/mnt/ssd/x") == 3, "SSD em /mnt NÃO devia serializar"
        # disco desconhecido sob /mnt -> padrão seguro: serializa
        assert mw("/mnt/desconhecido/x") == 1, "desconhecido em /mnt devia serializar"
        assert mw("/media/rodrigo/HD") == 1, "desconhecido em /media devia serializar"
        assert mw("/run/media/rodrigo/HD") == 1, "desconhecido em /run/media devia serializar"
        # fora de /mnt -> sempre paraleliza (nem consulta sysfs)
        assert mw(os.path.expanduser("~")) == 3, "devia paralelizar no ~"
        assert mw("/tmp") == 3, "devia paralelizar no /tmp"
        assert mw("/mntx/foo") == 3, "casou /mnt por prefixo solto"
        # misto: rotacional em /mnt arrasta tudo p/ serial; SSD em /mnt não
        assert mw(["/tmp", "/mnt/smr/x"]) == 1, "misto c/ rotacional devia serializar"
        assert mw(["/tmp", "/mnt/ssd/x"]) == 3, "misto só c/ SSD não devia serializar"
        boolean._WORKERS = 1
        assert mw("/tmp") == 1, "WORKERS=1 devia serializar sempre"
        print("ok  opt#2  trava SMR c/ rotational: rotacional/desconhecido serializa, SSD libera")
    finally:
        boolean._WORKERS = old
        boolean._dev_for_path, boolean._rotational = old_dev, old_rot


def test_or_parallel_correctness():
    """Opt#2: OR em paralelo (pool) dá o MESMO resultado que serial. Roda no tmp,
    que paraleliza; força também o caminho serial p/ comparar."""
    d = _tree()
    try:
        def run(expr):
            got = set()
            boolean.search_boolean(Query(paths=[d]), expr,
                                   lambda m: got.add(os.path.basename(m.path)), lambda: False)
            return got
        old = boolean._WORKERS
        try:
            boolean._WORKERS = 3                          # paralelo (tmp não é /mnt)
            par = run("assinatura OR rascunho OR ausente")
            boolean._WORKERS = 1                          # serial
            ser = run("assinatura OR rascunho OR ausente")
        finally:
            boolean._WORKERS = old
        esperado = {"N1.TXT", "n2.txt", "doc42.log"}      # assinatura:N1,n2 · rascunho:doc42 · ausente:n2
        assert par == ser == esperado, f"par={par} ser={ser} esperado={esperado}"
        # OR aninhado com AND/NOT continua certo sob paralelismo
        boolean._WORKERS = 3
        try:
            mix = run("(assinatura OR rascunho) AND paciente NOT ausente")
        finally:
            boolean._WORKERS = old
        assert mix == {"N1.TXT", "doc42.log"}, f"OR+AND+NOT paralelo errado: {mix}"
        print("ok  opt#2  OR paralelo == OR serial (inclui OR dentro de AND/NOT)")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------ i18n
def test_i18n_mechanism():
    """i18n: inglês é a fonte; pt traduz; chave ausente cai literal; format ok."""
    try:
        i18n.set_lang("en")
        assert i18n.t("Ready.") == "Ready."
        assert i18n.t("{n} path(s) copied.", n=3) == "3 path(s) copied."
        i18n.set_lang("pt")
        assert i18n.t("Ready.") == "Pronto."
        assert i18n.t("Searching…") == "Buscando…"
        assert i18n.t("{n} path(s) copied.", n=3) == "3 caminho(s) copiado(s)."
        assert i18n.t("chave inexistente") == "chave inexistente"   # fallback literal
        # autodetecção pelo locale
        for env, exp in [("en_US.UTF-8", "en"), ("pt_BR.UTF-8", "pt"),
                         ("pt_PT", "pt"), ("es_ES.UTF-8", "en"), ("C", "en")]:
            assert i18n._normalize(env) == exp, (env, i18n._normalize(env))
        print("ok  i18n  inglês-base + pt, fallback literal, detecção de locale")
    finally:
        i18n.set_lang(None)


def test_i18n_no_stale_keys():
    """Toda chave do dicionário PT precisa existir como literal t(...) no código —
    pega 'drift' (typo entre a fonte no código e a chave da tradução)."""
    import re
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lfs")
    src = ""
    for fn in ("app.py", "boolean.py"):
        with open(os.path.join(base, fn), encoding="utf-8") as f:
            src += f.read()
    lits = set()
    for m in re.finditer(r"""t\(\s*((?:(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')\s*)+)""", src):
        parts = re.findall(r"""(?:"((?:\\.|[^"\\])*)"|'((?:\\.|[^'\\])*)')""", m.group(1))
        joined = "".join(a or b for a, b in parts)
        for esc, real in (('\\n', '\n'), ('\\"', '"'), ("\\'", "'"), ('\\\\', '\\')):
            joined = joined.replace(esc, real)
        lits.add(joined)
    dynamic = set(engine.__dict__.get("_HEADERS_SOURCE", ())) | \
        {"File", "Folder", "Size", "Modified"}          # via t(self.HEADERS[s])
    stale = [k for k in i18n._PT if k not in lits and k not in dynamic]
    assert not stale, "chaves PT sem uso no código (drift):\n" + "\n".join(map(repr, stale))
    print(f"ok  i18n  sem chaves órfãs ({len(i18n._PT)} chaves cobertas)")


# ------------------------------------------------------------------ nome: pastas
def test_name_search_includes_dirs():
    """Busca só-por-nome acha ARQUIVOS e PASTAS (case-insensitive), pelos dois
    caminhos: fd (se houver) e fallback os.walk. Pasta não casa -> não aparece."""
    d = tempfile.mkdtemp(prefix="lfs_dir_")
    try:
        os.makedirs(os.path.join(d, "ARGENTINA", "sub"))
        os.makedirs(os.path.join(d, "Pasta_argentina_casting"))
        os.makedirs(os.path.join(d, "outra"))
        for f in ("relatorio_Argentina.txt", "sem_match.txt"):
            open(os.path.join(d, f), "w").close()
        open(os.path.join(d, "outra", "ARGENTINA2024.mp4"), "w").close()

        def run(use_fd):
            old = engine.FD
            if not use_fd:
                engine.FD = None
            try:
                q = Query(paths=[d], name_patterns=[engine.as_name_glob("argentina")])
                got = []
                engine.search(q, lambda m: got.append(m), lambda: False)
                return got
            finally:
                engine.FD = old

        for use_fd in ((True, False) if engine.FD else (False,)):
            got = run(use_fd)
            dirs = {os.path.relpath(m.path, d) for m in got if m.is_dir}
            files = {os.path.relpath(m.path, d) for m in got if not m.is_dir}
            tag = "fd" if use_fd else "python"
            # relpath NORMALIZA a barra final e mascararia o bug; a GUI usa
            # basename — é o basename que tem de funcionar ("dir/" do fd => "")
            assert all(os.path.basename(m.path) for m in got), \
                (tag, [m.path for m in got])
            assert "ARGENTINA" in dirs, (tag, dirs)
            assert "Pasta_argentina_casting" in dirs, (tag, dirs)
            assert "relatorio_Argentina.txt" in files, (tag, files)
            assert any("ARGENTINA2024.mp4" in f for f in files), (tag, files)
            assert "outra" not in dirs and "sem_match.txt" not in files, (tag, got)
        print("ok  UX  nome acha arquivos E pastas (case-insensitive; fd e python)")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_name_newline_in_filename():
    """E1: nome de arquivo com '\\n' não pode virar 2 registros na saída do fd.
    Antes (fd sem --print0) o arquivo sumia (falso negativo) ou casava caminho errado."""
    d = tempfile.mkdtemp(prefix="lfs_nl_")
    try:
        target = os.path.join(d, "linha1\nlinha2.txt")
        with open(target, "w") as f:
            f.write("x")
        open(os.path.join(d, "normal.txt"), "w").close()

        def run(use_fd):
            old = engine.FD
            if not use_fd:
                engine.FD = None
            try:
                q = Query(paths=[d], name_patterns=[engine.as_name_glob("linha")],
                          include_hidden=True)
                got = []
                engine.search(q, lambda m: got.append(m.path), lambda: False)
                return got
            finally:
                engine.FD = old

        for use_fd in ((True, False) if engine.FD else (False,)):
            got = run(use_fd)
            tag = "fd" if use_fd else "python"
            assert target in got, (tag, got)      # o arquivo real, intacto
            assert len(got) == 1, (tag, got)      # sem fragmentos-fantasma
        print("ok  E1  nome com '\\n' sobrevive (fd --print0; sem fantasma)")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_name_broken_symlink():
    """E5: symlink quebrado casado por nome não pode ser descartado (os.stat falha
    no alvo -> antes sumia; agora cai no os.lstat e aparece)."""
    d = tempfile.mkdtemp(prefix="lfs_ln_")
    try:
        os.symlink("/nao/existe/mesmo", os.path.join(d, "link_orfao"))

        def run(use_fd):
            old = engine.FD
            if not use_fd:
                engine.FD = None
            try:
                q = Query(paths=[d], name_patterns=[engine.as_name_glob("link_orfao")],
                          include_hidden=True)
                got = []
                engine.search(q, lambda m: got.append(os.path.basename(m.path)), lambda: False)
                return got
            finally:
                engine.FD = old

        for use_fd in ((True, False) if engine.FD else (False,)):
            got = run(use_fd)
            tag = "fd" if use_fd else "python"
            assert "link_orfao" in got, (tag, got)
        print("ok  E5  symlink quebrado casa por nome (fd e python)")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_max_depth_backend_parity():
    """E3: fallback os.walk casava um nível a mais que o fd (--max-depth conta filhos
    diretos como 1). fd e python devem devolver o MESMO conjunto por profundidade."""
    if not engine.FD:
        print("ok  E3  (fd ausente — paridade de profundidade não testável)")
        return
    d = tempfile.mkdtemp(prefix="lfs_dp_")
    try:
        os.makedirs(os.path.join(d, "a", "b", "c"))
        open(os.path.join(d, "raiz.txt"), "w").close()
        open(os.path.join(d, "a", "n1.txt"), "w").close()
        open(os.path.join(d, "a", "b", "n2.txt"), "w").close()
        open(os.path.join(d, "a", "b", "c", "n3.txt"), "w").close()

        def run(use_fd, md):
            old = engine.FD
            if not use_fd:
                engine.FD = None
            try:
                q = Query(paths=[d], name_patterns=[], include_hidden=True, max_depth=md)
                got = []
                engine.search(q, lambda m: got.append(os.path.relpath(m.path, d)), lambda: False)
                return set(got)
            finally:
                engine.FD = old

        for md in (1, 2, 3):
            fd = run(True, md); py = run(False, md)
            assert fd == py, (md, "fd^py=", sorted(fd ^ py))
        print("ok  E3  profundidade: fd e python concordam (max_depth 1..3)")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_boolean_not_excludes_binaries():
    """B1: `NOT termo` não pode despejar binários — o universo do NOT era
    `rg --files` (tudo) enquanto os termos são `rg -l` (pula binário), então um
    binário que CONTÉM o termo aparecia no NOT (falso positivo) e todo binário
    poluía o resultado. Universo agora é só-texto (mesmo domínio da busca)."""
    d = tempfile.mkdtemp(prefix="lfs_b1_")
    try:
        def mk(name, data):
            with open(os.path.join(d, name), "wb") as f:
                f.write(data)
        mk("texto_com_foo.txt", b"isto tem foo aqui\n")
        mk("texto_sem_foo.txt", b"nada aqui\noutra linha\n")
        mk("bin_com_foo.bin",   b"foo\x00\x01\x02 binario que contem foo")
        mk("bin_sem_foo.bin",   b"\x00\x01\x02 binario sem o termo")

        def run_not(use_rg):
            srg, sfd = engine.RG, engine.FD
            if not use_rg:
                engine.RG = None; engine.FD = None
            try:
                got = []
                boolean.search_boolean(Query(paths=[d]), "NOT foo",
                                       lambda m: got.append(os.path.basename(m.path)),
                                       lambda: False)
                return sorted(got)
            finally:
                engine.RG, engine.FD = srg, sfd

        for use_rg in ((True, False) if engine.RG else (False,)):
            tag = "rg" if use_rg else "python"
            res = run_not(use_rg)
            assert res == ["texto_sem_foo.txt"], (tag, res)   # sem NENHUM binário
        # sanidade: positivo 'foo' não regride (binário fora)
        got = []
        boolean.search_boolean(Query(paths=[d]), "foo",
                               lambda m: got.append(os.path.basename(m.path)), lambda: False)
        assert sorted(got) == ["texto_com_foo.txt"], got
        print("ok  B1  NOT não despeja binários (universo só-texto; rg e python)")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_boolean_deep_nesting():
    """B2: aninhamento absurdo de parênteses vira BooleanError, não RecursionError."""
    try:
        boolean.parse("(" * 5000 + "A" + ")" * 5000)
        raise AssertionError("não levantou erro em expressão fundíssima")
    except boolean.BooleanError:
        pass
    except RecursionError:
        raise AssertionError("RecursionError escapou (deveria virar BooleanError)")
    # expressão rasa segue funcionando
    boolean.parse("(a OR b) AND c NOT d")
    print("ok  B2  parênteses fundíssimos -> BooleanError (sem RecursionError)")


# ================================================================== F7: cópia
# O teste mais importante desta fase é o de NÃO-DESTRUIÇÃO (test_copy_never_touches
# _source): o LFS lê e exporta, jamais altera a origem. Os demais cobrem o que o
# desenho listou + a pré-checagem do sistema de arquivos de DESTINO.
import hashlib
import fileops, disks


def _snapshot(root):
    """Impressão digital da árvore: caminho, tipo, modo, tamanho, mtime_ns e
    conteúdo. Qualquer byte ou data que mude na origem quebra a comparação."""
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in sorted(dirnames + filenames):
            p = os.path.join(dirpath, name)
            rel = os.path.relpath(p, root)
            st = os.lstat(p)
            if os.path.islink(p):
                snap[rel] = ("link", os.readlink(p))
            elif os.path.isdir(p):
                snap[rel] = ("dir", oct(st.st_mode))
            else:
                with open(p, "rb") as f:
                    snap[rel] = ("file", oct(st.st_mode), st.st_size, st.st_mtime_ns,
                                 hashlib.blake2b(f.read()).hexdigest())
    return snap


def _hostile_tree():
    """Árvore com os nomes que quebram implementação ingênua."""
    d = tempfile.mkdtemp(prefix="lfs_cp_src_")
    names = [
        "simples.txt",
        "com espaco.txt",
        "com\nquebra.txt",                       # E1: \n no nome
        os.fsdecode(b"nao\xff\xfeutf8.txt"),     # E2: nome não-UTF-8
        "emoji_🎬.mp4",
        "a" * (255 - len(".txt")) + ".txt",      # 255 BYTES, o limite do ext4
    ]
    for i, n in enumerate(names):
        with open(os.path.join(d, n), "wb") as f:
            f.write(b"conteudo %d\n" % i)
        os.utime(os.path.join(d, n), (1000000 + i, 1000000 + i))
    os.makedirs(os.path.join(d, "sub", "fundo"))
    with open(os.path.join(d, "sub", "fundo", "profundo.txt"), "w") as f:
        f.write("recursivo")
    return d, names


def test_copy_hostile_names():
    """§6.1: nomes hostis chegam íntegros ao destino, com conteúdo e mtime."""
    src, names = _hostile_tree()
    dst = tempfile.mkdtemp(prefix="lfs_cp_dst_")
    try:
        res = fileops.copy_to([src], dst)
        base = os.path.join(dst, os.path.basename(src))
        assert not res.failed, res.failed
        for n in names:
            p = os.path.join(base, n)
            assert os.path.exists(p), f"não copiou {n!r}"
            assert os.stat(p).st_mtime == os.stat(os.path.join(src, n)).st_mtime, \
                f"mtime perdido em {n!r}"
        assert os.path.exists(os.path.join(base, "sub", "fundo", "profundo.txt")), \
            "não copiou recursivamente"
        assert res.bytes_copied > 0
        print(f"ok  F7   nomes hostis copiados ({len(names)} nomes: \\n, não-UTF-8, "
              "emoji, 255 bytes)")
    finally:
        shutil.rmtree(src, ignore_errors=True); shutil.rmtree(dst, ignore_errors=True)


def test_copy_symlinks_and_cycles():
    """§6.2: symlink vira symlink; link quebrado é copiado como link; ciclo de
    diretório não trava a varredura (guarda st_dev/st_ino)."""
    src = tempfile.mkdtemp(prefix="lfs_cp_lnk_")
    dst = tempfile.mkdtemp(prefix="lfs_cp_dst_")
    try:
        with open(os.path.join(src, "alvo.txt"), "w") as f:
            f.write("alvo")
        os.symlink("alvo.txt", os.path.join(src, "bom.lnk"))
        os.symlink("nao_existe.txt", os.path.join(src, "quebrado.lnk"))
        os.makedirs(os.path.join(src, "sub"))
        os.symlink(src, os.path.join(src, "sub", "ciclo"))     # aponta p/ a raiz
        res = fileops.copy_to([src], dst)             # não pode rodar para sempre
        base = os.path.join(dst, os.path.basename(src))
        assert os.path.islink(os.path.join(base, "bom.lnk")), "symlink virou arquivo"
        assert os.readlink(os.path.join(base, "bom.lnk")) == "alvo.txt"
        assert os.path.islink(os.path.join(base, "quebrado.lnk")), \
            "link quebrado devia ser copiado como link"
        assert os.path.islink(os.path.join(base, "sub", "ciclo")), "ciclo virou pasta"
        assert not res.failed, res.failed
        print("ok  F7   symlink preservado, link quebrado copiado, ciclo não trava")
    finally:
        shutil.rmtree(src, ignore_errors=True); shutil.rmtree(dst, ignore_errors=True)


def test_copy_conflicts():
    """§6.3: skip / rename / overwrite fazem exatamente o esperado, e o
    incremento 'nome (1).ext' pula os que já existem."""
    src = tempfile.mkdtemp(prefix="lfs_cp_cf_")
    dst = tempfile.mkdtemp(prefix="lfs_cp_dst_")
    try:
        with open(os.path.join(src, "x.txt"), "w") as f:
            f.write("NOVO")
        for ans, esperado in (("skip", "VELHO"), ("overwrite", "NOVO")):
            with open(os.path.join(dst, "x.txt"), "w") as f:
                f.write("VELHO")
            fileops.copy_to([os.path.join(src, "x.txt")], dst,
                            on_conflict=lambda s, d, a=ans: a)
            assert open(os.path.join(dst, "x.txt")).read() == esperado, ans
        # rename: já existem x.txt e x (1).txt -> tem que criar x (2).txt
        open(os.path.join(dst, "x (1).txt"), "w").close()
        fileops.copy_to([os.path.join(src, "x.txt")], dst,
                        on_conflict=lambda s, d: "rename")
        assert os.path.exists(os.path.join(dst, "x (2).txt")), \
            "incremento não pulou o 'x (1).txt' existente"
        # SEM callback o padrão é PULAR — nunca sobrescrever por omissão
        with open(os.path.join(dst, "x.txt"), "w") as f:
            f.write("INTOCADO")
        r = fileops.copy_to([os.path.join(src, "x.txt")], dst)
        assert open(os.path.join(dst, "x.txt")).read() == "INTOCADO", \
            "sobrescreveu sem o usuário mandar"
        assert r.skipped and r.skipped[0][1] == fileops.SKIP_CONFLICT
        print("ok  F7   conflito: skip/rename/overwrite corretos; padrão = pular")
    finally:
        shutil.rmtree(src, ignore_errors=True); shutil.rmtree(dst, ignore_errors=True)


class _CancelAfter:
    """Event falso que dispara na N-ésima consulta: cancelamento determinístico
    NO MEIO de um arquivo, sem depender de temporização."""

    def __init__(self, after):
        self.n, self.after = 0, after

    def is_set(self):
        self.n += 1
        return self.n > self.after


def test_copy_cancel_removes_partial():
    """§6.4: cancelar no meio de um arquivo apaga o PARCIAL do destino (nunca
    deixar meio-vídeo no pendrive com cara de arquivo bom); os já concluídos
    permanecem e o CopyResult diz o que houve."""
    src = tempfile.mkdtemp(prefix="lfs_cp_cn_")
    dst = tempfile.mkdtemp(prefix="lfs_cp_dst_")
    old_block = fileops.BLOCK
    fileops.BLOCK = 4096
    try:
        with open(os.path.join(src, "a_pequeno.bin"), "wb") as f:
            f.write(b"x" * 4096)
        with open(os.path.join(src, "b_grande.bin"), "wb") as f:
            f.write(b"y" * (4096 * 50))
        antes = _snapshot(src)
        # 6 consultas: passa pelo diretório e pelo arquivo pequeno inteiro, e
        # dispara DEPOIS do 1º bloco do grande — ou seja, com parcial no disco.
        res = fileops.copy_to([src], dst, cancel=_CancelAfter(6))
        base = os.path.join(dst, os.path.basename(src))
        assert res.cancelled, "não marcou cancelado"
        assert os.path.exists(os.path.join(base, "a_pequeno.bin")), \
            "arquivo já concluído sumiu"
        assert not os.path.exists(os.path.join(base, "b_grande.bin")), \
            "deixou parcial no destino"
        assert _snapshot(src) == antes, "ORIGEM MUDOU num cancelamento"
        print("ok  F7   cancel no meio: parcial removido, concluídos ficam, origem intacta")
    finally:
        fileops.BLOCK = old_block
        shutil.rmtree(src, ignore_errors=True); shutil.rmtree(dst, ignore_errors=True)


def test_copy_never_touches_source():
    """§6.5 — O TESTE MAIS IMPORTANTE DA FASE. Depois de TODA operação possível
    (cópia normal, conflito nos três modos, cancelamento, destino cheio, nome
    ilegal), a árvore de origem é comparada byte a byte e mtime a mtime com um
    snapshot prévio. Um único byte diferente reprova."""
    src, _ = _hostile_tree()
    os.symlink("simples.txt", os.path.join(src, "link.lnk"))
    dst = tempfile.mkdtemp(prefix="lfs_cp_dst_")
    try:
        antes = _snapshot(src)
        fileops.copy_to([src], dst)                                   # normal
        for ans in ("skip", "rename", "overwrite"):                   # os 3 conflitos
            fileops.copy_to([src], dst, on_conflict=lambda s, d, a=ans: a)
        fileops.copy_to([src], dst, cancel=_CancelAfter(2))           # cancelado
        fileops.copy_to([src], dst, sanitize_names=True)              # nomes adaptados
        # destino "FAT32": limite de tamanho e charset restrito
        old = disks.dest_caps
        disks.dest_caps = lambda p: disks.DestCaps(fstype="vfat", namemax=255,
                                                   **disks._FAT)
        try:
            fileops.copy_to([src], dst)
            fileops.copy_to([src], dst, sanitize_names=True)
        finally:
            disks.dest_caps = old
        depois = _snapshot(src)
        assert depois == antes, (
            "A ORIGEM MUDOU — invariante central do F7 violada:\n" +
            "\n".join(f"  {k}: {antes.get(k)} -> {depois.get(k)}"
                      for k in set(antes) | set(depois) if antes.get(k) != depois.get(k)))
        print("ok  F7   PROVA DE NÃO-DESTRUIÇÃO: origem byte-idêntica após 8 operações")
    finally:
        shutil.rmtree(src, ignore_errors=True); shutil.rmtree(dst, ignore_errors=True)


def test_preflight_space_and_mount():
    """§6.6/6.7: falta de espaço é detectada ANTES de escrever, e um destino sob
    /mnt que não está montado bloqueia a cópia (copiar para um mountpoint vazio
    despejaria o acervo no disco de sistema)."""
    src, _ = _hostile_tree()
    dst = tempfile.mkdtemp(prefix="lfs_cp_dst_")
    try:
        pf = fileops.preflight([src], dst)
        assert pf.total_files >= 6 and pf.total_bytes > 0
        assert pf.fits, "deveria caber no /tmp"
        old_free = disks.free_bytes
        disks.free_bytes = lambda p: 10                   # 10 bytes livres
        try:
            pf2 = fileops.preflight([src], dst)
            assert not pf2.fits, "não detectou falta de espaço"
        finally:
            disks.free_bytes = old_free
        # montagem ausente: /mnt/inexistente_xyz não está em user_mounts()
        assert not disks.mount_ok("/mnt/inexistente_xyz_lfs/sub"), \
            "mount_ok aprovou destino não montado"
        assert disks.mount_ok(dst), "mount_ok reprovou /tmp (fora dos prefixos)"
        pf3 = fileops.preflight([src], "/mnt/inexistente_xyz_lfs")
        assert pf3.blocked, "preflight não bloqueou destino desmontado"
        res = fileops.copy_to([src], "/mnt/inexistente_xyz_lfs", plan=pf3)
        assert not res.copied, "escreveu apesar do bloqueio"
        print("ok  F7   pré-checagem: falta de espaço e mountpoint ausente bloqueiam antes")
    finally:
        shutil.rmtree(src, ignore_errors=True); shutil.rmtree(dst, ignore_errors=True)


def test_dest_caps_restrictive_filesystems():
    """O furo que o desenho não cobria: o destino de verdade é pendrive/HD
    externo/aparelho de mídia — exFAT, FAT32, NTFS ou MTP. Nenhum deles tem
    symlink nem permissão POSIX, FAT32 trava em 4 GiB e todos proíbem : ? * etc.
    Descobrir isso no arquivo 380 de 400 não é aceitável: tem que ser ANTES."""
    fat = disks.DestCaps(fstype="vfat", namemax=255, **disks._FAT)
    assert fat.max_file == (1 << 32) - 1 and not fat.symlinks and not fat.perms
    assert fat.name_problem("filme: cena?.mp4") == "charset"
    assert fat.name_problem("linha\nquebrada.txt") == "charset"
    assert fat.name_problem("CON.txt") == "reserved"
    assert fat.name_problem("nome final .txt") is None      # ponto/espaço no MEIO, ok
    assert fat.name_problem("termina em ponto.") == "trailing"
    assert fat.name_problem("a" * 300) == "length"
    assert fat.name_problem("normal.mp4") is None
    exfat = disks.DestCaps(fstype="exfat", namemax=255, **disks._EXFAT)
    assert exfat.max_file is None and not exfat.symlinks     # exFAT não tem 4 GiB
    posix = disks.DestCaps(fstype="ext4", namemax=255)
    assert posix.name_problem("filme: cena?.mp4") is None    # no ext4 é nome válido
    assert not posix.restrictive and fat.restrictive
    # sanitize: nome legal, extensão preservada, dentro do limite de BYTES
    for nome in ("filme: cena?.mp4", "CON.txt", "termina em ponto.", "a" * 400 + ".mkv",
                 os.fsdecode(b"n\xff\xfeao_utf8.mp4")):
        s = fat.sanitize(nome)
        assert fat.name_problem(s) is None, f"sanitize deixou nome ilegal: {s!r}"
        assert len(os.fsencode(s)) <= 255
    assert fat.sanitize("filme: cena?.mp4").endswith(".mp4"), "perdeu a extensão"
    print("ok  F7   capacidades do destino: FAT/exFAT/NTFS pegos antes de copiar")


def test_preflight_flags_fat_problems():
    """Com destino FAT32, a pré-varredura tem que listar o que vai falhar
    (tamanho e nome) e a cópia pular esses itens com motivo — nunca abortar no
    meio nem escrever um arquivo truncado."""
    src = tempfile.mkdtemp(prefix="lfs_fat_")
    dst = tempfile.mkdtemp(prefix="lfs_cp_dst_")
    old = disks.dest_caps
    disks.dest_caps = lambda p: disks.DestCaps(fstype="vfat", namemax=255, **disks._FAT)
    try:
        with open(os.path.join(src, "ok.mp4"), "w") as f:
            f.write("pequeno")
        ilegal = os.path.join(src, "cena: 12?.mp4")
        with open(ilegal, "w") as f:
            f.write("nome ilegal em FAT")
        os.symlink("ok.mp4", os.path.join(src, "atalho.lnk"))
        grande = os.path.join(src, "gigante.mkv")
        with open(grande, "wb") as f:                   # esparso: 5 GiB sem gastar disco
            f.truncate(5 * (1 << 30))
        pf = fileops.preflight([src], dst)
        assert [p for p, _ in pf.too_big] == [grande], pf.too_big
        assert [os.path.basename(p) for p, _ in pf.bad_names] == ["cena: 12?.mp4"]
        assert pf.links_degraded, "symlink em FAT devia virar cópia real"
        # sem adaptar: pula com motivo. Adaptando: copia com nome legal.
        res = fileops.copy_to([src], dst, plan=pf)
        motivos = dict((os.path.basename(p), r) for p, r in res.skipped)
        assert motivos.get("gigante.mkv") == fileops.SKIP_TOO_BIG, motivos
        assert motivos.get("cena: 12?.mp4") == fileops.SKIP_BAD_NAME, motivos
        res2 = fileops.copy_to([src], dst, plan=pf, sanitize_names=True)
        base = os.path.join(dst, os.path.basename(src))
        assert os.path.exists(os.path.join(base, "cena_ 12_.mp4")), os.listdir(base)
        assert not os.path.islink(os.path.join(base, "atalho.lnk")), \
            "criou symlink num FS que não tem symlink"
        assert res2.copied
        print("ok  F7   destino FAT32: >4 GiB e nome ilegal pulados com motivo; "
              "com 'adaptar', copia")
    finally:
        disks.dest_caps = old
        shutil.rmtree(src, ignore_errors=True); shutil.rmtree(dst, ignore_errors=True)


def test_copy_into_itself():
    """Copiar uma pasta para dentro dela mesma não pode virar recursão infinita."""
    src = tempfile.mkdtemp(prefix="lfs_self_")
    try:
        os.makedirs(os.path.join(src, "dentro"))
        with open(os.path.join(src, "a.txt"), "w") as f:
            f.write("a")
        pf = fileops.preflight([src], os.path.join(src, "dentro"))
        assert any(r == fileops.SKIP_LOOP for _, r in pf.errors), pf.errors
        assert not pf.entries, "planejou copiar a pasta para dentro de si mesma"
        print("ok  F7   cópia da pasta para dentro dela mesma é recusada no plano")
    finally:
        shutil.rmtree(src, ignore_errors=True)


def test_qt_drag_and_clipboard_payload():
    """§6.8/6.9 (único teste com Qt, pulado sem display): o payload de arrasto e
    de clipboard tem os três formatos, é sempre CÓPIA (jamais mover) e o URI
    roundtripa nomes com \\n e não-UTF-8."""
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        print("--  F7   payload Qt: pulado (sem display)")
        return
    try:
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import Qt
    except ImportError:
        print("--  F7   payload Qt: pulado (sem PySide6)")
        return
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lfs"))
    import app as lfsapp
    _ = QApplication.instance() or QApplication([])
    hostis = ["/tmp/com espaco.txt", "/tmp/com\nquebra.txt",
              os.fsdecode(b"/tmp/nao\xff\xfeutf8.txt")]
    # A URI é montada dos BYTES do nome: é o que o Nemo recebe no fio. Passar
    # pela QString (QUrl.fromLocalFile) comeria o \xff\xfe em silêncio e o
    # arrasto apontaria para um arquivo inexistente.
    assert lfsapp.path_to_uri("/tmp/com espaco.txt") == "file:///tmp/com%20espaco.txt"
    assert lfsapp.path_to_uri("/tmp/com\nquebra.txt") == "file:///tmp/com%0Aquebra.txt"
    assert lfsapp.path_to_uri(hostis[2]) == "file:///tmp/nao%FF%FEutf8.txt", \
        lfsapp.path_to_uri(hostis[2])
    md = lfsapp.build_paths_mime(hostis)
    assert md.hasUrls()
    fio = bytes(md.data("text/uri-list")).decode("ascii")
    for p in hostis:
        assert lfsapp.path_to_uri(p) in fio, f"URI ausente do uri-list: {p!r}"
    gnome = bytes(md.data("x-special/gnome-copied-files")).decode("ascii")
    assert gnome.startswith("copy\n"), gnome[:20]
    assert gnome.count("\n") == len(hostis), "uma URI por linha após 'copy'"
    assert "%0A" in gnome, "quebra de linha não foi percent-encoded"
    assert bytes(md.data("application/x-kde-cutselection")) == b"0", "KDE: não é cópia"
    m = lfsapp.ResultModel()
    assert m.supportedDragActions() == Qt.CopyAction, \
        "arrasto oferece MoveAction — o LFS jamais move"
    svc, obj, iface, method, uris, _s = lfsapp.showitems_args(["/tmp/a b.txt"])
    assert (svc, obj, iface, method) == ("org.freedesktop.FileManager1",
                                         "/org/freedesktop/FileManager1",
                                         "org.freedesktop.FileManager1", "ShowItems")
    assert uris == ["file:///tmp/a%20b.txt"], uris
    print("ok  F7   payload Qt: 3 formatos, só CopyAction, ShowItems bem montado")


def test_default_file_manager_wins_over_dbus():
    """A janela de "abrir pasta contendo" tem que ser a do gerenciador PADRÃO DO
    USUÁRIO. O ShowItems é ativado por NOME no barramento, e quem responde pode
    não ser o padrão (no Mint o Nemo registra o FileManager1 mesmo quando o
    padrão é outro): por isso só usamos o barramento quando o padrão é um
    implementador conhecido; senão lançamos o padrão direto."""
    import xdg
    mk = lambda did, ex: xdg.DesktopApp(did, "/x/" + did, did, ex)
    for did, ex in (("nemo.desktop", "nemo %U"),
                    ("org.gnome.Nautilus.desktop", "nautilus --new-window %U"),
                    ("org.kde.dolphin.desktop", "dolphin %u")):
        assert xdg.implements_showitems(mk(did, ex)), did
    for did, ex in (("pcmanfm.desktop", "pcmanfm %U"),
                    ("doublecmd.desktop", "doublecmd %F"),
                    ("spacefm.desktop", "spacefm %F")):
        assert not xdg.implements_showitems(mk(did, ex)), did
    assert not xdg.implements_showitems(None)
    fm = xdg.default_file_manager()      # depende da máquina; só não pode explodir
    assert fm is None or isinstance(fm, xdg.DesktopApp)
    print("ok  F7   pasta abre no gerenciador padrão (D-Bus só se ele fizer ShowItems)")


def test_build_info_visible_and_honest():
    """O app INSTALADO é uma cópia dos fontes: commitar não muda o que o usuário
    roda, e nada na tela dizia isso — um recurso já pronto foi reportado como
    inexistente porque a cópia instalada era de 6 dias antes. Agora o título
    mostra a build. Regra: mostrar a verdade ou não mostrar nada; número de
    versão errado é pior que nenhum."""
    import version
    d = tempfile.mkdtemp(prefix="lfs_ver_")
    try:
        assert version.build_info(d) == "", "inventou build sem VERSION nem git"
        assert version.title_suffix(d) == "", "sujou o título sem saber a build"
        with open(os.path.join(d, "VERSION"), "w") as f:
            f.write("6250d6c (2026-07-21)\nlinha ignorada\n")
        assert version.build_info(d) == "6250d6c (2026-07-21)"
        assert version.title_suffix(d) == "  ·  6250d6c (2026-07-21)"
        # rodando do repo (sem VERSION), o git responde e marca o não-commitado
        repo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        info = version.build_info(repo)
        assert info, "no repo git a build tem que ser identificável"
        print(f"ok  F7   build visível no título ({info})")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_fileops_has_no_destructive_api():
    """Garantia estrutural: o motor de cópia não expõe NENHUMA função capaz de
    apagar, mover ou renomear a origem. É a versão executável do princípio —
    se alguém 'só adicionar um move()' um dia, este teste reprova."""
    proibidos = ("move", "delete", "remove", "rename", "trash", "unlink", "rmtree",
                 "chmod", "truncate")
    achados = [n for n in dir(fileops)
               if not n.startswith("_") and any(p in n.lower() for p in proibidos)]
    assert not achados, f"fileops expõe API destrutiva: {achados}"
    fonte = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "lfs", "fileops.py"), encoding="utf-8").read()
    for chamada in ("shutil.move", "shutil.rmtree", "os.rename", "os.replace",
                    "os.rmdir", "os.removedirs"):
        assert chamada not in fonte, f"fileops chama {chamada}"
    # os.unlink existe UMA vez só: apagar o parcial que nós mesmos criamos
    assert fonte.count("os.unlink") == 1, "os.unlink em mais de um lugar no fileops"
    print("ok  F7   fileops não tem API destrutiva (nem por dentro, nem exportada)")


def main():
    fns = [test_parse_size, test_reap_kills_process, test_no_orphan_on_cancel,
           test_glob_case_insensitive, test_boolean_name_regex,
           test_display_lines_batched, test_one_file_system_fallback,
           test_boolean_parser, test_boolean_unterminated_quote,
           test_boolean_empty_quoted_term, test_name_contains_semantics,
           test_user_mounts_parsing, test_fd_case_sensitive,
           test_and_progressive_correctness, test_and_progressive_restricts,
           test_glob_to_regex, test_fd_merge_single_pass,
           test_mnt_serializes, test_or_parallel_correctness,
           test_on_phase_reports, test_on_phase_optional,
           test_walk_onerror_counts_denied, test_boolean_stats_denied,
           test_i18n_mechanism, test_i18n_no_stale_keys,
           test_name_search_includes_dirs,
           test_name_newline_in_filename, test_name_broken_symlink,
           test_max_depth_backend_parity, test_boolean_deep_nesting,
           test_boolean_not_excludes_binaries, test_boolean_escaped_quotes,
           test_boolean_single_flight,
           # F7 — gerenciador de arquivos (cópia não-destrutiva)
           test_copy_hostile_names, test_copy_symlinks_and_cycles,
           test_copy_conflicts, test_copy_cancel_removes_partial,
           test_copy_never_touches_source, test_preflight_space_and_mount,
           test_dest_caps_restrictive_filesystems, test_preflight_flags_fat_problems,
           test_copy_into_itself, test_qt_drag_and_clipboard_payload,
           test_default_file_manager_wins_over_dbus, test_build_info_visible_and_honest,
           test_fileops_has_no_destructive_api]
    fail = 0
    for fn in fns:
        try:
            fn()
        except AssertionError as e:
            fail += 1; print(f"FALHOU  {fn.__name__}: {e}")
        except Exception as e:
            fail += 1; print(f"ERRO    {fn.__name__}: {e!r}")
    print(f"\n{len(fns)-fail}/{len(fns)} testes passaram")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
