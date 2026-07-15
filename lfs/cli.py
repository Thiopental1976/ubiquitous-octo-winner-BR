#!/usr/bin/env python3
"""Linux File Search — CLI (mesma core da GUI, para scripts/daemons)."""
from __future__ import annotations
import argparse, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine
from engine import Query


def main():
    ap = argparse.ArgumentParser(description="Busca ampla de arquivos (nome + conteúdo) sobre ripgrep/fd.")
    ap.add_argument("path", nargs="+", help="pasta(s) onde buscar")
    ap.add_argument("-n", "--name", default="",
                    help="nome CONTÉM o termo ('rotina' acha 'exames de rotina.txt'); "
                         "globs (* ? [) valem como digitados; vários separados por vírgula")
    ap.add_argument("-c", "--content", default="", help="texto/regex que o arquivo deve conter")
    ap.add_argument("-b", "--bool", dest="boolexpr", default="", metavar="EXPR",
                    help="busca BOOLEANA de conteúdo: '(A OR B) AND C NOT D' (| & ! e aspas)")
    ap.add_argument("-D", "--docs", action="store_true",
                    help="busca DENTRO de documentos (PDF/docx/epub/zip…) via ripgrep-all (rga)")
    ap.add_argument("--name-regex", action="store_true")
    ap.add_argument("--content-regex", action="store_true")
    ap.add_argument("-i", "--ignore-case", action="store_true", help="ignora caixa (padrão já é insensível; use -s p/ sensível)")
    ap.add_argument("-s", "--case-sensitive", action="store_true")
    ap.add_argument("-w", "--word", action="store_true", help="palavra inteira")
    ap.add_argument("--hidden", action="store_true")
    ap.add_argument("--gitignore", action="store_true", help="respeitar .gitignore")
    ap.add_argument("--one-fs", action="store_true", help="não cruzar mounts")
    ap.add_argument("--min-size", type=str, default=None, help="ex: 10M, 1G")
    ap.add_argument("--days", type=int, default=0, help="modificados nos últimos N dias")
    ap.add_argument("-0", "--print0", action="store_true", help="separa caminhos por NUL (para xargs -0)")
    ap.add_argument("-l", "--files-only", action="store_true", help="só o caminho (sem linhas de match)")
    args = ap.parse_args()

    parse_size = engine.parse_size            # §5: fonte única (era duplicado)

    # texto puro = "contém" (mesma semântica da GUI); glob explícito é respeitado
    names = [engine.as_name_glob(p) for p in args.name.replace(";", ",").split(",")
             if p.strip()] if not args.name_regex else ([args.name] if args.name else [])
    q = Query(
        paths=args.path, name_patterns=names, name_is_regex=args.name_regex,
        content=args.content, content_is_regex=args.content_regex,
        case_sensitive=args.case_sensitive, whole_word=args.word,
        include_hidden=args.hidden, respect_gitignore=args.gitignore,
        one_file_system=args.one_fs, min_size=parse_size(args.min_size),
        modified_after=(time.time()-args.days*86400) if args.days > 0 else None,
        documents=args.docs,
    )
    info = engine.engine_info()
    print(f"# motor: rg={info['ripgrep']} fd={info['fd']} rga={info['rga']}", file=sys.stderr)
    if args.docs and not engine.RGA:
        print("# aviso: --docs pedido mas 'rga' ausente; busca cairá no rg (não extrai PDF/docx)",
              file=sys.stderr)
    sep = "\0" if args.print0 else "\n"
    n = [0]
    def out(m):
        n[0] += 1
        if args.files_only or not m.lines:
            sys.stdout.write(m.path + sep)
        else:
            for ln, txt in m.lines:
                sys.stdout.write(f"{m.path}:{ln}:{txt}{sep}")
    if args.boolexpr:
        import boolean
        try:
            tot, dt = boolean.search_boolean(q, args.boolexpr, out)
        except boolean.BooleanError as e:
            print(f"erro na expressão booleana: {e}", file=sys.stderr); sys.exit(2)
    else:
        tot, dt = engine.search(q, out)
    print(f"\n# {tot} arquivos · {dt:.2f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
