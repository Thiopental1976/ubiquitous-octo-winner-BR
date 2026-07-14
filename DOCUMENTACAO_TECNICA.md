# Linux File Search — Documentação Técnica

> Documento de referência para **avaliação e depuração** do projeto. Descreve arquitetura,
> cada módulo, o fluxo de dados, o modelo de concorrência, a gramática da busca booleana,
> o modo documentos, o player de mídia, o sistema de temas e a matriz de dependências por distro.
>
> **Versão do documento:** 2026-07-14 · **Autor:** Rodrigo Toledo (com Andrômeda/Claude)
> **Licença:** aberta e gratuita (baseada na MIT, sem direito de revenda)

---

## 1. Visão geral

**Linux File Search** é um buscador de arquivos **nativo para Linux**, sem índice, com
resultados **ao vivo**, no espírito do *Agent Ransack / FileLocator Pro* do Windows. Ele busca
por **nome** (glob/regex), por **conteúdo** (texto/regex), com **expressões booleanas**
(`(A OR B) AND C NOT D`) e **dentro de documentos** (PDF/docx/epub/odt/zip). Tem GUI em
**PySide6** e uma **CLI** equivalente que reaproveita o mesmo núcleo.

O motor de busca são binários externos maduros — **ripgrep** (`rg`) para conteúdo e **fd**
para nome — com **fallback em Python puro** quando eles não existem, garantindo execução em
qualquer distro. O modo documentos usa **ripgrep-all** (`rga`).

### 1.1 Por que existe

- Buscadores do Windows (FileLocator, Everything, UltraSearch) leem a MFT/USN do NTFS, que
  **não existe no Linux**; sob Wine só enxergam o prefixo. São inúteis aqui.
- Origem prática: o menu do Cinnamon estava lento porque um override do applet
  `menu@cinnamon.org` fazia busca de arquivos **síncrona** em `/home` e `/mnt` a cada tecla.
  A função foi reimplementada aqui de forma **assíncrona** (thread), sem travar a interface.

### 1.2 Princípios de projeto

1. **Núcleo sem Qt** (`engine.py`, `boolean.py`) — testável e reutilizável pela GUI e pela CLI.
2. **Motores externos, nunca reimplementados** — portáveis e mantidos por terceiros.
3. **Degradação graciosa** — sem `rg`/`fd`, cai para Python; sem `rga`, sem modo documentos;
   sem `QtMultimedia`, sem player (imagens ainda funcionam).
4. **Streaming** — resultados aparecem durante a busca; nunca bloquear a UI thread.
5. **"Buscar tudo" por padrão** — `--no-ignore` e ocultos togglável, como o Agent Ransack.

---

## 2. Estrutura de arquivos

```
linux_file_search/
├── lfs/
│   ├── engine.py      # NÚCLEO sem Qt: Query/Match, backends rg/fd, fallback Python
│   ├── boolean.py     # Busca booleana: tokenizer → AST → conjuntos de arquivos
│   ├── cli.py         # Interface de linha de comando (mesma engine)
│   └── app.py         # GUI PySide6: form, tabela ao vivo, preview texto/mídia, temas
├── assets/
│   ├── icon.svg       # ícone-fonte (256×256, gradiente + lupa)
│   └── icon_{48,64,128,256}.png, icon.png   # rasterizações (via QtSvg)
├── install.sh         # instalador universal multi-distro
├── linux-file-search  # lançador (aponta pro venv com PySide6)
├── README.md          # documentação de usuário
├── DOCUMENTACAO_TECNICA.md  # este arquivo
├── LICENSE            # MIT
├── requirements.txt   # PySide6 (motores são pacotes de sistema)
└── .gitignore
```

---

## 3. Núcleo — `lfs/engine.py`

Módulo sem dependência de Qt. Define os tipos de dados, detecta binários e implementa quatro
iteradores de busca (dois por nome, dois por conteúdo) mais a API pública `search()`.

### 3.1 Detecção de binários

```python
_APP_BIN = ~/.local/share/linux-file-search/bin   # binários empacotados (rga/pandoc)
_which(*names)   # shutil.which + fallback no _APP_BIN (os.access X_OK)
RG  = _which("rg")                    # ripgrep
FD  = _which("fd", "fdfind")          # fd (Debian/Mint renomeiam para fdfind!)
RGA = _which("rga", "ripgrep-all")    # ripgrep-all
engine_info() -> {"ripgrep":…, "fd":…, "rga":…}   # texto "(ausente …)" se faltar
```

O `_which` procura **primeiro no PATH** e depois no diretório de binários empacotados, para que
o instalador possa fornecer `rga`/`pandoc` estáticos sem root.

### 3.2 Tipos de dados

**`Query`** (dataclass) — todos os parâmetros da busca:

| Campo | Tipo | Significado |
|---|---|---|
| `paths` | `list[str]` | pastas onde buscar |
| `name_patterns` | `list[str]` | globs (lista) OU 1 regex |
| `name_is_regex` | `bool` | interpreta `name_patterns[0]` como regex |
| `content` | `str` | texto/regex a conter (vazio ⇒ busca só por nome) |
| `content_is_regex` | `bool` | conteúdo é regex (senão `--fixed-strings`) |
| `documents` | `bool` | busca dentro de documentos via `rga` (F4) |
| `case_sensitive` | `bool` | sensível a caixa (padrão insensível) |
| `whole_word` | `bool` | palavra inteira (`--word-regexp`) |
| `recursive` | `bool` | entra em subpastas |
| `max_depth` | `int?` | profundidade máxima |
| `include_hidden` | `bool` | inclui ocultos |
| `follow_symlinks` | `bool` | segue links |
| `respect_gitignore` | `bool` | `False` = busca tudo (`--no-ignore`) |
| `one_file_system` | `bool` | não cruza mounts (`--one-file-system`) |
| `min_size`/`max_size` | `int?` | bytes |
| `modified_after`/`modified_before` | `float?` | epoch |
| `max_results` | `int` | teto (default 100000) |

