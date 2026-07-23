#!/usr/bin/env python3
# Sombrero File Search — Copyright (C) 2026 Rodrigo Toledo
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Este programa é software livre: você pode redistribuí-lo e/ou modificá-lo sob
# os termos da GNU General Public License, versão 3 ou posterior (ver LICENSE).
# Distribuído na esperança de ser útil, mas SEM QUALQUER GARANTIA.
"""Sombrero File Search — GUI (PySide6).

Busca ampla de arquivos estilo Agent Ransack / FileLocator Pro, sobre ripgrep+fd+rga,
NATIVA e portável entre distros. Motor em engine.py (sem Qt). A GUI só orquestra:
form -> worker em thread -> tabela ao vivo -> preview com destaque.

Recursos: nome+conteúdo, booleano (A OR B) AND C NOT D, documentos (PDF/docx/epub/zip).
Desenho: GARIMPO_Desenho_Busca_ripgrep.md (Fable 5) — nome final "Sombrero File Search".
"""
from __future__ import annotations
import os, sys, threading, time, queue
from urllib.parse import quote

from PySide6.QtCore import (Qt, QThread, Signal, QAbstractTableModel, QModelIndex,
                            QUrl, QTimer, QSortFilterProxyModel, QRect, QSize,
                            QByteArray, QMimeData)
from PySide6.QtGui import (QColor, QDesktopServices, QFont, QGuiApplication,
                           QIcon, QImageReader, QPixmap, QKeySequence, QShortcut,
                           QTextCharFormat, QTextCursor, QTextDocument)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QPushButton, QCheckBox, QLabel, QTableView, QPlainTextEdit,
    QFileDialog, QSplitter, QHeaderView, QSpinBox, QMenu, QTextEdit,
    QAbstractItemView, QToolButton, QFrame, QStackedWidget, QSlider, QSizePolicy,
    QLayout, QDialog, QDialogButtonBox, QProgressBar, QFormLayout, QMessageBox,
    QInputDialog, QTabWidget)

try:
    from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
    from PySide6.QtMultimediaWidgets import QVideoWidget
    HAS_MEDIA = True
except ImportError:                     # QtMultimedia opcional (portabilidade)
    HAS_MEDIA = False

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine, boolean, disks, fileops, xdg, version, searches, humane, resultfilter
from engine import Query, Match
from i18n import t

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets")

_IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".tif",
            ".tiff", ".ico", ".ppm", ".pgm", ".xpm"}
_VID_EXT = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".wmv", ".flv", ".m4v",
            ".mpg", ".mpeg", ".ts", ".3gp", ".ogv"}
_AUD_EXT = {".mp3", ".flac", ".wav", ".ogg", ".oga", ".m4a", ".aac", ".opus",
            ".wma", ".aiff", ".alac"}


def media_kind(path: str):
    """'image' | 'video' | 'audio' | None conforme a extensão."""
    ext = os.path.splitext(path)[1].lower()
    if ext in _IMG_EXT:
        return "image"
    if ext in _VID_EXT:
        return "video"
    if ext in _AUD_EXT:
        return "audio"
    return None


