"""Visual redesign for the GOTWiC Scanner desktop GUI.

Applied at runtime (no change to gotwic_gui.py, so the auto-updater never undoes it):
  * apply_theme(app)       dark "war room in winter" palette + stylesheet for every window/dialog
  * polish_main_window(w)  re-arranges the Scanner tab (sidebar + full-height results),
                           status pills and number formatting in the castle tables, and a header
                           with a live Chrome / game connection indicator.
Only existing widgets are moved; every signal, setting and behaviour of the app stays the same.
Disable with the environment variable GOTWIC_THEME=off.
"""
import json
import threading
import urllib.request

from PySide6.QtCore import Qt, QTimer, QRectF, QObject, QEvent
from PySide6.QtGui import QColor, QPalette, QPainter, QFont, QFontMetrics
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QPushButton, QScrollArea, QSizePolicy, QSplitter,
                               QStackedWidget, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
                               QVBoxLayout, QWidget)

# ---------------------------------------------------------------- tokens
NIGHT = '#1A2029'      # window background
PANEL = '#212935'      # cards, tables
RAISED = '#2A3441'     # inputs, buttons, headers
LINE = '#364252'       # borders, separators
TEXT = '#E4E9EF'
MUTED = '#8D9AAA'
FROST = '#7FB8DA'      # accent: primary action, focus, selection
OPEN = '#63CF8E'       # UNPROTECTED
SHIELD = '#D9777A'     # PROTECTED
FERVOR = '#E2B65C'     # UNKNOWN / Fervor