**`Match`** (dataclass) — um resultado: `path`, `size`, `mtime`, `is_dir`, `lines:
list[(lineno, texto)]` (até 200), `nmatch` (nº de casamentos).

### 3.3 Filtros comuns

- `_name_matcher(q)` → devolve `função(basename)->bool` (regex ou lista de globs;
  case-insensitive por padrão, à la Agent Ransack).
- `_passes_meta(q, st)` → aplica min/max size e modified_after/before sobre um `os.stat_result`.

### 3.4 Busca por NOME

- **`_iter_names_fd(q, cancel)`** — usa `fd`/`fdfind`. Um processo `fd` **por glob** (multi-glob),
  com `--absolute-path --type f`, flags de gitignore/hidden/symlink/one-fs/depth. Deduplica via
  `seen`. `stat` + `_passes_meta` por arquivo. Cancelamento via `proc.terminate()`.
- **`_iter_names_python(q)`** — fallback universal: `os.walk` com controle de profundidade,
  ocultos, symlinks e metadados. Poda `dns[:]` para não descer onde não deve.

### 3.5 Busca por CONTEÚDO

- **`_iter_content_rg(q, cancel)`** — o caminho principal. Monta `rg --json` (ou `rga --json`
  em modo documentos) e faz **parsing de eventos em streaming**:
  - `begin` → resolve o path, aplica filtro de nome-regex (o glob já vai como `--glob` no rg),
    `stat` + `_passes_meta`; guarda `cur = Match(...)`. **Em modo documentos**, se o path é
    interno a um container (ex.: `pacote.zip/interno.pdf`) e não tem `stat` no FS, emite
    `Match(path, 0, 0)` para **não perder o hit**.
  - `match` → acumula `nmatch += len(submatches)` e guarda até 200 linhas `(line_number, texto)`.
    `line_number` pode vir **`null`** (adaptadores de texto do rga) → tratado como `0`.
  - `end` → `yield cur`.
  - Em modo documentos **não** passa `--encoding auto` (o rga já entrega UTF-8).
  - Se o `Popen` falha (`OSError`), cai para o fallback Python.
- **`_iter_content_python(q, cancel)`** — varre nomes (via `_iter_names_python`) e faz "grep"
  em Python: lê linha a linha, aborta arquivo se achar `\x00` (binário), acumula linhas/nmatch.

### 3.6 API pública

```python
search(q, on_result, cancel=lambda:False, on_progress=lambda n:None) -> (total, segundos)
```

Escolhe o iterador: se há `content`, usa `rg` (ou `rga` p/ documentos) senão Python; se busca só
por nome, usa `fd` senão Python. Chama `on_result(Match)` em streaming, `on_progress(n)` a cada
25, respeita `cancel()` e `max_results`.

---

## 4. Busca booleana — `lfs/boolean.py` (recurso-assinatura, F3)

Implementa `(A OR B) AND C NOT D` resolvendo por **conjuntos de arquivos**.

### 4.1 Gramática e semântica

- **Termos**: palavra crua (até espaço/operador/parêntese) ou `"entre aspas"` (preserva espaços).
- **Operadores**: `AND OR NOT` (palavras, case-insensitive) e símbolos `& && | || !`.
- **Adjacência** = AND implícito (`foo bar` ≡ `foo AND bar`).
- **Precedência**: `NOT` (unário) > `AND` > `OR`. Parênteses agrupam.
- **NOT binário**: `A NOT B` ≡ `A AND (NOT B)`.

### 4.2 Pipeline

```
expr ──tokenize──▶ tokens ──_P.parse (descida recursiva)──▶ AST
AST ──_eval (conjuntos)──▶ arquivos-resultado
arquivos + termos positivos ──_display_lines (rg --json)──▶ linhas p/ preview
```

**AST**: `Term(text)`, `Not(node)`, `And(a,b)`, `Or(a,b)`. Erros ⇒ `BooleanError(ValueError)`.

**Parser** (`_P`), gramática de descida recursiva:
```
parse_or   := parse_and ( OR parse_and )*
parse_and  := parse_not ( (AND parse_not) | (NOT parse_not→Not) | (TERM|'(' →adjacência) )*
parse_not  := NOT parse_not | parse_atom
parse_atom := '(' parse_or ')' | TERM
```

### 4.3 Avaliação por conjuntos

- **`_files_with_term(term, q, cancel)`** → `set` de arquivos que contêm o termo, via `rg -l`
  (rápido); fallback `_files_with_term_py` (reusa `_iter_content_python`).
- **`_universe(q, cancel)`** → todos os candidatos, via `rg --files` (ou `_iter_names_python`).
  **Só é calculado se houver um `NOT`** (lazy, via `universe_box`).
- **`_eval`**: `And` = interseção `&`, `Or` = união `|`, `Not` = `universo − conjunto`.
  Cache por termo evita reconsultar o mesmo texto.
- **`_display_lines(pos_terms, files, q, cancel)`** — passada final: roda um único `rg --json`
  com todos os termos **positivos** (`positive_terms`, que ignora os negados) apenas sobre os
  arquivos-resultado, para preencher `Match.lines` do preview.

