<div align="center">

<img src="assets/icon_128.png" width="96" alt="Sombrero File Search">

# Sombrero File Search

**Busca de arquivos nativa para Linux — nome, conteúdo, booleano e dentro de documentos.**
*A native file-search tool for Linux, in the spirit of Agent Ransack / FileLocator Pro.*

![Python](https://img.shields.io/badge/Python-3.9%2B-3776ab)
![PySide6](https://img.shields.io/badge/GUI-PySide6-41cd52)
![ripgrep](https://img.shields.io/badge/engine-ripgrep%20%2B%20fd-orange)
![License](https://img.shields.io/badge/license-GPL--3.0--or--later-blue)

</div>

---

## O que é

Um buscador de arquivos **sem índice**, com resultados **ao vivo**, no espírito do
*Agent Ransack / FileLocator Pro* do Windows — mas nativo do Linux e portável entre distros.
O motor é o **ripgrep** (`rg`) para conteúdo e o **fd** para nome; ambos são mais rápidos que
os buscadores comerciais. Sem `rg`/`fd`, cai num fallback em Python puro (roda em qualquer lugar).

Os buscadores do Windows (FileLocator, Everything, UltraSearch) são **inúteis no Linux**: leem a
MFT/USN do NTFS, que não existe aqui. Este projeto reimplementa a função de forma nativa.

> 📖 **Manual completo:** [MANUAL.md](MANUAL.md) (English) · [MANUAL.pt-BR.md](MANUAL.pt-BR.md) (Português) — uso da GUI e todas as capacidades da CLI.

## Recursos

- 🔎 **Nome + conteúdo** — glob (`*.py`) ou regex, texto ou regex, com destaque no preview.
- 🧩 **Busca booleana** — `(nota OR laudo) AND paciente NOT rascunho`. Aceita `| & !` e `"aspas"`
  para frases. Precedência `NOT > AND > OR`, parênteses. Resolve por conjuntos de arquivos (`rg -l`).
- 📄 **Dentro de documentos** — busca em **PDF, docx, epub, odt, zip** via
  [ripgrep-all](https://github.com/phiresky/ripgrep-all) (opcional).
- 🎬 **Preview de mídia** — thumbnail de imagens e **player** de áudio/vídeo com transporte
  (⏮ ▶/⏸ ⏭), slider de posição e navegação entre as mídias dos resultados.
- 🌗 **Tema claro/escuro** — alternável (Ctrl+T), preferência salva.
- 🎛️ **Filtros** — tamanho mínimo, modificado nos últimos N dias, ocultos, `.gitignore`,
  não cruzar pontos de montagem (`--one-file-system`), palavra inteira, sensível a caixa.
- ⚡ **Ao vivo** — a tabela cresce durante a busca (streaming de `rg --json` numa thread).
- 🗂️ **Abas de busca** — várias buscas abertas ao mesmo tempo, cada uma com seu
  formulário e seus resultados (`Ctrl+N` nova, `Ctrl+↵` busca em nova aba, `Ctrl+W` fecha).
- ⭐ **Buscas salvas + histórico** — salve uma busca inteira (não só o termo) e
  reabra depois; as últimas buscas ficam no menu **Buscas ▾** (`Ctrl+S` salvar,
  `Ctrl+R` repetir) e `↑`/`↓` no campo de nome percorrem o histórico.
- 📤 **Exportar** — os resultados em **CSV** (uma linha por trecho casado) ou **JSON**
  (um objeto por arquivo), na ordem em que estão na tela (`Ctrl+E`).
- 📁 **Copiar arquivos** — arraste para outro app ou copie para uma pasta, com
  pré-checagem do destino (espaço, FAT32, nomes ilegais) e **ritmo de escrita em
  pendrive/removível** para não sequestrar o cache do sistema. Nunca move nem apaga a origem.
- 💻 **CLI equivalente** — mesma engine, com `--print0` para pipelines.

## Instalação

Três caminhos, na ordem em que provavelmente interessam:

| | quando usar | GUI funciona? |
|---|---|---|
| **AppImage** | qualquer distro, sem instalar nada | sim — Python e PySide6 vêm dentro |
| **.deb** | Debian/Ubuntu/Mint, integrado ao apt | precisa de PySide6 (veja abaixo) |
| **install.sh** | qualquer distro, instala no `~`, sem root | sim — usa o do sistema ou cria um venv |

### AppImage — um arquivo, nada a instalar

```bash
chmod +x Sombrero_File_Search-*.AppImage
./Sombrero_File_Search-*.AppImage                       # GUI
./Sombrero_File_Search-*.AppImage --cli ~/docs -n '*.pdf'   # a mesma CLI
```

Traz Python e Qt embutidos (~135 MB). Usa o `rg`/`fd` **do seu sistema** se existirem —
não os sequestra nem os duplica.

> Não há versão Flatpak, e é de propósito: este programa existe para varrer o disco
> inteiro, e a sandbox do Flatpak é o modelo errado para isso. Dar-lhe
> `--filesystem=host` seria anular a sandbox e ainda brigar com portais.

### .deb

```bash
sudo apt install ./sombrero-file-search_*_all.deb
lfs ~/docs -n '*.pdf'          # CLI: funciona já, só precisa de python3
sombrero-file-search              # GUI
```

O pacote é **magro** de propósito: `Depends: python3`, com `ripgrep` e `fd-find` como
*Recommends* (há fallback em Python puro, então declará-los como obrigatórios seria mentira).
O apt do Debian/Ubuntu/Mint **não tem PySide6** — nessas distros, a primeira execução da GUI
pede um comando único:

```bash
sombrero-file-search --setup-gui   # cria um venv no SEU home, sem root
```

### install.sh — instalador universal

Detecta apt/dnf/pacman/zypper; instala o app em `~/.local`, sem root:

```bash
git clone https://github.com/Thiopental1976/sombrero-file-search.git
cd sombrero-file-search
./install.sh
```

Ele instala `ripgrep`, `fd` e `poppler` pelo gerenciador da distro (com sua autorização), baixa
`ripgrep-all` e `pandoc` (binários estáticos, para o modo documentos) e prepara o PySide6 (do
sistema ou num venv próprio). Ao final, abra **Sombrero File Search** pelo menu ou rode `sombrero-file-search`.

### Manual

```bash
# dependências de sistema (exemplo Debian/Ubuntu/Mint)
sudo apt install ripgrep fd-find poppler-utils
pip install PySide6            # ou use o venv do install.sh
python3 lfs/app.py            # GUI
```

## Uso da CLI

```bash
lfs ~/projetos -n '*.py' -c "def main"          # nome + conteúdo
lfs ~/docs -c "laudo" --docs                     # dentro de PDF/docx/epub
lfs ~/notas -b '(nota OR laudo) AND paciente'    # booleano
lfs /dados -c erro -l --print0 | xargs -0 ...    # pipeline
lfs /repo -n '*.log' --json                      # NDJSON p/ automação (cron, scripts)
lfs /repo -c erro --nice-io                      # cede CPU/IO ao serviço do servidor
lfs ~/acervo -n relatorio --index               # busca por NOME acelerada por plocate
```

`-c` conteúdo · `-n` nome · `-b/--bool` booleano · `-D/--docs` documentos · `-l` só caminhos ·
`--print0` separador nulo · `--json` NDJSON · `--nice-io` baixa prioridade · `--index` índice. Rode `lfs --help` para tudo.

**Aceleração por índice (`--index`, só NOME):** opt-in explícito — o modo padrão é
sempre "o que está no disco AGORA". Com `--index`, a busca por nome consulta o
`plocate` (rápido), mas com **honestidade garantida**: (1) se qualquer parte do
caminho estiver **podada** do índice (`PRUNEFS`/`PRUNEPATHS` do `updatedb.conf` —
tipicamente rede, `/mnt`, `/media`, `/tmp`), o SFS **recusa com erro claro** em vez
de devolver um subtree faltando em silêncio; (2) cada resultado é **verificado vivo**
(`lstat`) — o que sumiu do disco desde o `updatedb` não sai; (3) a **data do índice**
aparece sempre. Conteúdo não é indexável → `--index` é recusado (use a busca viva).

**Para automação (`--json`):** um objeto JSON por match, um por linha (NDJSON) — campos
`path`, `size`, `mtime`, `is_dir`, `nmatch`, `lines[]` (o mesmo `Match.lines` lógico, sem
terminadores). Avisos vão **no mesmo stream** (`{"warn":"mount_dead",…}`, `{"warn":"denied",…}`)
e erro de expressão booleana vira `{"error":"boolean_expression",…}`. O **exit code** segue o
`grep`: **0** achou, **1** nada, **2** erro. Nome com `\n` é escapado pelo JSON e nunca racha o
framing de linha.

## Paridade `rg` ↔ fallback Python (divergências conhecidas)

O fallback em Python puro devolve o **mesmo resultado** do ripgrep na esmagadora maioria
dos casos — o harness de paridade roda 500 expressões booleanas aleatórias × 2000 arquivos
e exige zero divergência (`tests/test_parity_rg_python.py`). As poucas diferenças que existem
estão **documentadas de propósito** — nenhuma é surpresa:

- **`nmatch` (contador de "quão quente")** — com `rg`, conta por **ocorrência**; no fallback,
  por **linha que casa**. Só difere quando a mesma linha tem o termo mais de uma vez. O conjunto
  de arquivos e as linhas (número + texto) são idênticos. É um indicador, não um contrato.
- **UTF-16/UTF-32 com BOM** — o `rg` detecta o BOM e decodifica; o fallback abre em texto
  (UTF-8/locale) e **não acha** o termo. Afeta só o modo **sem ripgrep**, em arquivos de origem
  Windows. Instalar o `ripgrep` (é *Recommends*) resolve.
- **CRLF (`\r\n`) — RESOLVIDO.** Antes o `rg` entregava a linha **com** o `\r` final e o fallback
  (leitura universal-newline) **sem**. Agora os **dois** motores normalizam um `\r` final via
  `engine._logical_line`: `m.lines` carrega o texto **lógico** da linha, sem artefato de
  terminador (é o que o usuário lê, copia e o export CSV/JSON consome). A suíte de paridade trava
  o invariante com uma **sentinela** (`assert not txt.endswith("\r")` nos dois lados).
- **CR fora de CRLF (lone CR do Mac clássico, `\r\r\n`)** — divergência **estrutural** de
  *segmentação* de linha, não de texto: o `rg` separa registros só por `\n` (um arquivo lone-CR
  vira **1 linha gigante**), o Python em modo texto trata o CR como quebra (**N linhas**). Nenhum
  `rstrip` conserta numeração; é caso patológico/pré-OSX e **não é perseguido** — fica documentado
  e há um teste dirigido que **pina** a divergência (`rg=1`, `Python=N`) para virar regressão se
  alguém "consertar" um lado sem querer.
- **Codificações legadas sem BOM (Shift-JIS, GBK, EUC-KR…)** — aqui `rg` e fallback **concordam**:
  nenhum acha, porque o termo de busca é UTF-8 e o arquivo não. Não é divergência, é limitação
  compartilhada (qualquer ferramenta Unix). **CJK em UTF-8** — nomes de arquivo e conteúdo —
  funciona 100% nos dois motores.

## Arquitetura

```
lfs/engine.py   # core sem Qt: Query/Match + backends rg (conteúdo) / fd (nome) + fallback Python
lfs/boolean.py  # parser recursivo-descendente da busca booleana (tokenizer → AST → conjuntos)
lfs/app.py      # GUI PySide6: form, tabela ao vivo, preview texto/mídia, temas
lfs/cli.py      # CLI (mesma core)
lfs/fileops.py  # cópia não-destrutiva (F7): nunca move, renomeia nem apaga
lfs/disks.py    # capacidades do destino: FAT/exFAT/NTFS/MTP e seus limites
lfs/xdg.py      # mime, "abrir com", gerenciador de arquivos padrão
lfs/version.py  # identidade da build (o que está rodando é o que você acha?)
install.sh      # instalador universal (multi-distro)
packaging/      # build_deb.sh e build_appimage.sh (F6)
```

Para gerar os pacotes você mesmo:

```bash
./packaging/build_deb.sh        # ~3 s, precisa só de dpkg-deb
./packaging/build_appimage.sh   # ~10 min na 1ª vez (baixa Python + PySide6)
```

## Requisitos

- Python 3.9+ e **PySide6** (GUI).
- **ripgrep** e **fd** (recomendados; sem eles, fallback Python).
- Opcional: **ripgrep-all** + **pandoc**/**poppler** (modo documentos); **QtMultimedia** (player).

## Cuidado com discos SMR

Feito para rodar sobre acervos grandes, inclusive discos **SMR** e USB externos.
SMR (*Shingled Magnetic Recording*) grava trilhas sobrepostas "como telhas": lê bem
em sequência, mas sofre com escrita aleatória e, principalmente, com **leitura
concorrente** (as cabeças começam a saltar e o desempenho despenca) — ao contrário
do **CMR** convencional, que reescreve no lugar. O programa foi desenhado para poupar
esses discos:

- **nunca deixa `rg`/`fd` órfão** varrendo o disco em background (busca cortada ou
  janela fechada mata o processo);
- o **AND booleano restringe** o segundo termo aos arquivos que o primeiro já achou,
  lendo bem menos do disco;
- **`--one-file-system`** ("1 disco") evita cruzar para outro mount sem querer;
- **imagens grandes** não são decodificadas na hora (evita travar num SMR);
- o **paralelismo é consciente do disco**: termos independentes (`OR`) rodam em
  paralelo no SSD/CMR, mas a busca é **serializada** automaticamente quando algum
  caminho está em `/mnt` (ou `/media`, `/run/media`) sobre um disco **rotacional
  ou desconhecido**, poupando o SMR de *seek* concorrente. Um SSD/NVMe montado ali
  (checado via `/sys/block/<dev>/queue/rotational`) **não** é penalizado. O grau de
  paralelismo é afinável pela variável de ambiente **`LFS_WORKERS`** (padrão `3`;
  `LFS_WORKERS=1` serializa tudo). Detalhes na §14 da documentação técnica.

## Servidores, NAS e montagens de rede

O SFS roda tanto **num desktop buscando um NAS** (montagens NFS/SMB/SSHFS) quanto
**no próprio servidor** (headless, via SSH, sobre repositórios de dezenas de TB). O
que o protege nesse ambiente:

- **Watchdog de montagem morta.** Um NFS *hard-mount* com o servidor fora do ar trava
  o `stat()` em **D-state ininterruptível** — nem `kill` resolve, e um programa comum
  congela ali sem remédio. Antes de descer numa montagem de **rede**, o SFS sonda a
  vida dela numa *thread descartável* com timeout; se não responde, a montagem é
  **pulada com aviso visível** (nunca em silêncio, nunca travando). Se um NAS morre
  **no meio** da busca, o resultado carrega o aviso — **honestidade > completude**.
- **Classe de I/O por montagem.** Rede não serializa como SMR, mas tampouco pode
  sequestrar o pool: cada montagem de rede tem um teto de *workers* próprio, para que
  um link lento não afogue a busca nos discos locais. `gvfs` (celular/câmera) e
  `autofs` ficam **fora do "buscar em tudo"** por padrão — só entram se você der o
  caminho explícito (senão um "buscar em `/mnt`" acordaria todo *automount* da casa).
- **Nome de arquivo pelo protocolo.** Ao **copiar** para um destino de rede, o SFS já
  sabe o que cada um aceita: `nfs` é POSIX pleno; `cifs`/`smb` proíbe `: ? * < > |` e
  não tem *symlink*; `sshfs` tem *rename* atômico. E escreve **em ritmo** (como no
  pendrive), porque um CIFS/NFS lento acumula *writeback* global igual.
- **Fronteira visível.** Um "buscar em `/`" pode listar **antes** quais montagens serão
  tocadas e de que classe (disco/rede/SMR) — servidor com 40 montagens agradece.

**O que o SFS deliberadamente NÃO é:** um **indexador residente** estilo Everything/
Recoll. A identidade dele é **ferramenta viva e sem estado** — o que ele mostra é o que
está no disco **agora**, não um retrato de um banco de dados que pode estar velho.
Quando existe um índice do próprio sistema (`plocate`), o SFS pode se apoiar nele para
acelerar — **mas só sob `--index`, explícito, e nunca em silêncio** (recusa se a
cobertura estiver furada; ver acima). **Daemon de indexação próprio, não.** E não vira
serviço web: quem quer busca remota usa **SSH + `--json`**. (Mesma razão de não haver
Flatpak — ver *Instalação*.)

E o **Caçador de duplicatas** (menu *Duplicatas…*) **acha, mostra e exporta** grupos de
arquivos byte-idênticos — para CSV ou JSON — mas **nunca os apaga**. Nem com confirmação,
nem "só a lixeira", nem "só as cópias extras". *Lê e exporta, jamais altera* é a
identidade do produto inteira, e um botão de apagar seria o fim desse argumento: a
decisão de qual cópia morre é do humano, no gerenciador de arquivos dele, com os olhos
nos caminhos que o SFS mostrou. O motor de dedup é **código próprio do SFS**
(`lfs/dupes.py`), não dependência de outro projeto, e não tem — nem deve ganhar — função
de remoção.

## Licença

**GNU GPL v3 ou posterior** ([LICENSE](LICENSE)) — `SPDX-License-Identifier: GPL-3.0-or-later`.

Software livre de verdade: use, estude, modifique e redistribua à vontade. A única obrigação é
recíproca — quem distribuir uma versão modificada precisa distribuir o código-fonte dela sob a
mesma licença. É o que impede alguém fechar este trabalho e revendê-lo como produto próprio, e é
também o que permite ao projeto entrar em repositórios como Flathub, Debian e AUR (licença caseira
não é aceita por nenhum deles).

Copyright (C) 2026 Rodrigo Toledo. Distribuído SEM QUALQUER GARANTIA.