QSS = f"""
* {{ outline: 0; }}
QMainWindow, QDialog, QMessageBox, QInputDialog {{ background: {NIGHT}; }}
QWidget {{ color: {TEXT}; font-size: 13px; }}
QToolTip {{ background: {RAISED}; color: {TEXT}; border: 1px solid {LINE}; padding: 6px 8px; }}

QTabWidget::pane {{ border: 0; border-top: 1px solid {LINE}; top: -1px; }}
QTabBar {{ qproperty-drawBase: 0; }}
QTabBar::tab {{ background: transparent; color: {MUTED}; padding: 10px 18px; margin-right: 2px;
               border: 0; border-bottom: 2px solid transparent; font-weight: 500; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {FROST}; }}

QGroupBox {{ background: {PANEL}; border: 1px solid {LINE}; border-radius: 10px;
            margin-top: 22px; padding: 14px 12px 12px 12px; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 4px; top: 2px; color: {MUTED}; }}
QFrame#sidebar, QFrame#results {{ background: {PANEL}; border: 1px solid {LINE}; border-radius: 12px; }}
QFrame#header {{ background: {NIGHT}; border: 0; }}
QLabel#appTitle {{ font-size: 17px; font-weight: 700; }}
QLabel#appVersion, QLabel#hint {{ color: {MUTED}; }}
QLabel#sectionTitle {{ font-size: 14px; font-weight: 650; }}
QLabel#statusLine {{ color: {MUTED}; padding: 2px 0; }}
QLabel#connection {{ padding: 5px 12px; border-radius: 13px; background: {RAISED}; color: {MUTED}; }}
QLabel#connection[state="ok"] {{ color: {OPEN}; }}
QLabel#connection[state="warn"] {{ color: {FERVOR}; }}
QLabel#connection[state="off"] {{ color: {SHIELD}; }}

QLineEdit, QSpinBox, QComboBox {{ background: {RAISED}; border: 1px solid {LINE}; border-radius: 7px;
    padding: 6px 9px; min-height: 18px; selection-background-color: {FROST}; selection-color: {NIGHT}; }}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border: 1px solid {FROST}; }}
QLineEdit:read-only {{ color: {MUTED}; }}
QComboBox::drop-down {{ border: 0; }}
QComboBox QAbstractItemView {{ background: {RAISED}; border: 1px solid {LINE}; selection-background-color: {FROST};
    selection-color: {NIGHT}; padding: 4px; }}
QSpinBox::up-button, QSpinBox::down-button {{ border: 0; background: transparent; }}

QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {LINE}; background: {RAISED}; }}
QCheckBox::indicator:checked {{ background: {FROST}; border: 1px solid {FROST}; }}
QCheckBox:disabled {{ color: {MUTED}; }}

QPushButton {{ background: {RAISED}; border: 1px solid {LINE}; border-radius: 8px; padding: 8px 14px; font-weight: 500; }}
QPushButton:hover {{ border-color: {FROST}; }}
QPushButton:pressed {{ background: {LINE}; }}
QPushButton:disabled {{ color: #5C6878; border-color: {RAISED}; }}
QPushButton:checked {{ background: {FERVOR}; color: {NIGHT}; border-color: {FERVOR}; }}
QPushButton#primary {{ background: {FROST}; color: {NIGHT}; border: 0; font-weight: 700; padding: 11px 18px; font-size: 14px; }}
QPushButton#primary:hover {{ background: #9CCBE6; }}
QPushButton#primary:disabled {{ background: {RAISED}; color: #5C6878; }}
QPushButton#danger {{ background: transparent; color: {SHIELD}; border: 1px solid {SHIELD}; }}
QPushButton#danger:disabled {{ color: #5C6878; border-color: {RAISED}; }}
QPushButton#quiet {{ background: transparent; border: 1px solid {LINE}; color: {TEXT}; }}

QTableView, QTableWidget {{ background: {PANEL}; alternate-background-color: #252E3A; border: 0;
    gridline-color: transparent; selection-background-color: #34506A; selection-color: {TEXT}; }}
QHeaderView::section {{ background: {PANEL}; color: {MUTED}; border: 0; border-bottom: 1px solid {LINE};
    padding: 8px 10px; font-weight: 600; }}
QHeaderView::section:hover {{ color: {TEXT}; }}
QTableCornerButton::section {{ background: {PANEL}; border: 0; }}

QTextEdit {{ background: {NIGHT}; border: 1px solid {LINE}; border-radius: 8px; padding: 6px;
    color: #B8C3CF; font-family: Menlo, Consolas, monospace; font-size: 11px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {LINE}; border-radius: 4px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {LINE}; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QSplitter::handle {{ background: transparent; width: 12px; }}
QMenu {{ background: {RAISED}; border: 1px solid {LINE}; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 5px; }}
QMenu::item:selected {{ background: {FROST}; color: {NIGHT}; }}
QMessageBox QLabel {{ color: {TEXT}; }}
QFrame#segmented {{ background: {RAISED}; border: 1px solid {LINE}; border-radius: 9px; }}
QPushButton#segment {{ background: transparent; border: 0; border-radius: 7px; padding: 6px 10px;
    color: {MUTED}; font-weight: 600; }}
QPushButton#segment:hover {{ color: {TEXT}; }}
QPushButton#segment:checked {{ background: {FROST}; color: {NIGHT}; }}
QPushButton#segment:disabled {{ color: #5C6878; }}
QPushButton#segment:checked:disabled {{ background: #4E6F86; color: {NIGHT}; }}
QProgressBar {{ background: {LINE}; border: 0; border-radius: 5px; max-height: 10px; min-height: 10px; }}
QProgressBar::chunk {{ background: {FROST}; border-radius: 5px; }}
QLabel#sweepState {{ font-weight: 600; }}
QLabel#waitRandom {{ background: transparent; border: 1px dashed {LINE}; border-radius: 7px;
    padding: 6px 9px; min-height: 18px; color: {MUTED}; }}
QLabel#discreetHint {{ color: {MUTED}; font-size: 12px; padding: 0 0 2px 26px; }}
QLabel#estimate {{ color: {MUTED}; }}
QLabel#estimate[state="error"] {{ color: {FERVOR}; }}
QPushButton#miniSegment {{ background: {RAISED}; border: 1px solid {LINE}; border-radius: 0; padding: 3px 12px;
    font-size: 12px; color: {MUTED}; }}
QPushButton#miniSegment:checked {{ background: {LINE}; color: {TEXT}; border-color: {LINE}; }}
QPushButton#miniSegment:hover {{ color: {TEXT}; }}
QLabel#sweepCounts {{ color: {MUTED}; }}
QScrollArea#sideScroll, QScrollArea#sideScroll > QWidget > QWidget {{ background: transparent; border: 0; }}
"""


