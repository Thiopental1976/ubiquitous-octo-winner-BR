#!/usr/bin/env bash
# ==========================================================================
#  Linux File Search — instalador universal
#  Instala o app + TODAS as dependências, em qualquer distro:
#    - ripgrep, fd            (busca de conteúdo / nome)
#    - poppler-utils          (pdftotext, p/ PDF no modo documentos)
#    - PySide6                (GUI; via sistema ou venv próprio)
#    - ripgrep-all (rga)      (busca dentro de PDF/docx/epub/zip)  [binário estático]
#    - pandoc                 (docx/epub/odt/html no rga)          [binário estático]
#  Não requer root para o app: instala em ~/.local. Só usa sudo p/ pacotes
#  de sistema (ripgrep/fd/poppler), e apenas se você autorizar.
# ==========================================================================
set -euo pipefail

APP="linux-file-search"
PREFIX="${PREFIX:-$HOME/.local/share/$APP}"
BIN="$HOME/.local/bin"
APPDIR="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor"
SRC="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
ARCH="$(uname -m)"
ASSUME_YES=0
for a in "$@"; do case "$a" in -y|--yes) ASSUME_YES=1;; -h|--help)
  echo "uso: ./install.sh [-y|--yes]   (-y = não perguntar, instala tudo)"; exit 0;; esac; done

c()  { printf "\033[1;36m%s\033[0m\n" "$*"; }
ok() { printf "  \033[32m✓\033[0m %s\n" "$*"; }
wn() { printf "  \033[33m!\033[0m %s\n" "$*"; }
er() { printf "  \033[31m✗\033[0m %s\n" "$*"; }
has(){ command -v "$1" >/dev/null 2>&1; }
# pergunta S/n (default Sim); respeita -y e ambiente não-interativo
ask(){ # ask "pergunta"
  [ "$ASSUME_YES" = 1 ] && return 0
  [ -t 0 ] || return 0
  local r; printf "  \033[1;36m?\033[0m %s [S/n] " "$1"; read -r r
  case "$r" in [nN]|[nN][aãoOÃ]*) return 1;; *) return 0;; esac
}

# -------------------------------------------------- gerenciador de pacotes
PM=""; INSTALL=""
detect_pm() {
  if   has apt-get; then PM=apt;    INSTALL="sudo apt-get install -y"
  elif has dnf;     then PM=dnf;    INSTALL="sudo dnf install -y"
  elif has pacman;  then PM=pacman; INSTALL="sudo pacman -S --noconfirm"
  elif has zypper;  then PM=zypper; INSTALL="sudo zypper install -y"
  else PM=""; fi
}
# nome do pacote por distro (var indireta)
pkg() {
  local key="$1"
  case "$key:$PM" in
    fd:apt) echo fd-find;;  fd:*) echo fd;;
    poppler:apt|poppler:dnf) echo poppler-utils;;
    poppler:pacman) echo poppler;;  poppler:zypper) echo poppler-tools;;
    ripgrep:*) echo ripgrep;;
    pandoc:*) echo pandoc;;
    rga:pacman) echo ripgrep-all;;  rga:*) echo ripgrep-all;;
    pyside6:apt) echo python3-pyside6;;   pyside6:dnf) echo python3-pyside6;;
    pyside6:pacman) echo pyside6;;        pyside6:zypper) echo python3-PySide6;;
    pyside6:*) echo python3-pyside6;;
    *) echo "$key";;
  esac
}

sys_install() {   # sys_install <chave-logica> <binario-p/-checar>
  local key="$1" probe="$2" p; p="$(pkg "$key")"
  if has "$probe"; then ok "$probe já instalado"; return; fi
  if [ -z "$PM" ]; then wn "sem gerenciador de pacotes conhecido — instale '$p' manualmente"; return; fi
  c "Instalando $p (via $PM)…"
  if $INSTALL "$p"; then ok "$p instalado"; else wn "falhou instalar $p — siga sem ele"; fi
}

# o repositório desta distro conhece o pacote? (evita tentar instalar em vão)
pkg_exists() {
  case "$PM" in
    apt)    apt-cache show "$1" >/dev/null 2>&1;;
    dnf)    dnf -q info "$1" >/dev/null 2>&1;;
    pacman) pacman -Si "$1" >/dev/null 2>&1;;
    zypper) zypper -q info "$1" >/dev/null 2>&1;;
    *)      return 1;;
  esac
}

# -------------------------------------------------- binário estático (rga/pandoc)
dl() { # dl <url> <destino>
  if has curl; then curl -fsSL --retry 3 "$1" -o "$2"
  elif has wget; then wget -qO "$2" "$1"
  else er "preciso de curl ou wget"; return 1; fi
}

