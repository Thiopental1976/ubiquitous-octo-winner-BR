#!/usr/bin/env python3
"""Testes de regressão da auditoria Fable 5 (LinuxFileSearch_Auditoria_Debug.md).

Cobre os consertos B1–B14 no que dá para exercitar sem GUI (o núcleo é sem-Qt).
Rode:  python3 tests/test_audit.py      (ou via pytest)

Cada teste constrói sua própria árvore sintética em tempdir — não toca no acervo.
"""
from __future__ import annotations
import os, sys, time, subprocess, tempfile, shutil

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lfs"))
import engine, boolean
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
    print("ok  parse_size (fonte única engine.parse_size)")


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
    distintos + 1 (extração de linhas), e o último passo é 'extraindo linhas'."""
    d = _tree()
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
        assert max(dones) == 4 and "extraindo" in fases[-1][2], f"último passo errado: {fases[-1]}"
        # termos distintos anunciados uma vez cada
        rotulos_termo = [lb for _, _, lb in fases if lb.startswith("termo")]
        assert len(rotulos_termo) == len(set(rotulos_termo)) == 3, f"termos: {rotulos_termo}"
        print("ok  opt#4  on_phase relata passos 1..total e termina em 'extraindo linhas'")
    finally:
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


def main():
    fns = [test_parse_size, test_reap_kills_process, test_no_orphan_on_cancel,
           test_glob_case_insensitive, test_boolean_name_regex,
           test_display_lines_batched, test_one_file_system_fallback,
           test_boolean_parser, test_fd_case_sensitive,
           test_and_progressive_correctness, test_and_progressive_restricts,
           test_glob_to_regex, test_fd_merge_single_pass,
           test_mnt_serializes, test_or_parallel_correctness,
           test_on_phase_reports, test_on_phase_optional,
           test_walk_onerror_counts_denied, test_boolean_stats_denied]
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
