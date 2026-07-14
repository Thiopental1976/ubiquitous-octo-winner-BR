<div align="center">

# Ferramentas nativas para Linux — Andrômeda × Rodrigo

**Dois programas de mídia e busca, feitos para rodar bem sobre acervos grandes
(inclusive discos SMR e USB externos), sem depender de nada do Windows.**

*Native Linux tooling: AI video restoration (TopazLinux) and a live file searcher.*

</div>

---

## Por que este repositório

Duas frentes de trabalho, o mesmo espírito: software **nativo do Linux**, portável
entre distros, cuidadoso com o hardware (SSD/CMR **e** discos SMR lentos) e sem
índice mágico nem serviço pago. Aqui moram o código e a documentação do
**Linux File Search**; o **TopazLinux** vive no seu próprio diretório e está
descrito abaixo como parte do mesmo esforço.

| Projeto | O que faz | Onde está | Estado |
|---|---|---|---|
| **Linux File Search** | Busca de arquivos ao vivo — nome, conteúdo, booleano e dentro de documentos (PDF/docx/epub) | **este repositório** | Em uso; motor auditado e otimizado |
| **TopazLinux** | Realce e restauração de vídeo por IA (upscale, interpolação, denoise), nativo, com GUI Qt | `~/projetos/topazlinux/` | GUI em produção; treino local de modelos em curso |

---

## 1. Linux File Search  *(este repositório)*

Um buscador de arquivos **sem índice**, com resultados **ao vivo**, no espírito do
*Agent Ransack / FileLocator Pro* do Windows — mas nativo do Linux. O motor é o
**ripgrep** (`rg`) para conteúdo e o **fd** para nome, com fallback em Python puro
quando não há binários. Os buscadores do Windows (Everything, UltraSearch) são
inúteis aqui porque leem a MFT/USN do NTFS, que não existe no Linux; este projeto
reimplementa a função de forma nativa.

**Destaques**

- 🔎 **Nome + conteúdo** — glob (`*.py`) ou regex, com destaque no preview.
- 🧩 **Busca booleana** — `(nota OR laudo) AND paciente NOT rascunho`, com `| & !`,
  aspas para frases, precedência `NOT > AND > OR` e parênteses.
- 📄 **Dentro de documentos** — PDF, docx, epub, odt, zip via *ripgrep-all* (opcional).
- 🎬 **Preview de mídia** — thumbnail de imagens e player de áudio/vídeo.
- 🌗 **Tema claro/escuro**, filtros (tamanho, data, ocultos, `--one-file-system`), CLI equivalente.
- 🐢 **Consciente de disco SMR** — nunca deixa `rg`/`fd` órfão; o AND booleano restringe
  o disco lido; termos `OR` correm em paralelo no SSD mas **serializam** em `/mnt`
  (poupando o SMR de *seek* concorrente).

**Instalação e uso completo:** veja **[README_LINUX_FILE_SEARCH.md](README_LINUX_FILE_SEARCH.md)**
(instalação multi-distro, CLI, arquitetura) e **[DOCUMENTACAO_TECNICA.md](DOCUMENTACAO_TECNICA.md)**
(motor, parser booleano, otimizações e cuidados com SMR em detalhe).

```bash
git clone https://github.com/Thiopental1976/ubiquitous-octo-winner-BR.git
cd ubiquitous-octo-winner-BR
./install.sh
```

---

## 2. TopazLinux  *(realce/restauração de vídeo por IA)*

Um equivalente nativo ao *Topaz Video AI*, rodando no Linux sobre **ncnn + FFmpeg**
(com backend TensorRT em beta), sem Wine. Fluxo de trabalho: **Fable 5 desenha →
Andrômeda implementa**. Ambiente de referência: i7-13700K, RTX 4070 SUPER 12 GB,
Linux Mint 22.3.

**Arquitetura (resumo)**

- **Backend** (`topaz_engine.py`): chunking por tempo, resume por chunk, colorimetria
  bt709, áudio *copy-first*, detecção de cena, crop/trim, progresso ao vivo, cancel
  imediato, auto-UHD por VRAM.
- **GUI Qt** (`topaz_gui_qt/`): player mpv, trim I/O, crop arrastável, drag-and-drop,
  fila com pause/cancel reais, preview comparado, presets, telemetria — **em produção**.
- **Job API** (`topaz_jobs.py`): separação limpa GUI ↔ engine; a GUI só orquestra.

**Modelos — paridade Topaz ↔ código aberto**

Os modelos do Topaz são **temporais** (multi-frame); Real-ESRGAN/CUGAN são de imagem,
frame a frame — origem do *flicker*. A paridade vem em degraus:

1. **Mitigação** (feito): pré-denoise temporal + regrain pós-upscale.
2. **Modelos especializados no acervo** (em curso): *fine-tune* local com **neosr** —
   `cedro-anime-x2` (base realesr-animevideov3) e `cedro-real-x4` (base realesrgan-x4plus).
3. **Backend TensorRT** (paralelo): VSR temporal de verdade (RealBasicVSR / BasicVSR++).

Já incorporáveis sem treinar: OpenModelDB/AnimeJaNai como candidatos ncnn, GFPGAN
para rostos, RIFE 4.25/4.26 para interpolação.

**Treino local (T0→T5)** — venv dedicado com monitor térmico; dataset do próprio acervo
(elegibilidade ≥1080p, amostragem + filtros de escuro/borrado/dedup pHash, tiles HR 512²);
degradação sintética de 2ª ordem com *gate* humano; treino em duas fases (L1 → +GAN fraco);
validação honesta (PSNR/SSIM + A/B cego + teste de flicker); conversão PyTorch→ONNX→ncnn
com paridade numérica < 1/255. O acervo é **somente leitura**; pesos derivados são de uso pessoal.

> Documento mestre completo: `~/Downloads/TopazLinux_Documento_Mestre (1).md`.
> Código e handoffs em `~/projetos/topazlinux/`.

---

## Princípios comuns

- **Nativo e portável** — nada de Wine nem de índices proprietários; roda em qualquer distro.
- **O acervo é sagrado** — leitura sob montagem verificada; escrita só em áreas descartáveis.
- **Cuidado com o disco** — SMR e USB tratados com carinho (sem varredura órfã, sem *seek* concorrente à toa).
- **Nada "concluído" sem teste** — suíte de regressão no File Search; critério de aceitação no TopazLinux.

## Licença

**Aberto e gratuito** — baseada na MIT, porém **sem direito de revenda**: pode ser usado,
copiado, modificado e distribuído livremente, mas **não pode ser vendido**. Veja [LICENSE](LICENSE).