`search_boolean(q, expr, on_result, cancel, on_progress) -> (total, segundos)` orquestra tudo,
aplicando `_passes_meta` no fim (tamanho/data) e respeitando `max_results`/`cancel`.

---

## 5. CLI — `lfs/cli.py`

`argparse` sobre a mesma engine. Principais flags:

| Flag | Efeito |
|---|---|
| `path...` | pasta(s) (posicional, 1+) |
| `-n/--name` | globs separados por vírgula (`'*.py,*.txt'`) |
| `-c/--content` | texto/regex a conter |
| `-b/--bool EXPR` | busca booleana |
| `-D/--docs` | busca dentro de documentos (rga) |
| `--name-regex`, `--content-regex` | tratar como regex |
| `-s/--case-sensitive`, `-w/--word` | caixa / palavra inteira |
| `--hidden`, `--gitignore`, `--one-fs` | ocultos / respeitar .gitignore / não cruzar mounts |
| `--min-size 10M`, `--days N` | filtros de tamanho / data |
| `-l/--files-only` | só caminhos |
| `-0/--print0` | separador NUL (para `xargs -0`) |

Imprime o status do motor em `stderr` (`# motor: rg=… fd=… rga=…`) e avisa se `--docs` foi
pedido sem `rga`. Erros de expressão booleana saem com **exit code 2**.

Exemplos:
```bash
lfs ~/projetos -n '*.py' -c "def main"
lfs ~/docs -c laudo --docs
lfs ~/notas -b '(nota OR laudo) AND paciente NOT rascunho'
lfs /dados -c erro -l --print0 | xargs -0 du -h
```

---

## 6. GUI — `lfs/app.py` (PySide6)

### 6.1 Estrutura

- **`SearchWorker(QThread)`** — roda a busca fora da UI thread. Sinais: `batch(list[Match])`,
  `progress(int)`, `done(int, float)`, `error(str)`. Faz **throttle** dos resultados: emite
  lote a cada 100 ms **ou** a cada 200 itens (`_flush`). Ramo booleano vs. engine; `BooleanError`
  vira `error.emit` sem quebrar a thread. Cancelamento por flag `_cancel`.
- **`ResultModel(QAbstractTableModel)`** — colunas Arquivo/Pasta/Matches/Tamanho/Modificado.
  `append(matches)` usa `beginInsertRows` (crescimento incremental). Papéis: Display, alinhamento
  à direita (nº/tamanho), ToolTip (caminho completo), UserRole (o `Match`).
- **`MainWindow(QMainWindow)`** — monta a UI em `_build()`:
  - **Header** (`QFrame#header`): logo (icon_64), título/subtítulo, **badges de motor** (bolinha
    verde/cinza por rg/fd/rga) e o **botão de tema**.
  - **Barra de busca**: campo de conteúdo (grande) + `Buscar`/`Cancelar`.
  - **Nome + pasta**: glob de nome, pasta(s) separadas por `;`, botão `Procurar…`.
  - **Chips de opção** (`QCheckBox` estilizados como pílulas): Aa, palavra, booleano, documentos,
    regex conteúdo, regex nome, subpastas, ocultos, .gitignore, 1 disco, `Tam ≥`, `Últimos N d`.
  - **Splitter vertical**: tabela de resultados em cima, **preview** embaixo.
  - **Status bar** (QLabel).

### 6.2 Concorrência (fluxo)

```
start_search → _build_query → SearchWorker(q, boolexpr).start()
   worker.batch    → ResultModel.append   (tabela cresce ao vivo)
   worker.progress → status "N encontrados · Xs"
   worker.error    → status "expressão inválida: …"
   worker.done     → status "✔ N resultados · Xs"
Esc → cancel_search (flag) ; Ctrl+L foca conteúdo ; Ctrl+T alterna tema
```

A UI thread nunca faz I/O de busca. O `_cancel` é lido pelo iterador entre itens; processos
externos recebem `terminate()`.

### 6.3 Sistema de temas

- `THEMES = {"dark": {...}, "light": {...}}` — paletas com ~15 chaves (bg0..bg3, alt, border,
  txt, muted, accent, on_accent, green/amber/red…).
- `_STYLE_TMPL` — folha de estilo Qt com placeholders `{chave}`; `build_style(pal)` faz
  `.format(**pal)`.
- `apply_theme(name)` aplica o stylesheet, ajusta status e botão, e chama `_refresh_badges`.
- `toggle_theme()` inverte e **persiste** em `~/.config/linux-file-search/config.json`
  (`load_cfg`/`save_cfg`). Preferência é lida no `__init__`.
- **Nota de depuração:** os badges são reconstruídos em `_refresh_badges`; ao limpar o layout,
  usa-se `w.setParent(None)` **antes** de `deleteLater()` (senão, num `grab()` headless, os
  widgets antigos ainda aparecem sobrepostos ao novo conjunto).

### 6.4 Preview texto ↔ mídia (com player)

`_build_preview()` devolve um **`QStackedWidget`** com duas páginas:

- **Página 0 — texto** (`QPlainTextEdit` monospaçado): mostra as linhas casadas (`Match.lines`,
  com nº de linha) ou, se não houver, um "peek" das primeiras 80 linhas do arquivo (aborta em
  binário).
- **Página 1 — mídia**: um `QFrame#mediastage` com um `QStackedWidget` interno de 3 telas
  (imagem `QLabel` / áudio `♪` / vídeo `QVideoWidget`) + uma **barra de transporte**
  (`QFrame#mediabar`): `⏮` `▶/⏸` `⏭`, nome do arquivo, **slider de posição** e tempo `m:ss / m:ss`.

