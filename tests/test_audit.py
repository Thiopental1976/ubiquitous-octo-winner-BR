#!/usr/bin/env python3
"""Testes de regressão da auditoria Fable 5 (LinuxFileSearch_Auditoria_Debug.md).

Cobre os consertos B1–B14 no que dá para exercitar sem GUI (o núcleo é sem-Qt).
Rode:  python3 tests/test_audit.py      (ou via pytest)

Cada teste constrói sua própria árvore sintética em tempdir — não toca no acervo.
"""
from __future__ import annotations
import os, sys, time, subprocess, tempfile, shutil, errno

RAIZ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(RAIZ, "lfs"))
import engine, boolean, i18n
from engine import Query
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_parity_rg_python import test_parity_directed_and_property


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


# --------------------------------------------------- T1 (Fable): FIFO não trava
def test_t1_fifo_no_hang():
    """T1 (stress-test Fable): um FIFO/pipe na árvore fazia o open() do fallback
    Python bloquear PARA SEMPRE (pipe sem escritor). Guarda S_ISREG em
    _iter_content_python e _is_probably_text. Primeiro teste do projeto com
    arquivo não-regular. Roda a busca numa thread com timeout: se travar, o teste
    ACUSA em vez de pendurar a suíte."""
    import threading
    if not hasattr(os, "mkfifo"):
        print("ok  T1  (plataforma sem mkfifo — pulado)"); return
    d = tempfile.mkdtemp(prefix="lfs_fifo_")
    old_rg, old_rga = engine.RG, engine.RGA
    try:
        with open(os.path.join(d, "a.txt"), "w") as f: f.write("nada aqui\n")
        with open(os.path.join(d, "b.txt"), "w") as f: f.write("tem laudo aqui\n")
        os.mkfifo(os.path.join(d, "pipe.txt"))     # o vilão: open() sem escritor pendura
        engine.RG = engine.RGA = ""                # força o fallback Python (sem rg)
        out = {}
        def _go():
            got = []
            # positivo cai no _display_lines_py; o NOT exercita o _is_probably_text
            boolean.search_boolean(Query(paths=[d], content=""),
                                   "laudo NOT inexistentexyz",
                                   lambda m: got.append(m.path))
            out["files"] = {os.path.basename(p) for p in got}
        th = threading.Thread(target=_go, daemon=True)
        th.start(); th.join(timeout=15)
        assert not th.is_alive(), "T1: a busca TRAVOU no FIFO (open sem escritor)"
        assert out.get("files") == {"b.txt"}, f"esperava só b.txt, veio {out.get('files')}"
        print("ok  T1  FIFO na árvore não trava a busca de conteúdo (guarda S_ISREG)")
    finally:
        engine.RG, engine.RGA = old_rg, old_rga
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------- T2 (Fable): linhas sem rg
def test_t2_boolean_lines_without_rg():
    """T2 (stress-test Fable): sem ripgrep (Recommends recusável), a busca BOOLEANA
    devolvia arquivos SEM número de linha nem preview — enquanto a busca simples
    mostrava as linhas. Agora _display_lines colhe as linhas em Python. Bimodal:
    a asserção é a mesma com rg e sem rg."""
    d = tempfile.mkdtemp(prefix="lfs_t2_")
    old_rg, old_rga = engine.RG, engine.RGA
    try:
        p = os.path.join(d, "x.txt")
        with open(p, "w") as f: f.write("cabecalho\nlinha com laudo\nfim\n")
        for rg_on in (bool(old_rg), False):        # com rg (se houver) e sem rg
            engine.RG = old_rg if rg_on else ""
            engine.RGA = old_rga if rg_on else ""
            got = []
            boolean.search_boolean(Query(paths=[d], content=""),
                                   "laudo", lambda m: got.append(m))
            modo = "com rg" if rg_on else "sem rg"
            assert len(got) == 1, f"[{modo}] esperava 1 arquivo, veio {len(got)}"
            m = got[0]
            assert m.lines, f"[{modo}] busca booleana perdeu as linhas (preview vazio)"
            assert m.lines[0][0] == 2 and "laudo" in m.lines[0][1], f"[{modo}] {m.lines}"
        print("ok  T2  busca booleana traz linhas com E sem ripgrep (paridade)")
    finally:
        engine.RG, engine.RGA = old_rg, old_rga
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
    from boolean import parse, positive_terms, And, Or
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


def test_kernel_network_caps():
    """F9c §4.1 (desenho Fable): destinos de REDE do KERNEL (não-gvfs; o SO já
    montou). Sem eles, um CIFS caía em _DEFAULT_CAPS (POSIX otimista) e a
    pré-checagem LIBERAVA ':' '?' '*' num nome que o SMB recusa. Agora o charset
    segue o PROTOCOLO. Todos net=True → disparam o ritmo de escrita de rede (§4.2)."""
    # _caps_for é PURA: fstype de kernel entra direto na tabela (path/mp irrelevantes)
    for fs in ("nfs", "nfs4", "9p", "virtiofs"):
        caps, via = disks._caps_for(fs, "/mnt/nas/x", "/mnt/nas")
        assert caps["net"] and caps["symlinks"] and caps["perms"], f"{fs} devia ser POSIX+net"
        assert not via
    for fs in ("cifs", "smb3", "smbfs"):
        caps, _ = disks._caps_for(fs, "/mnt/win/x", "/mnt/win")
        assert caps["net"] and caps["label"] == "SMB" and not caps["symlinks"], f"{fs} devia ser SMB"
        assert caps["charset"], "SMB precisa proibir o charset do protocolo"
    for fs in ("fuse.sshfs", "sshfs"):
        caps, _ = disks._caps_for(fs, "/mnt/ssh/x", "/mnt/ssh")
        assert caps["net"] and caps["symlinks"] and caps["label"] == "SFTP", f"{fs} devia ser SFTP"
    # comportamento na borda: DestCaps CIFS recusa nome com ':' e marca net; NFS aceita
    smb = disks.DestCaps(fstype="cifs", **disks._NET_SMB)
    assert smb.net and smb.name_problem("cena: 12?.mp4") == "charset"
    nfs = disks.DestCaps(fstype="nfs4", **disks._NET_NFS)
    assert nfs.net and nfs.name_problem("cena: 12?.mp4") is None, "NFS é POSIX: nome válido"
    # pacing: destino de rede escreve em ritmo, igual removível (F9c §4.2)
    assert (getattr(nfs, "net", False) or getattr(nfs, "removable", False)), "net devia pacear"
    print("ok  F9c  caps de rede do kernel: nfs=POSIX, cifs=SMB (charset), sshfs=SFTP; net→pacing")


def test_mount_entry_sees_mtp_gvfs():
    """A1 (parecer Fable, confirmado AO VIVO no Philips PMC7230): um telefone/media
    player via MTP aparece no /proc/mounts como 'gvfsd-fuse … fuse.gvfsd-fuse', SEM
    nó em /dev/. O filtro antigo por /dev/ descartava essa linha, o casamento subia
    até '/' e o aparelho era classificado como o DISCO DE SISTEMA ext4 — o pior erro
    possível para uma cópia (sem aviso de MTP, sem checagem de nome).

    Linha e caminho são exatamente os capturados do PMC7230 em 2026-07-21."""
    mounts = [
        "/dev/mapper/vgmint-root / ext4 rw,relatime 0 0\n",
        "tmpfs /run tmpfs rw,nosuid,nodev 0 0\n",
        "tmpfs /run/user/1000 tmpfs rw,nosuid,relatime 0 0\n",
        "gvfsd-fuse /run/user/1000/gvfs fuse.gvfsd-fuse rw,nosuid,nodev,relatime,"
        "user_id=1000,group_id=1000 0 0\n",
    ]
    mtp = ("/run/user/1000/gvfs/mtp:host=Philips_Philips_PMC7230_4dbc38a7_-_"
           "527b8946_-_0e823200_-_0d411f79/Storage/Video")
    dev, mp, fstype = disks._mount_entry(mtp, mounts=disks._read_mounts(mounts))
    assert fstype == "fuse.gvfsd-fuse", f"não viu o MTP, viu {fstype!r} (regressão A1)"
    assert mp == "/run/user/1000/gvfs", f"ponto de montagem errado: {mp!r}"
    assert dev == "gvfsd-fuse", "dev do MTP deveria ser a source gvfs (sem /dev/)"
    # CLASSIFICAÇÃO POR ESQUEMA (§1.2/§2 do A2R): o fstype gvfs é UM só para todos
    # os backends; quem decide o perfil é o esquema no primeiro componente do
    # caminho. O contraexemplo sftp é obrigatório — sem ele o classificador não
    # foi testado, só a coincidência de o exemplo ser MTP.
    caps_mtp, via_mtp = disks._caps_for(fstype, mtp, mp)
    assert caps_mtp["label"] == "MTP" and via_mtp, "gvfs-MTP: perfil MTP + via_gvfs"
    sftp = "/run/user/1000/gvfs/sftp:host=servidorcedro/home/rodrigo/x"
    caps_sftp, via_sftp = disks._caps_for(fstype, sftp, mp)
    assert caps_sftp["label"] == "SFTP" and not via_sftp, \
        "CONTRAEXEMPLO: sftp por gvfs é POSIX remoto, NÃO MTP (não degrada nomes)"
    assert caps_sftp["symlinks"] and caps_sftp["perms"] and not caps_sftp["charset"], \
        "sftp deveria manter symlink/perms/charset livre (POSIX pleno)"
    smb = "/run/user/1000/gvfs/smb-share:server=nas,share=video/clipe.mp4"
    assert disks._caps_for(fstype, smb, mp)[0]["label"] == "SMB"
    assert disks._caps_for(fstype, "/run/user/1000/gvfs/dav:host=box/a", mp)[0]["label"] == "WebDAV"
    # raiz do gvfs sem componente e esquema desconhecido: conservador, NÃO-MTP
    for p in (mp, "/run/user/1000/gvfs/googledrive:host=x/a"):
        c, v = disks._caps_for(fstype, p, mp)
        assert not v and c["label"] == "rede", f"gvfs sem/esquema-novo deveria ser conservador: {p}"
    # o mapa de fstype NÃO deve mais classificar gvfs como MTP (a armadilha do §3.1)
    assert "fuse.gvfsd-fuse" not in disks._FS_CAPS and "gvfsd-fuse" not in disks._FS_CAPS, \
        "fuse.gvfsd-fuse não pode voltar ao _FS_CAPS: mtp/sftp/smb compartilham o fstype"
    # jmtpfs (FUSE real, fora do gvfs) continua MTP pela tabela de fstype
    assert disks._caps_for("fuse.jmtpfs", "/home/rodrigo/mtp/x", "/home/rodrigo/mtp")[0]["label"] == "MTP"
    # um caminho de disco normal continua casando o /dev/ real (sem regressão)
    dev2, mp2, fs2 = disks._mount_entry("/home/rodrigo/x",
                                        mounts=disks._read_mounts(mounts))
    assert dev2 == "/dev/mapper/vgmint-root" and fs2 == "ext4"
    # o leitor decodifica espaço escapado (\040) no ponto de montagem
    got = disks._read_mounts(["/dev/sdb1 /media/pen\\040drive vfat rw 0 0\n"])
    assert got == [("/dev/sdb1", "/media/pen drive", "vfat")]
    print("ok  A1   MTP/gvfs visível no mounts (PMC7230): não mais 'ext4 interno'")