def _arrow_images():
    import tempfile
    from pathlib import Path
    from PySide6.QtGui import QImage, QPen, QPolygonF
    from PySide6.QtCore import QPointF
    folder = Path(tempfile.gettempdir()) / 'gotwic_theme'
    folder.mkdir(exist_ok=True)
    paths = {}
    for name, pts in (('down', [(3, 4), (10, 11), (17, 4)]), ('up', [(3, 11), (10, 4), (17, 11)])):
        img = QImage(20, 14, QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(MUTED)); pen.setWidthF(2.2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap); pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.drawPolyline(QPolygonF([QPointF(x, y) for x, y in pts]))
        p.end()
        path = folder / f'{name}.png'
        img.save(str(path))
        paths[name] = path.as_posix()
    return paths


def apply_theme(app):
    app.setStyle('Fusion')
    pal = QPalette()
    for role, color in ((QPalette.ColorRole.Window, NIGHT), (QPalette.ColorRole.Base, PANEL),
                        (QPalette.ColorRole.AlternateBase, '#252E3A'), (QPalette.ColorRole.Button, RAISED),
                        (QPalette.ColorRole.Text, TEXT), (QPalette.ColorRole.WindowText, TEXT),
                        (QPalette.ColorRole.ButtonText, TEXT), (QPalette.ColorRole.Highlight, FROST),
                        (QPalette.ColorRole.HighlightedText, NIGHT), (QPalette.ColorRole.ToolTipBase, RAISED),
                        (QPalette.ColorRole.ToolTipText, TEXT), (QPalette.ColorRole.PlaceholderText, '#6B7888'),
                        (QPalette.ColorRole.Mid, LINE), (QPalette.ColorRole.Dark, NIGHT)):
        pal.setColor(role, QColor(color))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor('#5C6878'))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor('#5C6878'))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor('#5C6878'))
    app.setPalette(pal)
    arrows = _arrow_images()
    app.setStyleSheet(QSS + f"""
QComboBox::down-arrow {{ image: url({arrows['down']}); width: 10px; height: 7px; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 26px; }}
QSpinBox {{ padding-right: 24px; }}
QSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right; width: 22px; }}
QSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right; width: 22px; }}
QSpinBox::up-arrow {{ image: url({arrows['up']}); width: 9px; height: 6px; }}
QSpinBox::down-arrow {{ image: url({arrows['down']}); width: 9px; height: 6px; }}
""")


# ---------------------------------------------------------------- castle table rendering
STATUS = {'UNPROTECTED': ('Unprotected', OPEN), 'PROTECTED': ('Protected', SHIELD),
          'FERVOR': ('Fervor', FERVOR), 'UNKNOWN': ('Fervor', FERVOR)}


class CastleDelegate(QStyledItemDelegate):
    """Status pills, dimmed protected rows, right-aligned numbers, formatted power."""

    def __init__(self, table, status_col, power_col, name_col, numeric_cols, delta_col=None):
        super().__init__(table)
        self.status_col, self.power_col, self.name_col = status_col, power_col, name_col
        self.delta_col = delta_col
        self.numeric = set(numeric_cols)

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        status = str(index.sibling(index.row(), self.status_col).data() or '').upper()
        col = index.column()
        rect = opt.rect
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        if selected:
            painter.fillRect(rect, QColor('#34506A'))
        elif opt.features & QStyleOptionViewItem.ViewItemFeature.Alternate:
            painter.fillRect(rect, QColor('#252E3A'))
        else:
            painter.fillRect(rect, QColor(PANEL))
        if status == 'UNPROTECTED' and col == 0:
            painter.fillRect(QRectF(rect.left(), rect.top() + 3, 3, rect.height() - 6), QColor(OPEN))
        text = str(index.data() or '')
        if col == self.status_col and status in STATUS:
            label, color = STATUS[status]
            f = QFont(opt.font); f.setPointSizeF(max(8.0, f.pointSizeF() - 1)); f.setWeight(QFont.Weight.DemiBold)
            fm = QFontMetrics(f)
            w, h = fm.horizontalAdvance(label) + 20, fm.height() + 6
            pill = QRectF(rect.left() + 10, rect.center().y() - h / 2 + 0.5, w, h)
            fill = QColor(color); fill.setAlpha(38)
            painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(fill)
            painter.drawRoundedRect(pill, h / 2, h / 2)
            painter.setPen(QColor(color)); painter.setFont(f)
            painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, label)
            painter.restore()
            return
        color = QColor(MUTED if status == 'PROTECTED' else TEXT)
        f = QFont(opt.font)
        if col == self.power_col:
            try:
                v = float(text)
            except ValueError:
                v = None
            if not v or v <= 0:
                text, color = '—', QColor('#5C6878')
            else:
                text = f'{v:,.1f}'
                if status != 'PROTECTED':
                    f.setWeight(QFont.Weight.DemiBold)
        if col == self.delta_col and text:
            color = QColor(OPEN if text.startswith('+') and text.strip('+0.,') else SHIELD if text.startswith('-') else MUTED)
        if col == self.name_col and status == 'UNPROTECTED':
            f.setWeight(QFont.Weight.DemiBold)
        painter.setFont(f)
        painter.setPen(color)
        align = (Qt.AlignmentFlag.AlignRight if col in self.numeric else Qt.AlignmentFlag.AlignLeft) | Qt.AlignmentFlag.AlignVCenter
        inner = rect.adjusted(12, 0, -12, 0)
        painter.drawText(inner, align, QFontMetrics(f).elidedText(text, Qt.TextElideMode.ElideRight, inner.width()))
        painter.restore()


