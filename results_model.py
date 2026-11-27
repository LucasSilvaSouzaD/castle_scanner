"""Fast castle tables (Qt model/view) for the live results and the Targets tab.

A QTableWidget rebuilds one QTableWidgetItem per cell on every refresh, which takes seconds
once a sweep has collected ~100,000 castles. This model keeps plain dicts and only tells the
view what changed (rows appended, values updated), so tables stay responsive at any size.
Sorting is done by the model with one list.sort (fast), keeping the selection on the same rows.
"""
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableView

SORT_ROLE = Qt.ItemDataRole.UserRole + 1
RECORD_ROLE = Qt.ItemDataRole.UserRole + 2
STATUS_ORDER = {'UNPROTECTED': 0, 'UNKNOWN': 1, 'FERVOR': 1, 'PROTECTED': 2}
STATUS_TEXT = {'UNKNOWN': 'Fervor'}
STATUS_COLOR = {'PROTECTED': '#ff7777', 'UNPROTECTED': '#78e08f', 'UNKNOWN': '#ffd166'}


def _num(value, default=None):
    try:
        if value is None or str(value).strip() == '':
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


class Column:
    """key: record field; kind: text | int | power | status | delta; fmt: optional callable(value, record)."""

    def __init__(self, key, title, kind='text', fmt=None, align_right=None, tooltip=None):
        self.key, self.title, self.kind, self.fmt, self.tooltip = key, title, kind, fmt, tooltip
        self.align_right = kind in ('int', 'power', 'delta') if align_right is None else align_right


class CastleModel(QAbstractTableModel):
    def __init__(self, columns, parent=None):
        super().__init__(parent)
        self.columns = list(columns)
        self.rows = []

    # --- Qt API
    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal:
            if role == Qt.ItemDataRole.DisplayRole:
                return self.columns[section].title
            if role == Qt.ItemDataRole.ToolTipRole:
                return self.columns[section].tooltip
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        rec = self.rows[index.row()]
        col = self.columns[index.column()]
        value = rec.get(col.key)
        if role == Qt.ItemDataRole.DisplayRole:
            if col.fmt is not None:
                return col.fmt(value, rec)
            if col.kind == 'status':
                s = str(value or '').upper()
                return STATUS_TEXT.get(s, s)
            if col.kind == 'power':
                v = _num(value, 0.0)
                return f'{v:.3f}' if v else '0'
            if col.kind == 'delta':
                v = _num(value)
                return '' if v is None else f'{v:+,.1f}'
            if col.kind == 'int':
                v = _num(value)
                return '' if v is None else f'{int(v)}'
            return '' if value is None else str(value)
        if role == SORT_ROLE:
            if col.kind == 'status':
                return STATUS_ORDER.get(str(value or '').upper(), 3)
            if col.kind in ('int', 'power', 'delta'):
                return _num(value, -1e18)
            return str(value or '').casefold()
        if role == RECORD_ROLE:
            return rec
        if role == Qt.ItemDataRole.TextAlignmentRole:
            h = Qt.AlignmentFlag.AlignRight if col.align_right else Qt.AlignmentFlag.AlignLeft
            return int(h | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ForegroundRole:  # plain (unthemed) look, same colours as before
            color = STATUS_COLOR.get(str(rec.get('status') or '').upper())
            return QBrush(QColor(color)) if color else None
        return None

    # --- sorting (done here, not by a proxy: list.sort is C-speed; a proxy calls data() n·log n times)
    sort_column = None
    sort_descending = True

    def _sort_key(self):
        col = self.columns[self.sort_column]
        key, kind = col.key, col.kind
        if kind == 'status':
            return lambda r: STATUS_ORDER.get(str(r.get(key) or '').upper(), 3)
        if kind in ('int', 'power', 'delta'):
            def k(r, key=key):
                v = r.get(key)
                if isinstance(v, (int, float)):
                    return v
                return _num(v, -1e18)
            return k
        return lambda r: str(r.get(key) or '').casefold()

    def _sort_in_place(self):
        if self.sort_column is not None and 0 <= self.sort_column < len(self.columns):
            self.rows.sort(key=self._sort_key(), reverse=self.sort_descending)

    def _relayout(self, mutate):
        """Run mutate() (which reorders/extends self.rows) and keep selection on the same records."""
        self.layoutAboutToBeChanged.emit()
        old = self.persistentIndexList()
        held = [(i, self.rows[i.row()] if 0 <= i.row() < len(self.rows) else None) for i in old]
        mutate()
        if old:
            where = {id(r): n for n, r in enumerate(self.rows)}
            new = [self.index(where[id(rec)], i.column()) if rec is not None and id(rec) in where else QModelIndex()
                   for i, rec in held]
            self.changePersistentIndexList(old, new)
        self.layoutChanged.emit()

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        self.sort_column = column
        self.sort_descending = order == Qt.SortOrder.DescendingOrder
        self._relayout(self._sort_in_place)

    # --- bulk operations
    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = list(rows)
        self._sort_in_place()
        self.endResetModel()

    def append_rows(self, rows):
        rows = list(rows)
        if not rows:
            return
        if self.sort_column is None:
            first = len(self.rows)
            self.beginInsertRows(QModelIndex(), first, first + len(rows) - 1)
            self.rows.extend(rows)
            self.endInsertRows()
            return
        def mutate():
            self.rows.extend(rows)
            self._sort_in_place()  # timsort: already-sorted run + a short new run = nearly linear
        self._relayout(mutate)

    def resort(self):
        """Values used for sorting changed (e.g. power arrived)."""
        self._relayout(self._sort_in_place)

    def rows_changed(self, first=0, last=None):
        if not self.rows:
            return
        last = len(self.rows) - 1 if last is None else last
        self.dataChanged.emit(self.index(first, 0), self.index(last, len(self.columns) - 1))


def make_view(model, sort_column, descending=True, parent=None):
    view = QTableView(parent)
    view.setModel(model)
    view.setSortingEnabled(True)
    view.sortByColumn(sort_column, Qt.SortOrder.DescendingOrder if descending else Qt.SortOrder.AscendingOrder)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setAlternatingRowColors(True)
    view.setWordWrap(False)
    view.verticalHeader().setVisible(False)
    view.verticalHeader().setDefaultSectionSize(27)
    view.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
    header = view.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.setMinimumSectionSize(56)
    font = header.font(); font.setBold(True); header.setFont(font)
    view.setStyleSheet("QTableView { gridline-color: #d6dbe1; font-size: 10pt; }"
                       "QHeaderView::section { padding: 6px; font-weight: 600; }")
    return view, model


def selected_record(view):
    """The record (dict) of the selected row, or None."""
    sm = view.selectionModel()
    if sm is None:
        return None
    rows = sm.selectedRows()
    index = rows[0] if rows else sm.currentIndex()
    return index.data(RECORD_ROLE) if index.isValid() else None


def record_at(view, index):
    return index.data(RECORD_ROLE) if index is not None and index.isValid() else None