def test_search_profile_classification():
    """F9a §2.1 (desenho Fable): search_profile() classifica o caminho para a
    política de BUSCA. Montagens de REDE (nfs/cifs/sshfs/9p/lustre…) não
    serializam como SMR, mas ganham teto de workers por montagem e a marca
    is_network (que liga o watchdog). gvfs/autofs saem do 'buscar em tudo' por
    padrão. Puro, com montagens sintéticas — sem NAS real."""
    mounts_lines = [
        "/dev/mapper/vgmint-root / ext4 rw,relatime 0 0\n",
        "nas:/export/video /mnt/nas nfs4 rw,relatime,vers=4.2 0 0\n",
        "//nas/share /mnt/smb cifs rw,relatime 0 0\n",
        "sshuser@host:/srv /mnt/ssh fuse.sshfs rw,nosuid,nodev 0 0\n",
        "server:/gv /mnt/lustre lustre rw 0 0\n",
        "gvfsd-fuse /run/user/1000/gvfs fuse.gvfsd-fuse rw,nosuid,nodev 0 0\n",
        "auto /net autofs rw,relatime 0 0\n",
    ]
    M = disks._read_mounts(mounts_lines)
    P = lambda p: disks.search_profile(p, mounts=M)

    for path, fstype in (("/mnt/nas/a.mkv", "nfs4"), ("/mnt/smb/b", "cifs"),
                         ("/mnt/ssh/c", "fuse.sshfs"), ("/mnt/lustre/d", "lustre")):
        pr = P(path)
        assert pr.klass == "network", f"{path}: esperava network, veio {pr.klass}"
        assert pr.is_network and not pr.serialize, f"{path}: rede não serializa como SMR"
        assert pr.max_workers == disks.NET_WORKERS_PER_MOUNT, f"{path}: sem teto de workers"
        assert pr.enumerate_default, f"{path}: rede montada explícita entra na busca"

    g = P("/run/user/1000/gvfs/mtp:host=x/Storage")
    assert g.klass == "gvfs" and g.is_network, "gvfs deveria ser rede"
    assert not g.enumerate_default, "gvfs FORA do 'buscar em tudo' por padrão (§2.1)"

    a = P("/net/algum/ponto")
    assert a.klass == "autofs" and not a.enumerate_default, \
        "autofs: NÃO descer ao enumerar (senão acorda todo automount)"

    # coerência com path_needs_serial no eixo LOCAL: o disco de sistema (ext4, não
    # sob /mnt) não serializa e não é rede.
    root = P("/home/rodrigo/x")
    assert not root.is_network and not root.serialize, "raiz local: nem rede nem serial"

    # R1 (revisão Fable 23/07): classificar um root de REDE NÃO pode tocar o
    # filesystem no PAI — só string + /proc/mounts (local). Se um os.stat/lstat/
    # realpath/statvfs escapasse sobre o root, um mount frio/travado penduraria o pai
    # ANTES do fork da sonda (a sonda nem nasceria). Guard: com esses syscalls
    # armados p/ EXPLODIR, classificar um mount de rede tem de passar ileso.
    import os as _os
    _armados = {}
    for nome in ("stat", "lstat", "statvfs"):
        _armados[nome] = getattr(_os, nome)
    _rp = _os.path.realpath
    def _boom(*a, **k):
        raise AssertionError("classificação de rede tocou o filesystem no pai (R1)!")
    try:
        for nome in _armados:
            setattr(_os, nome, _boom)
        _os.path.realpath = _boom
        pr = disks.search_profile("/mnt/nas/a.mkv", mounts=M)
        assert pr.klass == "network" and pr.is_network, "rede deveria classificar FS-free"
    finally:
        for nome, fn in _armados.items():
            setattr(_os, nome, fn)
        _os.path.realpath = _rp
    print("ok  F9a  search_profile: rede/gvfs/autofs classificados; rede é FS-free (R1)")


def test_mount_alive_watchdog():
    """F9a §2.2 + F1/F2: mount_status() sonda a vida da montagem num PROCESSO
    descartável (fork) com timeout. VIVA responde 'alive' — inclusive se o stat
    negar com EACCES/EPERM/ENOENT/EIO (respondeu = viva). QUEBRADA (F2: stat
    responde na hora com ENOTCONN/ESTALE/EHOSTDOWN/ENODEV) = 'broken_mount'.
    TRAVADA (D-state do NFS morto, stat que nunca volta) estoura o timeout =
    'no_response' SEM travar o chamador — o filho preso é ABANDONADO (F1), nunca
    vira zumbi que impede a saída. _stat injetável: determinístico, sem NAS real."""
    import errno as _errno
    d = tempfile.mkdtemp(prefix="lfs_alive_")
    try:
        assert disks.mount_status(d, timeout=2.0) == "alive", "dir real: viva"
        assert disks.mount_alive(d, timeout=2.0), "mount_alive bool: viva"
        # F2: errno de montagem morta => broken_mount
        enotconn = lambda p: (_ for _ in ()).throw(OSError(_errno.ENOTCONN, "not connected"))
        assert disks.mount_status(d, timeout=2.0, _stat=enotconn) == "broken_mount"
        assert not disks.mount_alive(d, timeout=2.0, _stat=enotconn), "quebrada != viva"
        # EACCES respondeu = a montagem está VIVA (só negou o alvo)
        eacces = lambda p: (_ for _ in ()).throw(OSError(_errno.EACCES, "denied"))
        assert disks.mount_status(d, timeout=2.0, _stat=eacces) == "alive", "EACCES = viva"
        # F1: stat que TRAVA além do timeout = no_response, chamador livre, filho
        # abandonado (não pode travar o teste nem virar zumbi que impede a saída).
        hang = lambda p: time.sleep(30)
        t0 = time.time()
        dead = disks.mount_status(d, timeout=0.3, _stat=hang)
        elapsed = time.time() - t0
        assert dead == "no_response", "montagem travada deveria dar no_response"
        assert elapsed < 2.0, f"mount_status NÃO respeitou o timeout (levou {elapsed:.1f}s)"
        # o filho preso ficou registrado p/ reap oportunista, não perdido
        assert disks._abandoned_pids, "sonda travada deveria ter sido abandonada (F1)"
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("ok  F9a  mount_status: viva/quebrada/travada via processo, sem zumbi (F1/F2)")


def test_descent_gate_skips_dead_network_mount():
    """F9a §2.2: o gate de descida (`engine._live_roots`) PULA um root de rede cuja
    montagem não responde, registrando o aviso em stats['skipped_mounts'] — nunca
    silêncio, nunca trava. Root local passa direto (sem custo de sonda). Root de
    rede VIVO passa. Determinístico via monkeypatch de search_profile/mount_alive."""
    _disks = disks
    IOP = _disks.IOProfile
    prof_local = IOP("rotational", "/mnt/repo", "xfs", serialize=True,
                     is_network=False, max_workers=None, enumerate_default=True)
    prof_dead = IOP("network", "/mnt/nas", "nfs4", serialize=False,
                    is_network=True, max_workers=4, enumerate_default=True)
    prof_live = IOP("network", "/mnt/nas2", "cifs", serialize=False,
                    is_network=True, max_workers=4, enumerate_default=True)
    by_path = {"/mnt/repo": prof_local, "/mnt/nas/x": prof_dead, "/mnt/nas2/y": prof_live}
    # nas travou (no_response), nas2 vivo — o gate lê o status, não só um bool
    status = {"/mnt/nas": "no_response", "/mnt/nas2": "alive"}
    orig_prof, orig_status = _disks.search_profile, _disks.mount_status
    try:
        _disks.search_profile = lambda p, mounts=None: by_path[p]
        _disks.mount_status = lambda mp, timeout=3.0, **k: status[mp]
        stats: dict = {}
        roots = engine._live_roots(["/mnt/repo", "/mnt/nas/x", "/mnt/nas2/y"], stats)
        assert roots == ["/mnt/repo", "/mnt/nas2/y"], f"gate errou os vivos: {roots}"
        sk = stats.get("skipped_mounts", [])
        assert len(sk) == 1 and sk[0]["mount"] == "/mnt/nas", f"aviso ausente/errado: {sk}"
        assert sk[0]["fstype"] == "nfs4" and sk[0]["reason"] == "no_response"
        # F2: montagem QUEBRADA (responde ENOTCONN) é pulada com reason=broken_mount
        _disks.mount_status = lambda mp, timeout=3.0, **k: "broken_mount"
        st2: dict = {}
        assert engine._live_roots(["/mnt/nas/x"], st2) == [], "quebrada = pulada"
        assert st2["skipped_mounts"][0]["reason"] == "broken_mount", "motivo F2 ausente"
    finally:
        _disks.search_profile, _disks.mount_status = orig_prof, orig_status
    print("ok  F9a  gate de descida: NAS morto/quebrado pulado c/ aviso, vivo passa")