def style_castle_table(table, status_col, power_col, name_col, numeric_cols, delta_col=None):
    table.setStyleSheet('')
    table.setShowGrid(False)
    table.setFrameShape(QFrame.Shape.NoFrame)
    table.verticalHeader().setDefaultSectionSize(34)
    table.verticalHeader().setMinimumSectionSize(34)
    table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    table.horizontalHeader().setHighlightSections(False)
    table.setItemDelegate(CastleDelegate(table, status_col, power_col, name_col, numeric_cols, delta_col))
    # fill_table() calls resizeRowsToContents(); keep the roomier fixed height instead.
    table.resizeRowsToContents = lambda: None


def style_plain_table(table):
    table.setStyleSheet('')
    table.setShowGrid(False)
    table.setFrameShape(QFrame.Shape.NoFrame)
    table.verticalHeader().setDefaultSectionSize(30)
    table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    table.horizontalHeader().setHighlightSections(False)


# ---------------------------------------------------------------- layout helpers
def _label(text, name=None):
    lab = QLabel(text)
    if name:
        lab.setObjectName(name)
    return lab


def _field(label, widget):
    box = QVBoxLayout(); box.setSpacing(4)
    lab = _label(label, 'hint')
    box.addWidget(lab); box.addWidget(widget)
    return box


class _FixedHeightBox(QWidget):
    """Container that hides its word-wrapped labels' height-for-width from the parent layout.

    Without it the sidebar scroll area sizes itself from preferred heights and shows a
    scrollbar even when everything fits.
    """

    def hasHeightForWidth(self):
        return False


