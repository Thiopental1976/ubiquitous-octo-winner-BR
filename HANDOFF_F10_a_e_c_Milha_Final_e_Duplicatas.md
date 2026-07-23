# Handoff — F10 INTEIRO aplicado (A milha final humana + Duplicatas)

**De:** Andrômeda (Claude Opus 4.8) · **Para:** Fable 5
**Base:** desenho `DESENHO_F10_Milha_Final_Humana_e_Duplicatas.md`
**Repo:** sombrero-file-search · **Suite:** 115/115 verde · tudo commitado **e** empurrado.

> **Atualização 24/07 — bateria adversarial + P1 + soak (resposta ao teu capstone):**
> Rodei os **9 ataques do núcleo** no caminho novo "duplicatas dos resultados" que tu
> pediste antes de confiar nele — e **achou o bug que tu previu**: dois hardlinks de
> mesmo nome (um inode só) vinham marcados como *versões diferentes*. Corrigido em
> `name_verdicts` colapsando os `files` por inode (lstat) ANTES de classificar — hardlink
> deixou de ser cópia ou versão, virou um arquivo físico só. **+8 testes** (`cb79f8e`),
> suite 106→114.
> Depois os dois que tu marcaste como pendentes: **P1** (`7c2ba50`) — labels de disco
> colididos agora desambiguam com o mountpoint via `disks.menu_labels()` compartilhado
> pelos dois menus (busca + duplicatas), +1 teste → **115**. E o **soak** (`tests/soak_local.py`,
> fora da suíte rápida): **300 buscas + 100 cópias reais pelo worker + 50 previews**, medindo
> RSS por iteração. Resultado: **RSS chapado (Δ 0,0 MiB nas três fases)** — nenhum vazamento
> detectável. O maior risco residual que tu apontou está, por ora, refutado no offscreen.
> Falta só o metal de sábado (arrancar cabo no meio da escrita etc.).

> **Novidades desde a v1 deste handoff:** (1) o **F10b #4 e #5** entraram (seção própria
> abaixo); (2) o caçador de duplicatas foi **promovido de janela para ABA embutida** — a
> antiga decisão #1 está resolvida; (3) **o F10c ganhou a entrada por RESULTADOS da
> busca** (`77777f5`) — pedido do Rodrigo: *"as duplicatas a serem analisadas são as
> dos arquivos oriundos das buscas feitas pelo usuário"*. O caso de uso agora é o que
> ele descreveu: **o usuário busca, vê arquivos de mesmo nome espalhados pelos discos,
> e o dedup diz se são cópias idênticas ou versões diferentes.** Detalhes na subseção
> *F10c — entrada por resultados* abaixo. Suite 103→106.

---

## O que ficou pronto

### F10a — A milha final humana

**#1 Filtro-nos-resultados** (já estava; núcleo + GUI)
- `c200efd` núcleo: predicado puro, sem I/O (`resultfilter.py`).
- `f428edf` GUI: caixa de filtro + **discos por label** (não mountpoint) na caixa de seleção.

**#2 Painel de narrativa da busca — NO ALTO da GUI, legível** (pedido explícito do Rodrigo:
"barras e informações no alto, legíveis, não fonte miúda no fundo")
- `3051ea5` engine: emite `root_scanning` / `root_skipped` / `root_done` por raiz.
- `889507c` GUI: `QFrame#narrative` entre os chips e os resultados. Cabeçalho grande
  ("Varrendo 3/12 locais · 240 achados · 4s") + uma linha por raiz com bolinha colorida
  (verde=pronto, vermelho=inacessível, cor de acento=varrendo agora). Montagem de rede
  morta vira **linha vermelha, não popup**. Estado é por-aba; um widget só renderiza a
  aba visível.

**#3 Teclado de ponta a ponta** (`fff7f9e`)
- Esc inteligente (cancela busca / limpa filtro), ↑/↓ histórico no campo de nome,
  Ctrl+F filtro, Ctrl+L caminho, F3/Shift+F3 navegam matches no preview, Ctrl+R repete.
- MANUAL.md / MANUAL.pt-BR.md com as tabelas completas; refs velhas de F3/Ctrl+L
  corrigidas em DOCUMENTACAO_TECNICA e README.

### F10b — confiança (a parte que fideliza)

**#6 humane.py** (`e6aff54`, já era) — nenhum errno cru chega à tela; guarda AST no
`app.py` garante que toda string de erro passa por `humane.human_error`.

**#4 O momento depois da barra: "pode remover com segurança"** (`ed5cc0e`)
- Cópia concluída para disco **removível** → a barra **não some**: vira
  *"Copiado e sincronizado — seguro remover"* com botão **⏏ Ejetar** ao lado. A
  promessa é verdadeira — o ATOMIC faz `fsync` por arquivo.
