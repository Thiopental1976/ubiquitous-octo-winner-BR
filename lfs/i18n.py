"""Linux File Search — internationalization (i18n).

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
    "Open folder": "Abrir pasta",
    "Copy path(s)": "Copiar caminho(s)",

    # engine progress labels (boolean.py) — shown in the status line
    "term “{term}”": "termo “{term}”",
    "extracting lines": "extraindo linhas",
    "listing files (NOT)": "listando arquivos (NOT)",
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
