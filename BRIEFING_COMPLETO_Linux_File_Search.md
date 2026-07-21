# Linux File Search — briefing completo

Documento de contexto para **desenhar** uma mudança no projeto.
Gerado em 2026-07-21 a partir do repositório no commit `bcc06db` (working tree limpo,
33/33 testes passando).

> **Divisão de trabalho.** Quem lê este documento **desenha a mudança**: proposta, decisões de
> arquitetura, trade-offs, casos de borda a cobrir, critério de aceite. **A implementação é
> feita depois pelo Claude Code no ServidorCedro** — que tem os 13 discos reais, os discos SMR
> e o acervo para validar de verdade. Não é preciso entregar patch nem código pronto; entregue
> o desenho e o raciocínio. Se apontar código, aponte por arquivo e função (o briefing traz a
> API real de cada módulo na seção 3).

---

## 1. O que é e por que existe

Buscador de arquivos **nativo para Linux**, no espírito do **Agent Ransack / FileLocator Pro**
que o Rodrigo usava no Windows.

O problema de origem foi concreto: os buscadores do Windows são inúteis no Linux porque leem
MFT/USN do NTFS. E o menu do Cinnamon estava lento por um override que fazia busca de arquivo
**síncrona** em `/home` e `/mnt` a cada tecla digitada — em disco SMR isso trava o desktop.
O override foi revertido ao padrão e a função migrou para este app, assíncrono.

Dois requisitos que o Rodrigo fixou e **não devem ser renegociados**:

1. **Roda em qualquer distro** — sem depender de pacote específico, com fallback em Python puro
   quando `rg`/`fd` não existirem.
2. **O nome não muda.** É "Linux File Search". O codinome antigo ("garimpo") está aposentado.

Contexto de uso real: um servidor doméstico com **13 discos montados**, vários deles SMR e
lentos, com acervo de dezenas de milhares de vídeos. As decisões de performance do projeto
saíram desse ambiente, não de benchmark sintético.

---

## 2. Estado atual

| item | estado |
|---|---|
| Fases F0–F4 | prontas |
| Auditoria adversarial | feita (Fable 5) — B1–B6 + E1–E10 + A1/A3 corrigidos |
| Testes | **33/33** (`tests/test_audit.py`, runner próprio, sem pytest) |
| i18n | inglês é a fonte, pt por tabela |
| Publicado | GitHub `Thiopental1976/ubiquitous-octo-winner-BR`, branch `main` |
| Instalado | `~/.local/share/linux-file-search/` (venv PySide6 6.11.1, sem sudo) |
| Parecer externo | "projeto mais bem executado do ServidorCedro"; aprovado para uso diário |

Falta: **F5** (abas, buscas salvas, export) e **F6** (`.deb` / AppImage / diálogo de instalação
por distro). São os candidatos naturais a próxima mudança.

---

## 3. Arquitetura

```
lfs/
  engine.py   587 linhas   núcleo de busca, SEM Qt (importável de CLI/testes)
  boolean.py  666 linhas   parser + avaliador de expressões booleanas
  app.py     1153 linhas   GUI PySide6
  i18n.py     210 linhas   tradução; inglês-fonte
  cli.py       75 linhas   interface de linha de comando
tests/
  test_audit.py 860 linhas suíte de regressão (runner próprio)
install.sh                 instalação sem sudo, cria venv e launchers
```

**Regra de camada que importa:** `engine.py` e `boolean.py` **não importam Qt**. É o que permite
testar o motor sem display e rodar a CLI em pipeline. Qualquer mudança deve preservar isso.

### 3.1 `engine.py` — o motor

Dois caminhos, com autodetecção e fallback:

| busca | ferramenta | fallback |
|---|---|---|
| por nome | `fd` (`fdfind`) — `_iter_names_fd` | `os.walk` — `_iter_names_python` |
| por conteúdo | `rg --json` streaming — `_iter_content_rg` | leitura Python — `_iter_content_python` |
| dentro de PDF/docx/epub/zip | `rga` (ripgrep-all) | — (opcional) |

Peças principais:

- `Query` (dataclass) — todos os parâmetros da busca; `Match` — um resultado.
- `search(q, on_result, ...)` — ponto de entrada; empurra resultados por callback (streaming,
  não acumula lista).