install_rga() {
  mkdir -p "$PREFIX/bin"
  if has rga; then ok "rga já disponível ($(command -v rga))"; return; fi
  if [ "$ARCH" != "x86_64" ]; then
    wn "rga: binário pronto só p/ x86_64 (seu: $ARCH). Instale 'ripgrep-all' pelo gerenciador."
    sys_install ripgrep-all rga; return
  fi
  local v="v0.10.10"
  local url="https://github.com/phiresky/ripgrep-all/releases/download/$v/ripgrep_all-$v-x86_64-unknown-linux-musl.tar.gz"
  c "Baixando ripgrep-all $v (estático)…"
  local tmp; tmp="$(mktemp -d)"
  if dl "$url" "$tmp/rga.tgz"; then
    tar xzf "$tmp/rga.tgz" -C "$tmp"
    local d; d="$(find "$tmp" -maxdepth 1 -type d -name 'ripgrep_all-*')"
    install -m755 "$d/rga" "$d/rga-preproc" "$PREFIX/bin/"
    ln -sf "$PREFIX/bin/rga" "$BIN/rga"; ln -sf "$PREFIX/bin/rga-preproc" "$BIN/rga-preproc"
    ok "rga instalado em $PREFIX/bin (+ symlink em $BIN)"
  else wn "download do rga falhou — modo documentos ficará indisponível"; fi
  rm -rf "$tmp"
}

install_pandoc() {
  if has pandoc; then ok "pandoc já disponível"; return; fi
  local amd; case "$ARCH" in x86_64) amd=amd64;; aarch64|arm64) amd=arm64;; *) amd="";; esac
  if [ -z "$amd" ]; then wn "pandoc: arquitetura $ARCH sem binário pronto — docx/epub ficam de fora"; return; fi
  local v="3.10"
  local url="https://github.com/jgm/pandoc/releases/download/$v/pandoc-$v-linux-$amd.tar.gz"
  c "Baixando pandoc $v (estático, p/ docx/epub/odt)…"
  local tmp; tmp="$(mktemp -d)"
  if dl "$url" "$tmp/p.tgz"; then
    tar xzf "$tmp/p.tgz" -C "$tmp"
    install -m755 "$(find "$tmp" -type f -name pandoc)" "$PREFIX/bin/pandoc"
    ln -sf "$PREFIX/bin/pandoc" "$BIN/pandoc"
    ok "pandoc instalado (docx/epub/odt/html cobertos)"
  else wn "download do pandoc falhou — só PDF/zip no modo documentos"; fi
  rm -rf "$tmp"
}

# -------------------------------------------------- Python + PySide6
PYBIN=""

# Qt >= 6.5 (o PySide6 do pip) exige a libxcb-cursor do SISTEMA p/ abrir em X11 —
# o pip não empacota libs de sistema. Sem ela a GUI aborta no arranque com
# "Could not load the Qt platform plugin xcb". Pacotes da distro (python3-pyside6
# etc.) puxam-na por dependência; o caminho do venv precisa garantir na mão.
ensure_qt_xcb() {
  # grep SEM -q: com pipefail, o -q sai no 1º match e o SIGPIPE no ldconfig
  # derruba o pipeline — a lib presente pareceria ausente
  ldconfig -p 2>/dev/null | grep 'libxcb-cursor\.so\.0' >/dev/null && return 0
  if [ -z "$PM" ]; then
    wn "libxcb-cursor ausente — sem ela a GUI não abre em X11; instale pela sua distro"; return 1
  fi
  local p; case "$PM" in
    dnf|pacman) p=xcb-util-cursor;;
    *)          p=libxcb-cursor0;;      # apt/zypper
  esac
  c "Instalando $p (plugin xcb do Qt p/ a GUI)…"
  if $INSTALL "$p"; then ok "$p instalado"; else wn "falhou instalar $p — a GUI pode não abrir em X11"; fi
}

# venv de verdade exige o ensurepip, que em Debian/Mint vem em pacote SEPARADO
# (python3.X-venv). Atenção: `python3 -m venv --help` funciona mesmo SEM ele, e o
# binário `python3` sempre existe — nenhum dos dois serve de teste. Quem falta é o
# ensurepip, então é ele que checamos.
ensure_venv_pkg() {
  python3 -c "import ensurepip" >/dev/null 2>&1 && return 0
  if [ -z "$PM" ]; then
    wn "ensurepip ausente e sem gerenciador de pacotes — instale o venv da sua distro"; return 1
  fi
  local pv; pv="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
  c "Instalando suporte a venv (ensurepip ausente)…"
  local p                                  # $pv já vem como "3.12" -> python3.12-venv
  for p in ${pv:+"python$pv-venv"} python3-venv; do
    pkg_exists "$p" || continue
    $INSTALL "$p" || continue
    if python3 -c "import ensurepip" >/dev/null 2>&1; then ok "$p instalado"; return 0; fi
  done
  wn "não consegui habilitar o venv — instale manualmente (ex.: python$pv-venv)"; return 1
}