def test_list_search_targets_boundary_visibility():
    """F9a §2.3 (desenho Fable): visibilidade de fronteira. Um 'buscar em /mnt'
    tem que listar ANTES quais montagens serão tocadas e de que classe — o NAS
    que mora SOB o caminho pedido aparece com seu status de vida. Servidor com
    muitas montagens agradece. PURO (mounts + _alive injetáveis)."""
    M = disks._read_mounts([
        "/dev/root / ext4 rw 0 0\n",
        "server:/vol /mnt/nas nfs4 rw 0 0\n",
        "//win/share /mnt/win cifs rw 0 0\n",
        "/dev/sdb1 /mnt/local xfs rw 0 0\n",
    ])
    # mounts_under: só o que está ESTRITAMENTE dentro de /mnt
    assert disks.mounts_under("/mnt", M) == ["/mnt/local", "/mnt/nas", "/mnt/win"]
    assert disks.mounts_under("/mnt/nas", M) == []           # nada sob ele
    alive = {"/mnt/nas": False, "/mnt/win": True}            # nas morto, win vivo
    tg = disks.list_search_targets(["/mnt"], mounts=M,
                                   _alive=lambda mp, timeout=3.0, **k: alive.get(mp, True))
    by_mp = {t["mountpoint"]: t for t in tg}
    assert by_mp["/mnt/nas"]["is_network"] and by_mp["/mnt/nas"]["klass"] == "network"
    assert by_mp["/mnt/nas"]["alive"] is False, "NAS morto tem que aparecer como morto"
    assert by_mp["/mnt/win"]["is_network"] and by_mp["/mnt/win"]["alive"] is True
    assert by_mp["/mnt/local"]["is_network"] is False and by_mp["/mnt/local"]["alive"] is None
    # o próprio /mnt (resolvido p/ a raiz ext4) entra, local, sem sonda de vida
    assert any(not t["is_network"] and t["alive"] is None for t in tg)
    print("ok  F9a  visibilidade de fronteira: /mnt lista montagens-filhas + vida do NAS")


def test_dest_caps_statvfs_lies_on_vfat():
    """Achado do teste presencial (FAT32 real montado em loop): o statvfs do vfat
    responde f_namemax=1530 — 255 x 6, o pior caso de UTF-8 por unidade UTF-16.
    Confiar nele fazia a pré-checagem APROVAR um nome de 300 caracteres que o
    kernel recusa com ENAMETOOLONG no meio da fila. O limite do FAT/exFAT/NTFS é
    em CARACTERES (255 unidades UTF-16), não em bytes; medido no pendrive de
    verdade: 255 passa, 256 não. Por isso a tabela de capacidades tem maxchars, e
    ele manda no namemax que o kernel informou."""
    fat = disks.DestCaps(fstype="vfat", namemax=1530, **disks._FAT)
    assert fat.maxchars == 255
    assert fat.name_problem("a" * 300) == "length", "acreditou no f_namemax mentiroso"
    assert fat.name_problem("a" * 255) is None, "rejeitou nome que o FAT32 aceita"
    # 200 emoji = 800 bytes (passa folgado em 1530) mas 200 caracteres: legal.
    # 300 acentos = 600 bytes, também sob 1530, e ainda assim ilegal no FAT.
    assert fat.name_problem("é" * 300) == "length"
    s = fat.sanitize("é" * 300 + ".mkv")
    assert fat.name_problem(s) is None and s.endswith(".mkv")
    ext4 = disks.DestCaps(fstype="ext4", namemax=255)
    assert ext4.maxchars is None, "limite de caracteres é coisa de FAT, não de POSIX"
    print("ok  F7   limite de nome do FAT medido em caracteres, não no f_namemax")


def test_dest_caps_rejects_non_utf8_names():
    """Terceiro achado do presencial, e o único que a imagem FAT32 em loop NÃO
    pegou: um pendrive de verdade recusa nome que não é UTF-8 válido.

    vfat/exfat/ntfs guardam o nome em UTF-16 e o kernel converte na escrita; um
    byte indecodificável (foto de câmera, arquivo vindo de outro sistema) volta
    EINVAL. A imagem em loop aceitava porque eu a montei com iocharset=utf8; o
    udisks monta o removível com iso8859-1 + utf8, e aí o kernel valida. Sem
    isto, 'adaptar nomes' ficava LIGADO e mesmo assim o arquivo falhava — o
    usuário pediu adaptação e recebeu erro, que é o pior dos mundos."""
    fat = disks.DestCaps(fstype="vfat", namemax=255, **disks._FAT)
    quebrado = os.fsdecode(b"camera_\xff\xfe.jpg")
    assert fat.name_problem(quebrado) == "encoding"
    s = fat.sanitize(quebrado)
    assert fat.name_problem(s) is None
    s.encode("utf-8")                                  # tem que ser escrevível
    assert s.endswith(".jpg"), "perdeu a extensão"
    assert "FF" in s and "FE" in s, f"perdeu o byte original de vista: {s!r}"
    posix = disks.DestCaps(fstype="ext4", namemax=255)
    assert posix.name_problem(quebrado) is None, "no ext4 esse nome é legítimo"
    assert posix.sanitize(quebrado) == quebrado, "mexeu em nome válido no destino"
    print("ok  F7   nome não-UTF-8 pego antes do EINVAL do vfat (achado no pendrive)")


def test_cli_emits_bytes_for_hostile_names():
    """Também do presencial: buscar uma pasta com foto de câmera de nome quebrado
    matava a CLI inteira com UnicodeEncodeError na primeira linha — os resultados
    seguintes se perdiam. Nome de arquivo é sequência de BYTES; a saída tem que
    sair como bytes, senão `lfs ... -0 | xargs -0` não é confiável."""
    src = tempfile.mkdtemp(prefix="lfs_cli_")
    try:
        quebrado = os.path.join(src, os.fsdecode(b"camera_\xff\xfe.jpg"))
        open(quebrado, "w").close()
        open(os.path.join(src, "depois.txt"), "w").close()
        out = subprocess.run([sys.executable, "-m", "lfs.cli", "-n", "*", "-l", src],
                             capture_output=True, cwd=RAIZ)
        assert out.returncode == 0, out.stderr.decode("utf-8", "replace")
        assert os.fsencode(quebrado) in out.stdout, "o nome não-UTF-8 não saiu em bytes"
        assert b"depois.txt" in out.stdout, "a busca morreu no nome quebrado"
        print("ok  F7   CLI sobrevive a nome não-UTF-8 e emite os bytes originais")
    finally:
        shutil.rmtree(src, ignore_errors=True)


def test_cli_json_and_exit_codes():
    """F9b §3.1 (desenho Fable): a CLI `--json` é a interface de automação. NDJSON,
    um objeto por match; nome com \\n é ESCAPADO pelo json e NUNCA racha o framing
    de linha (o teste-chave do §5.6). Exit code estilo grep: 0=achou, 1=nada, 2=erro."""
    import json as _json
    src = tempfile.mkdtemp(prefix="lfs_json_")
    try:
        # nome hostil: contém \n — se o framing quebrar, vira 2 linhas e uma não é json
        hostil = os.path.join(src, "linha1\nlinha2.txt")
        open(hostil, "w").close()
        open(os.path.join(src, "normal.txt"), "w").close()
        run = lambda *a: subprocess.run([sys.executable, "-m", "lfs.cli", *a],
                                        capture_output=True, cwd=RAIZ)
        r = run("-n", "*.txt", "-l", "--json", src)
        assert r.returncode == 0, f"achou → 0, veio {r.returncode}: {r.stderr.decode('utf-8','replace')}"
        linhas = [ln for ln in r.stdout.decode("utf-8", "surrogatepass").splitlines() if ln.strip()]
        objs = []
        for ln in linhas:
            objs.append(_json.loads(ln))          # cada linha DEVE ser json válido (framing intacto)
        paths = {os.path.basename(o["path"]) for o in objs if "path" in o}
        assert "linha1\nlinha2.txt" in paths, f"nome com \\n não veio íntegro: {paths}"
        assert "normal.txt" in paths
        assert all({"path", "size", "mtime", "nmatch", "lines"} <= set(o) for o in objs if "path" in o)
        # nada encontrado → exit 1
        r2 = run("-n", "zzz_inexistente_zzz", "-l", "--json", src)
        assert r2.returncode == 1, f"nada → 1, veio {r2.returncode}"
        # erro de expressão booleana → exit 2, com objeto de erro no stream json
        r3 = run("-b", "(a AND", "--json", src)
        assert r3.returncode == 2, f"erro → 2, veio {r3.returncode}"
        errobj = _json.loads(r3.stdout.decode("utf-8", "surrogatepass").splitlines()[0])
        assert errobj.get("error") == "boolean_expression", errobj
        print("ok  F9b  CLI --json: framing sobrevive a \\n no nome; exit 0/1/2 estilo grep")
    finally:
        shutil.rmtree(src, ignore_errors=True)


