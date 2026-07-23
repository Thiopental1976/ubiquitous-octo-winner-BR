# Sombrero File Search — Copyright (C) 2026 Rodrigo Toledo
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Este programa é software livre: você pode redistribuí-lo e/ou modificá-lo sob
# os termos da GNU General Public License, versão 3 ou posterior (ver LICENSE).
# Distribuído na esperança de ser útil, mas SEM QUALQUER GARANTIA.
"""Sombrero File Search — internationalization (i18n).

English is the SOURCE language: every user-facing string in the code is written
in English and passed through `t()`. Other languages are translation tables keyed
by the English string. If a key is missing, `t()` returns the English source — so
a partially translated language degrades gracefully instead of showing blanks.

Language is picked from the system locale (LFS_LANG override, then the standard
LC_ALL / LC_MESSAGES / LANG / LANGUAGE chain). No Qt dependency, so the engine can
localize its progress labels too and the tests run without a display.

Adding a language = add a `{ "english source": "translation" }` dict to `_TABLES`
and list its code in `_SUPPORTED`. Placeholders use `str.format` names, e.g.
`t("{n} found", n=len(rows))`; the translation must keep the same `{names}`.
"""
import os

# English source -> Portuguese. Strings identical in both languages are omitted
# (t() falls back to the English source, which is already correct Portuguese, e.g.
# "Matches", ".gitignore", "Aa").
_PT = {
    # header / theme
    "broad file search — name · content · boolean · documents":
        "busca ampla de arquivos — nome · conteúdo · booleano · documentos",
    "Toggle light/dark theme (Ctrl+T)": "Alternar tema claro/escuro (Ctrl+T)",
    "☀  Light": "☀  Claro",
    "☾  Dark": "☾  Escuro",
    "available": "disponível",
    "missing": "ausente",

    # result columns
    "File": "Arquivo",
    "Folder": "Pasta",
    "Size": "Tamanho",
    "Modified": "Modificado",

    # primary (name) field
    "File name — e.g. report  ·  *.pdf  ·  exams*.txt   (empty = all)":
        "Nome do arquivo — ex.: rotina  ·  *.pdf  ·  exames*.txt   (vazio = todos)",
    "Search by NAME. Plain text means “contains”: report finds\n"
    "“routine exams.txt” in any extension. Multiple terms separated\n"
    "by comma (OR). Hand-typed globs (* ? [) are honored as typed.":
        "Busca por NOME. Texto puro significa “contém”: rotina acha\n"
        "“exames de rotina.txt” em qualquer extensão. Vários termos separados\n"
        "por vírgula (OU). Globs à mão (* ? [) são respeitados como digitados.",
    "  Search  ": "  Buscar  ",
    "Cancel": "Cancelar",

    # secondary (content / path) line
    "Content": "Conteúdo",
    "optional — text the file must contain (boolean: toggle the chip)":
        "opcional — texto que o arquivo deve conter (booleano: ligue o chip)",
    "In": "Em",
    "Folder(s)/mounts — separate with ';'": "Pasta(s)/mounts — separe por ';'",
    "Multiple starting points separated by ';' —\n"
    "e.g. ~/Documents;/mnt/archive;/media/backup":
        "Vários pontos de partida separados por ';' —\n"
        "ex.: ~/Documents;/mnt/acervo;/media/backup",
    "Browse…": "Procurar…",
    "Disks ▾": "Discos ▾",
    "All disks": "Todos os discos",
    "   —  tip: “Content” is filled, so this searched INSIDE files; "
    "clear it to match file/folder names.":
        "   —  dica: “Conteúdo” está preenchido, então buscou DENTRO dos arquivos; "
        "limpe-o para casar nomes de arquivo/pasta.",
    "Multi-disk search: add/remove mounted disks\n"
    "(/mnt, /media, /run/media) from the 'In' folder list.":
        "Busca multidiscos: inclui/retira discos montados\n"
        "(/mnt, /media, /run/media) da lista de pastas do 'Em'.",

    # option chips
    "Case sensitive": "Sensível a maiúsculas/minúsculas",
    "word": "palavra",
    "Whole word": "Palavra inteira",
    "boolean": "booleano",
    "Reads the Content field as an expression: (A OR B) AND C NOT D\n"
    "Also accepts | & !  and \"quotes\" for phrases. Precedence NOT>AND>OR.":
        "Interpreta o campo Conteúdo como expressão: (A OR B) AND C NOT D\n"
        "Também aceita | & !  e \"aspas\" p/ frases. Precedência NOT>AND>OR.",
    "documents": "documentos",
    "Searches INSIDE PDF/docx/epub/odt/zip… (ripgrep-all).":
        "Busca DENTRO de PDF/docx/epub/odt/zip… (ripgrep-all).",
    "Requires 'ripgrep-all' (rga) — run the installer.":
        "Requer 'ripgrep-all' (rga) — rode o instalador.",
    "content regex": "regex conteúdo",
    "name regex": "regex nome",
    "subfolders": "subpastas",
    "hidden": "ocultos",
    "Respect .gitignore rules": "Respeitar regras .gitignore",
    "1 disk": "1 disco",
    "--one-file-system: don't cross into other mount points":
        "--one-file-system: não entra em outros pontos de montagem",
    "Size ≥": "Tam ≥",
    "Last": "Últimos",

    # boolean field placeholders (toggled by the "boolean" chip)
    "Boolean expression:   (note OR report) AND patient NOT draft":
        "Expressão booleana:   (nota OR laudo) AND paciente NOT rascunho",
    "Content to contain (text or regex)…   — empty = search by name only":
        "Conteúdo a conter (texto ou regex)…   — vazio = busca só por nome",

    # preview / media
    "Select a result to see the snippet…":
        "Selecione um resultado para ver o trecho…",
    "Previous media": "Mídia anterior",
    "Play / pause": "Reproduzir / pausar",
    "Next media": "Próxima mídia",
    "Muted (default for privacy) — click to enable sound":
        "Mudo (padrão para privacidade) — clique p/ ativar o som",
    "image": "imagem",
    "image too large —\ndouble-click to open externally":
        "imagem muito grande —\nclique duplo p/ abrir externo",
    "(no image preview)": "(sem pré-visualização de imagem)",
    "(binary file — no text preview)": "(arquivo binário — sem preview de texto)",
    "   … (truncated)": "   … (truncado)",
    "(empty)": "(vazio)",
    "(no preview: {e})": "(sem preview: {e})",

    # dialogs / disks menu
    "Choose the folder": "Escolha a pasta",
    "Home folder (~)": "Pasta pessoal (~)",
    "(no external disk mounted)": "(nenhum disco externo montado)",

    # status line
    "Ready.": "Pronto.",
    "⚠  No valid folder in 'In:'.": "⚠  Nenhuma pasta válida em 'Em:'.",
    "⚠  Ignoring non-existent folder(s): {paths}":
        "⚠  Ignorando pasta(s) inexistente(s): {paths}",
    "Searching…": "Buscando…",
    " · {d} inaccessible": " · {d} inacessível(is)",
    "Searching…{tag}  {n} found · {sec}s{extra}{step}":
        "Buscando…{tag}  {n} encontrados · {sec}s{extra}{step}",
    "step {done}/{total}: {label}": "passo {done}/{total}: {label}",
    "⚠  Invalid boolean expression: {msg}":
        "⚠  Expressão booleana inválida: {msg}",
    "  ·  {d} inaccessible": "  ·  {d} inacessível(is)",
    "{icon}  {tot} result(s)  ·  {sec}s{extra}{cancel}":
        "{icon}  {tot} resultado(s)  ·  {sec}s{extra}{cancel}",
    "   (cancelled)": "   (cancelado)",
    "Cancelling…": "Cancelando…",
    "{n} path(s) copied.": "{n} caminho(s) copiado(s).",

    # context menu
    "Open file": "Abrir arquivo",
    "Copy path(s)": "Copiar caminho(s)",
    "Open with": "Abrir com",
    "Other command…": "Outro comando…",
    "Command (the file paths are appended):":
        "Comando (os caminhos dos arquivos são anexados):",
    "Open containing folder": "Abrir pasta do item",
    "Copy": "Copiar",
    "Copy to…": "Copiar para…",
    "Choose folder…": "Escolher pasta…",
    "Properties": "Propriedades",

    # F7: copiar para outro dispositivo — pré-checagem do destino
    "{n} file(s), {size} → {dest}": "{n} arquivo(s), {size} → {dest}",
    "Destination filesystem: {fs} · {free} free":
        "Sistema de arquivos do destino: {fs} · {free} livres",
    "BLOCKED: the destination mount point is not mounted. "
    "Copying there would fill the system disk instead.":
        "BLOQUEADO: o ponto de montagem do destino não está montado. "
        "Copiar para lá encheria o disco de sistema.",
    "BLOCKED: the destination is mounted read-only.":
        "BLOQUEADO: o destino está montado como somente leitura.",
    "BLOCKED: this destination does not accept direct file "
    "writing through its current mount. Connect the device "
    "through the file manager (MTP) to copy here.":
        "BLOQUEADO: este destino não aceita escrita direta de arquivo "
        "pela montagem atual. Conecte o aparelho pelo gerenciador de "
        "arquivos (MTP) para copiar aqui.",
    "BLOCKED: no permission to write to this destination.":
        "BLOQUEADO: sem permissão para escrever neste destino.",
    "BLOCKED: the destination reports no room for a test write.":
        "BLOQUEADO: o destino não tem espaço nem para uma escrita de teste.",
    "BLOCKED: a test write to the destination failed.":
        "BLOQUEADO: uma escrita de teste no destino falhou.",
    "MTP device: files are copied through the system transfer "
    "service (gio), like the file manager does — progress "
    "updates per file.":
        "Aparelho MTP: os arquivos são copiados pelo serviço de "
        "transferência do sistema (gio), como o gerenciador de arquivos "
        "faz — o progresso avança por arquivo.",
    "Network destination: the free-space estimate may be "
    "unreliable.":
        "Destino de rede: a estimativa de espaço livre pode ser "
        "não-confiável.",
    "Not enough free space: needs {need}, has {free}.":
        "Espaço livre insuficiente: precisa de {need}, tem {free}.",
    "{n} file(s) exceed the {fs} size limit and will be SKIPPED:":
        "{n} arquivo(s) excedem o limite de tamanho do {fs} e serão PULADOS:",
    "{n} name(s) are invalid on {fs}:": "{n} nome(s) são inválidos em {fs}:",
    "invalid characters for this filesystem":
        "caracteres inválidos para este sistema de arquivos",
    "name too long for this filesystem":
        "nome longo demais para este sistema de arquivos",
    "reserved name on this filesystem":
        "nome reservado neste sistema de arquivos",
    "name ends in space or dot (dropped by this filesystem)":
        "nome termina em espaço ou ponto (descartado por este sistema de arquivos)",
    "name is not valid UTF-8 (rejected by this filesystem)":
        "nome não é UTF-8 válido (recusado por este sistema de arquivos)",
    "{n} symlink(s) will be copied as real files ({fs} has no symlinks).":
        "{n} link(s) simbólico(s) virarão cópia real ({fs} não tem symlink).",
    "{n} broken symlink(s) cannot be copied to {fs} and will be skipped.":
        "{n} link(s) simbólico(s) quebrado(s) não cabem em {fs} e serão pulados.",
    "No problems found. Nothing in the source will be modified — "
    "this only creates copies.":
        "Nenhum problema encontrado. Nada na origem será modificado — "
        "isto apenas cria cópias.",
    "Adapt invalid names (replace illegal characters)":
        "Adaptar nomes inválidos (troca os caracteres ilegais)",

    # F7: conflito no destino
    "File already exists": "Arquivo já existe",
    "“{name}” already exists in the destination.":
        "“{name}” já existe no destino.",
    "Source:": "Origem:",
    "Destination:": "Destino:",
    "Apply to all conflicts in this copy":
        "Aplicar a todos os conflitos desta cópia",
    "Skip": "Pular",
    "Keep both": "Manter os dois",
    "Overwrite": "Sobrescrever",
    "Cancel copy": "Cancelar cópia",

    # F7: fila e resultado da cópia
    "Scanning source…": "Varrendo a origem…",
    "Copying {name}": "Copiando {name}",
    "{n} pending": "{n} na fila",
    "Queued — {n} copy job(s) pending.": "Na fila — {n} cópia(s) pendente(s).",
    "Cancelling copy…": "Cancelando cópia…",
    "Copy cancelled.": "Cópia cancelada.",
    "{n} copied": "{n} copiado(s)",
    "{n} skipped": "{n} pulado(s)",
    "{n} failed": "{n} com falha",
    "✔  Copy to {dest}: {summary}  ·  {size}  ·  "
    "nothing in the source was modified.":
        "✔  Cópia para {dest}: {summary}  ·  {size}  ·  "
        "nada na origem foi modificado.",
    "✖  Copy to {dest} stopped — the destination ran out of space "
    "({summary})  ·  {size}  ·  nothing in the source was modified.":
        "✖  Cópia para {dest} parou — o destino ficou sem espaço "
        "({summary})  ·  {size}  ·  nada na origem foi modificado.",
    "Copy finished with errors": "Cópia terminou com erros",
    "{n} item(s) copied to the clipboard.":
        "{n} item(ns) copiado(s) para a área de transferência.",
    "Added {n} folder(s) to search in.": "{n} pasta(s) adicionada(s) ao 'Em'.",

    # F7: propriedades (somente leitura)
    "Name:": "Nome:",
    "Folder:": "Pasta:",
    "Type:": "Tipo:",
    "Size:": "Tamanho:",
    "Modified:": "Modificado:",
    "Accessed:": "Acessado:",
    "Permissions:": "Permissões:",
    "Owner:": "Dono:",
    "Symlink to:": "Link para:",
    "Filesystem:": "Sistema de arquivos:",
    "Error:": "Erro:",
    "Compute checksum": "Calcular checksum",
    "{size}  ({n} file(s))": "{size}  ({n} arquivo(s))",

    # engine progress labels (boolean.py) — shown in the status line
    "term “{term}”": "termo “{term}”",
    "extracting lines": "extraindo linhas",
    "listing files (NOT)": "listando arquivos (NOT)",

    # boolean parser errors (boolean.py) — {frag}/{tok} já vêm como repr()
    "unclosed quote at: {frag}": "aspas sem fechamento em: {frag}",
    'empty term ("") in expression': 'termo vazio ("") na expressão',
    "empty expression": "expressão vazia",
    "unexpected token: {tok}": "token inesperado: {tok}",
    "missing ')'": "parêntese ')' faltando",
    "expected a term, got {tok}": "esperava termo, veio {tok}",
    "unknown node": "nó desconhecido",
    "expression too deeply nested": "expressão aninhada em excesso",

    # F5 — abas, buscas salvas, histórico, exportação
    "New search": "Nova busca",
    "Searches ▾": "Buscas ▾",
    "Save current search…  (Ctrl+S)": "Salvar busca atual…  (Ctrl+S)",
    "Export results…  (Ctrl+E)": "Exportar resultados…  (Ctrl+E)",
    "Remove saved…": "Remover salva…",
    "Recent": "Recentes",
    "Clear history": "Limpar histórico",
    "Save search": "Salvar busca",
    "Name for this search:": "Nome para esta busca:",
    "Search saved as “{name}”.": "Busca salva como “{name}”.",
    "Export results": "Exportar resultados",
    "CSV (*.csv);;JSON (*.json)": "CSV (*.csv);;JSON (*.json)",
    "Nothing to export — the result list is empty.":
        "Nada a exportar — a lista de resultados está vazia.",
    "⚠  Could not write {path}: {err}": "⚠  Não consegui escrever {path}: {err}",
    "✔  Exported {n} row(s) to {path}": "✔  Exportei {n} linha(s) para {path}",

    # --- F10a #1: filtro-nos-resultados ---
    "Filter": "Filtrar",
    "narrow these results — *.odt  ·  >2019-01  ·  space = AND   (Ctrl+F)":
        "refine estes resultados — *.odt  ·  >2019-01  ·  espaço = E   (Ctrl+F)",
    "Filters the results already found — never touches the disk.\n"
    "substring matches name or path · *.odt filters extension ·\n"
    ">2019-01 / <2020-01 filter the date · a space means AND.":
        "Filtra os resultados já achados — nunca toca o disco.\n"
        "trecho casa nome ou caminho · *.odt filtra extensão ·\n"
        ">2019-01 / <2020-01 filtram a data · espaço quer dizer E.",
    "{shown} of {total}": "{shown} de {total}",

    # --- F10a #2: painel de narrativa da busca (no alto, legível) ---
    "Scanning": "Varrendo",
    "Scanned": "Varri",
    "network": "rede",
    "unreachable": "inacessível",
    "scanning…": "varrendo…",
    "{n} found": "{n} achados",
    "{verb} {done}/{total} locations · {found} found · {sec}":
        "{verb} {done}/{total} locais · {found} achados · {sec}",

    # --- F10b #4: pós-cópia "seguro remover" + ejetar ---
    "⏏ Eject": "⏏ Ejetar",
    "Copied and synced — safe to remove.":
        "Copiado e sincronizado — seguro remover.",
    "Copy finished.": "Cópia concluída.",
    "Safe to unplug now.": "Pode remover com segurança agora.",
    "Could not eject the disk.": "Não deu para ejetar o disco.",

    # --- F10b #5: fila de cópia que sobrevive ao fechamento ---
    "Resume copies?": "Retomar cópias?",
    "You had {n} copy job(s) pending from last time.":
        "Você tinha {n} cópia(s) pendente(s) da última vez.",
    "Resume": "Retomar",
    "Discard": "Descartar",

    # --- F10c: caçador de duplicatas ---
    "Duplicates…": "Duplicatas…",
    "Find byte-identical files under the search paths.\n"
    "Shows and exports them — never deletes.":
        "Acha arquivos byte-idênticos sob os caminhos da busca.\n"
        "Mostra e exporta — nunca apaga.",
    "Duplicate hunter": "Caçador de duplicatas",
    "include empty files": "incluir arquivos vazios",
    "Ignore files smaller than this (e.g. 1M, 500K).":
        "Ignora arquivos menores que isto (ex.: 1M, 500K).",
    "  Scan  ": "  Varrer  ",
    "Disk": "Disco",
    "Export CSV…": "Exportar CSV…",
    "Export JSON…": "Exportar JSON…",
    "Close": "Fechar",
    "Choose folder": "Escolher pasta",
    "Choose at least one folder to scan.":
        "Escolha ao menos uma pasta para varrer.",
    "Scanning…": "Varrendo…",
    "Listing files…": "Listando arquivos…",
    "Comparing heads…": "Comparando cabeças…",
    "Hashing full files…": "Hasheando arquivos inteiros…",
    "Working…": "Trabalhando…",
    "{n} group(s) · {size} recoverable{extra}":
        "{n} grupo(s) · {size} recuperáveis{extra}",
    "  ·  {d} unreadable": "  ·  {d} ilegíveis",
    "{k} copies · {each} each · {waste} recoverable":
        "{k} cópias · {each} cada · {waste} recuperáveis",
    "Export duplicates": "Exportar duplicatas",
    "   —  exported ✔": "   —  exportado ✔",
    "system": "sistema",

    # --- humane.py (F10b #6): frases de erro humanas ---
    "The network location stopped responding.":
        "O local de rede parou de responder.",
    "No permission to read this.":
        "Sem permissão para ler isto.",
    "This item no longer exists.":
        "Este item não existe mais.",
    "The destination ran out of space.":
        "O destino ficou sem espaço.",
    "The destination is read-only.":
        "O destino é somente-leitura.",
    "The name is too long for the destination.":
        "O nome é longo demais para o destino.",
    "Read/write error — the disk may be failing.":
        "Erro de leitura/escrita — o disco pode estar falhando.",
    "The file is in use by another program.":
        "O arquivo está em uso por outro programa.",
    "Too many files are open at once — try again in a moment.":
        "Arquivos demais abertos ao mesmo tempo — tente de novo em instantes.",
    "A file with this name already exists.":
        "Já existe um arquivo com este nome.",
    "This is a folder, not a file.":
        "Isto é uma pasta, não um arquivo.",
    "Part of this path is not a folder.":
        "Parte deste caminho não é uma pasta.",
    "There are too many symbolic links in this path.":
        "Há links simbólicos demais neste caminho.",
    "The operation could not be completed.":
        "Não foi possível concluir a operação.",
    "The search continued in the other locations.":
        "A busca continuou nos demais locais.",
    "This item was skipped.":
        "Este item foi pulado.",
    "This file was not copied.":
        "Este arquivo não foi copiado.",
    "The copy stopped.":
        "A cópia parou.",
    "This file was skipped.":
        "Este arquivo foi pulado.",
    "This location was skipped.":
        "Este local foi pulado.",
}

# Source is English; only non-English languages need a table.
_TABLES = {"pt": _PT}
_SUPPORTED = ("en", "pt")

_LOCALE_VARS = ("LFS_LANG", "LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE")


def _normalize(value: str) -> str:
    """'pt_BR.UTF-8' / 'pt_BR:en' -> 'pt'; unsupported -> 'en'."""
    code = value.split(":")[0].split(".")[0].split("_")[0].strip().lower()
    if not code:
        return ""
    return code if code in _SUPPORTED else "en"


def _detect() -> str:
    for var in _LOCALE_VARS:
        raw = os.environ.get(var)
        if raw:
            code = _normalize(raw)
            if code:
                return code
    return "en"


_LANG = None


def current_lang() -> str:
    global _LANG
    if _LANG is None:
        _LANG = _detect()
    return _LANG


def set_lang(code: str):
    """Force a language (mainly for tests). Pass None to re-detect."""
    global _LANG
    _LANG = code


def t(s: str, **kw) -> str:
    """Translate `s` to the current language, then apply `str.format(**kw)`.
    Missing translation -> English source; missing key stays literal (safe)."""
    table = _TABLES.get(current_lang())
    out = table.get(s, s) if table else s
    return out.format(**kw) if kw else out