Detecção por extensão em `media_kind(path)` → `"image" | "video" | "audio" | None`
(`_IMG_EXT`, `_VID_EXT`, `_AUD_EXT`). Roteamento em `on_select`:

- **imagem** (sempre, é só `QtGui`): `QPixmap` escalado com `KeepAspectRatio`; reescala em
  `resizeEvent`; transporte desabilitado (rótulo "imagem").
- **vídeo/áudio** (só se `HAS_MEDIA`): `QMediaPlayer` + `QAudioOutput` (+ `QVideoWidget` p/ vídeo),
  `setSource` + `play()`. Sinais: `playbackStateChanged` (ícone ▶/⏸), `positionChanged`
  (slider+tempo, respeitando scrub), `durationChanged` (range), `mediaStatusChanged` (auto-avança
  no `EndOfMedia`).
- **prev/next** (`_nav_media(±1)`): navega entre as **linhas de mídia** dos resultados
  (`_media_rows()`), com **wrap**; `selectRow` dispara `on_select`.

**Portabilidade**: se `from PySide6.QtMultimedia import …` falhar, `HAS_MEDIA=False` — imagens
continuam funcionando e áudio/vídeo caem no preview de texto.

### 6.5 Ações de contexto

Menu de contexto e duplo-clique: **abrir arquivo**, **abrir pasta** (`QDesktopServices`),
**copiar caminho(s)** (clipboard). Multi-seleção suportada (até 10 no "abrir").

---

## 7. Modo documentos (F4) — ripgrep-all

`rga` expõe a **mesma CLI do rg** e, para PDF/docx/epub/odt/zip/tar, **extrai o texto** e
repassa em `--json` idêntico. Por isso o `engine._iter_content_rg` só troca o binário
(`rg`→`rga`) e o resto do parsing é reaproveitado. Adaptadores:

| Formato | Adaptador |
|---|---|
| PDF | poppler (`pdftotext`) |
| docx, epub, odt, html, ipynb | **pandoc** |
| zip, tar, gz | embutido no rga |

`line_number` pode vir `null` (adaptadores de texto) → tratado como 0. Caminho interno a
container não tem `stat` → `Match(path, 0, 0)` para não perder o hit.

---

## 8. Dependências e matriz por distro

| Dependência | Papel | Obrigatória? |
|---|---|---|
| Python ≥ 3.9 | runtime | sim |
| **PySide6** | GUI (traz QtMultimedia p/ o player) | sim (p/ GUI) |
| **ripgrep** (`rg`) | busca de conteúdo | recomendada (senão fallback Python) |
| **fd** (`fd`/`fdfind`) | busca por nome | recomendada (senão fallback Python) |
| **ripgrep-all** (`rga`) | modo documentos | opcional |
| **pandoc** | docx/epub/odt no rga | opcional |
| **poppler** (`pdftotext`) | texto de PDF no rga | opcional |

**Nome do pacote por gerenciador:**

| Lógico | apt (Debian/Ubuntu/Mint) | dnf (Fedora/RHEL) | pacman (Arch) | zypper (openSUSE) |
|---|---|---|---|---|
| ripgrep | `ripgrep` | `ripgrep` | `ripgrep` | `ripgrep` |
| fd | `fd-find` (bin `fdfind`) | `fd-find` | `fd` | `fd` |
| poppler | `poppler-utils` | `poppler-utils` | `poppler` | `poppler-tools` |
| ripgrep-all | `ripgrep-all`¹ | `ripgrep-all`¹ | `ripgrep-all` (AUR) | `ripgrep-all`¹ |
| pandoc | `pandoc` | `pandoc` | `pandoc` | `pandoc` |
| PySide6 | `python3-pyside6`² | `python3-pyside6`² | `pyside6` | `python3-PySide6` |

¹ Nem todo repositório traz `ripgrep-all`; o instalador então baixa o **binário estático** (musl,
x86_64) do GitHub. ² Se o PySide6 do sistema faltar, o instalador cria um **venv** e roda
`pip install PySide6`.

---

## 9. Instalação

### 9.1 Instalador universal (recomendado)

```bash
./install.sh
```

Fluxo (5 passos): detecta o gerenciador; **lista todas as dependências e pede confirmação**;
instala pacotes de sistema (com `sudo`, só se autorizado); baixa `rga`+`pandoc` estáticos se
faltarem; prepara PySide6 (sistema ou venv em `$PREFIX/venv`); copia o app para
`~/.local/share/linux-file-search/`, cria lançadores `linux-file-search` (GUI) e `lfs` (CLI),
ícones hicolor e atalho `.desktop`. Não precisa de root para o app (tudo em `~/.local`).

### 9.2 Manual

```bash
sudo apt install ripgrep fd-find poppler-utils   # exemplo Debian
pip install PySide6
python3 lfs/app.py     # GUI    |    python3 lfs/cli.py --help    # CLI
```

---

## 10. Testes e depuração

- **Suíte de regressão da auditoria**: `python3 tests/test_audit.py` — 8 testes
  auto-contidos (constroem árvore em tempdir, não tocam o acervo) cobrindo os
  consertos B1–B14 exercitáveis sem GUI. Ver §13.
- **Auto-teste de módulo**: `python3 lfs/engine.py <pasta> <termo>` e
  `python3 lfs/boolean.py <pasta> '<expr>'` imprimem AST/resultados.