def test_indexed_search_coverage_and_staleness():
    """F9b §3.2 + decisão A do Fable: aceleração por plocate, OPT-IN, com as três
    válvulas de honestidade — (1) recusa cobertura furada (poda), nunca degrada em
    silêncio; (2) verificação viva de staleness (arquivo sumiu do disco desde o
    updatedb => não sai); (3) conteúdo não é indexável => recusa. Determinístico
    com um índice REAL de brinquedo (`updatedb -o`, fixture §5.5 do Fable)."""
    import indexed
    if not (shutil.which("updatedb") and shutil.which("plocate")):
        print("~skip F9b  indexed: updatedb/plocate ausentes (feature opt-in)")
        return
    d = tempfile.mkdtemp(prefix="lfs_idx_")
    db = os.path.join(d, "toy.db")
    root = os.path.join(d, "acervo")
    os.makedirs(os.path.join(root, "sub"))
    open(os.path.join(root, "laudo_alfa.txt"), "w").close()
    open(os.path.join(root, "sub", "relatorio.txt"), "w").close()
    sumico = os.path.join(root, "some_engine.py")
    open(sumico, "w").close()
    try:
        rc = subprocess.run(["updatedb", "-o", db, "-U", root, "-l", "0"],
                            capture_output=True)
        assert rc.returncode == 0, f"updatedb falhou: {rc.stderr.decode('utf-8','replace')}"
        run = lambda args: subprocess.run(["plocate", "-d", db, *args],
                                          stdout=subprocess.PIPE).stdout or b""
        conf_vazia = {"prunepaths": [], "prunefs": set(), "prunenames": [], "prune_bind": True}

        def busca(nome, **qkw):
            q = engine.Query(paths=[root], name_patterns=[engine.as_name_glob(nome)], **qkw)
            return sorted(os.path.basename(m.path)
                          for m in indexed.search_indexed(q, conf=conf_vazia, _run=run))

        # (a) nome "engine" acha só o some_engine.py (contains, igual à busca viva)
        assert busca("engine") == ["some_engine.py"], busca("engine")
        # recursivo pega a submontagem lógica (sub/relatorio) por nome
        assert busca("relatorio") == ["relatorio.txt"], busca("relatorio")

        # (b) STALENESS: apaga o arquivo do DISCO (fica no índice) => não sai
        os.remove(sumico)
        assert busca("engine") == [], "arquivo sumido do disco não pode sair (staleness)"

        # (c) COBERTURA FURADA: conf com PRUNEFS que casa o fs do root, ou um
        # PRUNEPATH sob o root => recusa clara, nunca resultado parcial mudo
        conf_poda = {"prunepaths": [os.path.join(root, "sub")], "prunefs": set(),
                     "prunenames": [], "prune_bind": True}
        try:
            list(indexed.search_indexed(engine.Query(paths=[root],
                 name_patterns=["*"]), conf=conf_poda, _run=run))
            assert False, "root com poda dentro deveria RECUSAR"
        except indexed.IndexError_ as e:
            assert "sub" in str(e) and "busca viva" in str(e), str(e)

        # (d) CONTEÚDO não é indexável => recusa
        try:
            list(indexed.search_indexed(engine.Query(paths=[root], content="x"),
                                        conf=conf_vazia, _run=run))
            assert False, "conteúdo deveria RECUSAR no índice"
        except indexed.IndexError_ as e:
            assert "CONTEÚDO" in str(e) or "conteúdo" in str(e).lower(), str(e)

        # (e) o parse do updatedb.conf real: aspas agrupam, então dividimos DENTRO
        conf = indexed.parse_updatedb_conf('PRUNEFS="nfs CIFS fuse.sshfs"\nPRUNEPATHS="/mnt /tmp"')
        assert "cifs" in conf["prunefs"] and "fuse.sshfs" in conf["prunefs"], conf["prunefs"]
        assert "/mnt" in conf["prunepaths"] and "/tmp" in conf["prunepaths"], conf["prunepaths"]
        # e a cobertura recusa um root podado por caminho e por fstype
        assert indexed.index_coverage("/mnt/x", conf, mounts=[("srv:/e", "/mnt/x", "nfs4")])
        print("ok  F9b  indexed: opt-in, recusa poda, staleness viva, conteúdo barrado")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_probe_child_closes_inherited_fds():
    """R1 (revisão Fable 23/07): o "hang só na primeira vez" da CLI não era cold-start
    do FUSE — era o FILHO da sonda (mount_status) segurando as fds herdadas do pai.
    Preso num stat de mount morto (D-state), ele mantém o stdout do `--json` aberto e
    um leitor por pipe (o subprocess do teste, um xargs) só recebe EOF quando TODAS as
    pontas de escrita fecham. O pai já saiu, mas o filho-cadáver não. Prova
    determinística SEM NAS real: um pipe nosso, um stat lento que segura o filho vivo
    além do timeout, e a conferência de que o filho FECHOU a ponta de escrita herdada
    — o read vê EOF na hora, não espera o stat terminar."""
    import select as _select
    rp, wp = os.pipe()
    slow = lambda p: time.sleep(1.5)          # segura o filho vivo além do timeout
    t0 = time.time()
    st = disks.mount_status("/qualquer", timeout=0.2, _stat=slow)
    assert st == "no_response", st
    os.close(wp)                              # fecha a NOSSA ponta de escrita
    # se o filho fechou a cópia herdada de wp (fix R1), o pipe não tem mais escritor
    # => read vê EOF já; se a segurou, bloquearia ~1.5s até o filho sair.
    ready, _, _ = _select.select([rp], [], [], 0.6)
    assert ready, "EOF não chegou: o filho da sonda ainda segura a fd herdada (R1)"
    assert os.read(rp, 1) == b"", "esperado EOF (todas as pontas de escrita fechadas)"
    assert time.time() - t0 < 1.3, "read pendurou esperando o filho — fd vazou (R1)"
    os.close(rp)
    print("ok  R1   sonda: filho fecha fds herdadas (stdout do --json não pendura)")


def test_indexed_prunenames_coverage():
    """R3 (revisão Fable 23/07): PRUNENAMES poda subárvores POR NOME em qualquer
    profundidade e a cobertura ignorava — o "parcial podado" voltava pela porta dos
    fundos. Regra com paridade à busca viva: cobertura íntegra SÓ se todos os
    prunenames são ocultos E a busca não inclui ocultos (aí a busca viva pularia
    igual). node_modules (visível) ou .git com --hidden (a busca viva desceria) =>
    recusa."""
    import indexed
    base = {"prunepaths": [], "prunefs": set(), "prune_bind": True}
    M = [("d", "/x", "ext4")]
    # (a) prunename oculto + busca sem ocultos => coberto (a busca viva pula igual)
    conf = dict(base, prunenames=[".git"])
    assert indexed.index_coverage("/x", conf, mounts=M, include_hidden=False) == [], \
        "prunename oculto sem --hidden deveria ser cobertura íntegra"
    # (b) o MESMO com --hidden => a busca viva desceria em .git; o índice não => furo
    assert indexed.index_coverage("/x", conf, mounts=M, include_hidden=True), \
        "prunename oculto COM --hidden deveria recusar (índice omite .git)"
    # (c) prunename NÃO-oculto (node_modules) => sempre furo
    conf2 = dict(base, prunenames=["node_modules"])
    holes = indexed.index_coverage("/x", conf2, mounts=M, include_hidden=False)
    assert holes and holes[0]["reason"] == "prunename", holes
    print("ok  R3   PRUNENAMES: oculto+sem-hidden coberto; visível/--hidden recusa")


def test_indexed_symlink_root_translates():
    """R4 (revisão Fable 23/07): root que É/CONTÉM symlink. O plocate devolve o
    caminho REAL (resolvido); sem realpath, `_under` jogaria TODOS os candidatos fora
    do subtree => zero resultados apresentado como 'zero confiável' — mentira. Com o
    fix: casa no realpath, acha, e DEVOLVE o caminho na forma que o usuário deu (o
    symlink), pra UI não trocar o caminho dele."""
    import indexed
    d = tempfile.mkdtemp(prefix="lfs_sym_")
    try:
        realdir = os.path.join(d, "srv_dados")
        os.makedirs(realdir)
        open(os.path.join(realdir, "laudo.txt"), "w").close()
        link = os.path.join(d, "repo")                 # repo -> srv_dados
        os.symlink(realdir, link)
        # _run fake: o plocate devolve o caminho REAL (resolvido), como na vida real
        run = lambda args: os.fsencode(os.path.realpath(link) + "/laudo.txt") + b"\x00"
        conf_vazia = {"prunepaths": [], "prunefs": set(), "prunenames": [], "prune_bind": True}
        q = engine.Query(paths=[link], name_patterns=[engine.as_name_glob("laudo")])
        got = list(indexed.search_indexed(q, conf=conf_vazia, mounts=[], _run=run))
        assert len(got) == 1, f"symlink deu {len(got)} (esperado 1) — _under jogou fora?"
        assert got[0].path == os.path.join(link, "laudo.txt"), \
            f"devolveu {got[0].path!r}, esperado a forma do usuário (o symlink)"
        print("ok  R4   root symlink: acha via realpath e devolve o caminho do usuário")
    finally:
        shutil.rmtree(d, ignore_errors=True)


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


def test_copy_paces_writes_on_removable():
    """O ritmo de escrita (PACE) existe para NÃO travar a máquina inteira: as
    páginas sujas são um recurso global e um pendrive de 11,8 MB/s leva 46 s para
    drenar os 512 MiB de dirty desta máquina — foi assim que o desktop congelou
    com o SMR em 19/06. Este teste prova as duas metades da política: destino
    removível DRENA (fdatasync periódico), destino interno NÃO (num NVMe o fsync
    a cada 16 MiB só custaria vazão, sem proteger ninguém)."""
    src = tempfile.mkdtemp(prefix="lfs_pace_src_")
    dst = tempfile.mkdtemp(prefix="lfs_pace_dst_")
    with open(os.path.join(src, "video.mkv"), "wb") as f:
        f.write(b"\0" * (32 << 20))              # 32 MiB
    sincs = []
    real_sync, real_caps, real_pace = os.fdatasync, disks.dest_caps, fileops.PACE
    os.fdatasync = lambda fd: (sincs.append(fd), real_sync(fd))[1]
    # a drenagem só pode acontecer em fronteira de bloco (BLOCK = 4 MiB),
    # então PACE menor que isso não adianta: 32 MiB / 4 MiB = 8 drenagens.
    fileops.PACE = fileops.BLOCK
    try:
        for removivel, minimo, rotulo in ((False, 0, "interno"), (True, 6, "removível")):
            disks.dest_caps = lambda p, r=removivel: disks.DestCaps(
                fstype="ext4", namemax=255, removable=r)
            sincs.clear()
            fileops.copy_to([os.path.join(src, "video.mkv")], dst,
                            on_conflict=lambda s, d: "overwrite")
            n = len(sincs)
            if removivel:
                assert n >= minimo, f"destino removível não drenou ({n} fdatasync)"
            else:
                # 1 é o fsync final ("copiado" tem que significar "no disco")
                assert n <= 1, f"destino interno drenou demais ({n} fdatasync)"
    finally:
        os.fdatasync, disks.dest_caps, fileops.PACE = real_sync, real_caps, real_pace
        shutil.rmtree(src, ignore_errors=True); shutil.rmtree(dst, ignore_errors=True)
    print("ok  F7   escrita em ritmo no removível, sem penalizar disco interno")


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
    sys.path.insert(0, os.path.join(RAIZ, "lfs"))
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
        # marcador do git archive AINDA NÃO EXPANDIDO (é o que existe no
        # worktree) não pode virar "versão": tem que cair no git de verdade
        with open(os.path.join(d, "VERSION"), "w") as f:
            f.write("$Format:%h (%cs)$\n")
        assert version.build_info(d) == "", "mostrou o marcador cru como versão"
        # no repo git a build é identificável; num .zip extraído sem git, o
        # VERSION expandido pelo `git archive` responde. Uma das duas SEMPRE vale.
        repo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        info = version.build_info(repo)
        tem_git = os.path.exists(os.path.join(repo, ".git"))
        assert info or not tem_git, "no repo git a build tem que ser identificável"
        print(f"ok  F7   build visível no título ({info or 'sem git e sem VERSION'})")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_deb_version_is_dpkg_comparable():
    """F6 — a versão do pacote precisa ORDENAR. O hash do commit identifica, mas
    não diz o que é mais novo; o dpkg decide atualização comparando versões. O
    '~' faz o snapshot ordenar ANTES do 0.9.0 final, que é o certo para um
    pacote gerado do worktree."""
    import version as V
    v = V.deb_version()
    assert v.startswith(V.RELEASE)
    if os.path.isdir(os.path.join(RAIZ, ".git")):
        assert "~git" in v, f"sem carimbo de snapshot: {v}"
    if shutil.which("dpkg"):
        cmp = lambda a, op, b: subprocess.run(
            ["dpkg", "--compare-versions", a, op, b]).returncode == 0
        assert cmp(v, "lt", V.RELEASE), f"{v} deveria ser mais antigo que {V.RELEASE}"
        assert cmp(v, "gt", "0.8.0"), f"{v} deveria ser mais novo que 0.8.0"
    print(f"ok  F6   versão do pacote ordena no dpkg ({v})")