def fmt_ms(ms: int) -> str:
    s = max(0, ms // 1000)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def human_size(n: int) -> str:
    f = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or u == "TB":
            return f"{int(f)} {u}" if u == "B" else f"{f:.1f} {u}"
        f /= 1024
    return f"{f:.1f} TB"


def _grp(n: int) -> str:
    """Agrupa milhares para o contador ("3.000"/"3,000"). Separador segue o idioma."""
    import i18n as _i18n
    sep = "." if _i18n.current_lang() == "pt" else ","
    return f"{int(n):,}".replace(",", sep)


parse_size = engine.parse_size          # §5: fonte única (era duplicado aqui e no cli)


# ----------------------------------------------------------------- F7: interop
def path_to_uri(path: str) -> str:
    """Caminho -> URI file://, percent-encoded a partir dos BYTES do nome.

    Não dá para usar QUrl.fromLocalFile aqui: ela recebe uma QString, e um nome
    de arquivo não-UTF-8 (que no Linux é perfeitamente legal, e existe aos montes
    num acervo vindo de Windows/câmera) chega em Python como surrogate escape —
    a conversão para QString descarta esses bytes EM SILÊNCIO. A URI resultante
    apontaria para um arquivo que não existe, e o gerenciador diria só "não
    encontrado". fsencode + quote preserva byte a byte, que é como o
    text/uri-list é definido."""
    return "file://" + quote(os.fsencode(os.path.abspath(path)), safe="/")


def build_paths_mime(paths) -> QMimeData:
    """Carga de clipboard/arrasto que os gerenciadores de arquivo entendem.

    Três formatos, porque cada família lê o seu — é o que faz Ctrl+C aqui e
    Ctrl+V no Nemo funcionar de verdade:
      text/uri-list                    todo mundo (percent-encoding cobre \\n,
                                       espaço e nome não-UTF-8)
      x-special/gnome-copied-files     Nemo/Nautilus/Caja — 'copy\\n' + URIs
      application/x-kde-cutselection   Dolphin — '0' = é cópia, não recorte
    O LFS nunca escreve 'cut' nem lê o clipboard: não existe Colar aqui."""
    md = QMimeData()
    urls = [QUrl.fromEncoded(QByteArray(path_to_uri(p).encode("ascii")))
            for p in paths]
    md.setUrls(urls)                                   # text/uri-list
    md.setText("\n".join(paths))                       # soltar em terminal/editor
    enc = "\n".join(path_to_uri(p) for p in paths)
    md.setData("x-special/gnome-copied-files",
               QByteArray(("copy\n" + enc).encode("ascii")))
    md.setData("application/x-kde-cutselection", QByteArray(b"0"))
    return md


# D-Bus org.freedesktop.FileManager1: "abra a pasta COM o item selecionado".
# Padrão freedesktop implementado por Nemo, Nautilus, Dolphin e Thunar.
FM1_SERVICE = "org.freedesktop.FileManager1"
FM1_PATH = "/org/freedesktop/FileManager1"
FM1_IFACE = "org.freedesktop.FileManager1"


def showitems_args(paths):
    """Argumentos da chamada ShowItems. Função pura, separada da chamada, para o
    teste headless verificar a montagem da mensagem sem barramento nenhum."""
    uris = [path_to_uri(p) for p in paths]
    return (FM1_SERVICE, FM1_PATH, FM1_IFACE, "ShowItems", uris, "")


class FlowLayout(QLayout):
    """Layout que QUEBRA LINHA (estilo tags). A linha de chips em QHBoxLayout
    impunha ~1135px de largura mínima à janela — mais que uma tela 1080px em
    retrato, e o Muffin/Cinnamon suprime o botão de maximizar de janela que não
    cabe. Com quebra, o mínimo cai para a largura do maior chip."""
    def __init__(self, parent=None, hspacing=7, vspacing=6):
        super().__init__(parent)
        self._items, self._h, self._v = [], hspacing, vspacing

    def addItem(self, it): self._items.append(it)
    def count(self): return len(self._items)
    def itemAt(self, i): return self._items[i] if 0 <= i < len(self._items) else None
    def takeAt(self, i): return self._items.pop(i) if 0 <= i < len(self._items) else None
    def expandingDirections(self): return Qt.Orientations(0)
    def hasHeightForWidth(self): return True
    def heightForWidth(self, w): return self._arrange(QRect(0, 0, w, 0), dry=True)
    def sizeHint(self): return self.minimumSize()

    def setGeometry(self, r):
        super().setGeometry(r)
        self._arrange(r, dry=False)

    def minimumSize(self):
        s = QSize()
        for it in self._items:
            s = s.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return s + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _arrange(self, rect, dry):
        m = self.contentsMargins()
        x, y, row_h = rect.x() + m.left(), rect.y() + m.top(), 0
        right = rect.right() - m.right()
        for it in self._items:
            w, h = it.sizeHint().width(), it.sizeHint().height()
            if row_h and x + w > right:              # não coube: próxima linha
                x = rect.x() + m.left()
                y += row_h + self._v
                row_h = 0
            if not dry:
                it.setGeometry(QRect(x, y, w, h))
            x += w + self._h
            row_h = max(row_h, h)
        return y + row_h + m.bottom() - rect.y()


# ----------------------------------------------------------------- worker
class SearchWorker(QThread):
    batch = Signal(list)             # lista de Match
    progress = Signal(int)           # varridos parciais
    phase = Signal(int, int, str)    # opt#4: passo done/total + rótulo (modo booleano)
    done = Signal(int, float)        # total, segundos
    error = Signal(str)              # mensagem de erro (ex: sintaxe booleana)

    def __init__(self, q: Query, boolexpr: str = ""):
        super().__init__()
        self.q = q
        self.boolexpr = boolexpr
        self._cancel = False
        self._buf: list[Match] = []
        self._last = 0.0
        self.stats: dict = {"denied": 0}     # B8: contadores (inacessíveis etc.)

    def cancel(self):
        self._cancel = True

    def _flush(self, force=False):
        now = time.time()
        if self._buf and (force or now - self._last > 0.1 or len(self._buf) >= 200):
            self.batch.emit(self._buf)
            self._buf = []
            self._last = now

    def run(self):
        self._last = time.time()
        def on_result(m: Match):
            self._buf.append(m)
            self._flush()
        def on_prog(n):
            self.progress.emit(n)
        def on_phase(d, total, label):
            self.phase.emit(d, total, label)
        try:
            if self.boolexpr:
                tot, dt = boolean.search_boolean(self.q, self.boolexpr, on_result,
                                                 lambda: self._cancel, on_prog, on_phase,
                                                 stats=self.stats)      # N2: conta inacessíveis
            else:
                tot, dt = engine.search(self.q, on_result, lambda: self._cancel, on_prog,
                                        stats=self.stats)
        except boolean.BooleanError as e:
            self._flush(force=True)
            self.error.emit(humane.human_error(e))
            return
        self._flush(force=True)
        self.done.emit(tot, dt)


# ----------------------------------------------------------------- modelo
class ResultModel(QAbstractTableModel):
    HEADERS = ["File", "Folder", "Matches", "Size", "Modified"]   # source (EN); i18n em headerData
    SORT_ROLE = Qt.UserRole + 1

    def __init__(self):
        super().__init__()
        self.rows: list[Match] = []

    def rowCount(self, parent=QModelIndex()):
        return len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.HEADERS)

    def headerData(self, s, o, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and o == Qt.Horizontal:
            return t(self.HEADERS[s])
        return None

    def data(self, idx, role=Qt.DisplayRole):
        if not idx.isValid():
            return None
        m = self.rows[idx.row()]
        c = idx.column()
        if role == Qt.DisplayRole:
            if c == 0: return os.path.basename(m.path) + ("/" if m.is_dir else "")
            if c == 1: return os.path.dirname(m.path)
            if c == 2: return str(m.nmatch) if m.nmatch else ""
            if c == 3: return "" if m.is_dir else human_size(m.size)
            if c == 4: return time.strftime("%Y-%m-%d %H:%M", time.localtime(m.mtime)) if m.mtime else ""
        elif role == Qt.TextAlignmentRole and c in (2, 3):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        elif role == Qt.ToolTipRole:
            return m.path
        elif role == Qt.UserRole:
            return m
        elif role == ResultModel.SORT_ROLE:      # B14: chave numérica p/ ordenar
            if c == 0: return os.path.basename(m.path).lower()
            if c == 1: return os.path.dirname(m.path).lower()
            if c == 2: return m.nmatch
            if c == 3: return m.size
            if c == 4: return m.mtime
        return None

    def append(self, matches: list[Match]):
        if not matches:
            return
        a = len(self.rows)
        self.beginInsertRows(QModelIndex(), a, a + len(matches) - 1)
        self.rows.extend(matches)
        self.endInsertRows()

    def clear(self):
        self.beginResetModel()
        self.rows = []
        self.endResetModel()

    def match_at(self, row):
        return self.rows[row] if 0 <= row < len(self.rows) else None

    # ---- F7: arrastar para FORA (o gesto central: soltar no Nemo/desktop/e-mail)
    def flags(self, idx):
        f = super().flags(idx)
        if idx.isValid():
            f |= Qt.ItemIsDragEnabled
        return f

    def supportedDragActions(self):
        """SÓ copiar. Nem MoveAction: mesmo que o alvo peça mover, o LFS não
        oferece — o Nemo então copia. É a garantia não-destrutiva no nível do
        protocolo de arrasto, não só do menu."""
        return Qt.CopyAction

    def supportedDropActions(self):
        return Qt.CopyAction

    def mimeTypes(self):
        return ["text/uri-list", "text/plain"]

    def mimeData(self, indexes):
        # chegam 5 índices por linha (um por coluna): deduplica por linha
        rows = sorted({i.row() for i in indexes if i.isValid()})
        paths = [self.rows[r].path for r in rows if 0 <= r < len(self.rows)]
        return build_paths_mime(paths)


class ResultFilterProxy(QSortFilterProxyModel):
    """F10a #1 — filtro DENTRO dos resultados. Aplica o predicado puro do
    resultfilter sobre o Match JÁ carregado (nome, caminho, mtime lidos do
    UserRole) — nunca toca disco. Ordenação de coluna segue funcionando (herda
    do QSortFilterProxyModel). Filtro vazio => aceita tudo (caminho rápido)."""
    def __init__(self):
        super().__init__()
        self._pred = None
        self.setSortRole(ResultModel.SORT_ROLE)

    def set_filter_text(self, text: str):
        text = (text or "").strip()
        self._pred = resultfilter.compile_filter(text) if text else None
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        if self._pred is None:
            return True
        m = self.sourceModel().rows[row]     # Match; sem I/O — dados em memória
        name = os.path.basename(m.path)
        return self._pred(name, m.path, m.mtime)


# ----------------------------------------------------------------- temas
_CONFIG_BASE = os.path.expanduser(os.environ.get("XDG_CONFIG_HOME", "~/.config"))
CONFIG_DIR = os.path.join(_CONFIG_BASE, "sombrero-file-search")
CONFIG = os.path.join(CONFIG_DIR, "config.json")


def _migrate_old_config():
    """Rebranding Linux File Search -> Sombrero File Search (jul/2026): as buscas
    salvas e o histórico do F5 (mais o tema) viviam em ~/.config/linux-file-search.
    Quem já usava o programa não pode perder isso ao atualizar. Migração única e
    conservadora: só move se o diretório NOVO ainda não existe e o ANTIGO existe.
    Falha em silêncio — perder a config antiga é um aborrecimento, travar o
    arranque do app por causa dela seria pior."""
    old = os.path.join(_CONFIG_BASE, "linux-file-search")
    if os.path.isdir(old) and not os.path.exists(CONFIG_DIR):
        try:
            os.rename(old, CONFIG_DIR)
        except OSError:
            pass

def load_cfg() -> dict:
    try:
        import json
        with open(os.path.expanduser(CONFIG)) as f:
            return json.load(f)
    except Exception:
        return {}

def save_cfg(d: dict):
    try:
        import json
        os.makedirs(os.path.expanduser(CONFIG_DIR), exist_ok=True)
        with open(os.path.expanduser(CONFIG), "w") as f:
            json.dump(d, f, indent=2)
    except OSError:
        pass

THEMES = {
    "dark": dict(
        bg0="#0e1217", bg1="#151a21", bg2="#1b212a", bg3="#212936", alt="#0e1217",
        border="#262d38", border2="#33404f",
        txt="#e7ebf2", muted="#8b95a5", on_accent="#071018",
        accent="#4f9cf9", accent_hi="#6fb0ff", accent_dim="#274060",
        green="#34d399", amber="#f0a35e", red="#f87171",
    ),
    "light": dict(
        bg0="#eef1f6", bg1="#ffffff", bg2="#f2f5fa", bg3="#e6ebf3", alt="#f5f7fb",
        border="#d7dde8", border2="#c3ccda",
        txt="#1a2130", muted="#5c6675", on_accent="#ffffff",
        accent="#2f7ff0", accent_hi="#1f6fe0", accent_dim="#d7e6fc",
        green="#12a150", amber="#b3730a", red="#d13b3b",
    ),
}

_STYLE_TMPL = """
* {{ font-family: "Inter", "Segoe UI", "Ubuntu", "Noto Sans", sans-serif; font-size: 13px; }}
QMainWindow, QWidget#central {{ background: {bg0}; }}
QLabel {{ color: {txt}; background: transparent; }}
QLabel#title {{ font-size: 17px; font-weight: 700; color: {txt}; }}
QLabel#subtitle {{ color: {muted}; font-size: 12px; }}
QLabel#section {{ color: {muted}; font-size: 11px; font-weight: 600; }}
QFrame#header, QFrame#toolbar {{ background: {bg1}; border: 1px solid {border}; border-radius: 12px; }}
QFrame#hline {{ background: {border}; max-height: 1px; border: 0; }}

QLineEdit, QSpinBox, QPlainTextEdit {{
    background: {bg1}; color: {txt}; border: 1px solid {border};
    border-radius: 9px; padding: 7px 10px; selection-background-color: {accent_dim}; }}
QLineEdit:focus, QSpinBox:focus {{ border: 1px solid {accent}; }}
QLineEdit#primaryfield {{ font-size: 14px; padding: 9px 12px; }}
QPlainTextEdit {{ padding: 8px 10px; }}

QPushButton {{ background: {bg2}; color: {txt}; border: 1px solid {border2};
    border-radius: 9px; padding: 8px 18px; font-weight: 600; }}
QPushButton:hover {{ background: {bg3}; border-color: {accent}; }}
QPushButton:disabled {{ color: {muted}; background: {bg1}; border-color: {border}; }}
QPushButton#primary {{ background: {accent}; color: {on_accent}; border: 0; }}
QPushButton#primary:hover {{ background: {accent_hi}; }}
QPushButton#primary:disabled {{ background: {bg3}; color: {muted}; }}
QToolButton {{ background: {bg2}; color: {txt}; border: 1px solid {border2};
    border-radius: 9px; padding: 7px 10px; }}
QToolButton:hover {{ background: {bg3}; border-color: {accent}; }}

/* chips de opção (QCheckBox sem caixa, o rótulo inteiro vira pílula) */
QCheckBox {{ color: {muted}; background: {bg1}; border: 1px solid {border};
    border-radius: 13px; padding: 5px 12px; spacing: 0; }}
QCheckBox:hover {{ border-color: {border2}; color: {txt}; }}
QCheckBox:checked {{ color: {on_accent}; background: {accent}; border-color: {accent}; font-weight: 600; }}
QCheckBox:disabled {{ color: {muted}; background: {bg0}; border-color: {border}; }}
QCheckBox::indicator {{ width: 0; height: 0; }}

QTableView {{ background: {bg1}; color: {txt}; border: 1px solid {border};
    border-radius: 12px; gridline-color: {bg2};
    alternate-background-color: {alt}; selection-background-color: {accent_dim};
    selection-color: {txt}; outline: 0; }}
QTableView::item {{ padding: 5px 8px; border: 0; }}
QTableView::item:selected {{ background: {accent_dim}; }}
QHeaderView::section {{ background: {bg2}; color: {muted}; border: 0;
    border-right: 1px solid {border}; border-bottom: 1px solid {border};
    padding: 7px 8px; font-weight: 600; }}
QTableCornerButton::section {{ background: {bg2}; border: 0; }}

QSplitter::handle {{ background: transparent; height: 8px; }}

QMenu {{ background: {bg2}; color: {txt}; border: 1px solid {border2}; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 6px 20px; border-radius: 6px; }}
QMenu::item:selected {{ background: {accent_dim}; }}

QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {border2}; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {accent}; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {border2}; border-radius: 5px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QToolTip {{ background: {bg2}; color: {txt}; border: 1px solid {border2}; border-radius: 6px; padding: 5px 8px; }}

/* painel de mídia */
QFrame#mediastage {{ background: {bg0}; border: 1px solid {border}; border-radius: 12px; }}
QFrame#mediabar {{ background: {bg1}; border: 1px solid {border}; border-radius: 10px; }}
QLabel#medianame {{ color: {txt}; font-weight: 600; }}
QLabel#mediatime {{ color: {muted}; font-size: 12px; }}
QLabel#mediahint {{ color: {muted}; font-size: 34px; }}
QToolButton#transport {{ background: {bg2}; color: {txt}; border: 1px solid {border2};
    border-radius: 17px; min-width: 34px; min-height: 34px; font-size: 15px; padding: 0; }}
QToolButton#transport:hover {{ background: {bg3}; border-color: {accent}; }}
QToolButton#transport:disabled {{ color: {muted}; background: {bg1}; border-color: {border}; }}
QToolButton#play {{ background: {accent}; color: {on_accent}; border: 0;
    border-radius: 19px; min-width: 38px; min-height: 38px; font-size: 16px; }}
QToolButton#play:hover {{ background: {accent_hi}; }}
QToolButton#play:disabled {{ background: {bg3}; color: {muted}; }}
QSlider::groove:horizontal {{ height: 5px; background: {bg3}; border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: {accent}; border-radius: 3px; }}
QSlider::handle:horizontal {{ background: {txt}; width: 13px; height: 13px;
    margin: -5px 0; border-radius: 7px; }}
QSlider::handle:horizontal:hover {{ background: {accent_hi}; }}
"""

def build_style(pal: dict) -> str:
    return _STYLE_TMPL.format(**pal)


def _badge(name: str, present: bool, pal: dict) -> QLabel:
    """Selo de motor: nome + bolinha verde (presente) ou cinza (ausente)."""
    col = pal["green"] if present else pal["muted"]
    lab = QLabel(f'<span style="color:{col}">●</span> '
                 f'<span style="color:{pal["muted"]}">{name}</span>')
    lab.setToolTip(f"{name}: {t('available') if present else t('missing')}")
    return lab


# ----------------------------------------------------------------- F7: fila de cópia
class Ask:
    """Uma pergunta feita PELA thread de cópia À GUI (conflito, pré-checagem).
    A thread bloqueia até a resposta chegar; a GUI nunca espera pela thread —
    então não existe caminho de travamento em círculo. O `wait` tem timeout para
    que um cancelamento (ou o fechamento da janela) libere a thread na hora."""

    def __init__(self):
        self._ev = threading.Event()
        self.value = None

    def wait(self, timeout=0.2) -> bool:
        return self._ev.wait(timeout)

    def reply(self, value):
        self.value = value
        self._ev.set()


class CopyQueue:
    """FIFO de um worker só — decisão deliberada do desenho: a origem costuma ser
    SMR, e paralelizar leitura é o seek thrash que este projeto existe para
    evitar. Uma barra, um cancelar, raciocínio trivial (modelo Nemo).

    A6: fila BLOQUEANTE (queue.Queue). O worker persistente dorme em get() sem
    gastar CPU e acorda no instante em que um job chega — nada de recriar QThread
    por arrasto (a corrida antiga de reatribuir self.copier morreu com isso)."""

    def __init__(self):
        self._q = queue.Queue()

    def put(self, job):
        self._q.put(job)

    def get(self):
        return self._q.get()               # bloqueia até haver trabalho (ou sentinela)

    def pending(self) -> int:
        return self._q.qsize()             # aproximado, mas só alimenta um rótulo

    def drain(self):
        """Descarta os jobs ainda não iniciados (o 'cancelar tudo' da barra)."""
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass


class CopyWorker(QThread):
    ask_preflight = Signal(object, object)      # (Preflight, Ask)
    ask_conflict = Signal(str, str, object)     # (src, dst, Ask)
    progress = Signal(object)                   # CopyProgress
    job_started = Signal(str, int)              # destino, pendentes
    job_done = Signal(object, str)              # (CopyResult|None, destino)
    all_done = Signal()

    def __init__(self, queue: CopyQueue):
        super().__init__()
        self.q = queue
        self.cancel_ev = threading.Event()
        self._shutdown = False

    def cancel_all(self):
        """Cancela o job atual E descarta os pendentes — o 'Cancelar' de quem olha
        uma barra só. NÃO mata a thread (A6): ela volta a dormir na fila, pronta
        para o próximo arrasto. cancel_ev é rearmado no topo da próxima rodada."""
        self.cancel_ev.set()
        self.q.drain()

    def shutdown(self):
        """Encerra a thread de vez (só no closeEvent): aborta o job atual e injeta
        a sentinela que rompe o get() bloqueante. Sem terminate() no caminho feliz."""
        self._shutdown = True
        self.cancel_ev.set()
        self.q.put(None)

    def _await(self, ask: Ask, on_cancel):
        while not ask.wait(0.2):
            if self._shutdown or self.cancel_ev.is_set():
                return on_cancel
        return ask.value if ask.value is not None else on_cancel

    def _conflict(self, src, dst):
        a = Ask()
        self.ask_conflict.emit(src, dst, a)
        return self._await(a, ("cancel", True))

    def run(self):
        """Vive enquanto o app viver: dorme em get(), acorda por job, e ao fim de
        cada um volta a dormir. Um único QThread para toda a sessão."""
        while True:
            job = self.q.get()                      # BLOQUEIA (sentinela None = sair)
            if job is None or self._shutdown:
                break
            self.cancel_ev.clear()                  # zera o cancelamento da rodada anterior
            sources, dest, sanitize = job
            self.job_started.emit(dest, self.q.pending())
            try:
                pf = fileops.preflight(sources, dest)
            except Exception as e:                  # varredura nunca derruba a GUI
                self.job_done.emit(None, f"{dest}\n{e}")
                self._maybe_idle()
                continue
            a = Ask()
            self.ask_preflight.emit(pf, a)          # a GUI decide seguir ou não
            go = self._await(a, None)
            if not go:
                self.job_done.emit(None, dest)
                self._maybe_idle()
                continue
            sanitize = bool(go.get("sanitize", sanitize))
            res = fileops.copy_to(sources, dest,
                                  on_progress=self.progress.emit,
                                  on_conflict=self._conflict,
                                  cancel=self.cancel_ev,
                                  sanitize_names=sanitize, plan=pf)
            self.job_done.emit(res, dest)
            self._maybe_idle()

    def _maybe_idle(self):
        """Fila vazia → avisa a GUI que pode esconder a barra. (Se um novo job já
        chegou, o próximo get() o pega sem esconder nada.)"""
        if self.q.pending() == 0:
            self.all_done.emit()


class ConflictDialog(QDialog):
    """Já existe um arquivo com esse nome no destino. NUNCA sobrescreve por
    padrão: Pular é o botão default, Sobrescrever é escolha explícita."""

    def __init__(self, parent, src, dst):
        super().__init__(parent)
        self.setWindowTitle(t("File already exists"))
        v = QVBoxLayout(self)
        v.addWidget(QLabel(t("“{name}” already exists in the destination.",
                             name=os.path.basename(dst))))
        form = QFormLayout()
        form.addRow(t("Source:"), QLabel(self._desc(src)))
        form.addRow(t("Destination:"), QLabel(self._desc(dst)))
        v.addLayout(form)
        self.ck_all = QCheckBox(t("Apply to all conflicts in this copy"))
        v.addWidget(self.ck_all)
        bb = QDialogButtonBox()
        b_skip = bb.addButton(t("Skip"), QDialogButtonBox.AcceptRole)
        b_ren = bb.addButton(t("Keep both"), QDialogButtonBox.AcceptRole)
        b_ovr = bb.addButton(t("Overwrite"), QDialogButtonBox.DestructiveRole)
        bb.addButton(t("Cancel copy"), QDialogButtonBox.RejectRole)
        b_skip.setDefault(True)
        self.answer = "skip"
        b_skip.clicked.connect(lambda: self._pick("skip"))
        b_ren.clicked.connect(lambda: self._pick("rename"))
        b_ovr.clicked.connect(lambda: self._pick("overwrite"))
        bb.rejected.connect(lambda: self._pick("cancel"))
        v.addWidget(bb)

    @staticmethod
    def _desc(p):
        try:
            st = os.stat(p)
            return "%s · %s" % (human_size(st.st_size),
                                time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)))
        except OSError:
            return "—"

    def _pick(self, ans):
        self.answer = ans
        self.accept()