- **GUI headless** (sem display): `QT_QPA_PLATFORM=offscreen` + `MainWindow().grab().save(png)`
  para capturar telas; popular `model.append([...])` com `Match` fabricados evita depender de I/O.
- **Casos-limite booleanos já validados**: NOT líder, aspas, símbolos `| & !`, erro de sintaxe.
- **Player validado headless**: roteamento imagem/vídeo/áudio, habilitação do transporte,
  navegação prev/next com wrap, volta ao preview de texto.
- **Pegadinhas conhecidas**:
  - `fd` vira `fdfind` no Debian/Mint — resolvido em `_which`.
  - `rg` do Claude Code é **função de shell**, não binário — `shutil.which` não o vê; instale o
    `ripgrep` de verdade.
  - `line_number` `null` no rga; caminho interno a container sem `stat`.
  - badges: `setParent(None)` antes de `deleteLater()` (ver §6.3).

---

## 11. Limitações e backlog

- **F5 — Conforto**: abas simultâneas, buscas salvas/histórico, export CSV/JSON, mais atalhos.
- **F6 — Portabilidade**: empacotar `.deb` e **AppImage** (PySide6 embutido; evitar Flatpak, cuja
  sandbox brigaria com ler o filesystem inteiro).
- `rga` pré-compilado só para `x86_64`; em outras arquiteturas, instalar pelo gerenciador.
- Realce de preview e destaque só valem para termos **literais**; regex de conteúdo não é realçado.
- Ordenação por coluna é habilitada ao fim da busca (durante a busca, ordem de chegada).
- **Otimizações da §3 da auditoria: #1, #2, #3 e #4 concluídas; e o N2 também** (ver §13/§14).
- Contador de "inacessíveis" é atualizado ao fim de cada processo (no `_reap`) — inclusive no
  modo **booleano** e nos fallbacks Python (N2, §13.6). Não é ao vivo por processo, mas o total final é correto.

---

## 12. Referência rápida de símbolos

| Módulo | Símbolos-chave |
|---|---|
| `engine.py` | `Query`, `Match`, `search`, `engine_info`, `_which`, `_iter_content_rg`, `_iter_names_fd`, `_iter_content_python`, `_iter_names_python`, `_passes_meta`, `_name_matcher`, `_glob_to_regex`, `_merge_globs`, `_reap`, `_walk_onerror` |
| `boolean.py` | `parse`, `tokenize`, `search_boolean`, `positive_terms`, `_eval`, `_files_with_term`, `_universe`, `_display_lines`, `_term_set`, `_or_operands`, `_max_workers`, `_under_mount`, `_Phase`, `_all_terms`, `_reap_stats`, `_merge_denied`, `BooleanError`, `Term/Not/And/Or` |
| `cli.py` | `main` (argparse) |
| `app.py` | `MainWindow`, `SearchWorker`, `ResultModel`, `THEMES`, `build_style`, `media_kind`, `_build_preview`, `_show_media`, `_nav_media`, `apply_theme` |

---

## 13. Auditoria Fable 5 — consertos aplicados

Auditoria de debug (`LinuxFileSearch_Auditoria_Debug.md`, 14/07/2026) achou 14
bugs provados/por-revisão + otimizações. Todos os consertos abaixo estão
implementados e cobertos por `tests/test_audit.py` (o que não é GUI) e por smoke
headless (GUI).

| # | Problema | Conserto | Onde |
|---|---|---|---|
| **B1** | rg/fd órfão ao cortar/abandonar a busca (varre `/mnt` em background) | `try/finally` + `engine._reap()` (terminate→wait→kill, idempotente) | `engine._iter_content_rg`/`_iter_names_fd`; `boolean._files_with_term`/`_universe`/`_display_lines` |
| **B2** | glob de nome caixa-sensível no rg (contrato é insensível) | `--glob-case-insensitive` quando `not case_sensitive` | `engine._iter_content_rg`, `boolean._rg_base` |
| **B3** | booleano ignorava filtro de nome REGEX | pós-filtro `re.search` no basename | `boolean.search_boolean` |
| **B4** | perda silenciosa de linhas por ARG_MAX (60k caminhos → `{}`) | lotes de ~400 caminhos por invocação do rg, mesclando dicts | `boolean._display_lines` (`_BATCH`) |
| **B5** | crash ao fechar a janela com busca viva (`QThread destroyed`) | `closeEvent`: `cancel()` → `wait(3000)` → `_stop_media()` | `app.MainWindow.closeEvent` |
| **B6** | booleano + documentos não combinam (ilusão de buscar em PDF) | exclusão mútua na GUI (marcar um desabilita o outro) | `app._on_bool_toggled`/`_on_doc_toggled` |
| **B7** | preview sem destaque do termo (recurso-assinatura) | `QTextEdit.ExtraSelection` âmbar sobre termos positivos literais | `app._apply_highlight` |
| **B8** | sem heartbeat/contadores (busca longa parece travada) | `QTimer` 0,5 s + contagem de "inacessíveis" via `stderr` (`stats["denied"]`) | `app._heartbeat`; `engine._reap(..., stats)` |
| **B9** | `one_file_system` ignorado no fallback Python (cruza mounts) | compara `st_dev` do root e poda `dns` | `engine._iter_names_python` |
| **B10** | higiene de argv no fd (falta `--`) | `--` antes do padrão e dos paths | `engine._iter_names_fd` |
| **B11** | nova busca não parava a mídia | `_stop_media()` + preview p/ página 0 | `app.start_search` |
| **B12** | imagem gigante decodificada síncrona congela a UI | `QImageReader.setScaledSize` (decodifica já reduzido) + teto 64 MB | `app._load_image` |
| **B13** | autoplay de vídeo COM ÁUDIO ao selecionar (constrangimento) | começa **mudo** por padrão; botão 🔇/🔊 persistido no config | `app._toggle_mute`, `cfg["muted"]` |
| **B14** | colunas sem ordenação | `QSortFilterProxyModel` + `SORT_ROLE` numérico, ligado ao fim da busca | `app.ResultModel.SORT_ROLE`, `app.proxy` |