def test_deb_package_builds_and_is_well_formed():
    """F6 — constrói o .deb DE VERDADE e o inspeciona. Um teste que só lesse o
    script não pegaria umask errado, SIGPIPE na conferência nem campo faltando
    no control — os três erros que este build já cometeu."""
    if not shutil.which("dpkg-deb"):
        print("--  F6   .deb: pulado (sem dpkg-deb)"); return
    out = tempfile.mkdtemp(prefix="lfs_deb_")
    try:
        r = subprocess.run([os.path.join(RAIZ, "packaging", "build_deb.sh"), out],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        debs = [f for f in os.listdir(out) if f.endswith(".deb")]
        assert len(debs) == 1, f"esperava um .deb, achei {debs}"
        deb = os.path.join(out, debs[0])
        ctrl = subprocess.run(["dpkg-deb", "-f", deb], capture_output=True, text=True).stdout
        campos = dict(l.split(":", 1) for l in ctrl.splitlines() if ":" in l and not l.startswith(" "))
        for c in ("Package", "Version", "Architecture", "Maintainer", "Description", "Depends"):
            assert c in campos, f"control sem {c}"
        assert campos["Architecture"].strip() == "all", "Python puro não é arch-specific"
        # Depends MÍNIMO: rg/fd são Recommends porque há fallback em Python puro.
        assert "python3" in campos["Depends"]
        assert "ripgrep" not in campos["Depends"], "ripgrep não é obrigatório (há fallback)"
        assert "ripgrep" in campos.get("Recommends", "")
        # PySide6 não existe no apt de Debian/Ubuntu/Mint: depender dele tornaria
        # o pacote ininstalável na distro do próprio autor.
        deps = " ".join(campos.get(c, "") for c in
                        ("Depends", "Pre-Depends", "Recommends")).lower()
        assert "pyside" not in deps, "não pode depender de um pacote inexistente no apt"
        conteudo = subprocess.run(["dpkg-deb", "-c", deb], capture_output=True, text=True).stdout
        for f in ("/usr/bin/sfs", "/usr/bin/lfs", "/usr/bin/sombrero-file-search",
                  "/usr/share/doc/", "/usr/share/man/man1/sfs.1.gz",
                  "/usr/share/man/man1/lfs.1.gz", "/usr/lib/sombrero-file-search/lfs/engine.py"):
            assert f in conteudo, f"pacote sem {f}"
        assert " root/root " in conteudo, "arquivos não saíram como root:root"
        # Scripts de manutenção: um postinst não pode baixar nada nem rodar pip.
        for script in ("postinst", "postrm"):
            s = subprocess.run(["dpkg-deb", "-I", deb, script],
                               capture_output=True, text=True).stdout
            for proibido in ("pip", "curl", "wget", "apt-get", "python3 -m venv"):
                assert proibido not in s, f"{script} faz {proibido} — instalação não surpreende"
        print(f"ok  F6   .deb bem formado ({campos['Version'].strip()}, {os.path.getsize(deb)//1024} KiB)")
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_appimage_recipe_is_coherent():
    """F6 — o AppImage leva ~10 min para construir, então aqui checamos o que
    quebra silenciosamente na receita: o AppRun tem que servir GUI e CLI (um
    arquivo só), e não pode sequestrar o rg/fd do usuário."""
    recipe = open(os.path.join(RAIZ, "packaging", "build_appimage.sh"),
                  encoding="utf-8").read()
    assert "--cli" in recipe, "AppImage sem modo CLI: um arquivo tem que servir aos dois"
    assert 'export PATH="$PATH:$HERE/usr/bin"' in recipe, \
        "o PATH do sistema tem que vir PRIMEIRO (o rg do usuário é o que vale)"
    assert "PySide6-Essentials" in recipe, "Essentials evita arrastar o QtWebEngine"
    assert "flatpak" not in recipe.lower() or "sandbox" in recipe.lower()
    assert "GPL-3.0-or-later" in recipe, "AppStream sem a licença do projeto"
    for chave in ("appimagetool", "python-build-standalone"):
        assert chave in recipe
    # O binário construído, se existir, tem que rodar a CLI.
    imgs = [f for f in os.listdir(os.path.join(RAIZ, "dist"))
            if f.endswith(".AppImage")] if os.path.isdir(os.path.join(RAIZ, "dist")) else []
    if imgs:
        img = os.path.join(RAIZ, "dist", sorted(imgs)[-1])
        r = subprocess.run([img, "--cli", "--version"], capture_output=True, text=True,
                           env={"HOME": os.environ.get("HOME", "/tmp"), "PATH": "/usr/bin:/bin"})
        assert r.returncode == 0 and "GPL" in r.stdout, r.stdout + r.stderr
        print(f"ok  F6   AppImage coerente e executável ({os.path.basename(img)})")
    else:
        print("ok  F6   receita do AppImage coerente (binário não construído)")


def test_fileops_has_no_destructive_api():
    """Garantia estrutural do §0 do F7, agora por AST (não mais por substring cega):
    o motor de cópia NUNCA muta a origem. As estratégias A2R (ATOMIC/GUARDED)
    promovem um temporário POR CIMA DO ALVO com os.replace/os.rename — legítimo,
    é destino —, então a proibição não pode ser 'a string os.replace no arquivo'.
    A regra correta e executável: (1) nada de shutil.move/rmtree, os.rmdir/truncate
    (sem uso legítimo); (2) os.replace/rename/unlink/remove só podem tocar um
    NOME-LOCAL DE DESTINO conhecido, jamais a origem; (3) a origem nunca é aberta
    para escrita. Se alguém 'só adicionar um move()' um dia, isto reprova."""
    import ast
    proibidos = ("move", "delete", "trash", "rmtree", "truncate")
    achados = [n for n in dir(fileops)
               if not n.startswith("_") and any(p in n.lower() for p in proibidos)]
    assert not achados, f"fileops expõe API destrutiva: {achados}"

    caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "lfs", "fileops.py")
    arvore = ast.parse(open(caminho, encoding="utf-8").read())

    def dotted(node):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            return f"{node.value.id}.{node.attr}"
        return node.id if isinstance(node, ast.Name) else ""

    BANIDAS = {"shutil.move", "shutil.rmtree", "shutil.copytree",
               "os.rmdir", "os.removedirs", "os.truncate"}
    MUTAM = {"os.replace", "os.rename", "os.unlink", "os.remove"}
    DESTINOS = {"dst", "tmp", "part", "probe", "old", "cand"}   # nomes-locais de destino
    ORIGEM = {"src", "fi"}                                      # a origem e seu handle de leitura

    achou_mut = False
    for node in ast.walk(arvore):
        if not isinstance(node, ast.Call):
            continue
        nome = dotted(node.func)
        assert nome not in BANIDAS, f"fileops chama {nome} (sem uso legítimo)"
        if nome == "open" and node.args and isinstance(node.args[0], ast.Name) \
                and node.args[0].id in ORIGEM:
            modo = node.args[1] if len(node.args) > 1 else None
            m = modo.value if isinstance(modo, ast.Constant) else ""
            assert not ({"w", "a", "+", "x"} & set(m)), \
                f"open({node.args[0].id}, {m!r}): a ORIGEM aberta para ESCRITA"
        if nome in MUTAM:
            achou_mut = True
            assert node.args, f"{nome} sem argumento?"
            alvo = node.args[0]
            assert isinstance(alvo, ast.Name) and alvo.id in DESTINOS, \
                f"{nome} muta alvo não-destino (só {DESTINOS} são permitidos)"
            assert alvo.id not in ORIGEM, f"{nome} MUTA A ORIGEM ({alvo.id})!"
    assert achou_mut, "esperava ao menos um os.unlink de destino (guarda do parcial)"
    print("ok  F7   fileops nunca muta a origem (AST): mutação só em nomes de destino")


# ================================================================== F5: abas/buscas
# O F5 tem uma camada de DADOS (searches.py, sem Qt) e uma de GUI (abas em app.py).
# Aqui cobrimos a camada de dados, que é onde mora a promessa de "reabrir uma busca
# salva reproduz o resultado" e "config velho nunca quebra".
import searches


class _M:
    """Stand-in de engine.Match, só o que export() lê."""
    def __init__(self, path, size=10, mtime=0, nmatch=0, lines=None, is_dir=False):
        self.path, self.size, self.mtime = path, size, mtime
        self.nmatch, self.lines, self.is_dir = nmatch, lines or [], is_dir


def test_f5_form_roundtrip_and_compat():
    """Um snapshot normalizado sobrevive a ida-e-volta, e ler um snapshot de uma
    versão passada (sem uma chave) ou futura (com chave a mais) não quebra."""
    cheio = dict(searches.DEFAULTS, name="*.py", content="TODO", case=True,
                 paths="/a;/b", days=7, min_size="1M")
    assert searches.normalize(cheio) == searches.normalize(searches.normalize(cheio))
    # config ANTIGO: falta 'one_fs' e 'gitignore' → assume o padrão, não estoura
    velho = {"name": "x", "content": "y", "case": True}
    n = searches.normalize(velho)
    assert n["one_fs"] is False and n["gitignore"] is True and n["recursive"] is True
    # config FUTURO: chave desconhecida é descartada em silêncio
    futuro = dict(searches.DEFAULTS, name="z", campo_do_futuro=42)
    assert "campo_do_futuro" not in searches.normalize(futuro)
    # tipos são coagidos (bool/int vindos de JSON como string/num)
    sujo = dict(searches.DEFAULTS, case=1, days="3", word=0)
    s = searches.normalize(sujo)
    assert s["case"] is True and s["days"] == 3 and s["word"] is False
    print("ok  F5   snapshot do formulário: round-trip e compat de config velho/novo")


