<div align="center">

<img src="assets/icon_128.png" width="96" alt="Linux File Search">

# Linux File Search

**Busca de arquivos nativa para Linux — nome, conteúdo, booleano e dentro de documentos.**
*A native file-search tool for Linux, in the spirit of Agent Ransack / FileLocator Pro.*

![Python](https://img.shields.io/badge/Python-3.9%2B-3776ab)
![PySide6](https://img.shields.io/badge/GUI-PySide6-41cd52)
![ripgrep](https://img.shields.io/badge/engine-ripgrep%20%2B%20fd-orange)
![License](https://img.shields.io/badge/license-aberto%20e%20gratuito-blue)

</div>

---

## O que é

Um buscador de arquivos **sem índice**, com resultados **ao vivo**, no espírito do
*Agent Ransack / FileLocator Pro* do Windows — mas nativo do Linux e portável entre distros.
O motor é o **ripgrep** (`rg`) para conteúdo e o **fd** para nome; ambos são mais rápidos que
os buscadores comerciais. Sem `rg`/`fd`, cai num fallback em Python puro (roda em qualquer lugar).

Os buscadores do Windows (FileLocator, Everything, UltraSearch) são **inúteis no Linux**: leem a
MFT/USN do NTFS, que não existe aqui. Este projeto reimplementa a função de forma nativa.

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
- 💻 **CLI equivalente** — mesma engine, com `--print0` para pipelines.

## Instalação

Instalador universal (detecta apt/dnf/pacman/zypper; instala o app em `~/.local`, sem root):

```bash
git clone https://github.com/rrdtoledo/ubiquitous-octo-winner-BR.git
cd ubiquitous-octo-winner-BR
./install.sh
```

Ele instala `ripgrep`, `fd` e `poppler` pelo gerenciador da distro (com sua autorização), baixa
`ripgrep-all` e `pandoc` (binários estáticos, para o modo documentos) e prepara o PySide6 (do
sistema ou num venv próprio). Ao final, abra **Linux File Search** pelo menu ou rode `linux-file-search`.

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
```

`-c` conteúdo · `-n` nome · `-b/--bool` booleano · `-D/--docs` documentos · `-l` só caminhos ·
`--print0` separador nulo. Rode `lfs --help` para tudo.

## Arquitetura

```
lfs/engine.py   # core sem Qt: Query/Match + backends rg (conteúdo) / fd (nome) + fallback Python
lfs/boolean.py  # parser recursivo-descendente da busca booleana (tokenizer → AST → conjuntos)
lfs/app.py      # GUI PySide6: form, tabela ao vivo, preview texto/mídia, temas
lfs/cli.py      # CLI (mesma core)
install.sh      # instalador universal (multi-distro)
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
  caminho está em `/mnt` (ou `/media`, `/run/media`), poupando o SMR de *seek*
  concorrente. Detalhes na §14 da documentação técnica.

## Licença

**Aberto e gratuito** — baseada na MIT, porém **sem direito de revenda**: o software pode ser
usado, copiado, modificado, publicado e distribuído livremente, mas **não pode ser vendido** como
produto ou serviço pago; deve permanecer gratuito. Veja [LICENSE](LICENSE).