**Otimização §5 aplicada**: `parse_size` unificado em `engine.parse_size`
(era duplicado em `app.py` e `cli.py`); `seen` do fd só quando há múltiplos
padrões.

### 13.1 Verificação v2 (`LinuxFileSearch_v2_Verificacao.md`)

A re-auditoria por execução confirmou as 14 correções e achou dois pontos reais,
já corrigidos:

| # | Problema | Conserto | Onde |
|---|---|---|---|
| **N1** | fd usa *smart-case*: com "Aa" LIGADO, padrão minúsculo ainda casava `N1.TXT` (rg era sensível → os motores divergiam de novo) | força `--case-sensitive` quando `case_sensitive` | `engine._iter_names_fd` |
| **N2** | contador de inacessíveis ausente no modo booleano (`stderr=DEVNULL`) e no fallback Python (`os.walk` engolia erros) | captura stderr + `_reap_stats` thread-safe; `os.walk(onerror=…)` e `PermissionError` de arquivo | §13.6 |
| **N3** | teto de imagem só valia quando as dimensões não vinham do cabeçalho; PNG/TIFF grande com header decodificava o raster inteiro na UI | teto de 64 MB **incondicional** → placeholder "abrir externo" | `app._load_image` |

Pendência restante (não urgente): N4 (miudezas de revisão) — ver §11.

### 13.2 Otimização #1 — AND com restrição progressiva (implementada)

A maior otimização da §3 da auditoria. Antes, cada termo de um `AND` varria a
**árvore inteira** com `rg -l`; agora o resultado parcial do lado esquerdo vira o
`restrict` do lado direito, que passa a varrer **só aqueles arquivos** (em lotes de
`_BATCH`, reusando o mecanismo do B4). O termo mais à esquerda é a única varredura
cheia; os seguintes leem apenas o conjunto acumulado.

- **Onde**: `boolean._eval` (propaga `restrict` por AND/OR/NOT), `boolean._term_set`
  (cache do conjunto cheio × varredura restrita), `boolean._files_with_term(..., restrict=)`.
- **Correção**: preservada porque a interseção distribui — `(X∘Y)∩R = (X∩R)∘(Y∩R)` —
  e um `AND` com lado esquerdo vazio faz **curto-circuito** (não varre o direito).
  O cache guarda só conjuntos CHEIOS; resultados restritos nunca o poluem.
- **Ganho**: em `raro AND comum` numa árvore grande, a 2ª varredura cai de
  *toda a árvore* para *só os arquivos de `raro`* — de minutos para milissegundos,
  e **muito menos I/O de disco** (crucial nos SMR — ver §14).
- **Testes**: `test_and_progressive_correctness` (mesmos resultados em AND/OR/NOT) e
  `test_and_progressive_restricts` (prova que a 2ª parte varreu 1 arquivo, não 51).

### 13.3 Otimização #2 — termos independentes em paralelo, com trava SMR (implementada)

Os operandos de um `OR` são varreduras **independentes** (nenhum depende do outro),
então rodam em paralelo num `ThreadPoolExecutor` — mas **só quando o disco aguenta**.

- **Onde**: `boolean._eval` ganhou o parâmetro `pool`; o ramo `Or` achata a cadeia
  (`_or_operands`) e faz `pool.submit` de cada operando. `search_boolean` cria o pool
  só quando `_max_workers(q) > 1`.
- **Trava SMR** (`_max_workers` + `_path_needs_serial`): se **qualquer** path da busca
  estiver sob `/mnt`, `/media` ou `/run/media` **sobre um disco rotacional ou
  desconhecido**, retorna **1 worker** (serial) — as cabeças do mesmo disco brigariam
  por *seek*. Fora dali (`~`, `/tmp`, SSD), usa `_WORKERS` (3 por padrão, afinável por
  `LFS_WORKERS`). O casamento é por **componente inteiro** de caminho, então `/mntx` não
  conta como `/mnt`.
- **Refinamento v3 — `rotational`** (parecer final Fable 5): serializar TODO `/mnt`
  penalizava um SSD/NVMe montado ali sem necessidade. Agora `_path_needs_serial` resolve
  o path → nó de dispositivo (mount de prefixo mais longo em `/proc/mounts`, via
  `_dev_for_path`) → disco inteiro → lê `/sys/block/<disco>/queue/rotational` (`_rotational`,
  sobe da partição p/ o disco). **`0` (SSD) libera o paralelismo mesmo sob `/mnt`**; `1`
  (rotacional) ou desconhecido (`None`) **serializa** — padrão seguro, pois SMR
  *drive-managed* (os Seagate USB) se reportam como disco comum e não há detecção honesta
  de SMR pelo sysfs. Validado nos discos reais: `/mnt/optane`, `/mnt/SSD128Gb` (rot=0) →
  paralelo; `/mnt/HDInternoBaixo`, `/mnt/DiscoQ` (rot=1) → serial.
