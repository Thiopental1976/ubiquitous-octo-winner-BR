#!/usr/bin/env python3
"""Linux File Search — GUI (PySide6).

Busca ampla de arquivos estilo Agent Ransack / FileLocator Pro, sobre ripgrep+fd+rga,
NATIVA e portável entre distros. Motor em engine.py (sem Qt). A GUI só orquestra:
form -> worker em thread -> tabela ao vivo -> preview com destaque.

Recursos: nome+conteúdo, booleano (A OR B) AND C NOT D, documentos (PDF/docx/epub/zip).
Desenho: GARIMPO_Desenho_Busca_ripgrep.md (Fable 5) — nome final "Linux File Search".
"""
from __future__ import annotations
import os, sys, time

from PySide6.QtCore import (Qt, QThread, Signal, QAbstractTableModel, QModelIndex,
                            QUrl, QTimer, QSortFilterProxyModel)
from PySide6.QtGui import (QAction, QColor, QDesktopServices, QFont, QGuiApplication,
                           QIcon, QImageReader, QPixmap, QKeySequence, QShortcut,
                           QTextCharFormat, QTextCursor, QTextDocument)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QPushButton, QCheckBox, QLabel, QTableView, QPlainTextEdit,
    QFileDialog, QSplitter, QHeaderView, QSpinBox, QMenu, QTextEdit,
    QAbstractItemView, QToolButton, QFrame, QStackedWidget, QSlider, QSizePolicy)

try:
    from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
    from PySide6.QtMultimediaWidgets import QVideoWidget
    HAS_MEDIA = True
except ImportError:                     # QtMultimedia opcional (portabilidade)
    HAS_MEDIA = False

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine, boolean
from engine import Query, Match

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


parse_size = engine.parse_size          # §5: fonte única (era duplicado aqui e no cli)


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
        def on_phase(d, t, label):
            self.phase.emit(d, t, label)
        try:
            if self.boolexpr:
                tot, dt = boolean.search_boolean(self.q, self.boolexpr, on_result,
                                                 lambda: self._cancel, on_prog, on_phase)
            else:
                tot, dt = engine.search(self.q, on_result, lambda: self._cancel, on_prog,
                                        stats=self.stats)
        except boolean.BooleanError as e:
            self._flush(force=True)
            self.error.emit(str(e))
            return
        self._flush(force=True)
        self.done.emit(tot, dt)


# ----------------------------------------------------------------- modelo
class ResultModel(QAbstractTableModel):
    HEADERS = ["Arquivo", "Pasta", "Matches", "Tamanho", "Modificado"]
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
            return self.HEADERS[s]
        return None

    def data(self, idx, role=Qt.DisplayRole):
        if not idx.isValid():
            return None
        m = self.rows[idx.row()]
        c = idx.column()
        if role == Qt.DisplayRole:
            if c == 0: return os.path.basename(m.path)
            if c == 1: return os.path.dirname(m.path)
            if c == 2: return str(m.nmatch) if m.nmatch else ""
            if c == 3: return human_size(m.size)
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


# ----------------------------------------------------------------- temas
CONFIG_DIR = os.path.join(os.path.expanduser(
    os.environ.get("XDG_CONFIG_HOME", "~/.config")), "linux-file-search")
CONFIG = os.path.join(CONFIG_DIR, "config.json")

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
QLineEdit#content {{ font-size: 14px; padding: 9px 12px; }}
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
    lab.setToolTip(f"{name}: {'disponível' if present else 'ausente'}")
    return lab


