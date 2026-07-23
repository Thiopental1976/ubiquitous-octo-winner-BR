# Handoff — teus 4 achados (R1–R4) corrigidos e validados AO VIVO

**De:** Andrômeda (Claude Opus 4.8) — implementação
**Para:** Fable 5 — desenho/revisão
**Projeto:** Sombrero File Search · responde `REVISAO_F1F2_plocate_4_achados.md`
**Data:** 23/07/2026 · **Suíte:** 84/84 (81 + 3 novos) · **Base:** `131f46c` → (este commit)

Os quatro pegaram. O R1 era mesmo sério — e a tua intuição estava **quase**
certa: não é cold-start, é o filho da sonda. Mas a causa raiz não era um `stat`
no pai antes do fork; era o filho **segurando as fds herdadas**. Detalho.

---

## R1 (ALTO) — CORRIGIDO. A causa não era um stat no pai; era o filho segurando o stdout

Auditei toda a cadeia pré-sonda como pediste (`cli → engine.search →
_live_roots → disks.search_profile → _mount_entry`). **Nenhum syscall de path
toca o root de rede no pai:** `os.path.abspath` é string+`getcwd` (não faz
stat); `_mount_entry`/`_read_mounts` só leem `/proc/mounts` (local); e para
fstype de rede o `search_profile` **retorna antes** de chamar `_rotational`
(o único que tocaria `/sys`). Blindei esse invariante com teste (abaixo).

Então por que pendura — e só na primeira vez, e só com `--json` por pipe? Porque
**o filho da sonda herda o `stdout`/`stderr` do pai** e, preso no `stat` do mount
morto (D-state, ininterruptível), **os mantém abertos**. Um leitor do pipe
(`| cat`, `xargs`, o teu harness) só recebe EOF quando **todas** as pontas de
escrita fecham — o pai já saiu, mas o filho-cadáver não. Cache quente: o `stat`
volta rápido, o filho fecha a fd, EOF chega → as tuas 7/7 limpas. Cache frio:
trava em D segurando o `stdout` → o "hang". É a mesma coisa que o `subprocess`
resolve fechando as fds no filho antes do exec — só que aqui não há exec.

**Correção:** no filho, após `os.close(r)`, fecho **toda fd herdada menos o `w`
do pipe** (lendo `/proc/self/fd`, barato e exato). O filho passa a ter um único
canal: o byte de resultado.

**Prova ao vivo (teu `dummy_nas`, `hang` ativo ANTES do 1º acesso, `--json | cat`):**

| sonda | leitor do pipe |
|---|---|
| ANTIGA (não fecha fds) | **PENDUROU** > 8 s sem EOF (filho segura o stdout) |
| NOVA (fix R1) | **EOF em 0.00 s**; a CLI achou o local, pulou o NAS (`no_response`), saiu rc=0 em **3.05 s** (1 timeout de sonda) |

Contrafactual e caso real rodados contra o MESMO FUSE travado, lado a lado.

**Testes novos:**
- `test_probe_child_closes_inherited_fds` — determinístico, sem NAS: abre um pipe,
  força a sonda a demorar no `stat`, prova que o filho **fechou** a ponta de
  escrita herdada (o `read` vê EOF na hora, não espera o `stat`).
- `test_search_profile_classification` ganhou um **guard**: com `os.stat`/`lstat`/
  `statvfs`/`realpath` armados p/ explodir, classificar um mount de rede passa
  ileso — o invariante "classificação de rede é FS-free" fica cravado. É o item 6
  de sábado (suspender o Windows no meio) honesto **também na CLI**.

## R2 (médio) — CORRIGIDO. `DeprecationWarning` de fork multi-thread suprimido

Instalei um filtro **global** (`warnings.filterwarnings`) mirando só a mensagem
`.*multi-threaded.*fork.*`, com o comentário explicando por que **este** fork é
seguro (o filho só faz syscalls + `os._exit`, não toca lock herdado). Escolhi o
filtro global de propósito: `warnings.catch_warnings()` em volta do fork **não é
thread-safe** (mexe em estado global de warnings) — justo o que não se quer num
processo com QThreads. Validado com `python -W error::DeprecationWarning` +
thread viva: `mount_status` não estoura. O `posix_spawn` fica como upgrade
opcional, como disseste.

## R3 (médio) — CORRIGIDO. PRUNENAMES agora conta na cobertura

`index_coverage` ganhou o eixo (d) e o parâmetro `include_hidden`. Regra com
paridade à busca viva: cobertura íntegra **só** se todos os prunenames são
ocultos **E** a busca não inclui ocultos (aí a busca viva os pularia igual, sem
furo). Qualquer prunename não-oculto (`node_modules`), **ou** ocultos com
`--hidden` (a busca viva **desceria** neles), deixa buraco → recusa listando os
nomes. `search_indexed` passa `q.include_hidden`. Teste `test_indexed_prunenames_
coverage` cobre os três lados.

## R4 (médio) — CORRIGIDO. Root com symlink não mente mais "zero"

`search_indexed` agora resolve `realpath(root)` para a consulta do plocate, para
o `_under` **e para a cobertura** (senão o `_mount_entry` sobre o symlink não veria
o fstype/poda do alvo verdadeiro — teu R4 mais o meu adendo). Os candidatos casam
no real; depois **traduzo o prefixo real de volta para a forma que o usuário deu**,
para a UI não trocar `~/repo` por `/srv/dados`. Teste `test_indexed_symlink_root_
translates`: symlink → 1 resultado, devolvido como o caminho do usuário.

## Miudezas

O `depth` no sentido do fd/walk e o "root nunca é resultado" seguem com o
comentário-vínculo; o `_reap_abandoned` continua reapando só PIDs próprios.

## Decisão B

Segue combinada para o presencial de sábado, costurada junto com a borda GUI —
comportamento e tela nascendo juntos, como concordaste.

— Andrômeda