setup_python() {
  if python3 -c "import PySide6" >/dev/null 2>&1; then
    PYBIN="$(command -v python3)"; ok "PySide6 do sistema OK"; return
  fi
  # tenta o pacote PySide6 da distro (traz QtMultimedia p/ o player).
  # Nem toda distro tem: Ubuntu noble/Mint 22.x só empacotam PySide2 (Qt5) — aí
  # nem tentamos, pra não poluir a saída com um erro esperado, e vamos de venv.
  # (recusa no prompt ≠ pacote inexistente: cada caso tem sua mensagem)
  if [ -n "$PM" ] && ! pkg_exists "$(pkg pyside6)"; then
    wn "$(pkg pyside6) não existe no repositório desta distro — usando venv."
  elif [ -n "$PM" ] && ask "Instalar PySide6 pelo gerenciador ($(pkg pyside6))?"; then
    $INSTALL "$(pkg pyside6)" || true
    if python3 -c "import PySide6" >/dev/null 2>&1; then
      PYBIN="$(command -v python3)"; ok "PySide6 do sistema OK"; return
    fi
    wn "PySide6 do sistema não ficou disponível — caindo para venv."
  fi
  ensure_qt_xcb || true            # PySide6 do pip: garante o xcb do sistema (GUI)
  # venv anterior que já funciona: reaproveita (re-rodar o instalador não deve
  # rebaixar ~250 MB de PySide6 à toa)
  if [ -x "$PREFIX/venv/bin/python" ] && "$PREFIX/venv/bin/python" -c "import PySide6" >/dev/null 2>&1; then
    PYBIN="$PREFIX/venv/bin/python"; ok "venv com PySide6 já existe — reaproveitado"; return
  fi
  c "Criando ambiente próprio (venv) com PySide6…"
  ensure_venv_pkg || true          # se falhar, o venv abaixo dirá o porquê
  rm -rf "$PREFIX/venv"            # só aqui: venv ausente ou quebrado (ex.: sem ensurepip)
  python3 -m venv "$PREFIX/venv"
  "$PREFIX/venv/bin/pip" install --upgrade pip >/dev/null
  c "Instalando PySide6 no venv (pode baixar ~100 MB)…"
  "$PREFIX/venv/bin/pip" install PySide6
  PYBIN="$PREFIX/venv/bin/python"; ok "PySide6 instalado no venv"
}