- **Sem deadlock de pool aninhado**: as subtarefas submetidas recebem `pool=None`, então
  só o nível de `OR` alcançado pela thread principal paraleliza; um `OR` aninhado dentro
  de outro não tenta pegar mais workers (o que poderia travar o pool com todos os
  workers esperando por workers).
- **Thread-safe**: cache e universo são protegidos por `_cache_lock`; o I/O pesado
  (`_files_with_term`/`_universe`) roda **fora** do lock e a escrita usa `setdefault`
  (no pior caso de corrida, recalcula idêntico — idempotente).
- **Correção preservada**: cada `_files_with_term` paralelo ainda dá `_reap` no seu
  processo (B1), e a opt#1 (restrição do AND) segue intacta — o pool só distribui os
  irmãos de `OR`.
- **Testes**: `test_mnt_serializes` (mocka sysfs: rotacional/desconhecido em
  `/mnt|/media|/run/media` serializa, **SSD sob `/mnt` paraleliza**, `/mntx` não conta,
  misto com rotacional arrasta tudo p/ serial mas misto só-SSD não) e
  `test_or_parallel_correctness` (OR paralelo == OR serial, inclusive OR dentro de AND/NOT).

### 13.4 Otimização #3 — fd multi-glob → uma regex alternada (implementada)

No modo **só-nome**, o `fd` era chamado **uma vez por glob** (loop em `name_patterns`),
com dedup por `seen`. Com N globs, isso varria a árvore **N vezes** — N× o I/O, ruim
inclusive no SMR. Agora, quando há **>3 globs** (`_MERGE_GLOBS_MIN = 4`), eles são
fundidos numa **única regex alternada** e roda **um só `fd`**.

- **Onde**: `engine._glob_to_regex` (glob de basename → regex ancorada `^…$`,
  equivalente ao `fnmatch`: `*`→`.*`, `?`→`.`, classes `[...]` com `!`→`^`),
  `engine._merge_globs` (junta com `(?:a|b|…)`) e `engine._iter_names_fd` (decide
  fundir e troca `--glob` por regex).
- **Correção**: a regex é ancorada nos dois lados, então casa o **basename inteiro**
  como o glob. `test_glob_to_regex` compara caso a caso com `fnmatch.fnmatchcase`.
- **Guardas de segurança**: só funde globs **de basename** (sem `/` — glob de caminho
  fica no modo multi-fd, onde o `fd` casa a path toda); e a regex fundida é
  **validada com `re.compile`** antes — se falhar, cai no caminho antigo (um fd por
  glob). Nunca degrada silenciosamente para "nada encontrado".
- **`rg` não precisava**: no modo nome+conteúdo, o `rg` já recebe todos os `--glob`
  num **único processo** (uma passada); o problema das N varreduras era só do `fd`.
- **Ganho**: buscar `*.jpg *.png *.gif *.webp *.heic` num acervo passa de **5
  varreduras** para **1** — 5× menos I/O de diretório (ver §14).
- **Testes**: `test_glob_to_regex` (equivalência a fnmatch + recusa de glob com `/`) e
  `test_fd_merge_single_pass` (5 globs → **1 só** processo `fd`, união correta).

### 13.5 Otimização #4 — callback `on_phase` no booleano (implementada)

Uma busca booleana pesada (`(nota OR laudo) AND paciente NOT rascunho`) faz várias
varreduras `rg -l` — antes, a UI só dizia "Buscando…". Agora
`search_boolean(..., on_phase=cb)` relata a etapa: **"passo 2/4: termo 'paciente'"**
e, no fim, **"passo 4/4: extraindo linhas"**.

- **Onde**: `boolean._Phase` (contador de passos **thread-safe**), `boolean._all_terms`
  (conta os termos distintos do AST, positivos e negados), `_eval`/`_term_set`/
  `_universe_cached` recebem o `phase` e anunciam **só quando vão varrer o disco**
  (cache hit é instantâneo, não vira passo). Na GUI: sinal `SearchWorker.phase` →
  `MainWindow.on_phase` → texto no status (mostrado pelo heartbeat B8).
- **Total de passos**: termos distintos + 1 (a extração de linhas dos positivos).
  Cada termo é anunciado **uma vez** (dedup por nome), mesmo que a opt#1 o varra
  restrito depois.
- **Compatível com opt#2**: o contador é serializado por um `Lock` próprio, então a
  numeração sai coerente mesmo com os `OR` avaliados em paralelo (o I/O é que corre
  concorrente, não a contagem).
- **Retrocompatível**: `on_phase` é opcional (default `None`); a CLI, que chama
  `search_boolean(q, expr, out)`, continua igual.
- **Testes**: `test_on_phase_reports` (passos 1..total, total correto, último passo é
  "extraindo linhas", cada termo uma vez) e `test_on_phase_optional` (sem o callback,
  a busca funciona igual).

### 13.6 N2 — contagem de inacessíveis no modo booleano e nos fallbacks (implementado)

O contador de "N inacessível(is)" (B8) só funcionava na busca simples: o modo
**booleano** passava `stderr=DEVNULL` e o fallback Python engolia os erros do
`os.walk`. Agora o `stats['denied']` é preenchido em todos os caminhos.

- **Modo booleano**: `_files_with_term`, `_universe` e `_display_lines` passaram a
  **capturar o stderr** (tempfile) e contar via `_reap_stats`. `search_boolean` ganhou
  o parâmetro `stats` e o propaga por `_eval`/`_term_set`/`_universe_cached`. A GUI já
  passa `SearchWorker.stats`.
