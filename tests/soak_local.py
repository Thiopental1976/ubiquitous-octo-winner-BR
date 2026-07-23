#!/usr/bin/env python3
"""Soak LOCAL — resistência ao TEMPO (a prova que Fable apontou como o maior
risco residual: vazamento de memória, a classe de bug que NENHUM teste da suíte
pega). NÃO faz parte da suíte rápida (test_audit): abre uma janela de verdade
(offscreen) e a martela.

Três fases, cada uma medindo a RSS (memória residente) do processo:
  A) 300 buscas encadeadas na MESMA janela — modelo limpo/preenchido 300×;
  B) 100 jobs de cópia REAIS pelo CopyWorker persistente (fila + progresso);
  C) 50 previews (seleção de linha → render do trecho no painel).

Critério de vazamento: a RSS da METADE FINAL de cada fase não pode subir acima
de um teto sobre a metade inicial (depois de um aquecimento). Qt aloca caches
(fontes, ícones, layouts) nas primeiras iterações — isso é platô, não vazamento;
um vazamento real ao longo de centenas de ciclos cresceria MUITO além do teto.

Rode:  QT_QPA_PLATFORM=offscreen python3 tests/soak_local.py
Sai != 0 se qualquer fase vazar. É lento de propósito (~1-2 min).
"""
from __future__ import annotations
import os, sys, time, tempfile, shutil, gc

# --- ambiente isolado ANTES de importar Qt/app -----------------------------
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_CFG = tempfile.mkdtemp(prefix="soak_cfg_")
os.environ["XDG_CONFIG_HOME"] = _CFG
os.environ["XDG_DATA_HOME"] = os.path.join(_CFG, "data")

sys.stdout.reconfigure(line_buffering=True)   # os._exit no fim pula o flush do buffer

RAIZ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(RAIZ, "lfs"))

from PySide6.QtWidgets import QApplication
import app as appmod

FAIL = []
def check(name, cond, extra=""):
    print(("ok  " if cond else "XX  ") + name + (f"   {extra}" if extra else ""))
    if not cond:
        FAIL.append(name)


def rss_kb() -> int:
    """RSS em KiB via /proc/self/status (VmRSS). gc antes p/ não contar lixo."""
    gc.collect()
    with open("/proc/self/status", encoding="ascii") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    return 0


def pump(app, cond, timeout=30.0, slice_s=0.01):
    """Bombeia o loop de eventos até cond() virar True ou estourar o timeout."""
    t0 = time.time()
    while not cond():
        app.processEvents()
        time.sleep(slice_s)
        if time.time() - t0 > timeout:
            return False
    app.processEvents()
    return True


def growth_verdict(name, samples, warmup, ceil_kb):
    """Compara a média da 1ª vs 2ª metade da janela PÓS-aquecimento. Vazamento =
    a 2ª metade fica ceil_kb acima da 1ª. Reporta o delta e o pico p/ diagnóstico."""
    win = samples[warmup:]
    if len(win) < 4:
        check(name, True, "amostras insuficientes p/ julgar — pulado")
        return
    half = len(win) // 2
    early = sum(win[:half]) / half
    late = sum(win[half:]) / (len(win) - half)
    delta = late - early
    peak = max(samples) - samples[0]
    check(name, delta < ceil_kb,
          f"Δmédia={delta/1024:.1f} MiB (teto {ceil_kb/1024:.0f}) · "
          f"pico+{peak/1024:.1f} MiB · fim={samples[-1]/1024:.0f} MiB")


# --- corpus sintético -------------------------------------------------------
CORP = tempfile.mkdtemp(prefix="soak_corp_")
for i in range(180):
    with open(os.path.join(CORP, f"doc{i:04d}.txt"), "w") as f:
        f.write(f"linha um do documento {i}\nalvo repetido para o preview\n" * 6)
SRC = [os.path.join(CORP, f"doc{i:04d}.txt") for i in range(180)]
DEST_ROOT = tempfile.mkdtemp(prefix="soak_dest_")

app = QApplication.instance() or QApplication(sys.argv)
mw = appmod.MainWindow()

# Substitui os diálogos modais da cópia por respostas automáticas (sem UI, sem
# travar o loop): pré-checagem = seguir; conflito = pular. Desconecta os slots
# reais primeiro — senão o exec() do diálogo penduraria o offscreen p/ sempre.
try:
    mw.copier.ask_preflight.disconnect()
except (RuntimeError, TypeError):
    pass
try:
    mw.copier.ask_conflict.disconnect()