def _rebuild_scanner_tab(w):
    tabs = w.tabs
    old = tabs.widget(0)
    title = tabs.tabText(0)
    page = QWidget()
    root = QHBoxLayout(page); root.setContentsMargins(16, 16, 16, 16); root.setSpacing(0)
    split = QSplitter(Qt.Orientation.Horizontal); split.setChildrenCollapsible(False)
    root.addWidget(split)

    # Sidebar: what to scan (one kingdom or a sweep), shared options, run controls, totals, log.
    side = QFrame(); side.setObjectName('sidebar'); side.setMinimumWidth(384); side.setMaximumWidth(460)
    side_outer = QVBoxLayout(side); side_outer.setContentsMargins(0, 0, 0, 0)
    scroll = QScrollArea(); scroll.setObjectName('sideScroll'); scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    inner = QWidget()
    s = QVBoxLayout(inner); s.setContentsMargins(16, 14, 16, 14); s.setSpacing(8)
    scroll.setWidget(inner)
    side_outer.addWidget(scroll)

    head = QHBoxLayout(); head.setSpacing(10)
    head.addWidget(_label('Scan', 'sectionTitle'))
    head.addStretch(1)
    seg = QFrame(); seg.setObjectName('segmented')
    seg_lay = QHBoxLayout(seg); seg_lay.setContentsMargins(3, 3, 3, 3); seg_lay.setSpacing(2)
    one_btn = QPushButton('One kingdom'); one_btn.setObjectName('segment'); one_btn.setCheckable(True)
    sweep_btn = QPushButton('Kingdom sweep'); sweep_btn.setObjectName('segment'); sweep_btn.setCheckable(True)
    group = QButtonGroup(seg); group.setExclusive(True)
    group.addButton(one_btn, 0); group.addButton(sweep_btn, 1)
    seg_lay.addWidget(one_btn); seg_lay.addWidget(sweep_btn)
    head.addWidget(seg)
    s.addLayout(head)
    w.scan_mode_switch = seg

    pages = QStackedWidget()
    # One kingdom (or a short list)
    p1 = QWidget(); g1 = QGridLayout(p1); g1.setContentsMargins(0, 0, 0, 0)
    g1.setHorizontalSpacing(10); g1.setVerticalSpacing(8)
    w.kingdom_range.setPlaceholderText('542 - 550  or  550, 542, 547')
    g1.addLayout(_field('Kingdom', w.kingdom), 0, 0)
    g1.addLayout(_field('Mode', w.mode), 0, 1)
    g1.addLayout(_field('Batch kingdoms (optional)', w.kingdom_range), 1, 0, 1, 2)
    pages.addWidget(p1)
    # Kingdom sweep
    p2 = QWidget(); g2 = QGridLayout(p2); g2.setContentsMargins(0, 0, 0, 0)
    g2.setHorizontalSpacing(10); g2.setVerticalSpacing(8)
    head = QHBoxLayout(); head.setSpacing(0)
    head.addWidget(_label('Kingdoms', 'hint')); head.addStretch(1)
    for b in (w.sweep_kind_range, w.sweep_kind_list):
        b.setObjectName('miniSegment'); head.addWidget(b)
    g2.addLayout(head, 0, 0, 1, 2)
    w.sweep_from.setPrefix('From  '); w.sweep_to.setPrefix('To  ')
    g2.addWidget(w.sweep_from, 1, 0); g2.addWidget(w.sweep_to, 1, 1)
    g2.addWidget(w.sweep_list, 1, 0, 1, 2)
    g2.addWidget(w.sweep_estimate, 2, 0, 1, 2)
    w.sweep_estimate.setObjectName('estimate')
    wait_field = _field('Wait between kingdoms', w.sweep_wait)
    wait_field.addWidget(w.sweep_wait_random)
    g2.addLayout(wait_field, 3, 0)
    skip_box = QVBoxLayout(); skip_box.setSpacing(4); skip_box.addWidget(_label(' ', 'hint'))
    w.sweep_skip.setText('Skip failed kingdoms')
    skip_box.addWidget(w.sweep_skip)
    g2.addLayout(skip_box, 3, 1)
    w._sweep_plain_from_label = w._sweep_plain_to_label = None  # the old box (and its labels) is discarded
    w._sweep_kind_changed(w.sweep_kind_group.checkedId())
    pages.addWidget(p2)
    s.addWidget(pages)

    opts = QGridLayout(); opts.setHorizontalSpacing(10); opts.setVerticalSpacing(8)
    opts.addLayout(_field('Console shows', w.show_filter), 0, 0)
    opts.addLayout(_field('Buffer', w.buffer), 0, 1)
    s.addLayout(opts)
    w.verbose_power.setText('Look up castle power (slower)')
    w.enable_sql.setText('Import into MySQL after scan')
    s.addWidget(w.verbose_power)
    s.addWidget(w.enable_sql)
    s.addWidget(w.discreet)
    s.addWidget(w.discreet_hint)

    run = QHBoxLayout(); run.setSpacing(8)
    for btn in (w.start_btn, w.sweep_start_btn):
        btn.setObjectName('primary'); btn.setMinimumHeight(40)
    w.stop_btn.setObjectName('danger'); w.stop_btn.setText('Stop'); w.stop_btn.setMinimumHeight(40)
    w.stop_btn.setToolTip('Stops the scan (in manual mode: finishes and saves it)')
    run.addWidget(w.start_btn, 3); run.addWidget(w.sweep_start_btn, 3)
    w.sweep_resume_btn.setObjectName('quiet'); w.sweep_resume_btn.setMinimumHeight(40)
    run.addWidget(w.sweep_resume_btn, 3)
    run.addWidget(w.stop_btn, 2)
    s.addLayout(run)

    sweep_box = _FixedHeightBox(); sb = QVBoxLayout(sweep_box); sb.setContentsMargins(0, 0, 0, 0); sb.setSpacing(6)
    sweep_box.setMinimumHeight(84)
    sb.addWidget(w.sweep_progress)
    w.sweep_state.setObjectName('sweepState')
    w.sweep_counts.setObjectName('sweepCounts')
    for lab in (w.sweep_state, w.sweep_counts, w.sweep_estimate):
        lab.setWordWrap(True)
        lab.setMinimumWidth(0)
    sb.addWidget(w.sweep_state)
    sb.addWidget(w.sweep_counts)
    sb.addStretch(1)
    s.addWidget(sweep_box)

    w.status.setObjectName('statusLine'); w.status.setWordWrap(True)
    status_box = _FixedHeightBox(); stl = QVBoxLayout(status_box); stl.setContentsMargins(0, 0, 0, 0)
    stl.addWidget(w.status); status_box.setMinimumHeight(38)
    s.addWidget(status_box)

    def show_mode(index):
        pages.setCurrentIndex(index)
        w.start_btn.setVisible(index == 0)
        w.sweep_start_btn.setVisible(index == 1)
        w.sweep_resume_btn.setVisible(index == 1 and getattr(w, '_resume_available', False))
        sweep_box.setVisible(index == 1)
        status_box.setVisible(index == 0)  # the sweep box shows its own progress text
        try:
            from PySide6.QtCore import QSettings
            QSettings('GOTWiC', 'Scanner').setValue('ui_scan_mode', index)
        except Exception:
            pass
    group.idClicked.connect(show_mode)

    def select_mode(index):
        group.button(index).setChecked(True)
        show_mode(index)
    w.select_scan_mode = select_mode
    try:
        from PySide6.QtCore import QSettings
        start_mode = QSettings('GOTWiC', 'Scanner').value('ui_scan_mode', 0, type=int)
    except Exception:
        start_mode = 0
    if w.sweep_resume_btn.isVisibleTo(w) or getattr(w, 'sweep', None) is not None:
        start_mode = 1
    start_mode = 1 if start_mode == 1 else 0
    group.button(start_mode).setChecked(True)
    show_mode(start_mode)

    sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine); sep.setStyleSheet(f'color: {LINE}; background: {LINE}; max-height: 1px;')
    s.addWidget(sep)
    s.addWidget(_label('Totals', 'sectionTitle'))
    w.stats_table.setMinimumHeight(98)
    w.stats_table.verticalHeader().setDefaultSectionSize(28)
    w.stats_table.verticalHeader().setMinimumSectionSize(28)
    w.stats_table.resizeRowsToContents = lambda: None  # keep compact fixed rows (GUI calls this on refresh)
    w.stats_table.setMaximumHeight(150)
    w.stats_table.setHorizontalHeaderLabels(['Kingdom', 'Unprotected', 'Protected', 'Total'])
    w.stats_table.setToolTip('Unprotected count includes Fervor castles in brackets')
    sh = w.stats_table.horizontalHeader()
    sh.setMinimumSectionSize(40)
    # Fixed widths: ResizeToContents re-measures every row on each insert (seconds with 550 kingdoms).
    sh.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed); w.stats_table.setColumnWidth(0, 76)
    sh.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed); w.stats_table.setColumnWidth(3, 62)
    sh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
    sh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
    style_plain_table(w.stats_table)
    w.stats_table.verticalHeader().setDefaultSectionSize(28)
    s.addWidget(w.stats_table)
    w._stats_stick = _StickToBottom(w.stats_table)
    w.stats_table.installEventFilter(w._stats_stick)
    s.addWidget(_label('Messages', 'sectionTitle'))
    w.console.setMaximumHeight(16777215)
    w.console.setMinimumHeight(72)
    s.addWidget(w.console, 1)
    split.addWidget(side)

    # Results: filters glued to the table, then power actions.
    res = QFrame(); res.setObjectName('results')
    r = QVBoxLayout(res); r.setContentsMargins(16, 14, 16, 14); r.setSpacing(10)
    bar = QHBoxLayout(); bar.setSpacing(8)
    bar.addWidget(_label('Live results', 'sectionTitle'))
    bar.addStretch(1)
    w.player.setPlaceholderText('Find player')
    w.player.setMinimumWidth(180)
    bar.addWidget(w.player)
    w.live_kingdom_filter.setMinimumWidth(130)
    bar.addWidget(w.live_kingdom_filter)
    w.live_status_filter.setMinimumWidth(130)
    w.live_status_filter.setToolTip('Show only castles with this status')
    bar.addWidget(w.live_status_filter)
    w.minimum_power.setToolTip('Hide castles below this power')
    bar.addWidget(w.minimum_power)
    w.unknown_toggle.setToolTip('Show only Fervor castles (status not confirmed)')
    bar.addWidget(w.unknown_toggle)
    r.addLayout(bar)
    r.addWidget(w.live_table, 1)
    actions = QHBoxLayout(); actions.setSpacing(8)
    w.live_power_btn.setText('Get power of selected castle')
    w.live_power_btn.setToolTip('Also works for shielded castles. Tip: double-click a row.')
    actions.addWidget(w.live_power_btn)
    actions.addStretch(1)
    actions.addWidget(_label('Fill missing power:', 'hint'))
    for btn, text in ((w.bulk_unprotected_btn, 'Unprotected'), (w.bulk_unknown_btn, 'Fervor'),
                      (w.bulk_protected_btn, 'Protected')):
        btn.setText(text); btn.setObjectName('quiet')
        btn.setToolTip(f'Look up power for every {text.lower()} castle in the latest scan that has none yet')
        actions.addWidget(btn)
    r.addLayout(actions)
    split.addWidget(res)
    split.setStretchFactor(0, 0); split.setStretchFactor(1, 1)
    split.setSizes([400, 880])

    tabs.removeTab(0)
    tabs.insertTab(0, page, 'Scanner' if 'Scanner' in title else title)
    tabs.setCurrentIndex(0)
    old.deleteLater()