- **Thread-safe (opt#2)**: `_reap_stats` conta num dict **local** por processo e mescla
  no `stats` compartilhado sob `_cache_lock` (`_merge_denied`) — sem corrida mesmo com
  `OR` em paralelo. Os fallbacks Python usam o mesmo padrão (local → merge sob lock).
- **Fallback Python**: `engine._iter_names_python` agora passa `onerror=_walk_onerror(stats)`
  ao `os.walk` (conta `PermissionError` de diretório), e `_iter_content_python` conta
  também o `PermissionError` ao abrir arquivo. `engine.search` repassa `stats` a esses
  fallbacks (antes só o caminho `rg`/`fd` recebia).
- **Retrocompatível**: `stats` é opcional (default `None`); quem não passa (CLI) não conta.
- **Testes**: `test_walk_onerror_counts_denied` (diretório `chmod 000` no fallback conta
  ≥1) e `test_boolean_stats_denied` (booleano sobre a mesma árvore preenche `denied`,
  antes ficava 0). Ambos pulados como root (que ignora permissões).

---

## 14. Cuidado com discos SMR (e a diferença para CMR)

O Linux File Search é feito para rodar sobre acervos grandes espalhados em muitos
HDs — inclusive discos **SMR** e USB externos. Isso guia várias decisões do motor.

### 14.1 O que são SMR e CMR

- **CMR** (*Conventional Magnetic Recording*, também PMR): as trilhas **não se
  sobrepõem**. Cada setor pode ser reescrito no lugar. Escrita aleatória é
  previsível e rápida. É o disco "normal".
- **SMR** (*Shingled Magnetic Recording*): as trilhas são gravadas **sobrepostas
  como telhas** (daí *shingled*), o que aumenta a densidade/capacidade por um preço:
  reescrever um setor obriga a reescrever a faixa (*zone*) inteira ao redor. O disco
  usa uma zona de cache (CMR) e faz *garbage collection* depois. Consequências
  práticas:
  - **Leitura sequencial**: parecida com CMR (boa).
  - **Escrita/reescrita aleatória**: pode despencar para poucos MB/s quando o
    cache satura, com **travadas** enquanto o disco reorganiza as telhas.
  - **Seek concorrente** (vários leitores ao mesmo tempo, ou ler enquanto escreve)
    é especialmente ruim: as cabeças passam a saltar e o *throughput* real cai muito.
  - Drives **SMR device-managed** escondem tudo isso do SO — não dá para "ver" a
    zona; só dá para **evitar o padrão de acesso ruim**.

Muitos HDs de alta capacidade "de prateleira" (e vários USB externos) são SMR sem
avisar na caixa. No acervo do ServidorCedro, os discos mecânicos grandes tendem a
SMR; o CMR de referência é o WD Purple (DiscoL).

### 14.2 Como o programa trata isso

O princípio é simples: **ler o mínimo, uma vez, sem concorrência desnecessária, e
nunca deixar I/O pendurado**. Na prática:

- **Nada de rg/fd órfão** (B1): uma busca cortada ou uma janela fechada no meio
  **matam o processo** (`engine._reap`). Sem isso, um `rg` abandonado continuaria
  varrendo o disco inteiro em background — exatamente o *seek* concorrente que
  mata o SMR e ainda competiria com os daemons do acervo.
- **AND com restrição progressiva** (opt#1, §13.2): o segundo termo de um `AND`
  lê **só os arquivos que o primeiro já selecionou**, não a árvore toda. Menos
  arquivos abertos = menos I/O = menos castigo no SMR.
- **Multi-glob fundido** (opt#3, §13.4): buscar por vários tipos de arquivo
  (`*.jpg *.png *.gif …`) faz **uma única varredura** do `fd`, não uma por padrão —
  N× menos I/O de diretório num disco que odeia *seek*.
- **`--one-file-system` / chip "1 disco"**: evita que a varredura **cruze para
  outro ponto de montagem** sem querer (respeitado inclusive no fallback Python — B9).
  Útil para manter a busca dentro de um único USB e não acordar todos os discos.
- **Imagem grande não decodifica síncrona** (B12/N3): teto de 64 MB → placeholder.
  Um TIFF de 200 MB num SMR levaria segundos de leitura e **congelaria a UI**.
- **Streaming, não slurp**: o motor consome a saída do rg/fd linha a linha e emite
  resultados ao vivo; não acumula o disco inteiro em memória antes de mostrar nada.
- **Metadados por `os.stat`** só nos candidatos que já passaram no filtro de nome —
  não se faz `stat` de tudo.

### 14.3 Paralelismo consciente (opt#2, implementada)

A otimização **#2 (termos independentes em paralelo)** já está no código (§13.3) e é
ligada **quando os paths NÃO estão sob `/mnt` / `/media` / `/run/media` num disco
rotacional/desconhecido**: em CMR/SSD, 2–3 `rg` concorrentes aproveitam a CPU do i7; em
SMR/USB rotacional, o *seek* concorrente faria mais mal que bem, então lá a busca continua
**serializada** de propósito (`_max_workers` devolve 1). O **refinamento v3** olha o
`rotational` do sysfs, então um SSD/NVMe montado sob `/mnt` (ex.: `/mnt/optane`) **não** é
serializado à toa — só o rotacional (ou o desconhecido, por segurança) é. Essa é a regra de
ouro do projeto: **paralelizar onde o disco aguenta, serializar onde ele sofre**. O grau de
paralelismo é afinável por `LFS_WORKERS` (default 3; `1` serializa tudo).