# ----------------------------------------------------------------- janela
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Linux File Search")
        self.resize(1160, 760)
        ico = os.path.join(ASSETS, "icon_256.png")
        if os.path.exists(ico):
            self.setWindowIcon(QIcon(ico))
        self.worker: SearchWorker | None = None
        self.t0 = 0.0
        self._mode_tag = ""
        self._tick = QTimer(self)                 # B8: pulso de status a cada 0,5 s
        self._tick.setInterval(500)
        self._tick.timeout.connect(self._heartbeat)
        self.cfg = load_cfg()
        self.muted = bool(self.cfg.get("muted", True))   # B13: mídia começa muda
        self.theme = self.cfg.get("theme", "dark")
        if self.theme not in THEMES:
            self.theme = "dark"
        self._build()
        self.apply_theme(self.theme)
        QShortcut(QKeySequence(Qt.Key_Escape), self, self.cancel_search)
        QShortcut(QKeySequence("Ctrl+L"), self, lambda: self.ed_content.setFocus())
        QShortcut(QKeySequence("Ctrl+T"), self, self.toggle_theme)

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
        t = QLabel("Linux File Search"); t.setObjectName("title")
        s = QLabel("busca ampla de arquivos — nome · conteúdo · booleano · documentos")
        s.setObjectName("subtitle")
        tt.addWidget(t); tt.addWidget(s); hl.addLayout(tt); hl.addStretch(1)
        self.badges = QHBoxLayout(); self.badges.setSpacing(10)
        hl.addLayout(self.badges)
        hl.addSpacing(10)
        self.btn_theme = QToolButton(); self.btn_theme.setCursor(Qt.PointingHandCursor)
        self.btn_theme.setToolTip("Alternar tema claro/escuro (Ctrl+T)")
        self.btn_theme.clicked.connect(self.toggle_theme)
        hl.addWidget(self.btn_theme)
        root.addWidget(header)

        # ---------- barra de busca ----------
        r1 = QHBoxLayout(); r1.setSpacing(8)
        self.ed_content = QLineEdit(); self.ed_content.setObjectName("content")
        self.ed_content.setClearButtonEnabled(True)
        self.ed_content.setPlaceholderText(
            "Conteúdo a conter (texto ou regex)…   — vazio = busca só por nome")
        self.ed_content.returnPressed.connect(self.start_search)
        self.btn_search = QPushButton("  Buscar  "); self.btn_search.setObjectName("primary")
        self.btn_search.setDefault(True); self.btn_search.clicked.connect(self.start_search)
        self.btn_cancel = QPushButton("Cancelar"); self.btn_cancel.clicked.connect(self.cancel_search)
        self.btn_cancel.setEnabled(False)
        r1.addWidget(self.ed_content, 1); r1.addWidget(self.btn_search); r1.addWidget(self.btn_cancel)
        root.addLayout(r1)

        # ---------- nome + pasta ----------
        r2 = QHBoxLayout(); r2.setSpacing(8)
        lbl_n = QLabel("Nome"); lbl_n.setObjectName("section")
        self.ed_name = QLineEdit("*"); self.ed_name.setPlaceholderText("*.py, *.txt, *.pdf")
        self.ed_name.setMaximumWidth(260); self.ed_name.returnPressed.connect(self.start_search)
        lbl_e = QLabel("Em"); lbl_e.setObjectName("section")
        self.ed_path = QLineEdit(os.path.expanduser("~"))
        self.ed_path.setPlaceholderText("Pasta(s) — separe por ';'")
        self.ed_path.returnPressed.connect(self.start_search)
        btn_browse = QToolButton(); btn_browse.setText("Procurar…"); btn_browse.clicked.connect(self.browse)
        r2.addWidget(lbl_n); r2.addWidget(self.ed_name)
        r2.addSpacing(6)
        r2.addWidget(lbl_e); r2.addWidget(self.ed_path, 1); r2.addWidget(btn_browse)
        root.addLayout(r2)

        # ---------- chips de opção ----------
        bar = QFrame(); bar.setObjectName("toolbar")
        r3 = QHBoxLayout(bar); r3.setContentsMargins(12, 8, 12, 8); r3.setSpacing(7)
        self.ck_case = QCheckBox("Aa"); self.ck_case.setToolTip("Sensível a maiúsculas/minúsculas")
        self.ck_word = QCheckBox("palavra"); self.ck_word.setToolTip("Palavra inteira")
        self.ck_bool = QCheckBox("booleano"); self.ck_bool.setToolTip(
            "Interpreta o campo Conteúdo como expressão: (A OR B) AND C NOT D\n"
            "Também aceita | & !  e \"aspas\" p/ frases. Precedência NOT>AND>OR.")
        self.ck_bool.toggled.connect(self._on_bool_toggled)
        self.ck_doc = QCheckBox("documentos")
        self.ck_doc.toggled.connect(self._on_doc_toggled)      # B6
        if engine.RGA:
            self.ck_doc.setToolTip("Busca DENTRO de PDF/docx/epub/odt/zip… (ripgrep-all).")
        else:
            self.ck_doc.setEnabled(False)
            self.ck_doc.setToolTip("Requer 'ripgrep-all' (rga) — rode o instalador.")
        self.ck_crx = QCheckBox("regex conteúdo")
        self.ck_nrx = QCheckBox("regex nome")
        self.ck_rec = QCheckBox("subpastas"); self.ck_rec.setChecked(True)
        self.ck_hid = QCheckBox("ocultos")
        self.ck_git = QCheckBox(".gitignore"); self.ck_git.setToolTip("Respeitar regras .gitignore")
        self.ck_ofs = QCheckBox("1 disco"); self.ck_ofs.setToolTip(
            "--one-file-system: não entra em outros pontos de montagem")
        for w in (self.ck_case, self.ck_word, self.ck_bool, self.ck_doc, self.ck_crx,
                  self.ck_nrx, self.ck_rec, self.ck_hid, self.ck_git, self.ck_ofs):
            r3.addWidget(w)
        r3.addStretch(1)
        lsz = QLabel("Tam ≥"); lsz.setObjectName("section"); r3.addWidget(lsz)
        self.ed_minsz = QLineEdit(); self.ed_minsz.setFixedWidth(66)
        self.ed_minsz.setPlaceholderText("10M"); r3.addWidget(self.ed_minsz)
        lmod = QLabel("Últimos"); lmod.setObjectName("section"); r3.addWidget(lmod)
        self.sp_days = QSpinBox(); self.sp_days.setRange(0, 3650)
        self.sp_days.setSpecialValueText("—"); self.sp_days.setSuffix(" d")
        self.sp_days.setFixedWidth(70); r3.addWidget(self.sp_days)
        root.addWidget(bar)

        # ---------- resultados / preview ----------
        split = QSplitter(Qt.Vertical)
        self.model = ResultModel()
        self.proxy = QSortFilterProxyModel()          # B14: ordenação de colunas
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(ResultModel.SORT_ROLE)
        self.table = QTableView(); self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setSortingEnabled(False)           # ligado só ao fim da busca
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
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
        self.table.selectionModel().currentRowChanged.connect(self.on_select)
        self.table.doubleClicked.connect(self.open_file)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.context_menu)
        split.addWidget(self.table)

        split.addWidget(self._build_preview())
        split.setStretchFactor(0, 3); split.setStretchFactor(1, 2)
        split.setSizes([470, 240])
        root.addWidget(split, 1)

        # ---------- status ----------
        self.status = QLabel("Pronto.")
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self.status)   # estilo aplicado em apply_theme()

    # ---- preview (texto ↔ mídia)
    def _build_preview(self) -> QWidget:
        self.pv_stack = QStackedWidget()

        # página 0: trecho de texto
        self.preview = QPlainTextEdit(); self.preview.setReadOnly(True)
        self.preview.setLineWrapMode(QPlainTextEdit.NoWrap)
        f = QFont("monospace"); f.setStyleHint(QFont.Monospace); self.preview.setFont(f)
        self.preview.setPlaceholderText("Selecione um resultado para ver o trecho…")
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
        self.btn_prev.setText("⏮"); self.btn_prev.setToolTip("Mídia anterior")
        self.btn_prev.clicked.connect(lambda: self._nav_media(-1))
        self.btn_play = QToolButton(); self.btn_play.setObjectName("play")
        self.btn_play.setText("▶"); self.btn_play.setToolTip("Reproduzir / pausar")
        self.btn_play.clicked.connect(self._toggle_play)
        self.btn_next = QToolButton(); self.btn_next.setObjectName("transport")
        self.btn_next.setText("⏭"); self.btn_next.setToolTip("Próxima mídia")
        self.btn_next.clicked.connect(lambda: self._nav_media(1))
        self.btn_vol = QToolButton(); self.btn_vol.setObjectName("transport")  # B13
        self.btn_vol.setText("🔇" if self.muted else "🔊")
        self.btn_vol.setToolTip("Mudo (padrão para privacidade) — clique p/ ativar o som")
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
        self.btn_theme.setText("☀  Claro" if self.theme == "dark" else "☾  Escuro")
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
                "Expressão booleana:   (nota OR laudo) AND paciente NOT rascunho")
            if self.ck_doc.isChecked():           # B6: não combinam
                self.ck_doc.setChecked(False)
        else:
            self.ck_crx.setEnabled(True)
            self.ed_content.setPlaceholderText(
                "Conteúdo a conter (texto ou regex)…   — vazio = busca só por nome")
        # B6: booleano ainda não busca dentro de documentos — um desabilita o outro
        self.ck_doc.setEnabled(not on and bool(engine.RGA))

    def _on_doc_toggled(self, on):
        if on and self.ck_bool.isChecked():       # B6
            self.ck_bool.setChecked(False)
        self.ck_bool.setEnabled(not on)

    def browse(self):
        d = QFileDialog.getExistingDirectory(self, "Escolha a pasta",
                                             self.ed_path.text().split(";")[0] or os.path.expanduser("~"))
        if d:
            cur = self.ed_path.text().strip()
            self.ed_path.setText(f"{cur};{d}" if cur else d)

    def _build_query(self) -> Query | None:
        paths = [p.strip() for p in self.ed_path.text().split(";") if p.strip()]
        paths = [os.path.expanduser(p) for p in paths]
        bad = [p for p in paths if not os.path.exists(p)]
        paths = [p for p in paths if os.path.exists(p)]
        if not paths:
            self.status.setText("⚠  Nenhuma pasta válida em 'Em:'.")
            return None
        if bad:
            self.status.setText(f"⚠  Ignorando pasta(s) inexistente(s): {', '.join(bad)}")
        name_txt = self.ed_name.text().strip()
        if self.ck_nrx.isChecked():
            name_pats = [name_txt] if name_txt else []
        else:
            name_pats = [p.strip() for p in name_txt.replace(";", ",").split(",")
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

    def start_search(self):
        if self.worker and self.worker.isRunning():
            return
        q = self._build_query()
        if not q:
            return
        self._stop_media()                        # B11: nova busca cala a mídia
        self.pv_stack.setCurrentIndex(0)
        self.table.setSortingEnabled(False)       # B14: ordem de chegada durante a busca
        self.proxy.sort(-1)
        self.model.clear(); self.preview.clear()
        self.preview.setExtraSelections([])
        self.btn_search.setEnabled(False); self.btn_cancel.setEnabled(True)
        self.t0 = time.time()
        boolexpr = self.ed_content.text().strip() if self.ck_bool.isChecked() else ""
        # B7: termos positivos p/ o destaque no preview (literais; regex de conteúdo não realça)
        self._hl_cs = q.case_sensitive
        if boolexpr:
            try:
                self._hl_terms = boolean.positive_terms(boolean.parse(boolexpr))
            except Exception:
                self._hl_terms = []
        elif q.content and not q.content_is_regex:
            self._hl_terms = [q.content]
        else:
            self._hl_terms = []
        modes = []
        if boolexpr: modes.append("booleano")
        if q.documents: modes.append("documentos")
        self._mode_tag = f"  ({' + '.join(modes)})" if modes else ""
        self._phase_txt = ""                      # opt#4: passo atual (modo booleano)
        self.status.setText("Buscando…" + self._mode_tag)
        self.worker = SearchWorker(q, boolexpr)
        self.worker.batch.connect(self.model.append)
        self.worker.progress.connect(self.on_progress)
        self.worker.phase.connect(self.on_phase)
        self.worker.done.connect(self.on_done)
        self.worker.error.connect(self.on_error)
        self.worker.start()
        self._tick.start()                        # B8: heartbeat de status

    def cancel_search(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.status.setText("Cancelando…")

    def closeEvent(self, ev):
        """B5: fechar no meio de uma busca não pode derrubar o processo.
        Cancela o worker e espera a thread sair antes de aceitar o fechamento."""
        self._tick.stop()
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(3000)
        self._stop_media()
        super().closeEvent(ev)

    def _denied(self) -> int:
        return self.worker.stats.get("denied", 0) if self.worker else 0

    def _heartbeat(self):
        """B8: atualiza o status independentemente de lotes (busca longa não 'trava')."""
        d = self._denied()
        extra = f" · {d} inacessível(is)" if d else ""
        ph = getattr(self, "_phase_txt", "")
        step = f" · {ph}" if ph else ""           # opt#4: passo booleano atual
        self.status.setText(f"Buscando…{self._mode_tag}  {len(self.model.rows)} encontrados "
                            f"· {time.time()-self.t0:.1f}s{extra}{step}")

    def on_phase(self, done, total, label):
        """Opt#4: recebe 'passo done/total: label' do motor booleano e mostra no status."""
        self._phase_txt = f"passo {done}/{total}: {label}"
        self._heartbeat()

    def on_error(self, msg):
        self._tick.stop()
        self.btn_search.setEnabled(True); self.btn_cancel.setEnabled(False)
        self.status.setText(f"⚠  Expressão booleana inválida: {msg}")

    def on_progress(self, n):
        self._heartbeat()

    def on_done(self, tot, dt):
        self._tick.stop()
        self._phase_txt = ""                      # opt#4: fim das fases
        self.btn_search.setEnabled(True); self.btn_cancel.setEnabled(False)
        self.table.setSortingEnabled(True)        # B14: colunas ordenáveis ao fim
        cancelled = self.worker and self.worker._cancel
        icon = "■" if cancelled else "✔"
        d = self._denied()
        extra = f"  ·  {d} inacessível(is)" if d else ""
        self.status.setText(f"{icon}  {tot} resultado(s)  ·  {dt:.2f}s" + extra
                            + ("   (cancelado)" if cancelled else ""))

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
            self.lbl_time.setText("imagem")

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
            self.img_label.setText("imagem muito grande —\nclique duplo p/ abrir externo")
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
            self.img_label.setText("(sem pré-visualização de imagem)")
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
                        return "(arquivo binário — sem preview de texto)"
                    lines.append(f"{i:>5}: {line.rstrip()}")
                    if i >= n:
                        lines.append("   … (truncado)")
                        break
                return "\n".join(lines) if lines else "(vazio)"
        except OSError as e:
            return f"(sem preview: {e})"

    # ---- contexto
    def _sel_matches(self):
        rows = {i.row() for i in self.table.selectionModel().selectedRows()}
        out = [self._match_at_proxy(r) for r in sorted(rows)]
        return [m for m in out if m]

    def open_file(self, *a):
        for m in self._sel_matches()[:10]:
            QDesktopServices.openUrl(QUrl.fromLocalFile(m.path))

    def open_folder(self):
        for m in self._sel_matches()[:10]:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(m.path)))

    def copy_paths(self):
        ms = self._sel_matches()
        if ms:
            QGuiApplication.clipboard().setText("\n".join(m.path for m in ms))
            self.status.setText(f"{len(ms)} caminho(s) copiado(s).")

    def context_menu(self, pos):
        if not self.table.selectionModel().hasSelection():
            return
        mnu = QMenu(self)
        mnu.addAction("Abrir arquivo", self.open_file)
        mnu.addAction("Abrir pasta", self.open_folder)
        mnu.addSeparator()
        mnu.addAction("Copiar caminho(s)", self.copy_paths)
        mnu.exec(self.table.viewport().mapToGlobal(pos))


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Linux File Search")
    app.setApplicationDisplayName("Linux File Search")
    ico = os.path.join(ASSETS, "icon_256.png")
    if os.path.exists(ico):
        app.setWindowIcon(QIcon(ico))
    w = MainWindow(); w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