class PreflightDialog(QDialog):
    """O que a cópia vai fazer, ANTES de escrever um byte.

    Esta tela é a razão de a checagem de destino existir: o destino típico é
    pendrive/HD externo/aparelho de mídia, quase sempre exFAT/FAT32/NTFS/MTP.
    Descobrir no arquivo 380 de 400 que o sistema de arquivos não aceita ':' no
    nome — ou que trava em 4 GiB — não é uma forma aceitável de aprender isso."""

    @staticmethod
    def reason_text(why):
        """Chave estável do fileops -> frase traduzida. Escrita como t() literal
        (e não como tabela de strings) para o teste de i18n enxergar as chaves."""
        return {
            "charset": t("invalid characters for this filesystem"),
            "length": t("name too long for this filesystem"),
            "reserved": t("reserved name on this filesystem"),
            "trailing": t("name ends in space or dot (dropped by this filesystem)"),
            "encoding": t("name is not valid UTF-8 (rejected by this filesystem)"),
        }.get(why, why)

    @staticmethod
    def probe_text(kind):
        """Motivo do bloqueio pela sonda de escrita (§1.1) -> frase traduzida.
        Só aparece quando a estratégia foi BLOCKED: a rota de gravação não existe."""
        return {
            "notsup": t("BLOCKED: this destination does not accept direct file "
                        "writing through its current mount. Connect the device "
                        "through the file manager (MTP) to copy here."),
            "perm": t("BLOCKED: no permission to write to this destination."),
            "readonly": t("BLOCKED: the destination is mounted read-only."),
            "nospace": t("BLOCKED: the destination reports no room for a test write."),
        }.get(kind, t("BLOCKED: a test write to the destination failed."))

    @staticmethod
    def strategy_note(pf):
        """Nota informativa (NÃO bloqueia) sobre COMO a cópia será feita: destino
        MTP vai pela transferência do sistema (gio, a rota do Nemo); destino de
        rede tem estimativa de espaço não-confiável."""
        if pf.strategy == fileops.STRAT_GIO:
            return t("MTP device: files are copied through the system transfer "
                     "service (gio), like the file manager does — progress "
                     "updates per file.")
        if pf.caps and getattr(pf.caps, "net", False):
            return t("Network destination: the free-space estimate may be "
                     "unreliable.")
        return ""

    def __init__(self, parent, pf):
        super().__init__(parent)
        self.setWindowTitle(t("Copy to…"))
        self.resize(620, 420)
        self.pf = pf
        v = QVBoxLayout(self)
        head = QLabel(t("{n} file(s), {size} → {dest}",
                        n=pf.total_files, size=human_size(pf.total_bytes),
                        dest=pf.dest_dir))
        head.setWordWrap(True)
        v.addWidget(head)
        # A velocidade do link explica sozinha a maior parte das cópias "lentas
        # demais": num USB 2.0 (11 MB/s reais) 20 GiB levam meia hora, e isso é o
        # cabo, não o programa. Melhor dizer antes do que ouvir depois.
        linha = t("Destination filesystem: {fs} · {free} free",
                  fs=pf.caps.label, free=human_size(pf.free_bytes))
        enlace = disks.link_label(pf.caps.link_mbits)
        if enlace:
            linha += " · " + enlace
        v.addWidget(QLabel(linha))
        nota = self.strategy_note(pf)
        if nota:
            lab_nota = QLabel(nota)
            lab_nota.setWordWrap(True)
            v.addWidget(lab_nota)

        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        warn = []
        if not pf.mount_ok:
            warn.append(t("BLOCKED: the destination mount point is not mounted. "
                          "Copying there would fill the system disk instead."))
        if pf.caps.readonly:
            warn.append(t("BLOCKED: the destination is mounted read-only."))
        if pf.strategy == fileops.STRAT_BLOCKED:
            warn.append(self.probe_text(pf.write_probe.kind if pf.write_probe else ""))
        if not pf.fits:
            warn.append(t("Not enough free space: needs {need}, has {free}.",
                          need=human_size(pf.total_bytes), free=human_size(pf.free_bytes)))
        if pf.too_big:
            warn.append(t("{n} file(s) exceed the {fs} size limit and will be SKIPPED:",
                          n=len(pf.too_big), fs=pf.caps.label))
            warn += ["    %s  (%s)" % (os.path.basename(p), human_size(s))
                     for p, s in pf.too_big[:20]]
        if pf.bad_names:
            warn.append(t("{n} name(s) are invalid on {fs}:", n=len(pf.bad_names),
                          fs=pf.caps.label))
            warn += ["    %s  — %s" % (os.path.basename(p), self.reason_text(why))
                     for p, why in pf.bad_names[:20]]
        if pf.links_degraded:
            warn.append(t("{n} symlink(s) will be copied as real files "
                          "({fs} has no symlinks).", n=len(pf.links_degraded),
                          fs=pf.caps.label))
        if pf.links_broken:
            warn.append(t("{n} broken symlink(s) cannot be copied to {fs} and "
                          "will be skipped.", n=len(pf.links_broken), fs=pf.caps.label))
        for src, err in pf.errors[:10]:
            warn.append("%s: %s" % (src, err))
        self.details.setPlainText("\n".join(warn) if warn else
                                  t("No problems found. Nothing in the source will be "
                                    "modified — this only creates copies."))
        v.addWidget(self.details, 1)

        self.ck_fix = QCheckBox(t("Adapt invalid names (replace illegal characters)"))
        self.ck_fix.setChecked(bool(pf.bad_names))
        self.ck_fix.setEnabled(bool(pf.bad_names))
        v.addWidget(self.ck_fix)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText(t("Copy"))
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        if pf.blocked:
            bb.button(QDialogButtonBox.Ok).setEnabled(False)
        v.addWidget(bb)


class _DirSizeWorker(QThread):
    """Soma o tamanho de uma pasta em thread, com contagem progressiva e cancel:
    somar uma pasta do acervo num SMR pode levar minutos e não pode travar a GUI."""
    tick = Signal(int, int)          # arquivos, bytes

    def __init__(self, path):
        super().__init__()
        self.path = path
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        files = total = 0
        last = 0.0
        for root, dirs, names in os.walk(self.path, onerror=lambda e: None):
            if self._stop:
                return
            for n in names:
                try:
                    total += os.lstat(os.path.join(root, n)).st_size
                    files += 1
                except OSError:
                    pass
            now = time.time()
            if now - last > 0.2:
                last = now
                self.tick.emit(files, total)
        self.tick.emit(files, total)


class PropertiesDialog(QDialog):
    """Inspeção SOMENTE LEITURA. Não há campo editável aqui por decisão de
    escopo: o LFS não altera o que encontrou."""

    def __init__(self, parent, m):
        super().__init__(parent)
        self.setWindowTitle(t("Properties"))
        self.resize(560, 360)
        self.path = m.path
        self._sizer = None
        v = QVBoxLayout(self)
        form = QFormLayout()
        form.addRow(t("Name:"), self._sel(os.path.basename(m.path)))
        form.addRow(t("Folder:"), self._sel(os.path.dirname(m.path)))
        form.addRow(t("Type:"), self._sel(xdg.mime_for(m.path)))
        self.lbl_size = self._sel("…")
        form.addRow(t("Size:"), self.lbl_size)
        try:
            st = os.lstat(m.path)
            form.addRow(t("Modified:"), self._sel(
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))))
            form.addRow(t("Accessed:"), self._sel(
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_atime))))
            form.addRow(t("Permissions:"), self._sel(oct(st.st_mode & 0o7777)[2:]))
            form.addRow(t("Owner:"), self._sel("%d:%d" % (st.st_uid, st.st_gid)))
            if os.path.islink(m.path):
                form.addRow(t("Symlink to:"), self._sel(os.readlink(m.path)))
            if os.path.isdir(m.path):
                self._sizer = _DirSizeWorker(m.path)
                self._sizer.tick.connect(self._on_size)
                self._sizer.start()
            else:
                self.lbl_size.setText(human_size(st.st_size))
        except OSError as e:
            form.addRow(t("Error:"), self._sel(humane.human_error(e)))
        fs = disks.dest_caps(m.path)
        texto_fs = "%s (%s)" % (fs.label, fs.fstype or "?")
        if disks.link_label(fs.link_mbits):
            texto_fs += " · " + disks.link_label(fs.link_mbits)
        form.addRow(t("Filesystem:"), self._sel(texto_fs))
        v.addLayout(form)

        h = QHBoxLayout()
        self.lbl_sum = QLabel("")
        self.lbl_sum.setTextInteractionFlags(Qt.TextSelectableByMouse)
        btn = QPushButton(t("Compute checksum"))
        btn.clicked.connect(self._checksum)      # nunca automático: lê o arquivo inteiro
        h.addWidget(btn); h.addWidget(self.lbl_sum, 1)
        v.addLayout(h)

        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        v.addWidget(bb)

    @staticmethod
    def _sel(txt):
        lab = QLabel(txt)
        lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lab.setWordWrap(True)
        return lab

    def _on_size(self, files, total):
        self.lbl_size.setText(t("{size}  ({n} file(s))", size=human_size(total), n=files))

    def _checksum(self):
        import hashlib
        h = hashlib.blake2b()
        try:
            with open(self.path, "rb") as f:
                while True:
                    b = f.read(1 << 20)
                    if not b:
                        break
                    h.update(b)
            self.lbl_sum.setText("BLAKE2b " + h.hexdigest()[:32] + "…")
        except OSError as e:
            self.lbl_sum.setText(humane.human_error(e))

    def closeEvent(self, ev):
        if self._sizer is not None and self._sizer.isRunning():
            self._sizer.stop()
            self._sizer.wait(2000)
        super().closeEvent(ev)