def test_f5_history_dedup_and_cap():
    """Repetir a mesma busca REORDENA (sobe ao topo), não duplica; o histórico
    tem teto; busca vazia (sem nome/conteúdo/filtro) não entra."""
    cfg = {}
    searches.add_history(cfg, dict(searches.DEFAULTS, name="a"))
    searches.add_history(cfg, dict(searches.DEFAULTS, name="b"))
    searches.add_history(cfg, dict(searches.DEFAULTS, name="a"))   # repete 'a'
    nomes = [h["name"] for h in cfg["history"]]
    assert nomes == ["a", "b"], f"dedup/reordem falhou: {nomes}"
    # busca vazia é ignorada
    antes = len(cfg["history"])
    searches.add_history(cfg, dict(searches.DEFAULTS))
    assert len(cfg["history"]) == antes
    # teto
    cfg2 = {}
    for i in range(searches.HISTORY_CAP + 15):
        searches.add_history(cfg2, dict(searches.DEFAULTS, name="n%03d" % i))
    assert len(cfg2["history"]) == searches.HISTORY_CAP
    assert cfg2["history"][0]["name"] == "n%03d" % (searches.HISTORY_CAP + 14)
    print("ok  F5   histórico: sem duplicata, reordena no topo, respeita o teto")


def test_f5_saved_overwrite_in_place():
    """Salvar com nome existente sobrescreve NA MESMA POSIÇÃO (não cria segunda
    entrada, não pula para o fim)."""
    cfg = {}
    searches.save_search(cfg, "um", dict(searches.DEFAULTS, name="1"))
    searches.save_search(cfg, "dois", dict(searches.DEFAULTS, name="2"))
    searches.save_search(cfg, "um", dict(searches.DEFAULTS, name="1b"))  # sobrescreve
    lst = searches.saved_list(cfg)
    assert [n for n, _ in lst] == ["um", "dois"], f"posição mudou: {lst}"
    assert dict(lst)["um"]["name"] == "1b", "não sobrescreveu o conteúdo"
    searches.delete_search(cfg, "um")
    assert [n for n, _ in searches.saved_list(cfg)] == ["dois"]
    print("ok  F5   busca salva: sobrescreve no lugar por nome, apaga certo")


def test_f5_export_csv_json():
    """CSV: uma linha por trecho casado, com cabeçalho e ';'. JSON: um objeto por
    ARQUIVO com os trechos aninhados. Nomes hostis não corrompem o CSV."""
    import io, json, csv as _csv
    ms = [
        _M("/tmp/a b;c.txt", size=100, mtime=1_000_000, nmatch=2,
           lines=[(3, "linha três\n"), (9, 'com "aspas" e ; ponto-e-vírgula')]),
        _M("/tmp/only-name.bin", size=5, nmatch=0),           # busca só por nome
    ]
    # CSV
    buf = io.StringIO(); n = searches.export_csv(ms, buf)
    assert n == 3, f"esperava 3 linhas (2+1), veio {n}"
    buf.seek(0); linhas = list(_csv.DictReader(buf, delimiter=";"))
    assert linhas[0]["name"] == "a b;c.txt" and linhas[0]["line"] == "3"
    assert '"aspas"' in linhas[1]["text"] and ";" in linhas[1]["text"]
    assert linhas[2]["line"] == "" and linhas[2]["name"] == "only-name.bin"
    # JSON
    jbuf = io.StringIO(); nj = searches.export_json(ms, jbuf)
    assert nj == 2, "JSON é um objeto por arquivo"
    jbuf.seek(0); dados = json.load(jbuf)
    assert len(dados) == 2 and len(dados[0]["lines"]) == 2
    assert dados[0]["lines"][0]["line"] == 3
    # export() escolhe pela extensão
    import tempfile, os as _os
    d = tempfile.mkdtemp()
    pj = _os.path.join(d, "x.json"); pc = _os.path.join(d, "x.csv")
    assert searches.export(ms, pj) == 2 and searches.export(ms, pc) == 3
    assert json.load(open(pj))[0]["path"] == "/tmp/a b;c.txt"
    print("ok  F5   exportar: CSV por trecho (nomes hostis OK) e JSON por arquivo")


def test_f5_title_for():
    """Rótulo da aba: prioriza o que o usuário digitou; sem nome/conteúdo cai na
    última pasta (basename); trunca; nunca vazio."""
    assert searches.title_for(dict(searches.DEFAULTS, name="foo*")) == "foo*"
    assert searches.title_for(dict(searches.DEFAULTS, content="TODO")) == "TODO"
    assert searches.title_for(dict(searches.DEFAULTS,
             paths="/home/rodrigo/Documents/")) == "Documents"
    assert searches.title_for(dict(searches.DEFAULTS)) == "•"
    longo = searches.title_for(dict(searches.DEFAULTS, name="x" * 50), maxlen=22)
    assert len(longo) == 22 and longo.endswith("…")
    print("ok  F5   título da aba: prioriza o digitado, cai na pasta, trunca")


def test_write_probe_classifies_errno():
    """A2R §1.1: a sonda GRAVA de verdade no destino (metadado mente — o gvfs-MTP
    aceita statvfs e recusa open('wb')), não deixa lixo, e classifica o errno para
    a GUI dizer o porquê. O open é injetável para simular ENOTSUP/EACCES/EROFS sem
    hardware."""
    dst = tempfile.mkdtemp(prefix="lfs_probe_")
    try:
        p = fileops.probe_write(dst)
        assert p.ok, f"sonda real no /tmp deveria gravar (kind={p.kind})"
        assert not [f for f in os.listdir(dst) if f.startswith(".sombrero-probe")], \
            "sonda deixou arquivo para trás"

        def opener_que_falha(num):
            def _o(path, mode):
                raise OSError(num, os.strerror(num))
            return _o

        for num, kind in [(errno.ENOTSUP, "notsup"), (errno.EACCES, "perm"),
                          (errno.EPERM, "perm"), (errno.EROFS, "readonly"),
                          (errno.ENOSPC, "nospace"), (errno.EIO, "other")]:
            pr = fileops.probe_write(dst, _opener=opener_que_falha(num))
            assert not pr.ok and pr.errno == num and pr.kind == kind, \
                f"errno {num}: esperava kind={kind}, veio ok={pr.ok} kind={pr.kind}"
        assert not [f for f in os.listdir(dst) if f.startswith(".sombrero-probe")], \
            "sonda deixou lixo mesmo em falha (try/finally furado)"
        print("ok  A2R  sonda de escrita: grava, não deixa lixo, classifica ENOTSUP/EACCES/EROFS")
    finally:
        shutil.rmtree(dst, ignore_errors=True)


def test_decide_strategy_machine():
    """A2R §3.5: a estratégia é decidida UMA vez (taxonomia + sonda + gio), nunca
    por exceção no meio do lote. Quatro linhas de verdade, quatro veredictos."""
    ok = fileops.WriteProbe(True)
    falhou = fileops.WriteProbe(False, errno.ENOTSUP, "notsup")
    ext4 = disks.DestCaps(fstype="ext4", namemax=255)
    fat = disks.DestCaps(fstype="vfat", namemax=255, **disks._FAT)
    jmtp = disks.DestCaps(fstype="fuse.jmtpfs", namemax=255, **disks._MTP)
    gvfs_mtp = disks.DestCaps(fstype="fuse.gvfsd-fuse", namemax=255,
                              via_gvfs=True, **disks._MTP)
    # sonda OK + perfil com replace -> ATOMIC (POSIX, FAT, NTFS, exFAT, sftp)
    assert fileops.decide_strategy(ext4, ok, True) == fileops.STRAT_ATOMIC
    assert fileops.decide_strategy(fat, ok, False) == fileops.STRAT_ATOMIC
    # sonda OK + MTP por FUSE real (jmtpfs) -> GUARDED (sem os.replace atômico)
    assert fileops.decide_strategy(jmtp, ok, False) == fileops.STRAT_GUARDED
    # sonda FALHOU + via_gvfs + tem gio -> GIO (a rota do Nemo por `gio copy`)
    assert fileops.decide_strategy(gvfs_mtp, falhou, True) == fileops.STRAT_GIO
    # sonda FALHOU + via_gvfs mas SEM gio -> BLOCKED (barra no preflight)
    assert fileops.decide_strategy(gvfs_mtp, falhou, False) == fileops.STRAT_BLOCKED
    # sonda FALHOU num destino local (sem rota alternativa) -> BLOCKED
    assert fileops.decide_strategy(ext4, falhou, True) == fileops.STRAT_BLOCKED
    print("ok  A2R  máquina de estratégia: ATOMIC/GUARDED/GIO/BLOCKED decididos no preflight")


def test_part_path_respects_name_limits():
    """A2R §3.1: o temporário .sombrero-part tem que caber tanto quanto o arquivo
    final — num FAT com nome de 250 chars, dst+sufixo estouraria os 255; encurta
    o radical até caber, sem perder o sufixo (órfão inequívoco)."""
    fat = disks.DestCaps(fstype="vfat", namemax=255, **disks._FAT)
    p = fileops._part_path("/dst/" + "a" * 250 + ".mp4", fat)
    base = os.path.basename(p)
    assert base.endswith(fileops.PART_SUFFIX), "perdeu o sufixo .sombrero-part"
    assert len(os.fsencode(base)) <= 255 and len(base) <= 255, "temp estourou o limite"
    assert os.path.basename(fileops._part_path("/dst/v.bin", fat)) == \
        "v.bin" + fileops.PART_SUFFIX, "nome curto não deveria ser encurtado"
    print("ok  A2R  .sombrero-part cabe no limite do destino (encurta o radical se preciso)")