class _ConnectionWatcher(QObject):
    """Header pill: is Chrome reachable on the debug port, and is the game tab open?"""

    def __init__(self, window, label):
        super().__init__(window)
        self.window, self.label = window, label
        self.state = ('warn', 'Checking Chrome…')
        self.busy = False
        self.timer = QTimer(self); self.timer.timeout.connect(self.tick); self.timer.start(3000)
        self.tick()

    def check(self, port):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/json', timeout=1.5) as r:
                pages = json.load(r)
            if any('gtarcade' in p.get('url', '').lower() for p in pages if p.get('type') == 'page'):
                self.state = ('ok', 'Game connected')
            else:
                self.state = ('warn', 'Chrome open, game tab not found')
        except Exception:
            self.state = ('off', f'Chrome not reachable on port {port}')
        finally:
            self.busy = False

    def tick(self):
        state, text = self.state
        self.label.setText('●  ' + text)
        if self.label.property('state') != state:
            self.label.setProperty('state', state)
            self.label.style().unpolish(self.label); self.label.style().polish(self.label)
        if not self.busy:
            self.busy = True
            port = self.window.port.value() if hasattr(self.window, 'port') else 9222
            threading.Thread(target=self.check, args=(port,), daemon=True).start()


def _add_header(w):
    tabs = w.centralWidget()
    shell = QWidget()
    v = QVBoxLayout(shell); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(0)
    head = QFrame(); head.setObjectName('header')
    h = QHBoxLayout(head); h.setContentsMargins(20, 14, 20, 6); h.setSpacing(10)
    h.addWidget(_label('GOTWiC Scanner', 'appTitle'))
    version = w.windowTitle().rsplit('v', 1)[-1] if ' v' in w.windowTitle() else ''
    h.addWidget(_label(f'v{version}' if version else '', 'appVersion'))
    h.addStretch(1)
    conn = _label('●  Checking Chrome…', 'connection')
    conn.setToolTip('The scanner needs Chrome opened by RUN_MAC.command with the game map loaded')
    h.addWidget(conn)
    v.addWidget(head)
    w.takeCentralWidget()
    v.addWidget(tabs, 1)
    tabs.setDocumentMode(True)
    w.setCentralWidget(shell)
    w._connection_watcher = _ConnectionWatcher(w, conn)