- `parse_size(s)` — "10M", "1.5G"; retorna `None` para negativo.
- `as_name_glob(term)` / `_merge_globs` — se houver mais de 3 globs (`_MERGE_GLOBS_MIN = 4`),
  funde tudo numa regex só em vez de passar N `--glob` ao fd (**opt#3**).
- `user_mounts()` — lista os discos montados, alimenta o menu "Discos ▾".
- `engine_info()` — o que está disponível no sistema; a GUI mostra isso.

**Busca por nome acha ARQUIVOS E PASTAS** (case-insensitive), mas só quando o campo Conteúdo
está vazio. Com Conteúdo preenchido, só faz sentido arquivo. Vale nos dois caminhos:
`fd --type f/d/l` e o `os.walk`. Pasta aparece na tabela com `/` e sem tamanho.

### 3.2 `boolean.py` — expressões booleanas

`(A OR B) AND C NOT D`, com precedência, parênteses e frases entre aspas.

- `tokenize` → `parse` → AST de `Term` / `Not` / `And` / `Or`; erros viram `BooleanError`.
- Avaliação por **conjuntos de caminhos**: `_files_with_term` roda `rg -l` e devolve um `set`;
  AND é interseção, OR é união, NOT é subtração do universo.
- `_universe` — o conjunto de partida do NOT. **É só-texto**, mesmo domínio da busca positiva
  (ver B1 na seção de armadilhas).
- `_BATCH = 400` caminhos por invocação do `rg`, para não estourar `ARG_MAX`.

Otimizações, todas com teste de regressão:

1. **AND progressivo** — o resultado parcial restringe (`restrict=`) a varredura seguinte.
2. **OR em paralelo** — `ThreadPoolExecutor`, `_WORKERS = 3` (env `LFS_WORKERS`).
3. **Fusão de globs** — ver acima.
4. **`on_phase`** — callback de progresso ("passo 2/4"), classe `_Phase`.
5. **Single-flight** — dois operandos que pedem o mesmo termo (ou dois NOT que pedem o universo)
   não varrem o disco duas vezes; o primeiro computa, os demais esperam um `Event`.

**Trava de SMR — a parte mais específica deste projeto.** Paralelizar leitura em disco
rotacional torna tudo mais lento, não mais rápido (seek thrash). Então:

```
_path_needs_serial(ap)  ->  _dev_for_path(ap)  ->  _rotational(dev)
                            lê /sys/block/<disco>/queue/rotational
```

Sob `/mnt`, `/media`, `/run/media`: **SSD paraleliza; rotacional ou desconhecido serializa.**
Foi validado nos discos reais do servidor. Mudança que mexa em concorrência precisa preservar
isso, ou o app fica lento justamente na máquina para a qual foi feito.

### 3.3 `app.py` — GUI

- Busca roda em `QThread`; tabela atualiza ao vivo com **throttle de 100 ms**.
- Barra principal = campo **NOME** (foco em Ctrl+L). Campo Conteúdo é separado.
- Menu **"Discos ▾"** com **"All disks"** — liga/desliga todos os discos montados de uma vez,
  preservando pastas digitadas à mão. Pedido explícito para não selecionar disco a disco.
- Chips de filtro em `FlowLayout` (quebram linha; cabe em tela retrato).
- Preview com destaque do trecho, menu de contexto, tema escuro.
- Zero resultados **com o campo Conteúdo preenchido** mostra uma **dica** explicando que a busca
  foi DENTRO dos arquivos — era a pegadinha de digitar `*.mp4` no Conteúdo e receber nada.

### 3.4 `i18n.py`

Inglês é a **fonte** (as strings no código são inglês); português vem por tabela `_PT`.
Detecção por `LFS_LANG` > `LC_ALL` > `LC_MESSAGES` > `LANG` > `LANGUAGE`; idioma desconhecido
cai para inglês. Todos os textos da GUI passam por `t()`, inclusive os rótulos de progresso do
`boolean.py`. 85 chaves, com teste que falha se houver chave órfã.

O servidor está em `en_US`, então a GUI abre em inglês — e o Rodrigo prefere assim
("inglês é minha segunda língua, me sinto em casa").

**Decisão de design:** a `cli.py` é fixa em inglês e **não** importa i18n — convenção Unix.
Não "conserte" isso.

---

## 4. Armadilhas já pagas — leia antes de mexer

Cada uma custou um bug real. A lição de todas juntas: **teste na mesma forma que o consumidor
usa**, e teste com entradas hostis, não com nomes bonitos.

| id | o que era | fix |
|---|---|---|
| **E1** | `fd` sem `--print0`: nome com `\n` sumia ou casava caminho errado | `--print0` + leitura NUL-delimitada |
| **E2** | nomes não-UTF-8 (USB, acervo antigo) sumiam | `os.fsdecode` com surrogateescape |
| **E3** | fallback `os.walk` casava 1 nível a mais que o fd (`--max-depth` conta filhos diretos = 1) | gate por profundidade |
| **E4** | fallback com `follow_symlinks` travava em ciclo | guarda `(st_dev, st_ino)` |
| **E5** | symlink quebrado casado por nome sumia (`os.stat` falha no alvo) | fallback `os.lstat` |
| **E9** | `parse_size` aceitava negativo | retorna `None` |
| **E10** | `_iter_names_python` ignorava `cancel()` | passa a honrar |
| **B1** | `NOT termo` despejava binários: universo era `rg --files` (tudo), termos são `rg -l` (pula binário) → todo JPG/MP4 do acervo poluía | universo vira só-texto: `rg -l -e ""` com `_rg_base(matching=False)`; fallback usa heurística NUL nos 1ºs 8 KB |
| **B2** | parênteses fundíssimos → `RecursionError` | vira `BooleanError` |
| **B3** | frase entre aspas fechava na aspa interna | tokenizer respeita `\"` e `\\` |
| **B4** | frase só-espaços passava; `rg -e " "` casa quase toda linha | recusada como o `""` vazio |
| **B5** | OR paralelo varria o mesmo termo 2x | single-flight |
| **B6** | variável de laço `t` sombreava o tradutor `t()` | renomeadas |
| **A1** | `closeEvent` esperava 3 s e destruía `QThread` viva → órfão `rg`/`fd` | espera a thread sair bombeando eventos; `terminate()` só após 8 s |
| **A3** | habilitar ordenação no fim re-ordenava pela coluna 0 | zera o indicador antes |
| **fd `/`** | `fd --absolute-path` emite diretório com `/` final → `os.path.basename()` = `""` → **pasta sem nome na GUI** | `rstrip("/")` com guarda `len>1`. O teste passava porque comparava via `relpath` (normaliza a barra) e a GUI usa `basename` |

### Conhecidos e NÃO corrigidos de propósito

- Itens cosméticos de preview de imagem.
- (B1–B6 estão todos limpos; a lista acima é histórico.)

---

## 5. Como rodar

```bash
# instalado (sem sudo)
linux-file-search          # GUI
lfs --help                 # CLI

# a partir do repo
~/.local/share/linux-file-search/venv/bin/python -m lfs.app
~/.local/share/linux-file-search/venv/bin/python -m lfs.cli --help

# testes (runner próprio, NÃO usa pytest)
~/.local/share/linux-file-search/venv/bin/python tests/test_audit.py
# esperado: 33/33 testes passaram
```

Dependências: **PySide6 >= 6.5** (única dep Python). `ripgrep` e `fd` são pacotes de sistema —
sem eles o app funciona pelo fallback Python, mais lento. `ripgrep-all` + `pandoc` +
`poppler-utils` são opcionais (busca dentro de PDF/docx/epub/odt).

A CLI tem `--print0`, pensada para pipeline com o resto do acervo.

---

## 6. Backlog — candidatos a próxima mudança

- **F5** — abas, buscas salvas, export de resultados.
- **F6** — empacotamento: `.deb`, AppImage, diálogo de instalação por distro.
- Contagem de "inacessíveis" exposta na GUI (o motor já conta em `stats`).
- Refinamento cosmético do preview de imagem.

---

## 7. Invariantes — qualquer desenho precisa respeitar

1. `engine.py` e `boolean.py` **sem Qt**.
2. O **nome do app não muda**.
3. **Fallback Python puro** continua funcionando (portabilidade entre distros).
4. **Trava de SMR** preservada — é o que torna o app usável na máquina real.
5. **Inglês é a fonte** do i18n; texto novo de GUI passa por `t()` e ganha chave em `_PT`.
   CLI fica em inglês, sem i18n.
6. Todo fix ganha **teste de regressão** em `tests/test_audit.py`, escrito na forma que o
   consumidor real usa.
7. Streaming por callback — não acumular resultado em lista antes de mostrar.

---

## 8. Onde as coisas estão

| o quê | onde |
|---|---|
| repositório | `~/projetos/linux_file_search/` |
| instalação | `~/.local/share/linux-file-search/` |
| GitHub | `Thiopental1976/ubiquitous-octo-winner-BR`, branch `main` |
| doc técnica longa | `DOCUMENTACAO_TECNICA.md` (37 KB, neste repo) |
| doc de projeto | `PROJETO_Linux_File_Search.md` |
| README público | `README.md` / `README_LINUX_FILE_SEARCH.md` |
| parecer externo v3 | `~/Downloads/LinuxFileSearch_v3_Parecer_Final.md` |
| desenho original | `~/Downloads/GARIMPO_Desenho_Busca_ripgrep.md` |