def test_gio_strategy_uri_and_runner():
    """A2R §3.3: a estratégia GIO grava no gvfs-MTP pela rota do Nemo (`gio copy`).
    Testável sem hardware com o runner injetado: URI mtp:// correta (nome com
    espaço percent-encoded), overwrite remove o antigo antes, falha vira erro claro
    (não Errno 95 cru), e cancelar aborta o objeto parcial no aparelho."""
    caps = disks.DestCaps(fstype="fuse.gvfsd-fuse", mountpoint="/run/user/1000/gvfs",
                          via_gvfs=True, **disks._MTP)
    dst = "/run/user/1000/gvfs/mtp:host=Philips_PMC7230/Storage/cena teste.mp4"
    uri = fileops._mtp_uri(dst, caps)
    assert uri == "mtp://Philips_PMC7230/Storage/cena%20teste.mp4", uri
    d = tempfile.mkdtemp(prefix="lfs_gio_")
    try:
        src = os.path.join(d, "cena teste.mp4")
        with open(src, "wb") as f:
            f.write(b"z" * 2048)
        chamadas = []
        prog = fileops.CopyProgress()
        n = fileops._gio_copy(src, dst, caps, None, lambda *a: None, prog,
                              overwrite=True,
                              _runner=lambda argv, c: (chamadas.append(argv) or (0, "")))
        assert n == 2048 and prog.done_bytes == 2048, "progresso por arquivo não avançou"
        assert chamadas[0][:2] == ["gio", "remove"], "overwrite não removeu o antigo antes"
        assert chamadas[1] == ["gio", "copy", "--", src, uri], "comando gio copy errado"
        # falha do gio vira OSError legível
        try:
            fileops._gio_copy(src, dst, caps, None, lambda *a: None,
                              fileops.CopyProgress(), False,
                              _runner=lambda argv, c: (1, "erro X"))
            assert False, "gio copy com rc!=0 deveria levantar"
        except OSError as ex:
            assert "gio copy falhou" in str(ex), str(ex)
        # cancelamento: rc None no copy -> devolve None e emite um remove (aborta parcial)
        aborts = []

        def cancel_runner(argv, c):
            if argv[1] == "copy":
                return (None, "cancelado")
            aborts.append(argv)
            return (0, "")
        r = fileops._gio_copy(src, dst, caps, None, lambda *a: None,
                              fileops.CopyProgress(), False, _runner=cancel_runner)
        assert r is None and aborts and aborts[0][1] == "remove", \
            "cancelamento deveria abortar o objeto parcial"
        print("ok  A2R  GIO: URI mtp:// correta, overwrite remove antes, cancela abortando parcial")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_write_strategies_atomic_and_guarded():
    """A2R §3.1/§3.2: ATOMIC e GUARDED promovem um temporário POR CIMA do alvo — o
    arquivo antigo do destino só some quando o novo está íntegro. Cancelar no meio
    de uma SOBRESCRITA preserva o antigo (nunca um único meio-arquivo como única
    cópia), e não deixa .sombrero-part órfão no caminho feliz."""
    d = tempfile.mkdtemp(prefix="lfs_strat_")
    old_block = fileops.BLOCK
    fileops.BLOCK = 4096
    try:
        src = os.path.join(d, "src"); os.mkdir(src)
        dstdir = os.path.join(d, "dst"); os.mkdir(dstdir)
        novo = b"N" * (4096 * 10)
        with open(os.path.join(src, "v.bin"), "wb") as f:
            f.write(novo)
        with open(os.path.join(dstdir, "v.bin"), "wb") as f:
            f.write(b"VELHO")
        # ATOMIC overwrite: conteúdo novo, sem temporário órfão
        fileops.copy_to([os.path.join(src, "v.bin")], dstdir,
                        on_conflict=lambda s, dd: "overwrite")
        assert open(os.path.join(dstdir, "v.bin"), "rb").read() == novo
        assert not [f for f in os.listdir(dstdir) if f.endswith(fileops.PART_SUFFIX)], \
            "sobrou .sombrero-part após a promoção"
        # cancelar no meio da SOBRESCRITA: o VELHO2 tem que sobreviver
        with open(os.path.join(dstdir, "v.bin"), "wb") as f:
            f.write(b"VELHO2")
        r = fileops.copy_to([os.path.join(src, "v.bin")], dstdir,
                            on_conflict=lambda s, dd: "overwrite",
                            cancel=_CancelAfter(2))
        assert r.cancelled, "não marcou cancelado"
        assert open(os.path.join(dstdir, "v.bin"), "rb").read() == b"VELHO2", \
            "cancelamento no meio da sobrescrita destruiu o arquivo antigo (ATOMIC furou)"
        assert not [f for f in os.listdir(dstdir) if f.endswith(fileops.PART_SUFFIX)], \
            "cancelamento deixou .sombrero-part"
        # GUARDED (jmtpfs): mesmo resultado observável por outra sequência
        caps_guard = disks.DestCaps(fstype="fuse.jmtpfs", mountpoint=dstdir,
                                    namemax=255, **disks._MTP)
        prog = fileops.CopyProgress()
        n = fileops._write_file(os.path.join(src, "v.bin"),
                                os.path.join(dstdir, "v.bin"),
                                fileops.STRAT_GUARDED, caps_guard, None,
                                lambda *a: None, prog, 0, overwrite=True)
        assert n == 4096 * 10
        assert open(os.path.join(dstdir, "v.bin"), "rb").read() == novo
        assert not [f for f in os.listdir(dstdir) if f.endswith(fileops.PART_SUFFIX)], \
            "GUARDED deixou .sombrero-part"
        print("ok  A2R  ATOMIC/GUARDED: promoção por cima do alvo; cancelar preserva o antigo")
    finally:
        fileops.BLOCK = old_block
        shutil.rmtree(d, ignore_errors=True)