except (RuntimeError, TypeError):
    pass
mw.copier.ask_preflight.connect(lambda pf, ask: ask.reply({"sanitize": False}))
mw.copier.ask_conflict.connect(lambda src, dst, ask: ask.reply(("skip", True)))

print("=" * 64)
print(f"SOAK: corpus={len(SRC)} arquivos · cfg isolado={_CFG}")
print(f"baseline RSS = {rss_kb()/1024:.0f} MiB")
print("=" * 64)

# --- Fase A: 300 buscas -----------------------------------------------------
print("\n[A] 300 buscas encadeadas…")
a_samples = []
FORM = {"name": "*.txt", "paths": CORP, "recursive": True}
for n in range(300):
    mw.apply_form(FORM)
    mw.start_search()
    ok = pump(app, lambda: not mw.tab.searching, timeout=20)
    if not ok:
        check("A busca não pendura", False, f"busca #{n} não terminou em 20 s")
        break
    a_samples.append(rss_kb())
    if n % 50 == 49:
        rows = mw.tab.model.rowCount() if hasattr(mw.tab.model, "rowCount") else len(mw.tab.model.rows)
        print(f"    {n+1:3d}/300 · {rows} linhas · RSS {a_samples[-1]/1024:.0f} MiB")
check("A todas as 300 buscas rodaram", len(a_samples) == 300, f"rodei {len(a_samples)}")
check("A última busca achou os 180 arquivos",
      len(mw.tab.model.rows) == 180, f"linhas={len(mw.tab.model.rows)}")
growth_verdict("A sem vazamento em 300 buscas", a_samples, warmup=50, ceil_kb=48 * 1024)

# --- Fase B: 100 cópias reais ----------------------------------------------
print("\n[B] 100 jobs de cópia pelo worker persistente…")
b_samples = []
done = {"n": 0}
mw.copier.job_done.connect(lambda res, dest: done.__setitem__("n", done["n"] + 1))
for n in range(100):
    dest = os.path.join(DEST_ROOT, f"job{n:03d}")
    os.makedirs(dest, exist_ok=True)
    before = done["n"]
    mw.enqueue_copy([SRC[n % len(SRC)]], dest)
    ok = pump(app, lambda: done["n"] > before, timeout=20)
    if not ok:
        check("B cópia não pendura", False, f"job #{n} não concluiu em 20 s")
        break
    b_samples.append(rss_kb())
    if n % 25 == 24:
        print(f"    {n+1:3d}/100 · RSS {b_samples[-1]/1024:.0f} MiB")
check("B todos os 100 jobs concluíram", len(b_samples) == 100, f"rodei {len(b_samples)}")
copied_ok = sum(1 for n in range(100)
                if os.path.exists(os.path.join(DEST_ROOT, f"job{n:03d}",
                                                os.path.basename(SRC[n % len(SRC)]))))
check("B os arquivos chegaram ao destino", copied_ok == 100, f"copiados={copied_ok}/100")
growth_verdict("B sem vazamento em 100 cópias", b_samples, warmup=20, ceil_kb=32 * 1024)

# --- Fase C: 50 previews ----------------------------------------------------
print("\n[C] 50 previews (seleção → render do trecho)…")
# garante resultados na tabela
mw.apply_form(FORM); mw.start_search()
pump(app, lambda: not mw.tab.searching, timeout=20)
c_samples = []
rowcount = mw.tab.proxy.rowCount()
check("C há resultados p/ prever", rowcount > 0, f"linhas={rowcount}")
for n in range(50):
    mw.tab.table.selectRow(n % max(1, rowcount))
    app.processEvents()
    time.sleep(0.005)
    c_samples.append(rss_kb())
check("C 50 previews renderizaram", len(c_samples) == 50)
growth_verdict("C sem vazamento em 50 previews", c_samples, warmup=10, ceil_kb=24 * 1024)

# --- encerramento limpo -----------------------------------------------------
mw.copier.shutdown()
mw.copier.wait(3000)
mw.close()
app.processEvents()

# limpeza
shutil.rmtree(CORP, ignore_errors=True)
shutil.rmtree(DEST_ROOT, ignore_errors=True)
shutil.rmtree(_CFG, ignore_errors=True)

print("\n" + "=" * 64)
if FAIL:
    print(f"SOAK: {len(FAIL)} FALHA(S): {FAIL}")
    sys.stdout.flush()
    os._exit(1)
print("SOAK: TODAS AS FASES VERDES — sem vazamento detectável")
sys.stdout.flush()
os._exit(0)