# ----------------------------------------------------------------- janela
class SearchTab(QWidget):
    """Uma aba = uma busca INDEPENDENTE. Tabela, modelo e worker são dela; o
    formulário e o preview continuam sendo da janela, um só — é assim que o
    Agent Ransack se comporta, e duplicar o player de mídia por aba seria
    absurdo (dois vídeos tocando ao mesmo tempo).

    Guardar o SNAPSHOT do formulário na aba é o que faz a troca de aba fazer
    sentido: voltar para a aba 1 devolve exatamente o formulário que produziu
    aqueles resultados, e não o que está digitado agora.
    """

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.worker: SearchWorker | None = None
        self.pending: tuple | None = None      # (Query, boolexpr) esperando a vez (SMR)
        self.serial = False                    # varre disco rotacional? (gate do SMR)
        self.t0 = 0.0
        self.mode_tag = ""
        self.phase_txt = ""
        self.hl_terms: list[str] = []
        self.hl_cs = False
        self.status_text = t("Ready.")
        self.form: dict = {}
        self.model = ResultModel()
        self.proxy = ResultFilterProxy()              # B14 ordenação + F10a #1 filtro
        self.proxy.setSourceModel(self.model)
        self.table = QTableView(); self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setSortingEnabled(False)           # ligado só ao fim da busca
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        # F7: arrastar resultados para FORA (Nemo, desktop, e-mail). DragOnly:
        # a tabela não aceita drop — quem recebe pasta arrastada é a janela.
        self.table.setDragEnabled(True)
        self.table.setDragDropMode(QAbstractItemView.DragOnly)
        self.table.setDefaultDropAction(Qt.CopyAction)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
        hh = self.table.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.table.selectionModel().currentRowChanged.connect(win.on_select)
        self.table.doubleClicked.connect(win.open_file)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(win.context_menu)

        # ---- F10a #1: caixa de filtro DENTRO dos resultados (triagem grátis) ----
        # Busca cara uma vez; refinar aqui não toca disco. Fica acima da tabela.
        filt = QFrame(); filt.setObjectName("filterbar")
        fl = QHBoxLayout(filt); fl.setContentsMargins(10, 6, 10, 6); fl.setSpacing(8)
        lbl_f = QLabel(t("Filter")); lbl_f.setObjectName("section")
        self.ed_filter = QLineEdit(); self.ed_filter.setObjectName("filterfield")
        self.ed_filter.setClearButtonEnabled(True)
        self.ed_filter.setPlaceholderText(
            t("narrow these results — *.odt  ·  >2019-01  ·  space = AND   (Ctrl+F)"))
        self.ed_filter.setToolTip(t(
            "Filters the results already found — never touches the disk.\n"
            "substring matches name or path · *.odt filters extension ·\n"
            ">2019-01 / <2020-01 filter the date · a space means AND."))
        self.ed_filter.textChanged.connect(self._on_filter_changed)
        self.lbl_filter = QLabel(""); self.lbl_filter.setObjectName("section")
        fl.addWidget(lbl_f); fl.addWidget(self.ed_filter, 1); fl.addWidget(self.lbl_filter)
        self.filter_bar = filt

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(self.filter_bar)
        lay.addWidget(self.table)
        # o contador vivo "3.000 → 214" segue o modelo enchendo e o filtro mudando
        self.model.rowsInserted.connect(self._update_filter_count)
        self.model.modelReset.connect(self._update_filter_count)
        self.proxy.rowsInserted.connect(self._update_filter_count)
        self.proxy.rowsRemoved.connect(self._update_filter_count)
        self._update_filter_count()

    def _on_filter_changed(self, text):
        self.proxy.set_filter_text(text)
        self._update_filter_count()

    def _update_filter_count(self, *a):
        total = self.model.rowCount()
        shown = self.proxy.rowCount()
        if self.ed_filter.text().strip() and total:
            self.lbl_filter.setText(t("{shown} of {total}",
                                      shown=_grp(shown), total=_grp(total)))
        else:
            self.lbl_filter.setText("")

    @property
    def searching(self) -> bool:
        return bool(self.worker and self.worker.isRunning())

    def stop(self):
        """Cancela e ESPERA. Destruir um QThread vivo aborta o processo (mesma
        disciplina do closeEvent) — e fechar a aba leva o worker junto."""
        self.pending = None
        if self.worker is not None:
            if self.worker.isRunning():
                self.worker.cancel()
                deadline = time.time() + 8.0
                while self.worker.isRunning() and time.time() < deadline:
                    if self.worker.wait(100):
                        break
                    QApplication.processEvents()
                if self.worker.isRunning():
                    self.worker.terminate(); self.worker.wait(2000)
            self.worker = None


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        # O título carrega a BUILD: o app instalado é uma cópia dos fontes, então
        # commitar não muda o que o usuário roda. Ver version.py.
        self.setWindowTitle("Sombrero File Search" + version.title_suffix())
        # cabe SEMPRE na tela (monitor em retrato tem só ~1080 de largura útil);
        # janela maior que a tela perde o botão de maximizar no Muffin/Cinnamon
        scr = QGuiApplication.primaryScreen().availableGeometry()
        self.resize(min(1160, scr.width() - 24), min(760, scr.height() - 48))
        ico = os.path.join(ASSETS, "icon_256.png")
        if os.path.exists(ico):
            self.setWindowIcon(QIcon(ico))
        self._tick = QTimer(self)                 # B8: pulso de status a cada 0,5 s
        self._tick.setInterval(500)
        self._tick.timeout.connect(self._heartbeat)
        self.cfg = load_cfg()
        self.copy_q = CopyQueue()                 # F7: fila de cópia (um worker)
        # A6: worker persistente — criado UMA vez, vive toda a sessão, dorme na
        # fila entre jobs. Fim das QThreads recriadas por arrasto (e da corrida).
        self.copier = CopyWorker(self.copy_q)
        self.copier.ask_preflight.connect(self.on_ask_preflight)
        self.copier.ask_conflict.connect(self.on_ask_conflict)
        self.copier.progress.connect(self.on_copy_progress)
        self.copier.job_started.connect(self.on_copy_started)
        self.copier.job_done.connect(self.on_copy_done)
        self.copier.all_done.connect(self.on_copy_all_done)
        self.copier.start()
        self.muted = bool(self.cfg.get("muted", True))   # B13: mídia começa muda
        self.theme = self.cfg.get("theme", "dark")
        if self.theme not in THEMES:
            self.theme = "dark"
        self._build()
        self.apply_theme(self.theme)
        QShortcut(QKeySequence(Qt.Key_Escape), self, self.cancel_search)
        QShortcut(QKeySequence("Ctrl+L"), self, lambda: self.ed_name.setFocus())
        QShortcut(QKeySequence("Ctrl+T"), self, self.toggle_theme)
        # F7 — atalhos de resultado. Ficam na JANELA, não na tabela: com abas a
        # tabela troca debaixo do atalho, e um QShortcut preso à tabela da aba 1
        # agiria na aba errada (ou morreria com ela).
        QShortcut(QKeySequence.Copy, self, self.copy_selection)
        QShortcut(QKeySequence("Ctrl+Shift+C"), self, self.copy_paths)
        QShortcut(QKeySequence("Alt+Return"), self, self.properties)
        # F5 — conforto: abas, repetir, exportar, salvar.
        QShortcut(QKeySequence("Ctrl+N"), self, lambda: self.new_tab(focus=True))
        QShortcut(QKeySequence("Ctrl+W"), self, self.close_current_tab)
        QShortcut(QKeySequence("Ctrl+Return"), self, lambda: self.start_search(True))
        QShortcut(QKeySequence(Qt.Key_F3), self, self.repeat_last)
        QShortcut(QKeySequence("Ctrl+E"), self, self.export_results)
        QShortcut(QKeySequence("Ctrl+S"), self, self.save_current_search)
        self.setAcceptDrops(True)                 # soltar pasta = "procure aqui"
        self.ed_name.setFocus()                   # digitar e Enter, sem clique

    # ---- UI
    def _build(self):
        central = QWidget(); central.setObjectName("central"); self.setCentralWidget(central)
        root = QVBoxLayout(central); root.setContentsMargins(14, 12, 14, 12); root.setSpacing(10)

        # ---------- header ----------
        header = QFrame(); header.setObjectName("header")
        hl = QHBoxLayout(header); hl.setContentsMargins(14, 10, 14, 10); hl.setSpacing(12)
        logo = QLabel()
        pm = os.path.join(ASSETS, "icon_64.png")
        if os.path.exists(pm):
            logo.setPixmap(QPixmap(pm).scaled(40, 40, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        hl.addWidget(logo)
        tt = QVBoxLayout(); tt.setSpacing(0)
        ttl = QLabel("Sombrero File Search"); ttl.setObjectName("title")
        s = QLabel(t("broad file search — name · content · boolean · documents"))
        s.setObjectName("subtitle")
        tt.addWidget(ttl); tt.addWidget(s); hl.addLayout(tt); hl.addStretch(1)
        self.badges = QHBoxLayout(); self.badges.setSpacing(10)
        hl.addLayout(self.badges)
        hl.addSpacing(10)
        self.btn_theme = QToolButton(); self.btn_theme.setCursor(Qt.PointingHandCursor)
        self.btn_theme.setToolTip(t("Toggle light/dark theme (Ctrl+T)"))
        self.btn_theme.clicked.connect(self.toggle_theme)
        hl.addWidget(self.btn_theme)
        root.addWidget(header)

        # ---------- barra PRINCIPAL: nome do arquivo ----------
        # Achar arquivo é o caso primário (modelo Agent Ransack): o campo grande
        # ao lado do Buscar é o NOME. Texto puro = "contém" (rotina acha
        # "exames de rotina.txt"); globs (* ? [) valem literais.
        r1 = QHBoxLayout(); r1.setSpacing(8)
        self.ed_name = QLineEdit(); self.ed_name.setObjectName("primaryfield")
        self.ed_name.setClearButtonEnabled(True)
        self.ed_name.setPlaceholderText(
            t("File name — e.g. report  ·  *.pdf  ·  exams*.txt   (empty = all)"))
        self.ed_name.setToolTip(t(
            "Search by NAME. Plain text means “contains”: report finds\n"
            "“routine exams.txt” in any extension. Multiple terms separated\n"
            "by comma (OR). Hand-typed globs (* ? [) are honored as typed."))
        self.ed_name.returnPressed.connect(self.start_search)
        self.btn_search = QPushButton(t("  Search  ")); self.btn_search.setObjectName("primary")
        self.btn_search.setDefault(True); self.btn_search.clicked.connect(self.start_search)
        self.btn_cancel = QPushButton(t("Cancel")); self.btn_cancel.clicked.connect(self.cancel_search)
        self.btn_cancel.setEnabled(False)
        r1.addWidget(self.ed_name, 1); r1.addWidget(self.btn_search); r1.addWidget(self.btn_cancel)
        root.addLayout(r1)

        # ---------- linha secundária (opcional): conteúdo + pasta ----------
        r2 = QHBoxLayout(); r2.setSpacing(8)
        lbl_c = QLabel(t("Content")); lbl_c.setObjectName("section")
        self.ed_content = QLineEdit(); self.ed_content.setObjectName("content")
        self.ed_content.setClearButtonEnabled(True)
        self.ed_content.setPlaceholderText(
            t("optional — text the file must contain (boolean: toggle the chip)"))
        self.ed_content.returnPressed.connect(self.start_search)
        lbl_e = QLabel(t("In")); lbl_e.setObjectName("section")
        self.ed_path = QLineEdit(os.path.expanduser("~"))
        self.ed_path.setPlaceholderText(t("Folder(s)/mounts — separate with ';'"))
        self.ed_path.setToolTip(t("Multiple starting points separated by ';' —\n"
                                  "e.g. ~/Documents;/mnt/archive;/media/backup"))
        self.ed_path.returnPressed.connect(self.start_search)
        btn_browse = QToolButton(); btn_browse.setText(t("Browse…")); btn_browse.clicked.connect(self.browse)
        # multidiscos como OPÇÃO visível: marca/desmarca discos montados sem
        # o usuário precisar conhecer a sintaxe do ';'
        self.btn_disks = QToolButton(); self.btn_disks.setText(t("Disks ▾"))
        self.btn_disks.setToolTip(t("Multi-disk search: add/remove mounted disks\n"
                                    "(/mnt, /media, /run/media) from the 'In' folder list."))
        self.btn_disks.setPopupMode(QToolButton.InstantPopup)
        self.mnu_disks = QMenu(self)
        self.mnu_disks.setToolTipsVisible(True)          # mostra o mountpoint no hover
        self.mnu_disks.aboutToShow.connect(self._fill_disks_menu)
        self.btn_disks.setMenu(self.mnu_disks)
        r2.addWidget(lbl_c); r2.addWidget(self.ed_content, 3)
        r2.addSpacing(6)
        r2.addWidget(lbl_e); r2.addWidget(self.ed_path, 2)
        # F5: buscas salvas + histórico. Um botão só, porque as duas coisas
        # respondem à mesma pergunta ("quero aquela busca de novo") e separá-las
        # obrigaria o usuário a lembrar se salvou ou não.
        self.btn_saved = QToolButton(); self.btn_saved.setText(t("Searches ▾"))
        self.btn_saved.setToolTip(t("Saved searches and history — the whole form,\n"
                                    "not just the term (Ctrl+S saves the current one)."))
        self.btn_saved.setPopupMode(QToolButton.InstantPopup)
        self.mnu_saved = QMenu(self)
        self.mnu_saved.aboutToShow.connect(self._fill_saved_menu)
        self.btn_saved.setMenu(self.mnu_saved)
        r2.addWidget(self.btn_disks); r2.addWidget(self.btn_saved); r2.addWidget(btn_browse)
        root.addLayout(r2)

        # ---------- chips de opção (FlowLayout: quebra linha em janela estreita) ----------
        bar = QFrame(); bar.setObjectName("toolbar")
        r3 = FlowLayout(bar); r3.setContentsMargins(12, 8, 12, 8)
        self.ck_case = QCheckBox("Aa"); self.ck_case.setToolTip(t("Case sensitive"))
        self.ck_word = QCheckBox(t("word")); self.ck_word.setToolTip(t("Whole word"))
        self.ck_bool = QCheckBox(t("boolean")); self.ck_bool.setToolTip(t(
            "Reads the Content field as an expression: (A OR B) AND C NOT D\n"
            "Also accepts | & !  and \"quotes\" for phrases. Precedence NOT>AND>OR."))
        self.ck_bool.toggled.connect(self._on_bool_toggled)
        self.ck_doc = QCheckBox(t("documents"))
        self.ck_doc.toggled.connect(self._on_doc_toggled)      # B6
        if engine.RGA:
            self.ck_doc.setToolTip(t("Searches INSIDE PDF/docx/epub/odt/zip… (ripgrep-all)."))
        else:
            self.ck_doc.setEnabled(False)
            self.ck_doc.setToolTip(t("Requires 'ripgrep-all' (rga) — run the installer."))
        self.ck_crx = QCheckBox(t("content regex"))
        self.ck_nrx = QCheckBox(t("name regex"))
        self.ck_rec = QCheckBox(t("subfolders")); self.ck_rec.setChecked(True)
        self.ck_hid = QCheckBox(t("hidden"))
        self.ck_git = QCheckBox(".gitignore"); self.ck_git.setToolTip(t("Respect .gitignore rules"))
        self.ck_ofs = QCheckBox(t("1 disk")); self.ck_ofs.setToolTip(t(
            "--one-file-system: don't cross into other mount points"))
        for w in (self.ck_case, self.ck_word, self.ck_bool, self.ck_doc, self.ck_crx,
                  self.ck_nrx, self.ck_rec, self.ck_hid, self.ck_git, self.ck_ofs):
            r3.addWidget(w)
        # rótulo+campo num mini-widget: a quebra de linha não pode separá-los
        def _pair(label, field):
            box = QWidget(); h = QHBoxLayout(box)
            h.setContentsMargins(0, 0, 0, 0); h.setSpacing(5)
            lab = QLabel(label); lab.setObjectName("section")
            h.addWidget(lab); h.addWidget(field)
            return box
        self.ed_minsz = QLineEdit(); self.ed_minsz.setFixedWidth(66)
        self.ed_minsz.setPlaceholderText("10M")
        r3.addWidget(_pair(t("Size ≥"), self.ed_minsz))
        self.sp_days = QSpinBox(); self.sp_days.setRange(0, 3650)
        self.sp_days.setSpecialValueText("—"); self.sp_days.setSuffix(" d")
        self.sp_days.setFixedWidth(70)
        r3.addWidget(_pair(t("Last"), self.sp_days))
        root.addWidget(bar)

        # ---------- resultados / preview ----------
        split = QSplitter(Qt.Vertical)
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        mais = QToolButton(); mais.setText("  +  ")
        mais.setToolTip(t("New search tab (Ctrl+N)"))
        mais.setCursor(Qt.PointingHandCursor)
        mais.clicked.connect(lambda: self.new_tab(focus=True))
        self.tabs.setCornerWidget(mais, Qt.TopRightCorner)
        self.new_tab()
        split.addWidget(self.tabs)

        split.addWidget(self._build_preview())
        split.setStretchFactor(0, 3); split.setStretchFactor(1, 2)
        split.setSizes([470, 240])
        root.addWidget(split, 1)

        # ---------- fila de cópia (visível só quando há operação) ----------
        self.copy_bar = QFrame(); self.copy_bar.setObjectName("toolbar")
        cb = QHBoxLayout(self.copy_bar); cb.setContentsMargins(12, 7, 12, 7); cb.setSpacing(9)
        self.lbl_copy = QLabel(""); self.lbl_copy.setObjectName("section")
        self.pb_copy = QProgressBar(); self.pb_copy.setTextVisible(False)
        self.pb_copy.setFixedHeight(8)
        self.lbl_copy_rate = QLabel(""); self.lbl_copy_rate.setObjectName("subtitle")
        self.btn_copy_cancel = QPushButton(t("Cancel"))
        self.btn_copy_cancel.clicked.connect(self.cancel_copy)
        cb.addWidget(self.lbl_copy, 2); cb.addWidget(self.pb_copy, 3)
        cb.addWidget(self.lbl_copy_rate); cb.addWidget(self.btn_copy_cancel)
        self.copy_bar.setVisible(False)
        root.addWidget(self.copy_bar)

        # ---------- status ----------
        self.status = QLabel(t("Ready."))
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self.status)   # estilo aplicado em apply_theme()

    # ---- preview (texto ↔ mídia)
    def _build_preview(self) -> QWidget:
        self.pv_stack = QStackedWidget()

        # página 0: trecho de texto
        self.preview = QPlainTextEdit(); self.preview.setReadOnly(True)
        self.preview.setLineWrapMode(QPlainTextEdit.NoWrap)
        f = QFont("monospace"); f.setStyleHint(QFont.Monospace); self.preview.setFont(f)
        self.preview.setPlaceholderText(t("Select a result to see the snippet…"))
        self.pv_stack.addWidget(self.preview)

        # página 1: mídia (imagem / vídeo / áudio) + transporte
        panel = QWidget()
        pv = QVBoxLayout(panel); pv.setContentsMargins(0, 0, 0, 0); pv.setSpacing(8)

        stage = QFrame(); stage.setObjectName("mediastage")
        sv = QVBoxLayout(stage); sv.setContentsMargins(6, 6, 6, 6)
        self.media_view = QStackedWidget()
        self.img_label = QLabel(alignment=Qt.AlignCenter)
        self.img_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.img_label.setMinimumSize(1, 1)
        self.media_view.addWidget(self.img_label)                    # 0 imagem
        self.audio_ph = QLabel("♪", alignment=Qt.AlignCenter)
        self.audio_ph.setObjectName("mediahint")
        self.media_view.addWidget(self.audio_ph)                     # 1 áudio
        if HAS_MEDIA:
            self.video_widget = QVideoWidget()
            self.media_view.addWidget(self.video_widget)             # 2 vídeo
            self.player = QMediaPlayer()
            self.audio_out = QAudioOutput()
            self.player.setAudioOutput(self.audio_out)
            self.player.setVideoOutput(self.video_widget)
            self.player.playbackStateChanged.connect(self._on_play_state)
            self.player.positionChanged.connect(self._on_position)
            self.player.durationChanged.connect(self._on_duration)
            self.player.mediaStatusChanged.connect(self._on_media_status)
        else:
            self.player = None
            self.audio_out = None
        sv.addWidget(self.media_view)
        pv.addWidget(stage, 1)

        # barra de transporte
        bar = QFrame(); bar.setObjectName("mediabar")
        bl = QHBoxLayout(bar); bl.setContentsMargins(10, 7, 10, 7); bl.setSpacing(9)
        self.btn_prev = QToolButton(); self.btn_prev.setObjectName("transport")
        self.btn_prev.setText("⏮"); self.btn_prev.setToolTip(t("Previous media"))
        self.btn_prev.clicked.connect(lambda: self._nav_media(-1))
        self.btn_play = QToolButton(); self.btn_play.setObjectName("play")
        self.btn_play.setText("▶"); self.btn_play.setToolTip(t("Play / pause"))
        self.btn_play.clicked.connect(self._toggle_play)
        self.btn_next = QToolButton(); self.btn_next.setObjectName("transport")
        self.btn_next.setText("⏭"); self.btn_next.setToolTip(t("Next media"))
        self.btn_next.clicked.connect(lambda: self._nav_media(1))
        self.btn_vol = QToolButton(); self.btn_vol.setObjectName("transport")  # B13
        self.btn_vol.setText("🔇" if self.muted else "🔊")
        self.btn_vol.setToolTip(t("Muted (default for privacy) — click to enable sound"))
        self.btn_vol.clicked.connect(self._toggle_mute)
        self.lbl_media = QLabel(""); self.lbl_media.setObjectName("medianame")
        self.lbl_media.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.sld_pos = QSlider(Qt.Horizontal)
        self.sld_pos.setRange(0, 0)
        self.sld_pos.sliderMoved.connect(self._seek)
        self.sld_pos.sliderPressed.connect(lambda: setattr(self, "_scrubbing", True))
        self.sld_pos.sliderReleased.connect(self._seek_release)
        self.lbl_time = QLabel("0:00 / 0:00"); self.lbl_time.setObjectName("mediatime")
        self._scrubbing = False
        for w in (self.btn_prev, self.btn_play, self.btn_next):
            bl.addWidget(w)
        bl.addWidget(self.lbl_media)
        bl.addWidget(self.sld_pos, 1)
        bl.addWidget(self.lbl_time)
        bl.addWidget(self.btn_vol)
        pv.addWidget(bar)
        self.media_bar = bar

        self.pv_stack.addWidget(panel)
        return self.pv_stack

    # ---- tema
    def apply_theme(self, name: str):
        self.theme = name if name in THEMES else "dark"
        pal = THEMES[self.theme]
        self.setStyleSheet(build_style(pal))
        self.status.setStyleSheet(f"color:{pal['muted']}; padding:2px 4px;")
        self.btn_theme.setText(t("☀  Light") if self.theme == "dark" else t("☾  Dark"))
        self._refresh_badges(pal)

    def toggle_theme(self):
        self.apply_theme("light" if self.theme == "dark" else "dark")
        self.cfg["theme"] = self.theme
        save_cfg(self.cfg)

    def _refresh_badges(self, pal: dict):
        while self.badges.count():
            it = self.badges.takeAt(0)
            w = it.widget()
            if w:
                w.setParent(None); w.deleteLater()
        info = engine.engine_info()
        for nm, key in (("ripgrep", "ripgrep"), ("fd", "fd"), ("rga", "rga")):
            self.badges.addWidget(_badge(nm, not info[key].startswith("("), pal))

    # ---- ações
    def _on_bool_toggled(self, on):
        if on:
            self.ck_crx.setChecked(False); self.ck_crx.setEnabled(False)
            self.ed_content.setPlaceholderText(
                t("Boolean expression:   (note OR report) AND patient NOT draft"))
            if self.ck_doc.isChecked():           # B6: não combinam
                self.ck_doc.setChecked(False)
        else:
            self.ck_crx.setEnabled(True)
            self.ed_content.setPlaceholderText(
                t("Content to contain (text or regex)…   — empty = search by name only"))
        # B6: booleano ainda não busca dentro de documentos — um desabilita o outro
        self.ck_doc.setEnabled(not on and bool(engine.RGA))

    def _on_doc_toggled(self, on):
        if on and self.ck_bool.isChecked():       # B6
            self.ck_bool.setChecked(False)
        self.ck_bool.setEnabled(not on)

    def browse(self):
        d = QFileDialog.getExistingDirectory(self, t("Choose the folder"),
                                             self.ed_path.text().split(";")[0] or os.path.expanduser("~"))
        if d:
            cur = self.ed_path.text().strip()
            self.ed_path.setText(f"{cur};{d}" if cur else d)

    # ---- multidiscos ("Discos ▾")
    def _paths_list(self):
        return [p.strip() for p in self.ed_path.text().split(";") if p.strip()]

    def _fill_disks_menu(self):
        """Monta o menu na hora de abrir: home + discos montados AGORA (pen drive
        plugado depois da janela aberta aparece). Marcado = já está no 'Em'."""
        self.mnu_disks.clear()
        cur = set(self._paths_list())
        home = os.path.expanduser("~")
        mounts = engine.user_mounts()
        # "All disks": marca/desmarca TODOS os discos montados de uma vez
        if mounts:
            all_on = all(mp in cur for mp in mounts)
            aa = self.mnu_disks.addAction(t("All disks"))
            aa.setCheckable(True); aa.setChecked(all_on)
            aa.toggled.connect(self._toggle_all_disks)
            self.mnu_disks.addSeparator()
        for mp in [home] + mounts:
            if mp == home:
                label, tip = t("Home folder (~)"), home
            else:
                # preferência: nome do volume (label) ao mountpoint cru — o
                # mountpoint fica no tooltip, ainda descobrível.
                vol = disks.volume_label(mp)
                label, tip = (vol or mp), mp
            a = self.mnu_disks.addAction(label)
            a.setToolTip(tip)
            a.setCheckable(True); a.setChecked(mp in cur)
            a.toggled.connect(lambda on, mp=mp: self._toggle_path(mp, on))
        if not mounts:
            a = self.mnu_disks.addAction(t("(no external disk mounted)"))
            a.setEnabled(False)

    def _toggle_path(self, mp, on):
        cur = self._paths_list()
        if on and mp not in cur:
            cur.append(mp)
        elif not on and mp in cur:
            cur.remove(mp)
        self.ed_path.setText(";".join(cur))

    def _toggle_all_disks(self, on):
        """Marca/desmarca todos os discos montados sem tocar em outras pastas
        que o usuário tenha digitado à mão (ex.: home ou uma subpasta)."""
        mounts = engine.user_mounts()
        mset = set(mounts)
        cur = [p for p in self._paths_list() if p not in mset]   # preserva o resto
        if on:
            cur += mounts
        self.ed_path.setText(";".join(cur))

    # ---- F5: abas (a aba corrente É o "self.table/model/proxy" de antes)
    @property
    def tab(self) -> SearchTab:
        return self.tabs.currentWidget()

    @property
    def table(self):
        return self.tab.table

    @property
    def model(self):
        return self.tab.model

    @property
    def proxy(self):
        return self.tab.proxy

    @property
    def worker(self):
        return self.tab.worker

    def _all_tabs(self):
        return [self.tabs.widget(i) for i in range(self.tabs.count())]

    def new_tab(self, focus=False) -> SearchTab:
        tab = SearchTab(self)
        tab.form = self.form_state()
        i = self.tabs.addTab(tab, t("New search"))
        if focus:
            self.tabs.setCurrentIndex(i)
            self.ed_name.setFocus(); self.ed_name.selectAll()
        return tab

    def close_tab(self, i):
        """Fechar a ÚLTIMA aba não fecha o programa: ela é esvaziada. Uma janela
        de busca sem nenhuma aba não teria como voltar a ter uma."""
        tab = self.tabs.widget(i)
        if tab is None:
            return
        tab.stop()
        if self.tabs.count() == 1:
            tab.model.clear()
            tab.status_text = t("Ready.")
            self.tabs.setTabText(0, t("New search"))
            self._stop_media(); self.preview.clear(); self.pv_stack.setCurrentIndex(0)
            self._on_tab_changed(0)
            return
        self.tabs.removeTab(i)
        tab.deleteLater()

    def close_current_tab(self):
        self.close_tab(self.tabs.currentIndex())

    def _on_tab_changed(self, i):
        """Trocar de aba devolve o FORMULÁRIO daquela busca, não o que está
        digitado — senão o usuário olha resultados de uma busca com o formulário
        de outra na frente, que é a pior mentira possível numa GUI de busca."""
        if i < 0 or not hasattr(self, "status"):
            return
        tab = self.tabs.widget(i)
        if tab is None:
            return
        self.apply_form(tab.form)
        self.status.setText(tab.status_text)
        self.btn_search.setEnabled(not tab.searching)
        self.btn_cancel.setEnabled(tab.searching or bool(tab.pending))
        self._hl_terms, self._hl_cs = tab.hl_terms, tab.hl_cs
        self.on_select(tab.table.currentIndex(), QModelIndex())

    def _set_status(self, tab, txt):
        """Status é por aba: uma busca terminando no fundo não pode reescrever o
        que a aba visível está mostrando."""
        tab.status_text = txt
        if tab is self.tab:
            self.status.setText(txt)

    # ---- F5: snapshot do formulário (o que uma busca salva REALMENTE é)
    def form_state(self) -> dict:
        return searches.normalize({
            "name": self.ed_name.text(), "content": self.ed_content.text(),
            "paths": self.ed_path.text(),
            "name_regex": self.ck_nrx.isChecked(), "content_regex": self.ck_crx.isChecked(),
            "boolean": self.ck_bool.isChecked(), "documents": self.ck_doc.isChecked(),
            "case": self.ck_case.isChecked(), "word": self.ck_word.isChecked(),
            "recursive": self.ck_rec.isChecked(), "hidden": self.ck_hid.isChecked(),
            "gitignore": self.ck_git.isChecked(), "one_fs": self.ck_ofs.isChecked(),
            "min_size": self.ed_minsz.text(), "days": self.sp_days.value(),
        })

    def apply_form(self, form: dict):
        f = searches.normalize(form or {})
        self.ed_name.setText(f["name"]); self.ed_content.setText(f["content"])
        if f["paths"]:                       # aba nova nasce sem pastas: não apaga o campo
            self.ed_path.setText(f["paths"])
        self.ck_nrx.setChecked(f["name_regex"]); self.ck_crx.setChecked(f["content_regex"])
        self.ck_bool.setChecked(f["boolean"]); self.ck_doc.setChecked(f["documents"])
        self.ck_case.setChecked(f["case"]); self.ck_word.setChecked(f["word"])
        self.ck_rec.setChecked(f["recursive"]); self.ck_hid.setChecked(f["hidden"])
        self.ck_git.setChecked(f["gitignore"]); self.ck_ofs.setChecked(f["one_fs"])
        self.ed_minsz.setText(f["min_size"]); self.sp_days.setValue(f["days"])

    # ---- F5: buscas salvas + histórico
    def _fill_saved_menu(self):
        m = self.mnu_saved
        m.clear()
        m.addAction(t("Save current search…  (Ctrl+S)"), self.save_current_search)
        m.addAction(t("Export results…  (Ctrl+E)"), self.export_results)
        salvas = searches.saved_list(self.cfg)
        if salvas:
            m.addSeparator()
            for nome, form in salvas:
                a = m.addAction("★  " + nome)
                a.triggered.connect(lambda _=False, f=form: self._run_form(f))
            rem = m.addMenu(t("Remove saved…"))
            for nome, _f in salvas:
                rem.addAction(nome, lambda n=nome: self._forget(n))
        hist = self.cfg.get("history", [])
        if hist:
            m.addSeparator()
            cab = m.addAction(t("Recent")); cab.setEnabled(False)
            for form in hist[:12]:
                a = m.addAction("   " + self._form_label(form))
                a.triggered.connect(lambda _=False, f=form: self._run_form(f))
            m.addSeparator()
            m.addAction(t("Clear history"), self._clear_history)

    @staticmethod
    def _form_label(form) -> str:
        f = searches.normalize(form)
        partes = []
        if f["name"]:
            partes.append(f["name"])
        if f["content"]:
            partes.append("“%s”" % f["content"])
        rot = " · ".join(partes) or searches.title_for(f, 40)
        alvo = [x for x in f["paths"].split(";") if x.strip()]
        if alvo:
            rot += "   →  " + (os.path.basename(alvo[0].rstrip("/")) or alvo[0])
            if len(alvo) > 1:
                rot += " +%d" % (len(alvo) - 1)
        return rot if len(rot) <= 64 else rot[:63] + "…"

    def _run_form(self, form):
        """Abrir uma busca salva NÃO substitui a aba atual: abre outra. Quem
        guardou uma busca quer comparar com o que já está na tela."""
        self.new_tab(focus=True)
        self.apply_form(form)
        self.start_search()

    def save_current_search(self):
        f = self.form_state()
        nome, ok = QInputDialog.getText(self, t("Save search"), t("Name for this search:"),
                                        text=searches.title_for(f, 40))
        if not ok or not nome.strip():
            return
        searches.save_search(self.cfg, nome, f)
        save_cfg(self.cfg)
        self.status.setText(t("Search saved as “{name}”.", name=nome.strip()))

    def _forget(self, nome):
        searches.delete_search(self.cfg, nome)
        save_cfg(self.cfg)

    def _clear_history(self):
        self.cfg["history"] = []
        save_cfg(self.cfg)

    def repeat_last(self):
        """F3: repetir. Se a aba já tem uma busca, repete ESSA (o gesto clássico
        de F3 é "de novo"); aba virgem cai na última do histórico."""
        f = self.tab.form or (self.cfg.get("history") or [None])[0]
        if not f:
            return
        self.apply_form(f)
        self.start_search()

    def export_results(self):
        """Exporta o que a aba corrente achou, na ORDEM QUE ESTÁ NA TELA — se o
        usuário ordenou por tamanho, o CSV sai ordenado por tamanho."""
        tab = self.tab
        if not tab.model.rows:
            self.status.setText(t("Nothing to export — the result list is empty."))
            return
        base = searches.title_for(tab.form, 40).replace("/", "_").strip() or "results"
        alvo, _ = QFileDialog.getSaveFileName(
            self, t("Export results"), os.path.join(os.path.expanduser("~"), base + ".csv"),
            t("CSV (*.csv);;JSON (*.json)"))
        if not alvo:
            return
        if not os.path.splitext(alvo)[1]:
            alvo += ".csv"
        linhas = [tab.model.match_at(tab.proxy.mapToSource(tab.proxy.index(r, 0)).row())
                  for r in range(tab.proxy.rowCount())]
        linhas = [m for m in linhas if m is not None]
        try:
            n = searches.export(linhas, alvo)
        except OSError as e:
            self.status.setText(t("⚠  Could not write {path}: {err}", path=alvo,
                                  err=humane.human_error(e)))
            return
        self.status.setText(t("✔  Exported {n} row(s) to {path}", n=n, path=alvo))

    def _build_query(self) -> Query | None:

        paths = [p.strip() for p in self.ed_path.text().split(";") if p.strip()]
        paths = [os.path.expanduser(p) for p in paths]
        bad = [p for p in paths if not os.path.exists(p)]
        paths = [p for p in paths if os.path.exists(p)]
        if not paths:
            self.status.setText(t("⚠  No valid folder in 'In:'."))
            return None
        if bad:
            self.status.setText(t("⚠  Ignoring non-existent folder(s): {paths}", paths=', '.join(bad)))
        name_txt = self.ed_name.text().strip()
        if self.ck_nrx.isChecked():
            name_pats = [name_txt] if name_txt else []
        else:
            # texto puro = "contém" (rotina -> *rotina*); glob digitado é respeitado
            name_pats = [engine.as_name_glob(p) for p in name_txt.replace(";", ",").split(",")
                         if p.strip() and p.strip() != "*"]
        days = self.sp_days.value()
        mod_after = (time.time() - days * 86400) if days > 0 else None
        return Query(
            paths=paths,
            name_patterns=name_pats,
            name_is_regex=self.ck_nrx.isChecked(),
            content=self.ed_content.text(),
            content_is_regex=self.ck_crx.isChecked(),
            case_sensitive=self.ck_case.isChecked(),
            whole_word=self.ck_word.isChecked(),
            recursive=self.ck_rec.isChecked(),
            include_hidden=self.ck_hid.isChecked(),
            respect_gitignore=self.ck_git.isChecked(),
            one_file_system=self.ck_ofs.isChecked(),
            min_size=parse_size(self.ed_minsz.text()),
            modified_after=mod_after,
            documents=self.ck_doc.isChecked(),
        )

    def start_search(self, new_tab=False):
        tab = self.new_tab(focus=True) if new_tab else self.tab
        if tab.searching:
            return
        q = self._build_query()
        if not q:
            return
        tab.form = self.form_state()
        searches.add_history(self.cfg, tab.form)
        save_cfg(self.cfg)
        self.tabs.setTabText(self.tabs.indexOf(tab), searches.title_for(tab.form))
        self._stop_media()                        # B11: nova busca cala a mídia
        self.pv_stack.setCurrentIndex(0)
        tab.table.setSortingEnabled(False)        # B14: ordem de chegada durante a busca
        tab.proxy.sort(-1)
        tab.model.clear(); self.preview.clear()
        self.preview.setExtraSelections([])
        self.btn_search.setEnabled(False); self.btn_cancel.setEnabled(True)
        boolexpr = self.ed_content.text().strip() if self.ck_bool.isChecked() else ""
        # B7: termos positivos p/ o destaque no preview (literais; regex não realça)
        tab.hl_cs = q.case_sensitive
        if boolexpr:
            try:
                tab.hl_terms = boolean.positive_terms(boolean.parse(boolexpr))
            except Exception:
                tab.hl_terms = []
        elif q.content and not q.content_is_regex:
            tab.hl_terms = [q.content]
        else:
            tab.hl_terms = []
        self._hl_terms, self._hl_cs = tab.hl_terms, tab.hl_cs
        modes = []
        if boolexpr: modes.append(t("boolean"))
        if q.documents: modes.append(t("documents"))
        tab.mode_tag = f"  ({' + '.join(modes)})" if modes else ""
        tab.phase_txt = ""                        # opt#4: passo atual (modo booleano)
        # SMR: duas abas varrendo o MESMO disco rotacional ao mesmo tempo é
        # exatamente o seek thrash que a serialização interna evita — não
        # adiantaria serializar dentro de uma busca e deixar duas correrem
        # soltas. A segunda espera a primeira, e o status diz por quê.
        if self._must_wait(tab, q):
            tab.pending = (q, boolexpr)
            self._set_status(tab, t("Queued — waiting for the other search "
                                    "(same spinning disk; running both would thrash it)."))
            return
        self._launch(tab, q, boolexpr)

    @staticmethod
    def _serial_paths(q) -> bool:
        try:
            return any(disks.path_needs_serial(os.path.abspath(p)) for p in q.paths)
        except Exception:
            return False

    def _must_wait(self, tab, q) -> bool:
        eu = self._serial_paths(q)
        return any(o is not tab and o.searching and (eu or o.serial)
                   for o in self._all_tabs())

    def _launch(self, tab, q, boolexpr):
        tab.pending = None
        tab.serial = self._serial_paths(q)
        tab.t0 = time.time()
        self._set_status(tab, t("Searching…") + tab.mode_tag)
        w = SearchWorker(q, boolexpr)
        # Cada sinal carrega a ABA a que pertence: uma busca que termina no fundo
        # escreve no modelo e no status DELA, nunca no da aba que está na tela.
        w.batch.connect(tab.model.append)
        w.progress.connect(lambda _n, tb=tab: self._heartbeat_tab(tb))
        w.phase.connect(lambda d, tt, l, tb=tab: self.on_phase(tb, d, tt, l))
        w.done.connect(lambda tot, dt, tb=tab: self.on_done(tb, tot, dt))
        w.error.connect(lambda m, tb=tab: self.on_error(tb, m))
        tab.worker = w
        w.start()
        self._tick.start()                        # B8: heartbeat de status

    def cancel_search(self):
        tab = self.tab
        if tab.pending:                           # ainda na fila do SMR: nem começou
            tab.pending = None
            self._set_status(tab, t("Cancelled before starting."))
            self.btn_search.setEnabled(True); self.btn_cancel.setEnabled(False)
            return
        if tab.searching:
            tab.worker.cancel()
            self._set_status(tab, t("Cancelling…"))

    def closeEvent(self, ev):
        """B5/A1: fechar no meio de uma busca não pode derrubar o processo.
        Destruir um QThread ainda rodando aborta o app (e órfã o rg/fd), então
        esperamos a thread SAIR de fato antes de aceitar o fechamento. Bombeamos
        eventos p/ a UI não congelar; após um teto generoso (o cancelamento já é
        checado a cada bloco/linha, então some em ~1s), forçamos como último recurso."""
        self._tick.stop()
        for tab in self._all_tabs():              # F5: uma busca viva por aba
            tab.stop()
        # F7: mesma disciplina para a cópia. Uma cópia em curso NUNCA é abortada
        # à força no meio de um arquivo sem antes pedir cancel — o fileops apaga
        # o parcial do destino ao ser cancelado, e terminate() puro pularia isso,
        # deixando meio-vídeo no pendrive com cara de arquivo bom.
        if self.copier is not None and self.copier.isRunning():
            self.copier.shutdown()                # A6: sentinela rompe o get() + aborta o job
            deadline = time.time() + 8.0
            while self.copier.isRunning() and time.time() < deadline:
                if self.copier.wait(100):
                    break
                QApplication.processEvents()      # libera diálogos que a thread espera
            if self.copier.isRunning():
                self.copier.terminate()           # último recurso (job travado num I/O)
                self.copier.wait(2000)
        self._stop_media()
        super().closeEvent(ev)

    @staticmethod
    def _denied(tab) -> int:
        return tab.worker.stats.get("denied", 0) if tab.worker else 0

    def _searching_text(self, tab) -> str:
        d = self._denied(tab)
        extra = t(" · {d} inaccessible", d=d) if d else ""
        step = f" · {tab.phase_txt}" if tab.phase_txt else ""   # opt#4: passo booleano
        return t("Searching…{tag}  {n} found · {sec}s{extra}{step}",
                 tag=tab.mode_tag, n=len(tab.model.rows),
                 sec=f"{time.time()-tab.t0:.1f}", extra=extra, step=step)

    def _heartbeat_tab(self, tab):
        if tab.searching:
            self._set_status(tab, self._searching_text(tab))
            self._tab_badge(tab)

    def _tab_badge(self, tab):
        """O rótulo da aba carrega o contador: busca rodando em segundo plano
        precisa dizer que está viva sem roubar a tela de quem olha outra coisa."""
        i = self.tabs.indexOf(tab)
        if i < 0:
            return
        base = searches.title_for(tab.form)
        n = len(tab.model.rows)
        if tab.searching:
            self.tabs.setTabText(i, f"{base}  ({n}…)")
        else:
            self.tabs.setTabText(i, f"{base}  ({n})" if n else base)

    def _heartbeat(self):
        """B8: atualiza o status independentemente de lotes (busca longa não 'trava')."""
        vivo = False
        for tab in self._all_tabs():
            if tab.searching:
                vivo = True
                self._heartbeat_tab(tab)
        if not vivo:
            self._tick.stop()

    def on_phase(self, tab, done, total, label):
        """Opt#4: 'passo done/total: label' vindo do motor booleano."""
        tab.phase_txt = t("step {done}/{total}: {label}", done=done, total=total, label=label)
        self._heartbeat_tab(tab)

    def on_error(self, tab, msg):
        self._set_status(tab, t("⚠  Invalid boolean expression: {msg}", msg=msg))
        if tab is self.tab:
            self.btn_search.setEnabled(True); self.btn_cancel.setEnabled(False)
        self._start_pending()

    def _start_pending(self):
        """Uma busca acabou: a fila do SMR pode andar."""
        for tab in self._all_tabs():
            if tab.pending:
                q, boolexpr = tab.pending
                if not self._must_wait(tab, q):
                    self._launch(tab, q, boolexpr)
                    return

    def on_done(self, tab, tot, dt):
        tab.phase_txt = ""                        # opt#4: fim das fases
        if tab is self.tab:
            self.btn_search.setEnabled(True); self.btn_cancel.setEnabled(False)
        # A3: habilitar ordenação dispara um sort imediato pela coluna do indicador
        # (default = coluna 0 "Arquivo"), que embaralharia a ordem de chegada que o
        # usuário viu preencher. Zera o indicador antes p/ manter a ordem natural;
        # clicar num cabeçalho continua ordenando normalmente.
        tab.table.horizontalHeader().setSortIndicator(-1, Qt.AscendingOrder)
        tab.table.setSortingEnabled(True)         # B14: colunas ordenáveis ao fim
        cancelled = tab.worker and tab.worker._cancel
        icon = "■" if cancelled else "✔"
        d = self._denied(tab)
        extra = t("  ·  {d} inaccessible", d=d) if d else ""
        cancel = t("   (cancelled)") if cancelled else ""
        # dica: zero resultados COM Conteúdo preenchido = quase sempre o usuário
        # quis buscar por NOME (ex.: digitou "*.mp4" no Conteúdo). Aponta o caminho.
        tip = ""
        if tot == 0 and not cancelled and searches.normalize(tab.form)["content"]:
            tip = t("   —  tip: “Content” is filled, so this searched INSIDE files; "
                    "clear it to match file/folder names.")
        self._set_status(tab, t("{icon}  {tot} result(s)  ·  {sec}s{extra}{cancel}",
                                icon=icon, tot=tot, sec=f"{dt:.2f}", extra=extra,
                                cancel=cancel) + tip)
        self._tab_badge(tab)
        self._start_pending()

    # ---- mapeamento proxy (visual) -> source (dados)
    def _match_at_proxy(self, row: int):
        if row < 0 or row >= self.proxy.rowCount():
            return None
        src = self.proxy.mapToSource(self.proxy.index(row, 0))
        return self.model.match_at(src.row())

    # ---- preview
    def on_select(self, cur, prev):
        m = self._match_at_proxy(cur.row()) if cur.isValid() else None
        if not m:
            self._stop_media(); self.pv_stack.setCurrentIndex(0); self.preview.clear()
            return
        kind = media_kind(m.path)
        # vídeo/áudio só como mídia se o QtMultimedia existir; imagem sempre
        if kind == "image" or (kind in ("video", "audio") and HAS_MEDIA):
            self._show_media(m.path, kind)
        else:
            self._stop_media()
            self.pv_stack.setCurrentIndex(0)
            if m.lines:
                out = [m.path, "─" * 72]
                for ln, txt in m.lines[:200]:
                    loc = f"{ln:>6}: " if ln else "        "
                    out.append(loc + txt)
                self.preview.setPlainText("\n".join(out))
            else:
                head = self._peek(m.path)
                self.preview.setPlainText(m.path + "\n" + "─" * 72 + "\n" + head)
            self._apply_highlight()               # B7: realce dos termos positivos

    def _apply_highlight(self):
        """B7: fundo âmbar sobre as ocorrências dos termos positivos no preview."""
        terms = getattr(self, "_hl_terms", None)
        if not terms:
            self.preview.setExtraSelections([])
            return
        pal = THEMES[self.theme]
        fmt = QTextCharFormat()
        fmt.setBackground(QColor(pal["amber"]))
        fmt.setForeground(QColor(pal["on_accent"]))
        doc = self.preview.document()
        flags = QTextDocument.FindFlags()
        if getattr(self, "_hl_cs", False):
            flags |= QTextDocument.FindCaseSensitively
        sels = []
        for term in terms:
            if not term:
                continue
            cur = QTextCursor(doc)
            while True:
                cur = doc.find(term, cur, flags)
                if cur.isNull():
                    break
                sel = QTextEdit.ExtraSelection()
                sel.cursor = cur
                sel.format = fmt
                sels.append(sel)
        self.preview.setExtraSelections(sels)

    # ---- mídia
    def _show_media(self, path: str, kind: str):
        self.pv_stack.setCurrentIndex(1)
        self.lbl_media.setText(os.path.basename(path))
        self.lbl_media.setToolTip(path)
        if kind == "image":
            self._stop_media()
            self._img_path = path
            self.media_view.setCurrentWidget(self.img_label)
            self._load_image()                    # B12: decodifica já reduzido / com teto
            self._set_transport(playable=False)
        else:
            self.media_view.setCurrentIndex(2 if kind == "video" else 1)  # vídeo / ♪
            self._set_transport(playable=True)
            if self.audio_out is not None:
                self.audio_out.setMuted(self.muted)   # B13: começa MUDO por padrão
            self.player.setSource(QUrl.fromLocalFile(path))
            self.player.play()

    def _stop_media(self):
        if self.player is not None:
            self.player.stop()
            self.player.setSource(QUrl())

    def _set_transport(self, playable: bool):
        self.btn_play.setEnabled(playable)
        self.sld_pos.setEnabled(playable)
        self.btn_vol.setEnabled(playable and HAS_MEDIA)
        if not playable:
            self.sld_pos.setRange(0, 0)
            self.lbl_time.setText(t("image"))

    def _toggle_mute(self):
        """B13: liga/desliga o som e persiste a escolha no config."""
        self.muted = not self.muted
        if self.audio_out is not None:
            self.audio_out.setMuted(self.muted)
        self.btn_vol.setText("🔇" if self.muted else "🔊")
        self.cfg["muted"] = self.muted
        save_cfg(self.cfg)

    _IMG_CAP = 64 * 1024 * 1024      # B12: acima disso, não decodifica síncrono

    def _load_image(self):
        """B12: decodifica a imagem JÁ reduzida (QImageReader.setScaledSize) e com teto
        de tamanho — um TIFF de 200 MB num SMR não pode congelar a UI."""
        path = getattr(self, "_img_path", None)
        self._orig_pixmap = None
        if not path:
            return
        try:
            sz = os.path.getsize(path)
        except OSError:
            sz = 0
        # N3: teto INCONDICIONAL — setScaledSize só é fast-path real em JPEG; PNG/TIFF
        # decodificam o raster inteiro antes de escalar e congelariam a UI num SMR.
        if sz > self._IMG_CAP:
            self.img_label.setPixmap(QPixmap())
            self.img_label.setText(t("image too large —\ndouble-click to open externally"))
            return
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        orig = reader.size()               # lê o cabeçalho, não o raster inteiro
        area = self.media_view.size()
        tw, th = max(1, area.width() - 4), max(1, area.height() - 4)
        if orig.isValid() and (orig.width() > tw or orig.height() > th):
            reader.setScaledSize(orig.scaled(tw, th, Qt.KeepAspectRatio))
        img = reader.read()
        if img.isNull():
            self.img_label.setPixmap(QPixmap())
            self.img_label.setText(t("(no image preview)"))
            return
        self._orig_pixmap = QPixmap.fromImage(img)
        self.img_label.setText("")
        self.img_label.setPixmap(self._orig_pixmap)

    def _rescale_image(self):
        pm = getattr(self, "_orig_pixmap", None)
        if not pm or pm.isNull():
            return                          # sem pixmap (imagem gigante/erro): mantém o texto
        area = self.media_view.size()
        self.img_label.setPixmap(pm.scaled(
            max(1, area.width() - 4), max(1, area.height() - 4),
            Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _media_rows(self):
        """Linhas-proxy (ordem VISUAL da tabela) que são mídia reproduzível."""
        out = []
        for r in range(self.proxy.rowCount()):
            m = self._match_at_proxy(r)
            k = media_kind(m.path) if m else None
            if k == "image" or (k in ("video", "audio") and HAS_MEDIA):
                out.append(r)
        return out

    def _nav_media(self, step: int):
        rows = self._media_rows()
        if not rows:
            return
        cur = self.table.selectionModel().currentIndex().row()
        if cur in rows:
            i = rows.index(cur) + step
        else:                                   # nada de mídia selecionado ainda
            i = 0 if step > 0 else len(rows) - 1
        i %= len(rows)
        self.table.selectRow(rows[i])

    def _toggle_play(self):
        if self.player is None:
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _on_play_state(self, state):
        self.btn_play.setText("⏸" if state == QMediaPlayer.PlayingState else "▶")

    def _on_duration(self, dur):
        self.sld_pos.setRange(0, dur)

    def _on_position(self, pos):
        if not self._scrubbing:
            self.sld_pos.setValue(pos)
        dur = self.player.duration() if self.player else 0
        self.lbl_time.setText(f"{fmt_ms(pos)} / {fmt_ms(dur)}")

    def _on_media_status(self, status):
        if HAS_MEDIA and status == QMediaPlayer.EndOfMedia:
            self._nav_media(1)                  # auto-avança ao terminar

    def _seek(self, pos):
        if self.player is not None:
            self.player.setPosition(pos)

    def _seek_release(self):
        self._scrubbing = False
        if self.player is not None:
            self.player.setPosition(self.sld_pos.value())

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if getattr(self, "pv_stack", None) and self.pv_stack.currentIndex() == 1 \
                and self.media_view.currentWidget() is self.img_label:
            self._rescale_image()

    def _peek(self, path, n=80):
        try:
            with open(path, "r", errors="ignore") as f:
                lines = []
                for i, line in enumerate(f, 1):
                    if "\x00" in line:
                        return t("(binary file — no text preview)")
                    lines.append(f"{i:>5}: {line.rstrip()}")
                    if i >= n:
                        lines.append(t("   … (truncated)"))
                        break
                return "\n".join(lines) if lines else t("(empty)")
        except OSError as e:
            return t("(no preview: {e})", e=humane.human_error(e))

    # ---- contexto
    def _sel_matches(self):
        rows = {i.row() for i in self.table.selectionModel().selectedRows()}
        out = [self._match_at_proxy(r) for r in sorted(rows)]
        return [m for m in out if m]

    def open_file(self, *a):
        for m in self._sel_matches()[:10]:
            QDesktopServices.openUrl(QUrl.fromLocalFile(m.path))

    def open_folder(self):
        """Abre a pasta COM O ITEM SELECIONADO, via org.freedesktop.FileManager1
        (padrão que Nemo/Nautilus/Dolphin/Thunar implementam). Numa pasta do
        acervo com centenas de vídeos, abrir sem destacar obriga o usuário a
        reencontrar à mão o que o LFS acabou de achar."""
        ms = self._sel_matches()[:10]
        if not ms:
            return
        dirs = list(dict.fromkeys(os.path.dirname(m.path) for m in ms))
        # A janela tem que abrir no gerenciador PADRÃO DO USUÁRIO. O ShowItems é
        # ativado por nome no barramento, e quem registra o FileManager1 pode não
        # ser o padrão dele (no Mint o Nemo registra mesmo se o padrão for outro).
        # Então: só usa o barramento se o padrão for um implementador conhecido.
        fm = xdg.default_file_manager()
        if fm is None or xdg.implements_showitems(fm):
            if self._show_items([m.path for m in ms]):
                return
        if fm is not None and xdg.launch(fm, dirs):   # padrão do usuário, sem seleção
            return
        # último recurso: xdg-open pelo Qt (WM exótico, sistema sem associação)
        for d in dirs:
            QDesktopServices.openUrl(QUrl.fromLocalFile(d))

    def _show_items(self, paths) -> bool:
        if getattr(self, "_fm1_ok", None) is False:
            return False                       # já falhou antes: não tenta a cada clique
        try:
            from PySide6.QtDBus import QDBusConnection, QDBusInterface, QDBusMessage
        except ImportError:
            self._fm1_ok = False
            return False
        service, obj, iface, method, uris, startup = showitems_args(paths)
        try:
            bus = QDBusConnection.sessionBus()
            if not bus.isConnected():
                self._fm1_ok = False
                return False
            fm = QDBusInterface(service, obj, iface, bus)
            if not fm.isValid():
                self._fm1_ok = False
                return False
            reply = fm.call(method, uris, startup)
            if reply.type() == QDBusMessage.MessageType.ErrorMessage:
                self._fm1_ok = False
                return False
        except Exception:
            self._fm1_ok = False
            return False
        self._fm1_ok = True
        return True

    def open_with_menu(self, mnu: QMenu):
        """Submenu "Abrir com": aplicativos que declaram saber abrir este tipo."""
        mnu.clear()
        ms = self._sel_matches()
        if not ms:
            return
        paths = [m.path for m in ms[:10]]
        for app in xdg.apps_for(paths[0]):
            mnu.addAction(app.name, lambda _=False, a=app: xdg.launch(a, paths))
        mnu.addSeparator()
        mnu.addAction(t("Other command…"), self.open_with_other)

    def open_with_other(self):
        ms = self._sel_matches()
        if not ms:
            return
        cmd, ok = QInputDialog.getText(self, t("Open with"),
                                       t("Command (the file paths are appended):"))
        if ok and cmd.strip():
            xdg.launch_command(cmd.strip(), [m.path for m in ms[:10]])

    def copy_paths(self):
        ms = self._sel_matches()
        if ms:
            QGuiApplication.clipboard().setText("\n".join(m.path for m in ms))
            self.status.setText(t("{n} path(s) copied.", n=len(ms)))

    def copy_selection(self):
        """Ctrl+C: coloca os ARQUIVOS no clipboard (não o texto do caminho), nos
        três formatos que os gerenciadores leem — Ctrl+V no Nemo cola de verdade."""
        ms = self._sel_matches()
        if not ms:
            return
        QGuiApplication.clipboard().setMimeData(build_paths_mime([m.path for m in ms]))
        self.status.setText(t("{n} item(s) copied to the clipboard.", n=len(ms)))

    def properties(self):
        ms = self._sel_matches()
        if ms:
            PropertiesDialog(self, ms[0]).exec()

    # ---- F7: copiar para outro dispositivo
    def _recent_dests(self):
        return [d for d in self.cfg.get("copy_dests", []) if os.path.isdir(d)]

    def copy_to_dialog(self, dest=None):
        ms = self._sel_matches()
        if not ms:
            return
        if not dest:
            start = (self._recent_dests() or [os.path.expanduser("~")])[0]
            dest = QFileDialog.getExistingDirectory(self, t("Copy to…"), start)
        if not dest:
            return
        recents = [dest] + [d for d in self._recent_dests() if d != dest]
        self.cfg["copy_dests"] = recents[:8]
        save_cfg(self.cfg)
        self.enqueue_copy([m.path for m in ms], dest)

    def enqueue_copy(self, sources, dest):
        # A6: só enfileira. O worker persistente está dormindo em get() e acorda
        # sozinho — sem recriar QThread, sem flag `running`, sem corrida.
        self.copy_q.put((sources, dest, False))
        pending = self.copy_q.pending()
        if pending > 1:
            self.status.setText(t("Queued — {n} copy job(s) pending.", n=pending - 1))

    def on_ask_preflight(self, pf, ask):
        dlg = PreflightDialog(self, pf)
        ok = dlg.exec() == QDialog.Accepted
        ask.reply({"sanitize": dlg.ck_fix.isChecked()} if ok else False)

    def on_ask_conflict(self, src, dst, ask):
        dlg = ConflictDialog(self, src, dst)
        dlg.exec()
        ask.reply((dlg.answer, dlg.ck_all.isChecked()))

    def on_copy_started(self, dest, pending):
        self.copy_bar.setVisible(True)
        self.pb_copy.setRange(0, 0)                 # indeterminado durante a varredura
        self.lbl_copy.setText(t("Scanning source…"))
        self.lbl_copy_rate.setText(t("{n} pending", n=pending) if pending else "")

    def on_copy_progress(self, p):
        self.pb_copy.setRange(0, 1000)
        frac = (p.done_bytes / p.total_bytes) if p.total_bytes else 0
        self.pb_copy.setValue(int(frac * 1000))
        self.lbl_copy.setText(t("Copying {name}", name=os.path.basename(p.current_path)))
        self.lbl_copy_rate.setText("%s/s · %s / %s" % (
            human_size(int(p.speed_bps)), human_size(p.done_bytes),
            human_size(p.total_bytes)))

    def on_copy_done(self, res, dest):
        if res is None:
            self.status.setText(t("Copy cancelled."))
            return
        parts = [t("{n} copied", n=len(res.copied))]
        if res.skipped:
            parts.append(t("{n} skipped", n=len(res.skipped)))
        if res.failed:
            parts.append(t("{n} failed", n=len(res.failed)))
        if getattr(res, "out_of_space", False):
            # A4.3: encher o destino não é "cancelar" — dá o motivo exato
            self.status.setText(t("✖  Copy to {dest} stopped — the destination "
                                  "ran out of space ({summary})  ·  {size}  ·  "
                                  "nothing in the source was modified.",
                                  dest=dest, summary=", ".join(parts),
                                  size=human_size(res.bytes_copied)))
        else:
            self.status.setText(t("✔  Copy to {dest}: {summary}  ·  {size}  ·  "
                                  "nothing in the source was modified.",
                                  dest=dest, summary=", ".join(parts),
                                  size=human_size(res.bytes_copied)))
        if res.failed:
            QMessageBox.warning(self, t("Copy finished with errors"),
                                "\n".join(humane.human_error(e, context="copy",
                                                             target=os.path.basename(p))
                                          for p, e in res.failed[:15]))

    def on_copy_all_done(self):
        self.copy_bar.setVisible(False)

    def cancel_copy(self):
        if getattr(self, "copier", None) and self.copier.isRunning():
            self.copier.cancel_all()
            self.status.setText(t("Cancelling copy…"))

    # ---- F7: soltar pasta NA janela = "procure aqui" (não copia nada)
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        dirs = []
        for u in ev.mimeData().urls():
            p = u.toLocalFile()
            if not p:
                continue
            dirs.append(p if os.path.isdir(p) else os.path.dirname(p))
        cur = self._paths_list()
        for d in dirs:
            if d and d not in cur:
                cur.append(d)
        if dirs:
            self.ed_path.setText(";".join(cur))
            self.status.setText(t("Added {n} folder(s) to search in.", n=len(dirs)))
            ev.acceptProposedAction()

    def context_menu(self, pos):
        if not self.table.selectionModel().hasSelection():
            return
        mnu = QMenu(self)
        mnu.addAction(t("Open file"), self.open_file)
        sub = mnu.addMenu(t("Open with"))
        sub.aboutToShow.connect(lambda m=sub: self.open_with_menu(m))
        mnu.addAction(t("Open containing folder"), self.open_folder)
        mnu.addSeparator()
        mnu.addAction(t("Copy"), self.copy_selection)
        cp = mnu.addMenu(t("Copy to…"))
        for d in self._recent_dests():
            cp.addAction(d, lambda _=False, dd=d: self.copy_to_dialog(dd))
        if self._recent_dests():
            cp.addSeparator()
        cp.addAction(t("Choose folder…"), lambda: self.copy_to_dialog())
        mnu.addAction(t("Copy path(s)"), self.copy_paths)
        mnu.addSeparator()
        mnu.addAction(t("Properties"), self.properties)
        mnu.exec(self.table.viewport().mapToGlobal(pos))


def main():
    # Migração do rebranding: só no arranque REAL da GUI, nunca no import (assim
    # importar o módulo — em teste ou ferramenta — não mexe no ~/.config do usuário).
    _migrate_old_config()
    app = QApplication(sys.argv)
    app.setApplicationName("Sombrero File Search")
    app.setApplicationDisplayName("Sombrero File Search")
    # Amarra a janela ao .desktop instalado: sem isto o WM usa o WM_CLASS
    # genérico e o ícone da barra de tarefas some (mostra o de app desconhecido).
    app.setDesktopFileName("sombrero-file-search")
    ico = os.path.join(ASSETS, "icon_256.png")
    if os.path.exists(ico):
        app.setWindowIcon(QIcon(ico))
    w = MainWindow(); w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