def test_preflight_a2r_surfacing():
    """A borda A2R na GUI (app.py, PreflightDialog): probe_text explica o bloqueio
    da sonda de escrita e strategy_note informa a rota (GIO/rede). Testado SEM Qt,
    extraindo os dois staticmethods puros por AST e rodando com t()/fileops stub."""
    import ast
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lfs")
    with open(os.path.join(base, "app.py"), encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    funcs = {}
    for cls in tree.body:
        if isinstance(cls, ast.ClassDef) and cls.name == "PreflightDialog":
            for fn in cls.body:
                if isinstance(fn, ast.FunctionDef) and fn.name in ("probe_text", "strategy_note"):
                    seg = ast.get_source_segment(src, fn)
                    funcs[fn.name] = "\n".join(l for l in seg.splitlines()
                                               if l.strip() != "@staticmethod")
    assert set(funcs) == {"probe_text", "strategy_note"}, list(funcs)
    # namespace puro: t() devolve a fonte (com format), fileops só com as constantes
    class _FO:
        STRAT_GIO = "GIO"; STRAT_BLOCKED = "BLOCKED"
    ns = {"t": (lambda s, **k: s.format(**k) if k else s), "fileops": _FO}
    for code in funcs.values():
        exec(code, ns)
    probe_text, strategy_note = ns["probe_text"], ns["strategy_note"]
    # probe_text: cada kind dá frase 'BLOCKED', notsup fala de MTP, kinds diferem, fallback existe
    assert "MTP" in probe_text("notsup")
    assert probe_text("perm") != probe_text("notsup")
    for k in ("notsup", "perm", "readonly", "nospace", "kind-inexistente"):
        assert probe_text(k).startswith("BLOCKED"), (k, probe_text(k))
    # strategy_note: GIO -> nota gio; rede -> nota rede; caso comum -> vazio (não intromete)
    class _Caps:
        def __init__(self, net): self.net = net
    class _PF:
        def __init__(self, strat, net=False): self.strategy = strat; self.caps = _Caps(net)
    assert "gio" in strategy_note(_PF("GIO")).lower()
    assert strategy_note(_PF("ATOMIC")) == ""
    assert strategy_note(_PF("ATOMIC", net=True)) != ""
    print("ok  A2R  GUI: probe_text explica bloqueio + strategy_note informa rota (GIO/rede)")


def test_a4_1_copy_bytes_excludes_too_big():
    """A4.1: too_big é PULADO na cópia — não pode inflar a exigência de espaço.
    copy_bytes tem que descontá-lo; fits julga só o que vai ser gravado."""
    src = tempfile.mkdtemp(prefix="lfs_a41_")
    dst = tempfile.mkdtemp(prefix="lfs_a41_dst_")
    old = disks.dest_caps
    disks.dest_caps = lambda p: disks.DestCaps(fstype="vfat", namemax=255, **disks._FAT)
    try:
        with open(os.path.join(src, "ok.mp4"), "w") as f:
            f.write("x" * 100)
        grande = os.path.join(src, "gigante.mkv")
        with open(grande, "wb") as f:
            f.truncate(5 * (1 << 30))               # esparso, >4 GiB do FAT
        pf = fileops.preflight([src], dst)
        assert [p for p, _ in pf.too_big] == [grande]
        # total_bytes inclui o gigante; copy_bytes NÃO
        assert pf.total_bytes >= 5 * (1 << 30)
        assert pf.copy_bytes < 1024, pf.copy_bytes
        # com o gigante fora, cabe folgado; fits não pode ser refém do que é pulado
        pf.free_bytes = 10 * 1024
        assert pf.fits, (pf.free_bytes, pf.copy_bytes, pf.total_bytes)
        print("ok  A4.1 copy_bytes desconta too_big; fits julga só o que grava")
    finally:
        disks.dest_caps = old
        shutil.rmtree(src, ignore_errors=True); shutil.rmtree(dst, ignore_errors=True)


def test_a4_2_native_symlink_counts_in_total():
    """A4.2: symlink recriado como link nativo incrementa done_files no loop;
    o preflight tem que contá-lo em total_files, senão a barra passa de 100%."""
    src = tempfile.mkdtemp(prefix="lfs_a42_")
    dst = tempfile.mkdtemp(prefix="lfs_a42_dst_")
    try:
        with open(os.path.join(src, "alvo.txt"), "w") as f:
            f.write("conteudo")
        os.symlink("alvo.txt", os.path.join(src, "atalho"))   # symlink válido
        pf = fileops.preflight([src], dst)                    # destino ext4: symlinks OK
        seen = []
        fileops.copy_to([src], dst, plan=pf,
                        on_progress=lambda p: seen.append((p.done_files, p.total_files)))
        assert seen, "progresso nunca reportado"
        done_max = max(d for d, _ in seen)
        total = seen[-1][1]
        assert done_max <= total, (done_max, total)
        base = os.path.join(dst, os.path.basename(src))
        assert os.path.islink(os.path.join(base, "atalho")), "symlink devia ser nativo"
        print("ok  A4.2 symlink nativo entra em total_files (done nunca passa total)")
    finally:
        shutil.rmtree(src, ignore_errors=True); shutil.rmtree(dst, ignore_errors=True)


def test_a4_3_out_of_space_flag():
    """A4.3: ENOSPC marca out_of_space (≠ cancelamento do usuário), pra GUI dar
    o motivo certo em vez de 'Cópia cancelada'."""
    src = tempfile.mkdtemp(prefix="lfs_a43_")
    dst = tempfile.mkdtemp(prefix="lfs_a43_dst_")
    old = fileops._write_file
    def _no_space(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")
    try:
        with open(os.path.join(src, "a.bin"), "w") as f:
            f.write("dado")
        fileops._write_file = _no_space
        res = fileops.copy_to([src], dst)
        assert res.out_of_space is True, "ENOSPC devia marcar out_of_space"
        assert res.cancelled is True, "e ainda interrompe o lote"
        assert res.failed, "o arquivo que estourou vai p/ failed"
        # cancelamento REAL do usuário não é out_of_space
        fileops._write_file = old
        res2 = fileops.copy_to([src], dst, cancel=_CancelAfter(1))
        assert res2.out_of_space is False
        print("ok  A4.3 out_of_space distingue 'encheu' de 'cancelado'")
    finally:
        fileops._write_file = old
        shutil.rmtree(src, ignore_errors=True); shutil.rmtree(dst, ignore_errors=True)


def test_a4_4_sys_disk_whole_disk():
    """A4.4: disco inteiro sem partição (mmcblk0, nvme0n1) não pode ter o dígito
    comido pela regex — o próprio nome é o disco."""
    listed = {"/sys/block/mmcblk0", "/sys/block/nvme0n1", "/sys/block/sda"}
    old_real, old_isdir = os.path.realpath, os.path.isdir
    def _fake_isdir(p):
        if p.startswith("/sys/block/"):
            return p in listed
        return old_isdir(p)
    try:
        os.path.realpath = lambda p: p
        os.path.isdir = _fake_isdir
        assert disks._sys_disk("/dev/mmcblk0") == "mmcblk0"
        assert disks._sys_disk("/dev/nvme0n1") == "nvme0n1"
        assert disks._sys_disk("/dev/nvme0n1p3") == "nvme0n1"   # partição ainda sobe
        assert disks._sys_disk("/dev/sda1") == "sda"            # sd sem regressão
        assert disks._sys_disk("/dev/sda") == "sda"
        print("ok  A4.4 _sys_disk devolve o disco inteiro (mmcblk0/nvme0n1), não come o dígito")
    finally:
        os.path.realpath = old_real
        os.path.isdir = old_isdir


def test_a6_persistent_copy_worker():
    """A6: um único CopyWorker vive a sessão inteira, dormindo numa fila
    bloqueante. Sem PySide6 aqui, então verifico os invariantes por AST no fonte:
    (1) CopyWorker é construído UMA vez e NÃO dentro de enqueue_copy; (2) enqueue
    só enfileira; (3) CopyQueue é bloqueante (get/put/drain, sem lista `jobs`);
    (4) run() dorme em self.q.get(); (5) há shutdown() e o closeEvent o chama."""
    import ast
    raiz = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    tree = ast.parse(open(os.path.join(raiz, "lfs", "app.py")).read())

    def _find_class(name):
        return next(n for n in ast.walk(tree)
                    if isinstance(n, ast.ClassDef) and n.name == name)

    def _methods(cls):
        return {m.name for m in cls.body if isinstance(m, ast.FunctionDef)}

    def _func_in_class(cls, name):
        return next(m for m in cls.body
                    if isinstance(m, ast.FunctionDef) and m.name == name)

    def _calls_named(node, fname):
        return [c for c in ast.walk(node)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                and c.func.id == fname]

    def _calls_attr(node, attr):
        return [c for c in ast.walk(node)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr == attr]

    cq = _find_class("CopyQueue")
    cw = _find_class("CopyWorker")
    mw = _find_class("MainWindow")

    # (3) fila bloqueante: interface de queue, nada de lista `jobs`/flag `running`
    cq_methods = _methods(cq)
    assert {"put", "get", "drain", "pending"} <= cq_methods, cq_methods
    cq_src = ast.get_source_segment(open(os.path.join(raiz, "lfs", "app.py")).read(), cq)
    assert "queue.Queue" in cq_src, "CopyQueue devia embrulhar queue.Queue"
    assert ".jobs" not in cq_src and "running" not in cq_src, \
        "sobrou o modelo antigo de lista/flag na CopyQueue"

    # (1) CopyWorker construído UMA vez em todo o módulo
    builds = [c for c in ast.walk(tree)
              if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
              and c.func.id == "CopyWorker"]
    assert len(builds) == 1, "CopyWorker deve ser criado exatamente uma vez, achei %d" % len(builds)

    # (2) e esse build NÃO está em enqueue_copy; enqueue só chama put()
    enq = _func_in_class(mw, "enqueue_copy")
    assert not _calls_named(enq, "CopyWorker"), "enqueue_copy não pode recriar o worker"
    assert _calls_attr(enq, "put"), "enqueue_copy devia só enfileirar (put)"
    assert not _calls_attr(enq, "start"), "enqueue_copy não pode dar start no worker"

    # o build único vive no __init__ da janela, com start()
    init = _func_in_class(mw, "__init__")
    assert _calls_named(init, "CopyWorker"), "worker deve nascer no __init__"
    assert _calls_attr(init, "start"), "worker deve ser iniciado no __init__"

    # (4) run() dorme na fila; (5) shutdown existe e o closeEvent o aciona
    run = _func_in_class(cw, "run")
    assert any(c.func.attr == "get" for c in _calls_attr(run, "get")), \
        "run() deve bloquear em self.q.get()"
    assert "shutdown" in _methods(cw), "falta o desligamento limpo (shutdown)"
    close = _func_in_class(mw, "closeEvent")
    assert _calls_attr(close, "shutdown"), "closeEvent deve chamar copier.shutdown()"
    print("ok  A6   CopyWorker persistente: 1 thread p/ a sessão, fila bloqueante, shutdown limpo")


def test_a3_same_op_sanitize_collision():
    """A3: dois nomes ilegais que o sanitize funde ('a?b'/'a*b' -> 'a_b') não
    podem disparar o diálogo de conflito como se fosse arquivo pré-existente —
    a intenção de adaptar é óbvia, então numera sozinho ('a_b (1)')."""
    src = tempfile.mkdtemp(prefix="lfs_a3_")
    dst = tempfile.mkdtemp(prefix="lfs_a3_dst_")
    old = disks.dest_caps
    disks.dest_caps = lambda p: disks.DestCaps(fstype="vfat", namemax=255, **disks._FAT)
    try:
        with open(os.path.join(src, "a?b.mkv"), "w") as f:
            f.write("primeiro")
        with open(os.path.join(src, "a*b.mkv"), "w") as f:
            f.write("segundo")
        asked = []
        pf = fileops.preflight([src], dst)
        res = fileops.copy_to([src], dst, plan=pf, sanitize_names=True,
                              on_conflict=lambda s, d, a: asked.append(d) or "skip")
        assert not asked, "sanitize-colisão da MESMA operação não devia perguntar"
        base = os.path.join(dst, os.path.basename(src))
        nomes = sorted(os.listdir(base))
        assert "a_b.mkv" in nomes and "a_b (1).mkv" in nomes, nomes
        assert len(res.copied) == 2, res.copied
        print("ok  A3   colisão pós-sanitize da mesma operação numera sozinha (sem diálogo)")
    finally:
        disks.dest_caps = old
        shutil.rmtree(src, ignore_errors=True); shutil.rmtree(dst, ignore_errors=True)


def main():
    fns = [test_parse_size, test_reap_kills_process, test_no_orphan_on_cancel,
           test_glob_case_insensitive, test_boolean_name_regex,
           test_display_lines_batched, test_t1_fifo_no_hang,
           test_t2_boolean_lines_without_rg, test_one_file_system_fallback,
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
           test_dest_caps_restrictive_filesystems,
           test_kernel_network_caps,
           test_mount_entry_sees_mtp_gvfs,
           test_write_probe_classifies_errno, test_decide_strategy_machine,
           # F9a — perfil de I/O de rede + watchdog de montagem morta + gate de descida
           test_search_profile_classification, test_mount_alive_watchdog,
           test_descent_gate_skips_dead_network_mount,
           test_list_search_targets_boundary_visibility,
           test_part_path_respects_name_limits, test_gio_strategy_uri_and_runner,
           test_write_strategies_atomic_and_guarded, test_preflight_a2r_surfacing,
           test_a4_1_copy_bytes_excludes_too_big,
           test_a4_2_native_symlink_counts_in_total,
           test_a4_3_out_of_space_flag, test_a4_4_sys_disk_whole_disk,
           test_a3_same_op_sanitize_collision, test_a6_persistent_copy_worker,
           test_dest_caps_statvfs_lies_on_vfat,
           test_dest_caps_rejects_non_utf8_names,
           test_cli_emits_bytes_for_hostile_names,
           test_cli_json_and_exit_codes,
           test_indexed_search_coverage_and_staleness,
           # Revisão Fable 23/07 (4 achados sobre F1/F2/plocate)
           test_probe_child_closes_inherited_fds,
           test_indexed_prunenames_coverage,
           test_indexed_symlink_root_translates,
           test_preflight_flags_fat_problems,
           test_copy_paces_writes_on_removable,
           test_copy_into_itself, test_qt_drag_and_clipboard_payload,
           test_default_file_manager_wins_over_dbus, test_build_info_visible_and_honest,
           test_fileops_has_no_destructive_api,
           # F5 — abas, buscas salvas, histórico, exportação
           test_f5_form_roundtrip_and_compat, test_f5_history_dedup_and_cap,
           test_f5_saved_overwrite_in_place, test_f5_export_csv_json,
           test_f5_title_for,
           # F6 — empacotamento
           test_deb_version_is_dpkg_comparable,
           test_deb_package_builds_and_is_well_formed,
           test_appimage_recipe_is_coherent,
           # Campanha 2 / Bloco 1 — paridade rg ↔ fallback Python
           test_parity_directed_and_property]
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