def polish_main_window(w):
    if getattr(w, '_gotwic_polished', False):
        return
    w._gotwic_polished = True
    # The unthemed layout's minimum height (all scanner widgets stacked) can force the window taller
    # before this runs; the rearranged layout fits in 820 px, so size it by intent, not by that minimum.
    screen = w.screen().availableGeometry() if w.screen() else None
    target = (max(1280, w.width()), 820)
    if screen is not None:
        target = (min(target[0], screen.width()), min(target[1], screen.height()))
    try:
        _rebuild_scanner_tab(w)
    except Exception as exc:  # never block the app because of cosmetics
        print(f'Theme: scanner layout skipped ({exc})')
    # Live results: Kingdom, Castle, X, Y, Truce Status, Power (M)
    style_castle_table(w.live_table, status_col=4, power_col=5, name_col=1, numeric_cols=(2, 3, 5))
    w.live_table.setColumnWidth(0, 84); w.live_table.setColumnWidth(2, 64); w.live_table.setColumnWidth(3, 64)
    w.live_table.setColumnWidth(4, 140); w.live_table.setColumnWidth(5, 110)
    hdr = w.live_table.horizontalHeader()
    hdr.setMinimumSectionSize(56)
    # Explorer: Castle, X, Y, Truce Status, Power (M), last_seen_utc
    style_castle_table(w.castles_table, status_col=3, power_col=4, name_col=0, numeric_cols=(1, 2, 4))
    from PySide6.QtWidgets import QTableWidgetItem
    w.castles_table.setHorizontalHeaderItem(5, QTableWidgetItem('Last seen (UTC)'))
    w.castles_table.horizontalHeader().setMinimumSectionSize(56)
    ch = w.castles_table.horizontalHeader()
    ch.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    ch.setStretchLastSection(False)
    for col, width in ((1, 64), (2, 64), (3, 140), (4, 110), (5, 200)):
        w.castles_table.setColumnWidth(col, width)
    w.get_power_btn.setText('Get power of selected castle')
    w.get_power_btn.setMaximumWidth(260)
    w.summary.setObjectName('hint')
    w.search.setPlaceholderText('Search player, ID, status or coordinate')
    for t in (w.scan_table, w.move_table):
        style_plain_table(t)
    for tab_index in range(1, w.tabs.count()):
        page = w.tabs.widget(tab_index)
        if page.layout():
            page.layout().setContentsMargins(18, 16, 18, 16)
            page.layout().setSpacing(10)
    if hasattr(w, 'targets_table'):
        style_castle_table(w.targets_table, status_col=5, power_col=6, name_col=1,
                           numeric_cols=(2, 3, 4, 6, 7, 8, 9), delta_col=7)
        w.targets_summary.setObjectName('hint')
        w.targets_table.setColumnWidth(0, 84)
        for b in (w.targets_copy_btn, w.targets_refresh_btn):
            b.setObjectName('quiet')
    _tidy_settings(w)
    for i, text in enumerate(('Scanner', 'Targets', 'Castle explorer', 'Scans && moves', 'Settings')):
        if i < w.tabs.count():
            w.tabs.setTabText(i, text)
    _add_header(w)
    w.setMinimumSize(1080, 680)
    # Re-arranging widgets during the first Show makes Qt adopt the new size hint; restore the intended size.
    w.resize(*target)
    QTimer.singleShot(0, lambda: w.resize(*target))
    QTimer.singleShot(250, lambda: w.resize(*target))