- Ejetar usa `gio mount -e` **ou** `udisksctl power-off -b <dev>`, o que existir;
  sem nenhum dos dois, o botão nem aparece (sem dependência nova). O comando é
  `disks.eject_command(mp, dev, which=…)` — **puro, `which` injetável** (testado).
- Cópia > 30 s numa janela minimizada/inativa → **notificação de desktop**
  (`notify-send` ou bandeja Qt, usa-se-existir).

**#5 Fila de cópia sobrevive ao fechamento** (`ed5cc0e`)
- Jobs pendentes + o interrompido persistidos em `config.json` a cada **transição**
  de job (não por arquivo — barato). Módulo **puro** novo `lfs/copyjobs.py`
  (serializa/valida/snapshot/pending/clear).
- Na abertura: *"Você tinha N cópias pendentes — retomar?"* [Retomar] [Descartar].
- Retomada **idempotente** por desenho: a escrita ATOMIC garante que concluído vira
  **conflito** (a política decide; padrão da GUI = Pular) e `.sombrero-part` órfão é
  lixo reconhecível. Destino não montado é barrado no preflight → o job fica
  pendente, não some.

**4 testes novos** (suite 99→103):
- `test_eject_command_prefers_gio_then_udisks` (gio → udisksctl → None, `which` fake);
- `test_copyjobs_snapshot_roundtrip`, `test_copyjobs_rejects_malformed`;
- `test_copyjobs_resume_is_idempotent` — **o teste do `kill -9`**: 2 jobs, "morte" no
  meio (um arquivo já copiado + um `.part` órfão), reabrir e retomar → estado final
  **idêntico** ao de uma execução sem morte. Rodado no nível do `fileops.copy_to`,
  headless. Smoke offscreen confirmou a barra "seguro remover" + Ejetar + o prompt de
  retomada lendo a fila do config.

### F10c — Caçador de duplicatas NATIVO

**A linha vermelha primeiro:** o SFS **acha, mostra e exporta** duplicatas — **nunca apaga**.
Nem com confirmação, nem "só a lixeira". `lfs/dupes.py` não tem função de remoção e não
deve ganhar uma. Recusa escrita no README ao lado das outras (`49ffb70`).

**Núcleo** (`9340c65`) — `lfs/dupes.py`, **código próprio do SFS** (não dependência; o
motor do cedro `dedup_layer1.py` serve só de **oráculo** nos testes de paridade):
- Estágio 0 — identidade física por `(st_dev, st_ino)`: **hardlinks são UM candidato,
  não duplicatas**. Symlinks fora. Tamanho 0 fora por padrão.
- Estágio 1 — tamanho (só grupos com ≥2).
- Estágio 2 — hash de cabeça BLAKE2b 64 KiB.
- Estágio 3 — hash completo BLAKE2b, **sequencial por dispositivo** (ordena por st_dev —
  disciplina SMR), `fadvise DONTNEED`, cancel por bloco, progresso honesto em bytes.
- Grupos ordenados por bytes desperdiçados. `export()` CSV/JSON com `surrogateescape`
  (nomes hostis do acervo não estouram).

**GUI — ABA embutida** (`9bb32b7` como janela, promovida a aba em commit desta
sessão): o app ganhou um **workspace de topo com duas páginas isoladas** —
*🔍 Buscar* (a busca inteira: formulário, abas de resultado, preview, cópia) e
*⧉ Duplicatas* (o caçador). É a "aba própria com os mesmos chips de caminho" que teu
desenho pediu, **resolvendo a decisão #1 da versão anterior deste handoff** (eu tinha
feito como janela não-modal e deixado para promover no presencial — o Rodrigo pediu
para fazer já).
- Campos de raízes (semeados a partir da busca), incluir-vazios, tamanho-mínimo;
  Varrer/Cancelar. Cabeçalho grande + barra de progresso; árvore
  [Arquivo / Tamanho / **Disco por label**]. Export CSV / Export JSON.
- **Por que duas páginas e não a aba de duplicatas dentro de `self.tabs`:** há dezenas
  de acessos a `self.tab.*` (tabela, modelo, formulário, worker) que só fazem sentido
  numa aba de BUSCA; uma aba de duplicatas ali dentro os quebraria. Páginas separadas
  dão isolamento total, cada modalidade com entradas e ciclo de vida próprios.
- `DuplicatesWindow(QDialog)` → `DuplicatesPanel(QWidget)`: saiu título/tamanho-mínimo/
  botão *Close*; entrou `seed()` (semeia só se vazio — não pisa no que o usuário digitou)
  e `shutdown()` (a janela principal aborta o hash em voo no `closeEvent`). O botão
  *Duplicatas…* da toolbar **pula para a aba** (e passou a analisar os resultados da
  busca — ver a subseção *entrada por RESULTADOS* logo abaixo).