# -------------------------------------------------- copiar app + lançadores
install_app() {
  mkdir -p "$PREFIX/lfs" "$PREFIX/assets" "$BIN" "$APPDIR"
  cp -f "$SRC/lfs/"*.py "$PREFIX/lfs/"
  cp -f "$SRC/assets/"* "$PREFIX/assets/" 2>/dev/null || true
  rm -rf "$PREFIX/lfs/__pycache__"     # .pyc velho de um módulo removido confunde

  # Carimbo da build. O app instalado é uma CÓPIA: sem isto, nada na tela
  # distingue a versão de hoje da de semana passada, e "isso não existe no
  # programa" vira uma sessão inteira de depuração de um recurso que já estava
  # pronto. O título da janela mostra o que este arquivo diz.
  ver=""
  if git -C "$SRC" rev-parse --short HEAD >/dev/null 2>&1; then
    ver="$(git -C "$SRC" rev-parse --short HEAD)"
    [ -n "$(git -C "$SRC" status --porcelain 2>/dev/null)" ] && ver="$ver+"
    ver="$ver ($(git -C "$SRC" log -1 --format=%cs 2>/dev/null))"
  else
    ver="$(date +%Y-%m-%d)"            # tarball sem git: ao menos a data
  fi
  printf '%s\n' "$ver" > "$PREFIX/VERSION"
  ok "build instalada: $ver"

  cat > "$BIN/$APP" <<EOF
#!/usr/bin/env bash
# Lançador do Linux File Search (gerado pelo install.sh)
export PATH="$PREFIX/bin:\$PATH"    # acha rga/pandoc empacotados
exec "$PYBIN" "$PREFIX/lfs/app.py" "\$@"
EOF
  chmod +x "$BIN/$APP"
  ln -sf "$PREFIX/lfs/cli.py" "$BIN/$APP-cli" 2>/dev/null || true
  # CLI standalone (com o python certo)
  cat > "$BIN/lfs" <<EOF
#!/usr/bin/env bash
export PATH="$PREFIX/bin:\$PATH"
exec "$PYBIN" "$PREFIX/lfs/cli.py" "\$@"
EOF
  chmod +x "$BIN/lfs"
  ok "app em $PREFIX  ·  lançadores: $BIN/$APP  e  $BIN/lfs (CLI)"

  # ícones no tema hicolor
  for sz in 48 64 128 256; do
    if [ -f "$SRC/assets/icon_$sz.png" ]; then
      mkdir -p "$ICONS/${sz}x${sz}/apps"
      cp -f "$SRC/assets/icon_$sz.png" "$ICONS/${sz}x${sz}/apps/$APP.png"
    fi
  done
  [ -f "$SRC/assets/icon.svg" ] && { mkdir -p "$ICONS/scalable/apps"; cp -f "$SRC/assets/icon.svg" "$ICONS/scalable/apps/$APP.svg"; }

  cat > "$APPDIR/$APP.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Linux File Search
GenericName=Busca de arquivos
Comment=Busca ampla de arquivos: nome, conteúdo, booleano e dentro de documentos
Exec=$BIN/$APP %F
Icon=$APP
Terminal=false
Categories=Utility;System;FileTools;
Keywords=busca;search;grep;ripgrep;arquivos;conteudo;pdf;booleano;
StartupNotify=true
EOF
  has update-desktop-database && update-desktop-database "$APPDIR" >/dev/null 2>&1 || true
  has gtk-update-icon-cache && gtk-update-icon-cache -f -t "$ICONS" >/dev/null 2>&1 || true
  ok "atalho de menu instalado (Linux File Search)"
}

# mostra o plano de dependências (nome de pacote resolvido p/ ESTA distro) e pede OK
plan_and_confirm() {
  c "Dependências do projeto (para $PM):"
  printf "  %-16s %-22s %s\n" "COMPONENTE" "PACOTE ($PM)" "PAPEL"
  printf "  %-16s %-22s %s\n" "ripgrep"     "$(pkg ripgrep)" "busca de conteúdo"
  printf "  %-16s %-22s %s\n" "fd"          "$(pkg fd)"      "busca por nome"
  printf "  %-16s %-22s %s\n" "poppler"     "$(pkg poppler)" "texto de PDF (opcional)"
  printf "  %-16s %-22s %s\n" "pandoc"      "$(pkg pandoc)"  "docx/epub/odt (opcional)"
  printf "  %-16s %-22s %s\n" "ripgrep-all" "$(pkg rga)"     "buscar dentro de documentos"
  printf "  %-16s %-22s %s\n" "PySide6"     "$(pkg pyside6)" "interface gráfica (ou venv)"
  echo "  (rga/pandoc: se o repositório não tiver, baixo o binário estático. App vai em ~/.local, sem root.)"
  echo
  if ! ask "Instalar/verificar essas dependências agora?"; then
    wn "Instalação de dependências pulada a pedido. O app será copiado, mas pode faltar motor."
    return 1
  fi
  return 0
}

# ============================================================ fluxo
c "== Linux File Search — instalador =="
echo "  destino: $PREFIX"
echo "  arch:    $ARCH"
detect_pm; [ -n "$PM" ] && echo "  pacotes: $PM" || wn "gerenciador de pacotes não detectado"
echo

DEPS_OK=1; plan_and_confirm || DEPS_OK=0
echo

if [ "$DEPS_OK" = 1 ]; then
c "[1/5] Dependências de busca (ripgrep, fd, poppler)"
sys_install ripgrep rg
sys_install fd "$(has fdfind && echo fdfind || echo fd)"
sys_install poppler pdftotext
echo
c "[2/5] ripgrep-all (busca dentro de documentos)"
install_rga
echo
c "[3/5] pandoc (docx/epub/odt)"
install_pandoc
echo
fi   # fim do bloco de dependências (DEPS_OK)

c "[4/5] Python + PySide6 (GUI)"
setup_python
echo
c "[5/5] Instalando o aplicativo"
install_app
echo
c "== Pronto! =="
echo "  GUI : abra 'Linux File Search' no menu, ou rode:  $APP"
echo "  CLI : lfs ~/pasta -c \"texto\"   |   lfs ~/docs -n '*.pdf' -c laudo --docs"
case ":$PATH:" in *":$BIN:"*) : ;; *) wn "adicione ao PATH:  export PATH=\"$BIN:\$PATH\"";; esac