def _tidy_settings(w):
    from PySide6.QtWidgets import QPushButton, QScrollArea
    idx = w.tabs.count() - 1
    page = w.tabs.widget(idx)
    for btn in page.findChildren(QPushButton):
        if btn.text() == 'Delete all CSV files':
            btn.setObjectName('danger')
        btn.setMaximumWidth(260)
    for lab in page.findChildren(QLabel):
        if lab.text().startswith(('Applies to', '0 = keep', 'SQL import is enabled')):
            lab.setObjectName('hint'); lab.setWordWrap(True)
    page.setMaximumWidth(760)
    holder = QWidget()
    lay = QHBoxLayout(holder); lay.setContentsMargins(0, 0, 0, 0)
    title = w.tabs.tabText(idx)
    w.tabs.removeTab(idx)
    lay.addWidget(page, 1); lay.addStretch(0)
    page.setVisible(True)
    scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setStyleSheet(f'QScrollArea, QScrollArea > QWidget > QWidget {{ background: {NIGHT}; }}')
    scroll.setWidget(holder)
    w.tabs.insertTab(idx, scroll, title)


class _StickToBottom(QObject):
    """Keep the TOTAL row (last) visible when the Totals table changes size."""

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, obj.scrollToBottom)
        return False


class _MainWindowWatcher(QObject):
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Show and type(obj).__name__ == 'MainWindow':
            polish_main_window(obj)
        return False


def install(app):
    apply_theme(app)
    watcher = _MainWindowWatcher(app)
    app.installEventFilter(watcher)
    app._gotwic_theme_watcher = watcher