**9 testes novos** (suite 90→99), incluindo:
- `test_dupes_hardlink_is_not_duplicate`, `..._across_devices_groups` (monkeypatch st_dev),
  `..._cancel_leaves_no_state`, `..._no_delete_api` (guarda AST: nada de remove/unlink/
  rmtree/rename no módulo), `..._export_csv_json_hostile_names`,
  `..._parity_with_oracle` (carrega dedup_layer1.py e confirma grupos idênticos).

### F10c — entrada por RESULTADOS da busca (cópia vs versão) — `77777f5`

O pedido do Rodrigo reposicionou o F10c: a entrada natural do dedup são **os arquivos
que a busca achou**, não pastas soltas. O cenário-alvo, nas palavras dele: *"o usuário
faz uma busca e vê que ela contém arquivos que parecem iguais, distribuídos entre
discos; o dedup diz a ele se são cópias ou versões diferentes do arquivo de mesmo
nome"*. E — *"ter a aba de busca ampla por duplicados tem seu valor sim"* — a varredura
standalone **fica**, agora com o mesmo seletor de discos da busca.

**Motor** (`dupes.py`, segue sendo código próprio, não dependência):
- `find_duplicates_in_files(files, …)` — mesma disciplina de I/O do funil (inode →
  tamanho → cabeça → hash completo, sequencial por dispositivo/SMR, `fadvise DONTNEED`,
  cancel por bloco, progresso em bytes), mas a **porta de entrada é uma lista de
  arquivos**, não `os.walk`. Refatorei o funil comum em `_dedup(cands, …)`; as duas
  portas (`_walk` da varredura e `_collect` da lista) partilham-no — zero divergência
  de comportamento entre elas (travado por `test_dupes_in_files_matches_walk`).
- `name_verdicts(files, groups) -> [NameGroup]` — o **veredito ancorado no NOME**, que é
  a pergunta do usuário. **Sem I/O**: deriva dos grupos de conteúdo já hasheados.
  Chave-de-conteúdo por caminho = digest do grupo; quem não caiu em grupo é único (usa o
  próprio caminho). Por basename repetido: `IDENTICAL` (todos byte-idênticos → cópias),
  `DIVERGENT` (mesmo nome, conteúdos todos distintos → versões), `MIXED`. Dois de mesmo
  nome com **tamanhos diferentes** saem como versão **sem precisar hashear** (nunca
  entram no mesmo grupo de tamanho).

**GUI** — o `DuplicatesPanel` mantém as **duas modalidades**:
1. **⇊ Analisar resultados da busca** (botão-manchete no topo do painel; o botão
   *Duplicatas…* da toolbar também dispara isto quando há resultados). Roda
   `find_duplicates_in_files` sobre `self.tab.model.rows` e mostra a **árvore ancorada
   no nome**: selo 🟢 *cópias idênticas* / 🟠 *versões diferentes — mesmo nome* / 🟡
   *mistura*, com **disco por label** e o **tamanho** ao lado (versões costumam diferir
   já no tamanho). Cabeçalho: "N idênticos · M versões diferentes · … · X recuperável".
2. **Varredura ampla** standalone (raízes + `📂`), agora com o **mesmo menu `Discos ▾`
   da busca** — *Todos os discos* de uma vez + discos por **label**. Vista ancorada em
   conteúdo (bytes desperdiçados), como antes.

`DupWorker` ganhou o parâmetro `files=` (modo lista) ao lado de `roots=`. A linha
vermelha do F10c segue intacta: **acha, mostra, exporta — jamais apaga.**

**3 testes novos** (suite 103→106): `test_dupes_in_files_matches_walk` (lista ≡
varredura), `test_dupes_name_verdicts_copy_version_mixed` (distingue cópia/versão/
mistura + desperdício), `test_dupes_name_verdicts_diff_size_is_divergent_without_hash`.

---

## Decisões que quero teu olhar

1. ✅ **RESOLVIDA — Entrada da GUI do F10c:** era janela não-modal, virou **aba embutida**
   nesta sessão (o Rodrigo pediu para fazer já, não no presencial). Detalhes na subseção
   *GUI — ABA embutida* acima. A forma final é a que teu desenho pedia.

2. **Notificação de ejeção sem tray real:** no smoke offscreen usei `notify-send`/bandeja
   com fallback silencioso. No teu ambiente headless (cron), nenhum dos dois existe e a
   notificação simplesmente não sai — de propósito, é um extra e não um dever. Se quiser
   que ela apareça num log, é um `log()` a mais, me diz.

3. **Nada mais pendente no F10.** a, b e c fechados e testados (103/103).

## O que falta (fora do F10, pra sábado presencial)

- Borda/visual da GUI.
- **SMB real** contra o Win 11 na LAN (F9 — busca em rede de verdade).
- Checklist do Philips PMC 7230.
