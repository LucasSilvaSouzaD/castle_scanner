#!/usr/bin/env python3
# GOTWiC Scanner GUI package release: 1.0.9.22
"""GOTWiC Scanner desktop GUI. Runs the original scanner as a separate process."""
import csv
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import threading
from web_upload import upload_csv, UPLOAD_ENDPOINT
from discord_webhook import validate_webhook, send_kingdom_report, send_test
from gotwic_scanner_v2_2 import ignored_castle
from power_timing import PROFILES, DEFAULT_PROFILE
from csv_storage import CSV_DIR, migrate_root_csv
from results_model import CastleModel, Column, make_view, selected_record, record_at
import history_db

from PySide6.QtCore import Qt, QTimer, QSettings
from PySide6.QtGui import QColor, QBrush
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QMenu,
    QPushButton, QSpinBox, QTabWidget, QTableWidget, QTableWidgetItem, QTextEdit,
    QVBoxLayout, QWidget, QHeaderView, QProgressBar)
import time
import shutil
import random
import urllib.request

BASE = Path(__file__).resolve().parent
SCANNER = BASE / 'gotwic_scanner_v2_2.py'
COLUMNS = ['kingdom','x','y','map_id','name','player_id','status','status_source','last_status_utc','last_seen_utc','observations']

_SNAPSHOT_CACHE = {}  # path -> (mtime_ns, size, rows); unchanged CSVs are parsed only once


def read_snapshots(folder):
    scans = []
    seen = set()
    for path in sorted(folder.glob('*.csv'), reverse=True):
        if not re.match(r'^\d+_\d{8}_\d{6}_\d{6}(?:_migrated_\d+)?\.csv$', path.name):
            continue
        seen.add(path)
        try:
            st = path.stat()
            cached = _SNAPSHOT_CACHE.get(path)
            if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
                if cached[2] is not None:
                    scans.append((path, cached[2]))
                continue
        except OSError:
            continue
        _SNAPSHOT_CACHE[path] = (st.st_mtime_ns, st.st_size, None)
        try:
            with path.open(encoding='utf-8-sig', newline='') as f:
                reader = csv.DictReader(f)
                if not set(COLUMNS).issubset(reader.fieldnames or []):
                    continue
                rows = list(reader)
            stamps = {r['last_seen_utc'] for r in rows}
            if len(stamps) > 1:
                continue
            _SNAPSHOT_CACHE[path] = (st.st_mtime_ns, st.st_size, rows)
            scans.append((path, rows))
        except (OSError, UnicodeError, csv.Error):
            continue
    for stale in [p for p in _SNAPSHOT_CACHE if p.parent == folder and p not in seen]:
        _SNAPSHOT_CACHE.pop(stale, None)
    return scans

EXPLORER_MAX_ROWS = 20000  # table rows rendered at once in Castle Explorer
_COUNT_CACHE = {}  # id(rows) -> (rows, (unprotected, protected, unknown))


def _snapshot_counts(rows):
    hit = _COUNT_CACHE.get(id(rows))
    if hit is not None and hit[0] is rows:
        return hit[1]
    counts = [0, 0, 0]
    for r in rows:
        st = r['status']
        if st == 'UNPROTECTED': counts[0] += 1
        elif st == 'PROTECTED': counts[1] += 1
        elif st == 'UNKNOWN': counts[2] += 1
    _COUNT_CACHE[id(rows)] = (rows, tuple(counts))
    return tuple(counts)


def movements(scans):
    """Compare consecutive snapshots of each kingdom; never infer moves from absence."""
    by_kingdom = {}
    for path, rows in reversed(scans):
        if rows:
            kingdom = rows[0]['kingdom']
        else:
            kingdom = path.name.split('_')[0]
        by_kingdom.setdefault(kingdom, []).append((path, rows))
    found = []
    for kingdom, snapshots in by_kingdom.items():
        previous = None
        for path, rows in snapshots:
            current = {r['player_id']: r for r in rows if r['player_id'].strip() and r['x'] and r['y']}
            if previous is not None:
                for pid, row in current.items():
                    old = previous.get(pid)
                    if old and (old['x'],old['y']) != (row['x'],row['y']):
                        found.append([kingdom, row['name'], pid, f"{old['x']},{old['y']}",
                                      f"{row['x']},{row['y']}", path.name])
            previous = current
    return list(reversed(found))


def parse_kingdom_selection(value):
    """Validate a single kingdom, inclusive ascending range, or 2–3 unique comma-separated kingdoms."""
    value = value.strip()
    if re.fullmatch(r'[0-9]+', value):
        number = int(value)
        if 1 <= number <= 999999:
            return [number]
        raise ValueError('Kingdom must be between 1 and 999999.')
    if ',' in value:
        parts = value.split(',')
        if not 2 <= len(parts) <= 3 or any(not re.fullmatch(r'\s*[0-9]+\s*', p) for p in parts):
            raise ValueError('Use 2 or 3 comma-separated kingdom numbers, e.g. 550, 542, 547.')
        kingdoms = [int(p.strip()) for p in parts]
        if any(not 1 <= k <= 999999 for k in kingdoms):
            raise ValueError('Every kingdom must be between 1 and 999999.')
        if len(set(kingdoms)) != len(kingdoms):
            raise ValueError('Duplicate kingdoms are not allowed.')
        return kingdoms
    match = re.fullmatch(r'([0-9]+)\s*-\s*([0-9]+)', value)
    if match:
        first, last = map(int, match.groups())
        if not (1 <= first < last <= 999999):
            raise ValueError('Ending kingdom must be greater than starting kingdom (1..999999).')
        return list(range(first, last + 1))
    raise ValueError('Enter a single kingdom, an ascending range (542 - 550), or up to 3 comma-separated kingdoms.')

SWEEP_MAX_KINGDOMS = 5000


def _as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_kingdom_list(text):
    """'100, 200, 300, 401-403' -> [100, 200, 300, 401, 402, 403] (your order, duplicates removed)."""
    out, seen = [], set()
    for raw in re.split(r'[,;\s]+', str(text or '').strip()):
        if not raw:
            continue
        m = re.fullmatch(r'(\d+)(?:-(\d+))?', raw)
        if not m:
            raise ValueError(f'Can\'t read "{raw}". Use numbers and ranges, e.g. 100, 200, 401-403.')
        a = int(m.group(1)); b = int(m.group(2) or a)
        if a < 1 or b < 1 or a > 999999 or b > 999999:
            raise ValueError('Kingdoms go from 1 to 999999.')
        if b < a:
            raise ValueError(f'"{raw}": the range must go up, e.g. {b}-{a}.')
        if len(out) + (b - a + 1) > SWEEP_MAX_KINGDOMS:
            raise ValueError(f'At most {SWEEP_MAX_KINGDOMS:,} kingdoms per sweep.')
        for k in range(a, b + 1):
            if k not in seen:
                seen.add(k); out.append(k)
    if not out:
        raise ValueError('Type at least one kingdom, e.g. 100, 200, 401-403.')
    return out


DISCREET_WAIT = (5, 15)          # seconds between two kingdoms, drawn at random
DISCREET_BREAK_EVERY = (18, 30)  # a short break after this many kingdoms
DISCREET_BREAK = (40, 100)       # length of that break, in seconds
DISCREET_HINT = ('Random map route and irregular pace. Sweeps also go in random order, '
                 'wait 5–15 s between kingdoms and take a short break now and then.')


def describe_kingdoms(kingdoms, limit=6):
    """[100, 200, 401, 402, 403] -> '100, 200, 401-403' (compact, for messages)."""
    parts, i = [], 0
    while i < len(kingdoms):
        j = i
        while j + 1 < len(kingdoms) and kingdoms[j + 1] == kingdoms[j] + 1:
            j += 1
        parts.append(str(kingdoms[i]) if i == j else f'{kingdoms[i]}-{kingdoms[j]}')
        i = j + 1
    return ', '.join(parts[:limit]) + ('…' if len(parts) > limit else '')


class NumericItem(QTableWidgetItem):
    """Sort numeric table cells by value while displaying the original text."""
    def __lt__(self, other):
        try:
            left = float(self.data(Qt.ItemDataRole.UserRole))
            right = float(other.data(Qt.ItemDataRole.UserRole))
            return left < right
        except (TypeError, ValueError):
            return super().__lt__(other)


class TruceItem(QTableWidgetItem):
    def __lt__(self, other):
        order = {'UNPROTECTED':0, 'UNKNOWN':1, 'PROTECTED':2}
        return order.get(self.text().upper(),3) < order.get(other.text().upper(),3)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f'GOTWiC Scanner — Desktop Manager — v{APP_VERSION}')
        self.resize(1150, 780)
        self.proc = None
        self.batch_kingdoms = []
        self.batch_completed = 0
        self.scan_config = None
        self.current_scan_kingdom = None
        self.messages = queue.Queue()
        self.scans = []
        self.latest = []
        self.active_scan_csv = None  # Exact CSV emitted by the latest completed GUI scan.
        self.pending_scan_csv = None
        self.live_results = []          # every castle of the current run (all kingdoms of a sweep)
        self.live_index = {}            # (kingdom, x, y, name) -> record
        self.live_pending = []          # new records not yet shown
        self.live_dirty_full = False    # a full re-filter is needed (status changed, filter changed)
        self.upload_pending = 0
        self.upload_queue = queue.Queue()
        self.upload_worker = None
        self.upload_last_csv = None
        # Session-only alliance tags; never persisted to scanner output files.
        self.gui_alliances = {}
        self.power_proc = None
        self.power_target = None
        self.bulk_power_queue = []
        self.bulk_power_total = 0
        self.bulk_power_done = 0
        self.bulk_power_failed = 0
        self.bulk_power_saved = 0
        self.bulk_power_csv = None
        self.bulk_power_status = None
        self.bulk_worker = None
        self.manual_power_reports = []
        self.bulk_messages = queue.Queue()
        # KINGDOM SWEEP state (see the sweep_* methods below).
        self.sweep = None
        self.kingdom_totals = {}          # kingdom -> [unprotected, protected, total, unknown] of finished kingdoms
        self.caffeinate_proc = None
        self.data_dirty = False
        self.last_data_refresh = 0.0
        self._movement_cache = None
        self.live_refresh_timer = QTimer(self)
        self.live_refresh_timer.setSingleShot(True)
        self.live_refresh_timer.setInterval(350)
        self.live_refresh_timer.timeout.connect(self._flush_live)
        self.sweep_retry_timer = QTimer(self)  # "waiting for a power lookup" re-check; stopped by _finish_sweep
        self.sweep_retry_timer.setSingleShot(True)
        self.sweep_retry_timer.setInterval(2000)
        self.sweep_retry_timer.timeout.connect(lambda: self._sweep_launch_next())
        self.sweep_wait_timer = QTimer(self)
        self.sweep_wait_timer.setInterval(250)
        self.sweep_wait_timer.timeout.connect(self._sweep_wait_tick)
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.build_scanner()
        self.console.document().setMaximumBlockCount(3000)  # bounded log over multi-hour sweeps
        self.build_explorer()
        self.build_history()
        self.build_settings()
        self.history = None
        self.targets_dirty = True
        try:
            self.history = history_db.History()
        except Exception as exc:  # the scanner must keep working without the history
            self.history = None
            self._history_error = str(exc)
        self.build_targets()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(150)
        moved, errors = migrate_root_csv()
        if moved:
            self.console.append(f'CSV migration: moved {len(moved)} file(s) into {CSV_DIR}')
        for error in errors:
            self.console.append('CSV migration warning: ' + error)
        self.cleanup_expired_csv()
        self.refresh_data()
        self.check_sql_on_startup()
        self.tabs.currentChanged.connect(self._refresh_data_if_dirty)
        self.tabs.currentChanged.connect(lambda *_: self._targets_maybe_refresh())
        if self.history is not None:
            folder = Path(self.folder.text())
            def import_old_snapshots():
                try:
                    keep = self.web_settings.value('csv_retention_days', 14, type=int)
                    self.messages.put(('history_import', history_db.import_folder(folder, keep_days=keep)))
                except Exception as exc:
                    self.messages.put(('history_import', (0, 0, str(exc))))
            threading.Thread(target=import_old_snapshots, daemon=True).start()
        self._sweep_offer_resume()

    def check_sql_on_startup(self):
        """Check actual database access without blocking the GUI startup."""
        self.enable_sql.setChecked(False)
        self.enable_sql.setEnabled(False)
        self.enable_sql.setToolTip('Checking MySQL connection...')
        def worker():
            try:
                import pymysql
                conn = pymysql.connect(
                    host=os.getenv('GOTWIC_DB_HOST', '127.0.0.1'),
                    port=int(os.getenv('GOTWIC_DB_PORT', '3306')),
                    user=os.getenv('GOTWIC_DB_USER', 'root'),
                    password=os.getenv('GOTWIC_DB_PASSWORD', ''),
                    database=os.getenv('GOTWIC_DB_NAME') or 'scanner_results',
                    connect_timeout=3, read_timeout=3, write_timeout=3)
                try:
                    with conn.cursor() as cursor:
                        cursor.execute('SELECT 1')
                        cursor.fetchone()
                finally:
                    conn.close()
                self.messages.put(('sql_check', (True, 'SQL connected — import enabled')))
            except Exception as exc:
                self.messages.put(('sql_check', (False, f'SQL unavailable — CSV only ({exc})')))
        threading.Thread(target=worker, daemon=True).start()

    def build_scanner(self):
        tab = QWidget(); layout = QVBoxLayout(tab)
        controls = QGroupBox('Scan configuration'); form = QGridLayout(controls)
        self.kingdom = QSpinBox(); self.kingdom.setRange(1,999999)
        last_kingdom = QSettings('GOTWiC', 'Scanner').value('last_successful_kingdom', 1, type=int)
        self.kingdom.setValue(last_kingdom if 1 <= last_kingdom <= 999999 else 1)
        self.kingdom_range = QLineEdit(); self.kingdom_range.setPlaceholderText('e.g. 542 - 550 or 550, 542, 547 (max 3)')
        self.mode = QComboBox(); self.mode.addItems(['auto','manual'])
        self.show_filter = QComboBox(); self.show_filter.addItems(['ALL','OPEN'])
        self.player = QLineEdit(); self.player.setPlaceholderText('Filter current scan by player name'); self.player.textChanged.connect(self.refresh_live_table)
        self.minimum_power = QComboBox()
        self.minimum_power.addItem('ALL (no minimum)', 0.0)
        for millions in (1000, 1250, 1500, 1750, 2000):
            self.minimum_power.addItem(f'{millions / 1000:.3f} billion', float(millions))
        self.minimum_power.currentIndexChanged.connect(self.refresh_live_table)
        self.buffer = QSpinBox(); self.buffer.setRange(1,48); self.buffer.setValue(24)
        self.verbose_power = QCheckBox('Castle power (verbose) — slower scan')
        self.verbose_power.toggled.connect(lambda *_: getattr(self, 'sweep_estimate', None) and self._update_sweep_estimate())
        self.enable_sql = QCheckBox('SQL import (automatic after scan)')
        self.enable_sql.setChecked(False)
        self.enable_sql.setEnabled(False)
        self.enable_sql.setToolTip('SQL import activates automatically when the database is reachable.')
        form.addWidget(QLabel('Kingdom'),0,0); form.addWidget(self.kingdom,0,1)
        form.addWidget(QLabel('Batch kingdoms'),0,2); form.addWidget(self.kingdom_range,0,3)
        form.addWidget(QLabel('Mode'),0,4); form.addWidget(self.mode,0,5)
        form.addWidget(QLabel('Show'),1,0); form.addWidget(self.show_filter,1,1)
        form.addWidget(QLabel('Player'),1,2); form.addWidget(self.player,1,3)
        form.addWidget(QLabel('Buffer'),2,0); form.addWidget(self.buffer,2,1)
        form.addWidget(QLabel('Minimum Power'),2,2); form.addWidget(self.minimum_power,2,3)
        form.addWidget(self.verbose_power,3,0,1,2)
        form.addWidget(self.enable_sql,3,2,1,2)
        self.discreet = QCheckBox('Discreet mode')
        self.discreet.setChecked(QSettings('GOTWiC', 'Scanner').value('discreet_mode', False, type=bool))
        self.discreet.setToolTip('Makes scanning less regular: ' + DISCREET_HINT[0].lower() + DISCREET_HINT[1:]
                                 + '\nThe same map points are covered, so results are identical. Slower.')
        self.discreet_hint = QLabel(DISCREET_HINT)
        self.discreet_hint.setObjectName('discreetHint')
        self.discreet_hint.setWordWrap(True)
        self.discreet.toggled.connect(self._discreet_toggled)
        form.addWidget(self.discreet,4,0,1,2)
        form.addWidget(self.discreet_hint,4,2,1,4)
        layout.addWidget(controls)
        self.build_sweep_controls(layout)
        buttons = QHBoxLayout()
        self.start_btn = QPushButton('Start scan'); self.start_btn.clicked.connect(self.start_scan)
        self.stop_btn = QPushButton('Finish manual scan / Stop'); self.stop_btn.clicked.connect(self.stop_scan); self.stop_btn.setEnabled(False)
        buttons.addWidget(self.start_btn); buttons.addWidget(self.stop_btn)
        layout.addLayout(buttons)
        self.status = QLabel('Idle — open Chrome with remote debugging and load the game map.')
        layout.addWidget(self.status)
        live_header = QHBoxLayout()
        live_header.addWidget(QLabel('Live castle results — protected: light red; unprotected: light green'))
        live_header.addStretch()
        self.live_kingdom_filter = QComboBox()
        self.live_kingdom_filter.addItem('All kingdoms', None)
        self.live_kingdom_filter.setToolTip('Show the results of one kingdom of this run')
        self.live_kingdom_filter.currentIndexChanged.connect(self.refresh_live_table)
        live_header.addWidget(self.live_kingdom_filter)
        live_header.addWidget(QLabel('Display'))
        self.live_status_filter = QComboBox()
        self.live_status_filter.addItems(['ALL', 'PROTECTED', 'UNPROTECTED', 'Fervor'])
        self.live_status_filter.currentTextChanged.connect(self.refresh_live_table)
        live_header.addWidget(self.live_status_filter)
        self.unknown_toggle = QPushButton('Fervor')
        self.unknown_toggle.setToolTip('Show castles with underlying UNKNOWN status (Fervor)')
        self.unknown_toggle.setCheckable(True)
        self.unknown_toggle.clicked.connect(self.toggle_unknown_filter)
        self.live_status_filter.currentIndexChanged.connect(self.sync_unknown_toggle)
        live_header.addWidget(self.unknown_toggle)
        layout.addLayout(live_header)
        self.live_model = CastleModel([
            Column('kingdom', 'Kingdom', 'int', align_right=False), Column('display', 'Castle'),
            Column('x', 'X', 'int'), Column('y', 'Y', 'int'),
            Column('status', 'Truce Status', 'status'), Column('power', 'Power (M)', 'power')], self)
        self.live_table, self.live_proxy = make_view(self.live_model, 5, True, self)
        self.live_table.horizontalHeader().setStretchLastSection(False)
        self.live_table.setColumnWidth(0, 95)
        self.live_table.setColumnWidth(1, 200)
        self.live_table.setColumnWidth(2, 90)
        self.live_table.setColumnWidth(3, 90)
        self.live_table.setColumnWidth(4, 145)
        self.live_table.setColumnWidth(5, 115)
        self.live_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        results_row = QHBoxLayout()
        results_row.addWidget(self.live_table, 1)
        stats_box = QGroupBox('Scan Statistics')
        stats_box.setMinimumWidth(315)
        stats_layout = QVBoxLayout(stats_box)
        self.stats_table = QTableWidget(0, 4)
        self.stats_table.setHorizontalHeaderLabels(['Kingdom', 'Untruced', 'Truced', 'Total'])
        self.stats_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.stats_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.stats_table.verticalHeader().setVisible(False)
        self.stats_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.stats_table.setAlternatingRowColors(True)
        stats_layout.addWidget(self.stats_table)
        results_row.addWidget(stats_box)
        layout.addLayout(results_row, 3)
        self.live_table.doubleClicked.connect(lambda index: self.request_power_at_live_row(index.row(), index.column()))
        self.live_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.live_table.customContextMenuRequested.connect(self.show_live_context_menu)
        self.live_power_btn = QPushButton('Get Power for selected castle (including truced)')
        self.live_power_btn.clicked.connect(self.request_power_from_live)
        layout.addWidget(self.live_power_btn)
        bulk_row = QHBoxLayout()
        self.bulk_protected_btn = QPushButton('Get Power — PROTECTED (missing only)')
        self.bulk_unprotected_btn = QPushButton('Get Power — UNPROTECTED (missing only)')
        self.bulk_unknown_btn = QPushButton('Get Power — Fervor (missing only)')
        self.bulk_protected_btn.clicked.connect(lambda: self.start_bulk_power('PROTECTED'))
        self.bulk_unprotected_btn.clicked.connect(lambda: self.start_bulk_power('UNPROTECTED'))
        self.bulk_unknown_btn.clicked.connect(lambda: self.start_bulk_power('UNKNOWN'))
        bulk_row.addWidget(self.bulk_protected_btn)
        bulk_row.addWidget(self.bulk_unprotected_btn)
        bulk_row.addWidget(self.bulk_unknown_btn)
        layout.addLayout(bulk_row)
        self.console = QTextEdit(); self.console.setReadOnly(True)
        self.console.setMaximumHeight(155)
        layout.addWidget(QLabel('Scanner messages'))
        layout.addWidget(self.console, 1)
        self.tabs.addTab(tab,'Scanner & Console')

    def build_sweep_controls(self, layout):
        """KINGDOM SWEEP: scan a whole range of kingdoms, one at a time, with a pause in between."""
        settings = QSettings('GOTWiC', 'Scanner')
        self.sweep_group = QGroupBox('Kingdom sweep')
        grid = QGridLayout(self.sweep_group)
        self.sweep_from = QSpinBox(); self.sweep_from.setRange(1, 999999)
        self.sweep_to = QSpinBox(); self.sweep_to.setRange(1, 999999)
        self.sweep_from.setValue(settings.value('sweep_ui_from', 1, type=int))
        self.sweep_to.setValue(settings.value('sweep_ui_to', 550, type=int))
        self.sweep_wait = QSpinBox(); self.sweep_wait.setRange(0, 120); self.sweep_wait.setSuffix(' s')
        self.sweep_wait.setValue(settings.value('sweep_ui_wait', 5, type=int))
        self.sweep_wait.setToolTip('Pause between two kingdoms')
        self.sweep_skip = QCheckBox('Skip failed kingdoms (retry once)')
        self.sweep_skip.setChecked(settings.value('sweep_ui_skip', True, type=bool))
        self.sweep_skip.setToolTip('A kingdom that fails twice is listed as failed and the sweep moves on. '
                                   'Three failures in a row pause the sweep.')
        for widget, key in ((self.sweep_from, 'sweep_ui_from'), (self.sweep_to, 'sweep_ui_to'),
                            (self.sweep_wait, 'sweep_ui_wait')):
            widget.valueChanged.connect(lambda value, k=key: QSettings('GOTWiC', 'Scanner').setValue(k, value))
        self.sweep_skip.toggled.connect(lambda v: QSettings('GOTWiC', 'Scanner').setValue('sweep_ui_skip', v))
        self.sweep_kind_range = QPushButton('Range'); self.sweep_kind_range.setCheckable(True)
        self.sweep_kind_list = QPushButton('List'); self.sweep_kind_list.setCheckable(True)
        self.sweep_kind_group = QButtonGroup(self)
        self.sweep_kind_group.setExclusive(True)
        self.sweep_kind_group.addButton(self.sweep_kind_range, 0)
        self.sweep_kind_group.addButton(self.sweep_kind_list, 1)
        self.sweep_list = QLineEdit()
        self.sweep_list.setPlaceholderText('100, 200, 300, 401-403')
        self.sweep_list.setToolTip('Kingdom numbers and ranges, in the order you want them scanned')
        self.sweep_list.setText(str(settings.value('sweep_ui_list', '') or ''))
        self.sweep_list.textChanged.connect(lambda v: QSettings('GOTWiC', 'Scanner').setValue('sweep_ui_list', v))
        kind = settings.value('sweep_ui_kind', 0, type=int)
        (self.sweep_kind_list if kind == 1 else self.sweep_kind_range).setChecked(True)
        self.sweep_kind_group.idClicked.connect(self._sweep_kind_changed)
        self.sweep_estimate = QLabel('')
        self.sweep_estimate.setObjectName('hint')
        self.sweep_estimate.setWordWrap(True)
        for sig in (self.sweep_from.valueChanged, self.sweep_to.valueChanged, self.sweep_wait.valueChanged,
                    self.sweep_list.textChanged):
            sig.connect(lambda *_: self._update_sweep_estimate())
        self.sweep_start_btn = QPushButton('Start sweep')
        self.sweep_start_btn.clicked.connect(lambda _checked=False: self.start_sweep())
        self.sweep_resume_btn = QPushButton('Resume')
        self.sweep_resume_btn.clicked.connect(lambda _checked=False: self.resume_sweep())
        self.sweep_resume_btn.setVisible(False)
        self.sweep_progress = QProgressBar(); self.sweep_progress.setRange(0, 1); self.sweep_progress.setValue(0)
        self.sweep_progress.setTextVisible(False)
        self.sweep_state = QLabel('Scans the kingdoms one after another, with a pause in between.')
        self.sweep_state.setWordWrap(True)
        self.sweep_counts = QLabel('')
        self.sweep_counts.setWordWrap(True)
        grid.setContentsMargins(8, 4, 8, 4); grid.setVerticalSpacing(4)
        kind_row = QHBoxLayout(); kind_row.setSpacing(0)
        kind_row.addWidget(self.sweep_kind_range); kind_row.addWidget(self.sweep_kind_list)
        grid.addLayout(kind_row, 2, 0, 1, 2)
        self._sweep_plain_from_label = QLabel('From'); self._sweep_plain_to_label = QLabel('To')
        grid.addWidget(self._sweep_plain_from_label, 0, 0); grid.addWidget(self.sweep_from, 0, 1)
        grid.addWidget(self._sweep_plain_to_label, 0, 2); grid.addWidget(self.sweep_to, 0, 3)
        grid.addWidget(self.sweep_list, 2, 2, 1, 4)
        grid.addWidget(self.sweep_estimate, 2, 6, 1, 3)
        grid.addWidget(QLabel('Wait'), 0, 4); grid.addWidget(self.sweep_wait, 0, 5)
        self.sweep_wait_random = QLabel(f'Random, {DISCREET_WAIT[0]}–{DISCREET_WAIT[1]} s')
        self.sweep_wait_random.setObjectName('waitRandom')
        self.sweep_wait_random.setToolTip('Discreet mode picks a new pause for every kingdom')
        grid.addWidget(self.sweep_wait_random, 0, 5)
        grid.addWidget(self.sweep_skip, 0, 6)
        grid.addWidget(self.sweep_start_btn, 0, 7); grid.addWidget(self.sweep_resume_btn, 0, 8)
        grid.addWidget(self.sweep_progress, 1, 0, 1, 3)
        grid.addWidget(self.sweep_state, 1, 3, 1, 3)
        grid.addWidget(self.sweep_counts, 1, 6, 1, 3)
        self.sweep_counts.setWordWrap(False); self.sweep_state.setWordWrap(False)
        layout.addWidget(self.sweep_group)
        self._sweep_kind_changed(self.sweep_kind_group.checkedId())
        self._apply_discreet_ui()

    # ---------------------------------------------------------------- DISCREET MODE
    def _discreet_toggled(self, on):
        QSettings('GOTWiC', 'Scanner').setValue('discreet_mode', bool(on))
        self._apply_discreet_ui()

    def _apply_discreet_ui(self):
        on = self.discreet.isChecked()
        self.discreet_hint.setVisible(on)
        wait = getattr(self, 'sweep_wait', None)
        if wait is not None:
            wait.setEnabled(not on and getattr(self, 'sweep', None) is None)
            wait.setVisible(not on)
            self.sweep_wait_random.setVisible(on)
        if getattr(self, 'sweep_estimate', None) is not None:
            self._update_sweep_estimate()

    def _sweep_pause_for(self, sw):
        """Seconds to wait before the next kingdom, and whether it is a short break."""
        if not sw.get('discreet'):
            return self.sweep_wait.value(), False
        rng = random.SystemRandom()
        sw['since_break'] += 1
        if sw['since_break'] >= sw['break_after']:
            sw['since_break'] = 0
            sw['break_after'] = rng.randint(*DISCREET_BREAK_EVERY)
            return rng.uniform(*DISCREET_BREAK), True
        return rng.uniform(*DISCREET_WAIT), False

    # ---------------------------------------------------------------- KINGDOM SWEEP logic
    def _sweep_kind_changed(self, kind):
        is_list = kind == 1
        QSettings('GOTWiC', 'Scanner').setValue('sweep_ui_kind', 1 if is_list else 0)
        for w in (self.sweep_from, self.sweep_to,
                  getattr(self, '_sweep_plain_from_label', None), getattr(self, '_sweep_plain_to_label', None)):
            if w is not None:
                try:
                    w.setVisible(not is_list)
                except RuntimeError:  # label removed by the theme's own layout
                    pass
        self.sweep_list.setVisible(is_list)
        hook = getattr(self, 'sweep_kind_layout_hook', None)  # gui_theme swaps its own field rows
        if callable(hook):
            hook(is_list)
        self._update_sweep_estimate()

    def _sweep_targets(self):
        """Kingdoms to scan from the UI, in order. Raises ValueError with a readable message."""
        if self.sweep_kind_group.checkedId() == 1:
            return parse_kingdom_list(self.sweep_list.text())
        first, last = self.sweep_from.value(), self.sweep_to.value()
        if first > last:
            raise ValueError('"To kingdom" must be equal to or greater than "From kingdom".')
        return list(range(first, last + 1))

    def _update_sweep_estimate(self):
        if getattr(self, 'sweep', None) is not None:
            return
        try:
            kingdoms = self._sweep_targets()
        except ValueError as exc:
            self.sweep_estimate.setText(str(exc))
            self.sweep_estimate.setProperty('state', 'error')
        else:
            n = len(kingdoms)
            s_ = QSettings('GOTWiC', 'Scanner')
            per = s_.value('sweep_avg_kingdom_s', 35.0, type=float)
            if self.discreet.isChecked():
                per = s_.value('sweep_avg_kingdom_s_discreet', per + 20.0, type=float)
                gaps = max(0, n - 1)
                breaks = gaps // sum(DISCREET_BREAK_EVERY) * 2
                seconds = n * per + gaps * sum(DISCREET_WAIT) / 2 + breaks * sum(DISCREET_BREAK) / 2
            else:
                seconds = n * per + max(0, n - 1) * self.sweep_wait.value()
            text = f"{n:,} kingdom{'' if n == 1 else 's'}, about {self._duration(seconds)}"
            if self.discreet.isChecked():
                text += ' in discreet mode'
            if self.verbose_power.isChecked():
                text += ' (longer with castle power on)'
            self.sweep_estimate.setText(text)
            self.sweep_estimate.setProperty('state', '')
        self.sweep_estimate.style().unpolish(self.sweep_estimate)
        self.sweep_estimate.style().polish(self.sweep_estimate)

    @staticmethod
    def _sweep_saved_queue(s):
        """Remaining kingdoms of an unfinished sweep (also reads the older from/next/to format)."""
        raw = str(s.value('sweep_queue', '') or '')
        queue = [int(x) for x in raw.split(',') if x.strip().isdigit()]
        if not queue and not raw:
            nxt, last = s.value('sweep_next', 0, type=int), s.value('sweep_to', 0, type=int)
            if 0 < nxt <= last:
                queue = list(range(nxt, last + 1))
        return queue

    def _busy(self):
        return (self.proc is not None or self.power_proc is not None
                or self.bulk_power_status is not None or self.sweep is not None)

    def _scan_config(self, mode='auto'):
        maskfile = BASE / 'gotwic_kingdom542_production_mask.json'
        return {
            'mode': mode, 'port': self.port.value(),
            'show': self.show_filter.currentText(), 'buffer': self.buffer.value(),
            'mask': str(maskfile), 'sql': self.enable_sql.isChecked(),
            'verbose': self.verbose_power.isChecked(),
            'timing': self.power_timing_profile.currentData(),
            'discreet': self.discreet.isChecked(),
        }

    def _chrome_ready(self):
        """Quick check (max ~1.5 s) that Chrome answers and the game tab is open."""
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{self.port.value()}/json', timeout=1.5) as r:
                pages = json.load(r)
        except Exception:
            return f'Chrome is not reachable on port {self.port.value()}. Open it with RUN_MAC.command.'
        if not any('gtarcade' in str(p.get('url', '')).lower() for p in pages if p.get('type') == 'page'):
            return 'Game tab not found. Open the game map in the Chrome window.'
        return None

    def start_sweep(self, first=None, last=None, failed=None, resume=False, kingdoms=None):
        if self._busy():
            QMessageBox.warning(self, 'Busy', 'Finish the active scan or power lookup first.')
            return
        if kingdoms is None:
            if isinstance(first, bool) or isinstance(last, bool):
                first = last = None
            try:
                if first is not None and last is not None:
                    first, last = int(first), int(last)
                    if first < 1 or first > last:
                        raise ValueError('"To kingdom" must be equal to or greater than "From kingdom" (from 1).')
                    kingdoms = list(range(first, last + 1))
                else:
                    kingdoms = self._sweep_targets()
            except ValueError as exc:
                QMessageBox.warning(self, 'Kingdom sweep', str(exc))
                return
        kingdoms = [int(k) for k in kingdoms]
        if not kingdoms:
            return
        first, last = min(kingdoms), max(kingdoms)
        if not SCANNER.exists():
            QMessageBox.critical(self, 'Missing scanner', str(SCANNER)); return
        if not (BASE / 'gotwic_kingdom542_production_mask.json').exists():
            QMessageBox.warning(self, 'Missing mask', str(BASE / 'gotwic_kingdom542_production_mask.json')); return
        problem = self._chrome_ready()
        if problem:
            QMessageBox.warning(self, 'Kingdom sweep', problem)
            return
        s0 = QSettings('GOTWiC', 'Scanner')
        if not resume and getattr(self, '_resume_available', False):
            pending = self._sweep_saved_queue(s0)
            nxt0 = pending[0] if pending else '?'
            answer = QMessageBox.question(self, 'Kingdom sweep',
                f'An unfinished sweep can resume from kingdom {nxt0} ({len(pending)} left).\n'
                f'Start a new sweep of {len(kingdoms)} kingdoms instead? The unfinished one will be discarded.',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        if resume:
            total = max(s0.value('sweep_total', 0, type=int), len(kingdoms))
            done_before = total - len(kingdoms)
            completed_before = s0.value('sweep_completed', 0, type=int)
        else:
            total, done_before, completed_before = len(kingdoms), 0, 0
        discreet = self.discreet.isChecked()
        if discreet and not resume and len(kingdoms) > 1:
            kingdoms = list(kingdoms)
            random.SystemRandom().shuffle(kingdoms)
        origin = first
        self.sweep = {
            'from': first, 'to': last, 'queue': list(kingdoms), 'total': total,
            'processed': done_before, 'completed': completed_before, 'failed': list(failed or []), 'attempts': {},
            'origin': origin, 'processed_at_start': done_before,
            'consecutive_failures': 0, 'started': time.monotonic(), 'per_kingdom': [],
            'started_utc': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
            'unprotected_total': 0, 'stopping': False, 'current': None, 'kingdom_started': None,
            'wait_until': None, 'next': None,
            'discreet': discreet, 'since_break': 0, 'on_break': False,
            'break_after': random.SystemRandom().randint(*DISCREET_BREAK_EVERY),
        }
        self.scan_config = self._scan_config('auto')
        if not resume:
            self.console.clear()
            self._reset_live_results()
        self.player.clear()
        self.refresh_live_table()
        s = QSettings('GOTWiC', 'Scanner')
        s.setValue('sweep_active', True); s.setValue('sweep_from', first); s.setValue('sweep_to', last)
        s.setValue('sweep_queue', ','.join(map(str, kingdoms))); s.setValue('sweep_total', total)
        s.setValue('sweep_next', kingdoms[0]); s.setValue('sweep_failed', ','.join(map(str, self.sweep['failed'])))
        s.setValue('sweep_completed', completed_before)
        self.sweep_progress.setRange(0, total); self.sweep_progress.setValue(done_before)
        self._resume_available = False
        self.sweep_resume_btn.setVisible(False)
        self._show_sweep_mode()
        self._sweep_set_running(True)
        self._keep_awake(True)
        shown = sorted(kingdoms) if discreet else kingdoms
        contiguous = shown == list(range(first, last + 1))
        what = (str(first) if first == last else f'{first} to {last}') if contiguous else describe_kingdoms(shown)
        if resume:
            self.console.append(f'Kingdom sweep resumed at {kingdoms[0]}: {len(kingdoms)} kingdoms left ({what})'
                                + (' in discreet mode' if discreet else ''))
        elif discreet:
            self.console.append(f'Kingdom sweep in discreet mode: {what} ({total} kingdoms) in random order, '
                                f'{DISCREET_WAIT[0]}–{DISCREET_WAIT[1]} s between kingdoms, short breaks')
        else:
            self.console.append(f"Kingdom sweep: {what} ({total} kingdom{'' if total == 1 else 's'}), "
                                f'{self.sweep_wait.value()} s between kingdoms')
        self._sweep_launch_next()

    def resume_sweep(self):
        s = QSettings('GOTWiC', 'Scanner')
        queue = self._sweep_saved_queue(s)
        failed = [int(x) for x in str(s.value('sweep_failed', '') or '').split(',') if x.strip().isdigit()]
        if not queue:
            self.sweep_resume_btn.setVisible(False)
            return
        self.start_sweep(failed=failed, resume=True, kingdoms=queue)

    def _sweep_offer_resume(self, describe=True):
        s = QSettings('GOTWiC', 'Scanner')
        queue = self._sweep_saved_queue(s) if s.value('sweep_active', False, type=bool) else []
        unfinished = bool(queue)
        self._resume_available = unfinished
        if unfinished:
            nxt = queue[0]
            self.sweep_resume_btn.setText(f'Resume from {nxt}')
            self.sweep_resume_btn.setToolTip(f'Continue the unfinished sweep: {len(queue)} kingdoms left '
                                             f'({describe_kingdoms(sorted(queue))})')
            if describe:
                self.sweep_state.setText(f'Unfinished sweep: {len(queue)} kingdoms left, next is {nxt}.')
        self.sweep_resume_btn.setVisible(bool(unfinished))
        if unfinished:
            self._show_sweep_mode()

    def _show_sweep_mode(self):
        select = getattr(self, 'select_scan_mode', None)  # provided by gui_theme (sidebar switch)
        if callable(select):
            select(1)

    def _sweep_set_running(self, running):
        for w in (self.sweep_kind_range, self.sweep_kind_list, self.sweep_list,
                  self.sweep_from, self.sweep_to, self.sweep_wait, self.sweep_skip, self.sweep_start_btn,
                  self.start_btn, self.kingdom, self.kingdom_range, self.mode, self.show_filter, self.buffer,
                  self.verbose_power, self.live_power_btn, self.bulk_protected_btn,
                  self.bulk_unprotected_btn, self.bulk_unknown_btn, self.get_power_btn, self.discreet):
            w.setEnabled(not running)
        if not running:
            self.sweep_wait.setEnabled(not self.discreet.isChecked())
        if not running:
            self.enable_sql.setEnabled(self.enable_sql.toolTip().startswith('SQL connected'))
        else:
            self.enable_sql.setEnabled(False)
        self.stop_btn.setEnabled(running)
        if running:
            if self.stop_btn.text() != 'Stop sweep':
                self._stop_btn_idle_text = self.stop_btn.text()
            self.stop_btn.setText('Stop sweep')
        else:
            self.stop_btn.setText(getattr(self, '_stop_btn_idle_text', self.stop_btn.text()))
        mode_switch = getattr(self, 'scan_mode_switch', None)
        if mode_switch is not None:
            mode_switch.setEnabled(not running)

    def _keep_awake(self, on):
        if on and sys.platform == 'darwin' and self.caffeinate_proc is None and shutil.which('caffeinate'):
            try:
                self.caffeinate_proc = subprocess.Popen(['caffeinate', '-dimsu', '-w', str(os.getpid())])
            except OSError:
                self.caffeinate_proc = None
        elif not on and self.caffeinate_proc is not None:
            try:
                self.caffeinate_proc.terminate()
            except OSError:
                pass
            self.caffeinate_proc = None

    def _sweep_launch_next(self):
        sw = self.sweep
        if sw is None:
            return
        if sw['stopping']:
            self._finish_sweep(stopped=True, reason='Stopped by you')
            return
        if not sw['queue']:
            self._finish_sweep(stopped=False)
            return
        if self.power_proc is not None or self.bulk_power_status is not None:
            self.sweep_state.setText('Waiting for the power lookup to finish…')
            self.sweep_retry_timer.start()
            return
        if self.proc is not None:
            return
        kingdom = sw['queue'].pop(0)
        sw['current'] = kingdom
        sw['kingdom_started'] = time.monotonic()
        sw['wait_until'] = None
        retry = sw['attempts'].get(kingdom, 0)
        self.sweep_state.setText(f'Scanning kingdom {kingdom}' + (' (retry)' if retry else '')
                                 + (' in discreet mode' if sw.get('discreet') else ''))
        self.status.setText(f'Kingdom sweep: scanning kingdom {kingdom}')
        self._sweep_update_counts()
        self.batch_kingdoms = [kingdom]
        self._start_next_kingdom()
        if self.proc is None:  # launch failed (_start_next_kingdom already reported it)
            self._finish_sweep(stopped=True, reason='The scanner could not be started', resume_at=kingdom)

    def _sweep_kingdom_finished(self, success):
        """Called when the scanner process of the current sweep kingdom has exited."""
        sw = self.sweep
        kingdom = sw['current']
        if sw['kingdom_started']:
            sw['per_kingdom'].append(time.monotonic() - sw['kingdom_started'])
        if not success:
            # Partial results of a failed attempt must not stay in the table or the totals.
            self._drop_live_kingdom(kingdom)
        if sw['stopping']:
            if success:
                sw['completed'] += 1
                sw['processed'] += 1
                self.sweep_progress.setValue(sw['processed'])
                self._finish_sweep(stopped=True, reason='Stopped by you')
            else:
                self._finish_sweep(stopped=True, reason='Stopped by you', resume_at=kingdom)
            return
        if success:
            sw['completed'] += 1
            sw['processed'] += 1
            sw['consecutive_failures'] = 0
        else:
            sw['attempts'][kingdom] = sw['attempts'].get(kingdom, 0) + 1
            sw['consecutive_failures'] += 1
            if not self.sweep_skip.isChecked():
                self.console.append(f'Kingdom {kingdom} failed; sweep paused.')
                self._finish_sweep(stopped=True, reason=f'Kingdom {kingdom} failed', resume_at=kingdom)
                return
            if sw['consecutive_failures'] >= 3:
                problem = self._chrome_ready()
                reason = problem or 'Three attempts failed in a row. Check Chrome.'
                self._finish_sweep(stopped=True, reason=reason, resume_at=kingdom)
                return
            if sw['attempts'][kingdom] < 2:
                self.console.append(f'Kingdom {kingdom} failed; retrying once after the pause.')
                sw['queue'].insert(0, kingdom)
            else:
                self.console.append(f'Kingdom {kingdom} failed twice; skipped.')
                sw['failed'].append(kingdom)
                sw['processed'] += 1
        nxt = sw['queue'][0] if sw['queue'] else None
        s = QSettings('GOTWiC', 'Scanner')
        s.setValue('sweep_queue', ','.join(map(str, sw['queue'])))
        s.setValue('sweep_next', nxt or 0)
        s.setValue('sweep_completed', sw['completed'])
        s.setValue('sweep_failed', ','.join(map(str, sw['failed'])))
        self.sweep_progress.setValue(sw['processed'])
        self._sweep_update_counts()
        if not sw['queue']:
            self._finish_sweep(stopped=False)
            return
        wait, sw['on_break'] = self._sweep_pause_for(sw)
        if sw['on_break']:
            self.console.append(f'Short break of {self._duration(wait)} before kingdom {nxt}.')
        sw['next'] = nxt
        sw['wait_until'] = time.monotonic() + wait
        if wait <= 0:
            self._sweep_launch_next()
        else:
            self._sweep_wait_tick()
            self.sweep_wait_timer.start()

    def _sweep_wait_tick(self):
        sw = self.sweep
        if sw is None or sw.get('wait_until') is None:
            self.sweep_wait_timer.stop()
            return
        left = sw['wait_until'] - time.monotonic()
        if left <= 0:
            self.sweep_wait_timer.stop()
            sw['wait_until'] = None
            self._sweep_launch_next()
            return
        secs = int(left + 0.999)
        if sw.get('on_break'):
            self.sweep_state.setText(f'Short break, next kingdom ({sw["next"]}) in {secs // 60}:{secs % 60:02d}')
        else:
            self.sweep_state.setText(f'Next kingdom ({sw["next"]}) in {secs} s')

    @staticmethod
    def _duration(seconds):
        seconds = int(max(0, seconds))
        h, rem = divmod(seconds, 3600)
        m, sec = divmod(rem, 60)
        if h:
            return f'{h} h {m:02d} min'
        if m:
            return f'{m} min {sec:02d} s'
        return f'{sec} s'

    def _sweep_update_counts(self):
        sw = self.sweep
        if sw is None:
            return
        elapsed = time.monotonic() - sw['started']
        remaining = sw['total'] - sw['processed']
        text = f"{sw['processed']} of {sw['total']} kingdoms done, {len(sw['failed'])} failed"
        text += f"\nElapsed {self._duration(elapsed)}"
        session_done = sw['processed'] - sw.get('processed_at_start', 0)
        if session_done > 0 and remaining:
            per = elapsed / session_done
            text += f", about {self._duration(per * remaining)} left"
        self.sweep_counts.setText(text)

    def _finish_sweep(self, stopped, reason=None, resume_at=None):
        sw = self.sweep
        if sw is None:
            return
        self.sweep_wait_timer.stop()
        self.sweep_retry_timer.stop()
        self.sweep = None
        self.scan_config = None
        self.batch_kingdoms = []
        self._keep_awake(False)
        self._sweep_set_running(False)
        s = QSettings('GOTWiC', 'Scanner')
        if stopped:
            remaining = list(sw['queue'])
            if resume_at is not None and (not remaining or remaining[0] != resume_at):
                remaining.insert(0, resume_at)
            s.setValue('sweep_queue', ','.join(map(str, remaining)))
            s.setValue('sweep_next', remaining[0] if remaining else 0)
            s.setValue('sweep_active', bool(remaining))
        else:
            s.setValue('sweep_active', False)
            s.setValue('sweep_queue', '')
            s.setValue('sweep_next', 0)
        if len(sw['per_kingdom']) >= 3:  # learn the real time per kingdom for the estimate
            per = sorted(sw['per_kingdom'])[len(sw['per_kingdom']) // 2]
            key = 'sweep_avg_kingdom_s_discreet' if sw.get('discreet') else 'sweep_avg_kingdom_s'
            default = s.value('sweep_avg_kingdom_s', 35.0, type=float) + (20.0 if sw.get('discreet') else 0.0)
            old = s.value(key, default, type=float)
            s.setValue(key, round(0.5 * old + 0.5 * per, 1))
        failed = sorted(set(sw['failed']))
        elapsed = self._duration(time.monotonic() - sw['started'])
        if not stopped:
            head = 'Sweep finished'
        elif reason in (None, 'Stopped by you'):
            head = 'Sweep stopped'
        else:
            head = 'Sweep paused'
        n = sw['completed']
        detail = f"{n} kingdom{'' if n == 1 else 's'} scanned"
        if failed:
            listed = ', '.join(map(str, failed[:6])) + ('…' if len(failed) > 6 else '')
            detail += f", {len(failed)} failed ({listed})"
        detail += f" in {elapsed}."
        self._sweep_offer_resume(describe=False)
        self._update_sweep_estimate()
        if stopped and reason not in (None, 'Stopped by you'):
            detail = f'{reason}\n{detail}'
        self.sweep_state.setText(head)
        self.sweep_counts.setText(detail)
        self.status.setText(head)
        self.console.append(f'{head}: ' + detail.replace(chr(10), ' '))
        self.refresh_data()
        self.data_dirty = False
        self.refresh_live_table()
        self._send_sweep_summary({
            'from': sw['from'], 'to': sw['to'], 'completed': sw['completed'], 'failed': failed,
            'skipped_reason': reason if stopped else None, 'unprotected_total': sw['unprotected_total'],
            'started_utc': sw['started_utc'],
            'finished_utc': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
            'stopped': bool(stopped)})

    def _send_sweep_summary(self, summary):
        """Hook for a future Discord sweep summary (postponed: discord_webhook has no such function yet).

        `summary` = {'from', 'to', 'completed', 'failed', 'skipped_reason', 'unprotected_total',
        'started_utc', 'finished_utc', 'stopped'}. Per-kingdom reports are already sent by
        queue_discord_report() after every completed kingdom.
        """
        return None

    def _archive_kingdom_totals(self, kingdom):
        counts = self.kingdom_totals.get(str(kingdom), [0, 0, 0, 0])
        if self.sweep is not None:
            self.sweep['unprotected_total'] += counts[0]

    def schedule_live_refresh(self):
        """Coalesce table rebuilds: at most ~3 per second, however fast castle lines arrive."""
        if not self.live_refresh_timer.isActive():
            self.live_refresh_timer.start()

    def _refresh_data_if_dirty(self, *_):
        if self.data_dirty and self.tabs.currentIndex() != 0:
            self.data_dirty = False
            self.refresh_data()

    # ---------------------------------------------------------------- TARGETS (history-based)
    TARGET_STATUS = (('Unprotected', ('UNPROTECTED',)), ('Unprotected + Fervor', ('UNPROTECTED', 'UNKNOWN')),
                     ('All castles', None))

    @staticmethod
    def _age(seen):
        """'2026-11-25 21:53:47' (UTC) -> hours ago (float) or None."""
        try:
            t = dt.datetime.strptime(str(seen)[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=dt.timezone.utc)
        except (TypeError, ValueError):
            return None
        return max(0.0, (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 3600)

    @staticmethod
    def _age_text(hours):
        if hours is None:
            return ''
        if hours < 1:
            return f'{int(hours * 60)} min'
        if hours < 48:
            return f'{hours:.0f} h'
        return f'{hours / 24:.0f} d'

    def build_targets(self):
        settings = QSettings('GOTWiC', 'Scanner')
        tab = QWidget(); layout = QVBoxLayout(tab)
        bar = QHBoxLayout()
        self.t_status = QComboBox()
        for label, statuses in self.TARGET_STATUS:
            self.t_status.addItem(label, statuses)
        self.t_power = QComboBox()
        for label, m in (('Any power', 0), ('10 M+', 10), ('50 M+', 50), ('100 M+', 100), ('250 M+', 250),
                         ('500 M+', 500), ('1 B+', 1000)):
            self.t_power.addItem(label, m)
        self.t_level = QComboBox()
        for label, lv in (('Any level', 0), ('Level 20+', 20), ('Level 23+', 23), ('Level 25', 25)):
            self.t_level.addItem(label, lv)
        self.t_kingdom = QComboBox(); self.t_kingdom.addItem('All kingdoms', None)
        self.t_seen = QComboBox()
        for label, h in (('Seen in the last 24 h', 24), ('Seen in the last 3 days', 72),
                         ('Seen in the last 7 days', 168), ('Any time', None)):
            self.t_seen.addItem(label, h)
        self.t_exclude = QLineEdit()
        self.t_exclude.setPlaceholderText('Hide alliances, e.g. FYR, FY2')
        self.t_exclude.setToolTip('Alliance tags to leave out (yours and your allies), separated by commas')
        self.t_search = QLineEdit(); self.t_search.setPlaceholderText('Find player or alliance')
        for combo, key, default in ((self.t_status, 't_status', 0), (self.t_power, 't_power', 0),
                                    (self.t_level, 't_level', 0), (self.t_seen, 't_seen', 1)):
            combo.setCurrentIndex(max(0, min(combo.count() - 1, settings.value(key, default, type=int))))
            combo.currentIndexChanged.connect(lambda i, k=key: (QSettings('GOTWiC', 'Scanner').setValue(k, i),
                                                               self._targets_schedule()))
        self.t_kingdom.currentIndexChanged.connect(lambda *_: self._targets_schedule())
        self.t_exclude.setText(str(settings.value('t_exclude', '') or ''))
        self.t_exclude.textChanged.connect(lambda v: (QSettings('GOTWiC', 'Scanner').setValue('t_exclude', v),
                                                      self._targets_schedule()))
        self.t_search.textChanged.connect(lambda *_: self._targets_schedule())
        for w in (self.t_status, self.t_power, self.t_level, self.t_kingdom, self.t_seen):
            bar.addWidget(w)
        bar.addWidget(self.t_exclude, 1); bar.addWidget(self.t_search, 1)
        layout.addLayout(bar)
        num = lambda v, r: '' if v is None else f'{int(v):,}'
        self.targets_model = CastleModel([
            Column('kingdom', 'Kingdom', 'int', align_right=False),
            Column('display', 'Castle'),
            Column('x', 'X', 'int'), Column('y', 'Y', 'int'),
            Column('level', 'Lv', 'int', tooltip='Castle level'),
            Column('status', 'Truce Status', 'status'),
            Column('power', 'Power (M)', 'power', tooltip='Latest known power'),
            Column('power_delta', 'Change (M)', 'delta',
                   tooltip='Power change since the previous scan where this player\'s power was known.\n'
                           'Up: training or healing troops. Down: lost troops (hit) or spent them.'),
            Column('kills', 'Kills', 'int', fmt=num, tooltip='Troops killed (from the profile or the ranking)'),
            Column('merit', 'Merit', 'int', fmt=num, tooltip='Merit score (from the profile or the ranking)'),
            Column('unprotected_hours', 'Unprotected', 'int',
                   fmt=lambda v, r: '' if v is None else f"{self._age_text(v)} ({r.get('unprotected_scans', 0)} scan{'' if r.get('unprotected_scans', 0) == 1 else 's'})",
                   tooltip='How long (and in how many scans in a row) the castle has had no bubble'),
            Column('seen_hours', 'Last seen', 'int', fmt=lambda v, r: '' if v is None else self._age_text(v) + ' ago'),
        ], self)
        self.targets_table, _ = make_view(self.targets_model, 6, True, self)
        self.targets_table.horizontalHeader().setStretchLastSection(False)
        self.targets_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for col, width in ((0, 70), (2, 56), (3, 56), (4, 48), (5, 130), (6, 100), (7, 100), (8, 100),
                           (9, 100), (10, 130), (11, 100)):
            self.targets_table.setColumnWidth(col, width)
        self.targets_table.doubleClicked.connect(lambda index: self.copy_target_coordinates())
        layout.addWidget(self.targets_table, 1)
        foot = QHBoxLayout()
        self.targets_summary = QLabel('')
        foot.addWidget(self.targets_summary, 1)
        self.targets_copy_btn = QPushButton('Copy coordinates')
        self.targets_copy_btn.setToolTip('Copies "K:1 X:489 Y:488" of the selected castle (double-click a row does the same)')
        self.targets_copy_btn.clicked.connect(self.copy_target_coordinates)
        self.targets_refresh_btn = QPushButton('Refresh')
        self.targets_refresh_btn.clicked.connect(lambda: self.refresh_targets(force=True))
        foot.addWidget(self.targets_copy_btn); foot.addWidget(self.targets_refresh_btn)
        layout.addLayout(foot)
        self.targets_timer = QTimer(self); self.targets_timer.setSingleShot(True); self.targets_timer.setInterval(300)
        self.targets_timer.timeout.connect(lambda: self.refresh_targets(force=True))
        self.tabs.insertTab(1, tab, 'Targets')
        self.targets_tab = tab
        if self.history is None:
            self.targets_summary.setText('History unavailable: ' + getattr(self, '_history_error', 'unknown error'))

    def _targets_visible(self):
        return getattr(self, 'targets_tab', None) is not None and self.tabs.currentWidget() is self.targets_tab

    def _targets_schedule(self):
        self.targets_dirty = True
        if self._targets_visible():
            self.targets_timer.start()

    def _targets_maybe_refresh(self):
        if self.targets_dirty and self._targets_visible():
            self.refresh_targets(force=True)

    def _record_history(self, csv_path, kingdom, report):
        if self.history is None or not csv_path:
            return
        try:
            castles = [dict(c, kingdom=kingdom) for c in (report.get('castles') or [])
                       if not ignored_castle(c.get('name'), c.get('x'), c.get('y'))]
            self.history.record_scan(Path(csv_path).name, kingdom, report.get('scan_utc'), castles)
        except Exception as exc:
            self.console.append(f'History: could not record kingdom {kingdom} ({exc})')
            return
        self.targets_dirty = True
        if self._targets_visible() and not self.targets_timer.isActive():
            self.targets_timer.start(2000 if self.sweep is not None else 300)

    def refresh_targets(self, force=False):
        if self.history is None or (not force and not self.targets_dirty):
            return
        self.targets_dirty = False
        current = self.t_kingdom.currentData()
        kingdoms = self.history.kingdoms()
        if [self.t_kingdom.itemData(i) for i in range(1, self.t_kingdom.count())] != kingdoms:
            self.t_kingdom.blockSignals(True)
            self.t_kingdom.clear(); self.t_kingdom.addItem('All kingdoms', None)
            for k in kingdoms:
                self.t_kingdom.addItem(f'Kingdom {k}', k)
            i = self.t_kingdom.findData(current)
            self.t_kingdom.setCurrentIndex(i if i >= 0 else 0)
            self.t_kingdom.blockSignals(False)
        exclude = [a for a in re.split(r'[,;\s]+', self.t_exclude.text()) if a]
        try:
            rows = self.history.targets(statuses=self.t_status.currentData(), min_power=float(self.t_power.currentData() or 0),
                                        min_level=int(self.t_level.currentData() or 0), kingdom=self.t_kingdom.currentData(),
                                        exclude_alliances=exclude, search=self.t_search.text().strip(),
                                        seen_within_hours=self.t_seen.currentData())
        except Exception as exc:
            self.targets_summary.setText(f'History query failed: {exc}')
            return
        rows = [r for r in rows if not ignored_castle(r.get('name'), r.get('x'), r.get('y'))]
        for r in rows:
            tag = r.get('alliance')
            r['display'] = f"[{tag}] {r['name']}" if tag else r['name']
            r['seen_hours'] = self._age(r.get('seen'))
            r['unprotected_hours'] = self._age(r.get('unprotected_since')) if r.get('unprotected_since') else None
        self.targets_model.set_rows(rows)
        self.targets_table.setColumnWidth(0, max(self.targets_table.columnWidth(0), 84))
        info = self.history.counts()
        if not info['scans']:
            self.targets_summary.setText('No scans yet. Run a scan or a kingdom sweep; results build up here over time.')
        else:
            first = self._age(info['first'])
            total = getattr(self.history, 'last_total', len(rows))
            shown = (f"{len(rows):,} castle{'s' if len(rows) != 1 else ''} match" if total <= len(rows) else
                     f"Showing the {len(rows):,} strongest of {total:,} matching castles")
            self.targets_summary.setText(
                f"{shown}. History: {info['scans']:,} scans, {info['players']:,} players, "
                f"going back {self._age_text(first)}.")

    def copy_target_coordinates(self):
        record = selected_record(self.targets_table)
        if record is None:
            self.targets_summary.setText('Select a castle first.')
            return
        text = f"K:{record['kingdom']} X:{record['x']} Y:{record['y']}"
        QApplication.clipboard().setText(text)
        previous = self.targets_summary.text()
        if previous.startswith('Copied '):
            previous = getattr(self, '_targets_summary_saved', '')
        self._targets_summary_saved = previous
        message = f"Copied {text} ({record['name']})"
        self.targets_summary.setText(message)
        QTimer.singleShot(4000, lambda: self.targets_summary.text() == message
                          and self.targets_summary.setText(self._targets_summary_saved))

    def build_explorer(self):
        tab = QWidget(); layout = QVBoxLayout(tab)
        bar = QHBoxLayout(); self.search = QLineEdit(); self.search.setPlaceholderText('Filter by player, ID, status or coordinate'); self.search.textChanged.connect(self.filter_castles)
        self.status_filter = QComboBox(); self.status_filter.addItems(['ALL','UNPROTECTED','PROTECTED','Fervor']); self.status_filter.currentTextChanged.connect(self.filter_castles)
        self.explorer_history = QComboBox()
        self.explorer_history.addItems(['Latest Scan', 'All Saved Scans'])
        self.explorer_history.currentIndexChanged.connect(self.filter_castles)
        bar.addWidget(QLabel('Scan History')); bar.addWidget(self.explorer_history)
        bar.addWidget(self.search); bar.addWidget(self.status_filter); layout.addLayout(bar)
        self.summary = QLabel('No snapshots loaded'); layout.addWidget(self.summary)
        self.castles_table = self.make_table(['Castle', 'X', 'Y', 'Truce Status', 'Power (M)', 'last_seen_utc']); layout.addWidget(self.castles_table)
        self.castles_table.sortItems(4, Qt.SortOrder.DescendingOrder)
        self.castles_table.cellDoubleClicked.connect(self.request_power_at_row)
        self.castles_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.castles_table.customContextMenuRequested.connect(self.show_castle_context_menu)
        self.get_power_btn = QPushButton('Get Power for selected castle (including truced)')
        self.get_power_btn.clicked.connect(self.request_power)
        layout.addWidget(self.get_power_btn)
        self.tabs.addTab(tab,'Castle Explorer')

    def build_history(self):
        tab = QWidget(); layout = QVBoxLayout(tab)
        self.scan_table = self.make_table(['File','Kingdom','Castles','Unprotected','Protected','Unknown'])
        self.move_table = self.make_table(['Kingdom','Player','Player ID','Previous X,Y','New X,Y','Detected in scan'])
        layout.addWidget(QLabel('CSV scan snapshots')); layout.addWidget(self.scan_table)
        layout.addWidget(QLabel('Movement between consecutive snapshots (matching player IDs)')); layout.addWidget(self.move_table)
        self.tabs.addTab(tab,'Scan & Movement History')

    def build_settings(self):
        tab = QWidget(); layout = QVBoxLayout(tab)
        form = QFormLayout()
        self.port = QSpinBox(); self.port.setRange(1,65535); self.port.setValue(9222)
        self.folder = QLineEdit(str(CSV_DIR)); self.folder.setReadOnly(True)
        choose = QPushButton('Choose CSV folder'); choose.clicked.connect(self.choose_folder)
        folder_row = QHBoxLayout(); folder_row.addWidget(self.folder); folder_row.addWidget(choose)
        form.addRow('Chrome debugging port',self.port); form.addRow('CSV snapshot folder',folder_row)
        layout.addLayout(form)
        reload_btn = QPushButton('Reload CSV snapshots'); reload_btn.clicked.connect(self.refresh_data)
        layout.addWidget(reload_btn)
        delete_csv_btn = QPushButton('Delete all CSV files')
        delete_csv_btn.clicked.connect(self.delete_all_csv_files)
        layout.addWidget(delete_csv_btn)
        self.power_timing_profile = QComboBox()
        for key in PROFILES:
            self.power_timing_profile.addItem(key.capitalize(), key)
        form.addRow('Power Check Timing', self.power_timing_profile)
        layout.addWidget(QLabel('Applies to individual, bulk and scan-time power lookups. Changes are saved automatically.'))
        self.csv_retention_days = QSpinBox()
        self.csv_retention_days.setRange(0, 36500)
        self.csv_retention_days.setSpecialValueText('0 — keep indefinitely')
        form.addRow('Auto CSV cleanup (days)', self.csv_retention_days)
        layout.addWidget(QLabel('0 = keep CSV files indefinitely; cleanup runs at startup and after scans.'))
        layout.addWidget(QLabel('SQL import is enabled when MySQL is reachable; CSV storage is always active.'))
        # The only website-upload setting; enabled by default on upgrade and fresh install.
        self.web_enabled = QCheckBox('Enable CSV upload')
        layout.addWidget(self.web_enabled)
        self.web_settings = QSettings('GOTWiC', 'Scanner')
        self.buffer.setValue(max(1, min(48, self.web_settings.value('scan_buffer', 24, type=int))))
        self.buffer.valueChanged.connect(lambda value: self.web_settings.setValue('scan_buffer', value))
        saved_profile = str(self.web_settings.value('power_timing_profile', DEFAULT_PROFILE))
        index = self.power_timing_profile.findData(saved_profile)
        self.power_timing_profile.setCurrentIndex(index if index >= 0 else self.power_timing_profile.findData(DEFAULT_PROFILE))
        self.power_timing_profile.currentIndexChanged.connect(
            lambda _index: self.web_settings.setValue('power_timing_profile', self.power_timing_profile.currentData()))
        self.csv_retention_days.setValue(self.web_settings.value('csv_retention_days', 14, type=int))
        self.csv_retention_days.valueChanged.connect(lambda days: self.web_settings.setValue('csv_retention_days', days))
        # New setting key intentionally enables uploads by default for upgrades from
        # releases where the old checkbox was forcibly disabled.
        self.web_enabled.setChecked(self.web_settings.value('web_upload_enabled_v10915', True, type=bool))
        self.web_enabled.toggled.connect(self.save_web_settings)

        discord_group = QGroupBox('Discord Webhook Reporting')
        discord_form = QFormLayout(discord_group)
        self.discord_enabled = QCheckBox('Enable automatic Discord reporting after each kingdom')
        self.discord_url = QLineEdit()
        self.discord_url.setPlaceholderText('https://discord.com/api/webhooks/...')
        self.discord_url.setEchoMode(QLineEdit.EchoMode.Password)
        self.discord_mode = QComboBox()
        self.discord_mode.addItem('UNPROTECTED only', 'unprotected')
        self.discord_mode.addItem('PROTECTED and UNPROTECTED', 'all')
        self.discord_min_power = QComboBox()
        for millions, label in ((500, '500 Million'), (1000, '1 Billion'),
                                (1500, '1.5 Billion'), (2000, '2 Billion')):
            self.discord_min_power.addItem(label, millions)
        self.discord_test = QPushButton('Test Webhook')
        self.discord_test.clicked.connect(self.test_discord_webhook)
        self.discord_status = QLabel('Disabled')
        discord_form.addRow(self.discord_enabled)
        discord_form.addRow('Webhook URL', self.discord_url)
        discord_form.addRow('Report mode', self.discord_mode)
        discord_form.addRow('Minimum castle power', self.discord_min_power)
        discord_form.addRow(self.discord_test)
        discord_form.addRow('Status', self.discord_status)
        layout.addWidget(discord_group)
        self.discord_enabled.setChecked(self.web_settings.value('discord_enabled', False, type=bool))
        self.discord_url.setText(str(self.web_settings.value('discord_url', '') or ''))
        saved_discord_mode = str(self.web_settings.value('discord_mode', 'unprotected'))
        mode_index = self.discord_mode.findData(saved_discord_mode)
        self.discord_mode.setCurrentIndex(mode_index if mode_index >= 0 else 0)
        saved_discord_power = self.web_settings.value('discord_min_power_millions', 500, type=int)
        power_index = self.discord_min_power.findData(saved_discord_power)
        self.discord_min_power.setCurrentIndex(power_index if power_index >= 0 else 0)
        self.discord_min_power.currentIndexChanged.connect(
            lambda _: self.web_settings.setValue('discord_min_power_millions', self.discord_min_power.currentData()))
        self.discord_enabled.toggled.connect(lambda v: self.web_settings.setValue('discord_enabled', v))
        self.discord_url.textChanged.connect(lambda v: self.web_settings.setValue('discord_url', v.strip()))
        self.discord_mode.currentIndexChanged.connect(
            lambda _: self.web_settings.setValue('discord_mode', self.discord_mode.currentData()))


        layout.addStretch()
        self.tabs.addTab(tab,'Settings')

    def test_discord_webhook(self):
        try:
            url = validate_webhook(self.discord_url.text())
        except ValueError as exc:
            self.discord_status.setText(str(exc))
            return
        self.discord_test.setEnabled(False)
        self.discord_status.setText('Testing...')
        def worker():
            try:
                send_test(url)
                self.messages.put(('discord_test', (True, 'Webhook test succeeded')))
            except Exception:
                self.messages.put(('discord_test', (False, 'Webhook test failed; check URL and connectivity')))
        threading.Thread(target=worker, daemon=True).start()

    def queue_discord_report(self, kingdom, castles, manual=False):
        if not self.discord_enabled.isChecked():
            return
        try:
            url = validate_webhook(self.discord_url.text())
        except ValueError:
            self.console.append('Discord: invalid webhook configuration; report skipped')
            return
        mode = self.discord_mode.currentData()
        min_power = int(self.discord_min_power.currentData() or 500)
        snapshot = list(castles)
        self.console.append(f'Discord: queuing kingdom {kingdom} {"manual power" if manual else "scan"} report')
        def worker():
            try:
                count, messages = send_kingdom_report(url, kingdom, snapshot, mode, min_power_millions=min_power, allow_unknown=manual, manual=manual)
                self.messages.put(('discord_report', (True, kingdom, count, messages)))
            except Exception:
                self.messages.put(('discord_report', (False, kingdom, 0, 0)))
        threading.Thread(target=worker, daemon=True).start()

    def save_web_settings(self):
        self.web_settings.setValue('web_upload_enabled_v10915', self.web_enabled.isChecked())

    def start_web_upload(self, path):
        """Queue a snapshot only after a power lookup saved its updated values."""
        if not self.web_enabled.isChecked():
            return
        csv_path = Path(path)
        if not csv_path.is_file():
            return
        self.upload_queue.put(csv_path)
        self.upload_pending += 1
        if self.upload_worker is None or not self.upload_worker.is_alive():
            self.upload_worker = threading.Thread(target=self._upload_worker, daemon=True)
            self.upload_worker.start()

    def _upload_worker(self):
        while True:
            csv_path = self.upload_queue.get()
            try:
                result = upload_csv(csv_path, UPLOAD_ENDPOINT)
                self.messages.put(('upload_ok', (csv_path.name, result)))
            except Exception as exc:
                self.messages.put(('upload_error', (csv_path.name, str(exc))))
            finally:
                self.upload_queue.task_done()

    @staticmethod
    def make_table(headers):
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        t.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        t.setAlternatingRowColors(True)
        t.setShowGrid(True)
        t.setWordWrap(False)
        t.verticalHeader().setVisible(False)
        t.verticalHeader().setDefaultSectionSize(27)
        t.setSortingEnabled(True)
        t.sortItems(0, Qt.SortOrder.AscendingOrder)
        header = t.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        header.setMinimumSectionSize(85)
        font = header.font()
        font.setBold(True)
        header.setFont(font)
        t.setStyleSheet(
            "QTableWidget { gridline-color: #d6dbe1; font-size: 10pt; }"
            "QHeaderView::section { padding: 6px; font-weight: 600; }"
        )
        return t

    @staticmethod
    def fill_table(table, rows, row_statuses=None):
        header = table.horizontalHeader()
        sort_col = header.sortIndicatorSection()
        sort_order = header.sortIndicatorOrder()
        table.setSortingEnabled(False)
        table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            status = row_statuses[i] if row_statuses and i < len(row_statuses) else None
            if status is None and len(row) > 6 and str(row[6]).upper() in (
                'PROTECTED', 'UNPROTECTED', 'UNKNOWN', 'FERVOR'
            ):
                status = str(row[6]).upper()
            if status == 'PROTECTED':
                text_color = QColor('#ff7777')  # light red text
            elif status == 'UNPROTECTED':
                text_color = QColor('#78e08f')  # light green text
            elif status in ('UNKNOWN', 'FERVOR'):
                text_color = QColor('#ffd166')  # light amber text
            else:
                text_color = None
            for j, value in enumerate(row):
                column_name = table.horizontalHeaderItem(j).text()
                item_type = (TruceItem if column_name == 'Truce Status' else
                             NumericItem if column_name in ('Power (M)', 'X', 'Y') else
                             QTableWidgetItem)
                item = item_type(str(value if value is not None else ''))
                if column_name in ('Power (M)', 'X', 'Y'):
                    try: item.setData(Qt.ItemDataRole.UserRole, float(value) if value is not None and str(value).strip() else -1.0)
                    except (ValueError, TypeError): item.setData(Qt.ItemDataRole.UserRole, -1.0)
                if str(table.horizontalHeaderItem(j).text()) == 'Truce Status':
                    item.setData(Qt.ItemDataRole.UserRole, {'UNPROTECTED':0,'UNKNOWN':1,'FERVOR':1,'PROTECTED':2}.get(str(value).upper(),3))
                if text_color is not None:
                    item.setForeground(QBrush(text_color))
                table.setItem(i, j, item)
        table.setSortingEnabled(True)
        if sort_col >= 0: table.sortItems(sort_col, sort_order)
        table.resizeRowsToContents()

    def toggle_unknown_filter(self, checked):
        self.live_status_filter.setCurrentIndex(3 if checked else 0)

    def sync_unknown_toggle(self, *_):
        self.unknown_toggle.setChecked(self.live_status_filter.currentIndex() == 3)

    def remember_gui_alliance(self, kingdom, x, y, name, alliance):
        tag = str(alliance or '').strip()
        if tag and len(tag) <= 32 and not any(c in tag for c in '[]\\r\\n'):
            self.gui_alliances[(str(kingdom), int(x), int(y), str(name))] = tag

    def gui_player_name(self, kingdom, x, y, name):
        tag = self.gui_alliances.get((str(kingdom), int(x), int(y), str(name)))
        return f'[{tag}] {name}' if tag else str(name)

    # ---------------------------------------------------------------- live results (model/view)
    def _reset_live_results(self):
        self.live_results = []
        self.live_index = {}
        self.live_pending = []
        self.kingdom_totals = {}
        self.live_kingdom_filter.blockSignals(True)
        self.live_kingdom_filter.clear()
        self.live_kingdom_filter.addItem('All kingdoms', None)
        self.live_kingdom_filter.blockSignals(False)
        self.live_model.set_rows([])

    def _live_counts(self, kingdom):
        return self.kingdom_totals.setdefault(str(kingdom), [0, 0, 0, 0])  # unprotected, protected, total, fervor

    @staticmethod
    def _count_delta(counts, status, sign):
        status = str(status or '').upper()
        if status == 'UNPROTECTED':
            counts[0] += sign
        elif status == 'PROTECTED':
            counts[1] += sign
        elif status == 'UNKNOWN':
            counts[3] += sign
        counts[2] += sign

    def _add_live_castle(self, kingdom, name, x, y, status):
        key = (str(kingdom), x, y, name)
        record = self.live_index.get(key)
        if record is not None:  # same castle reported again: update in place
            if record['status'] != status:
                counts = self._live_counts(kingdom)
                self._count_delta(counts, record['status'], -1)
                self._count_delta(counts, status, +1)
                record['status'] = status
                self.live_dirty_full = True
            self.schedule_live_refresh()
            return
        record = {'kingdom': kingdom, 'name': name, 'x': x, 'y': y, 'status': status, 'power': 0,
                  'display': self.gui_player_name(kingdom, x, y, name)}
        self.live_index[key] = record
        self.live_results.append(record)
        self.live_pending.append(record)
        self._count_delta(self._live_counts(kingdom), status, +1)
        if self.live_kingdom_filter.findData(kingdom) < 0:
            self.live_kingdom_filter.addItem(f'Kingdom {kingdom}', kingdom)
        self.schedule_live_refresh()

    def _drop_live_kingdom(self, kingdom):
        """Remove every result of one kingdom (a failed attempt) from the run."""
        k = str(kingdom)
        self.live_results = [r for r in self.live_results if str(r.get('kingdom')) != k]
        self.live_index = {key: r for key, r in self.live_index.items() if key[0] != k}
        self.live_pending = [r for r in self.live_pending if str(r.get('kingdom')) != k]
        self.kingdom_totals.pop(k, None)
        i = self.live_kingdom_filter.findData(kingdom)
        if i > 0:
            if self.live_kingdom_filter.currentIndex() == i:
                self.live_kingdom_filter.setCurrentIndex(0)
            self.live_kingdom_filter.removeItem(i)
        self.refresh_live_table()

    def _live_filter(self):
        status_filter = self.live_status_filter.currentText().split(" ", 1)[0].upper()
        if status_filter == "FERVOR":
            status_filter = "UNKNOWN"
        minimum_millions = float(self.minimum_power.currentData() or 0)
        player_term = self.player.text().strip().casefold()
        kingdom = self.live_kingdom_filter.currentData()

        def accept(record):
            if kingdom is not None and str(record.get('kingdom')) != str(kingdom):
                return False
            if status_filter != 'ALL' and str(record.get('status', '')).upper() != status_filter:
                return False
            if minimum_millions > 0:
                try:
                    if float(record.get('power') or 0) < minimum_millions:
                        return False
                except (TypeError, ValueError):
                    return False
            if player_term and player_term not in str(record.get('display') or record.get('name') or '').casefold():
                return False
            return True
        return accept

    def refresh_live_table(self, *_):
        """Full re-filter (filters changed, power arrived, a kingdom finished). New castles use _flush_live."""
        accept = self._live_filter()
        self.live_pending = []
        self.live_dirty_full = False
        self.live_model.set_rows([r for r in self.live_results if accept(r)])
        self._update_stats_table()

    def _flush_live(self):
        """Show castles that arrived since the last refresh (append only; cheap at any table size)."""
        if self.live_dirty_full:
            self.refresh_live_table()
            return
        if self.live_pending:
            accept = self._live_filter()
            new_rows = [r for r in self.live_pending if accept(r)]
            self.live_pending = []
            self.live_model.append_rows(new_rows)
        self._update_stats_table()

    def _update_stats_table(self):
        # One row per kingdom of this run, then TOTAL (independent of the table filters).
        grouped = self.kingdom_totals
        ordered = sorted(grouped, key=lambda k: (not k.isdigit(), int(k) if k.isdigit() else k))
        totals = [sum(grouped[k][i] for k in ordered) for i in range(4)]
        rows = ordered + ['TOTAL']
        if self.stats_table.rowCount() != len(rows):
            self.stats_table.setRowCount(len(rows))
        for row, kingdom in enumerate(rows):
            values = grouped[kingdom] if kingdom != 'TOTAL' else totals
            display = [kingdom, f'{values[0]:,} ({values[3]:,})', f'{values[1]:,}', f'{values[2]:,}']
            for col, value in enumerate(display):
                item = self.stats_table.item(row, col)
                if item is not None and item.text() == value:
                    continue
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | (Qt.AlignmentFlag.AlignLeft if col == 0 else Qt.AlignmentFlag.AlignRight))
                if col == 1:
                    item.setForeground(QBrush(QColor('#78e08f')))
                elif col == 2:
                    item.setForeground(QBrush(QColor('#ff8e8e')))
                if kingdom == 'TOTAL':
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                self.stats_table.setItem(row, col, item)
        self.stats_table.resizeRowsToContents()
        QTimer.singleShot(0, self.stats_table.scrollToBottom)
        QTimer.singleShot(200, self.stats_table.scrollToBottom)  # again after the sidebar re-lays out

    def choose_folder(self):
        folder=QFileDialog.getExistingDirectory(self,'CSV folder',self.folder.text())
        if folder:
            self.folder.setText(folder); self.refresh_data()

    def cleanup_expired_csv(self):
        """Remove CSVs older than the configured retention using creation time on Windows."""
        days = self.web_settings.value('csv_retention_days', 14, type=int)
        if days <= 0:
            return
        import time
        cutoff = time.time() - days * 86400
        folder = Path(self.folder.text())
        deleted = 0
        failures = []
        for path in folder.glob('*.csv'):
            try:
                if path.stat().st_ctime < cutoff:
                    path.unlink()
                    deleted += 1
            except OSError as exc:
                failures.append(f'{path.name}: {exc}')
        if deleted or failures:
            for attr in ('active_scan_csv', 'pending_scan_csv', 'upload_last_csv'):
                candidate = getattr(self, attr)
                if candidate is not None and not candidate.is_file():
                    setattr(self, attr, None)
            self.console.append(f'Automatic CSV cleanup ({days} days): deleted {deleted} file(s)')
            if failures:
                self.console.append('Automatic CSV cleanup errors: ' + '; '.join(failures))

    def delete_all_csv_files(self):
        """Immediately remove every .csv file in the selected snapshot folder."""
        folder = Path(self.folder.text())
        deleted = 0
        failures = []
        for path in folder.glob('*.csv'):
            try:
                path.unlink()
                deleted += 1
            except OSError as exc:
                failures.append(f'{path.name}: {exc}')
        # Forget deleted snapshots and any pending upload reference to a missing file.
        if self.active_scan_csv is not None and not self.active_scan_csv.is_file():
            self.active_scan_csv = None
        if self.pending_scan_csv is not None and not self.pending_scan_csv.is_file():
            self.pending_scan_csv = None
        if self.upload_last_csv is not None and not self.upload_last_csv.is_file():
            self.upload_last_csv = None
        self.refresh_data()
        self.console.append(f'CSV cleanup: deleted {deleted} file(s) from {folder}')
        if failures:
            self.console.append('CSV cleanup errors: ' + '; '.join(failures))
        self.status.setText(f'Deleted {deleted} CSV file(s)' + (f'; {len(failures)} failed' if failures else ''))

    def refresh_data(self):
        self.last_data_refresh = time.monotonic()
        self.scans=read_snapshots(Path(self.folder.text()))
        # The scanner can write CSV files outside the GUI's configured folder.
        # Never silently substitute an older snapshot for the current run.
        if self.active_scan_csv is not None:
            path = self.active_scan_csv
            cached = next((rs for p, rs in self.scans if p.resolve() == path.resolve()), None)
            if cached is not None:
                self.scans = [(p, rs) for p, rs in self.scans if p.resolve() != path.resolve()]
                self.scans.insert(0, (path, cached))
                path = None
        if self.active_scan_csv is not None and path is not None:
            try:
                with path.open(encoding='utf-8-sig', newline='') as stream:
                    reader = csv.DictReader(stream)
                    if not set(COLUMNS).issubset(reader.fieldnames or []):
                        raise ValueError('CSV columns do not match the scanner format')
                    rows = list(reader)
                if len({r['last_seen_utc'] for r in rows}) > 1:
                    raise ValueError('CSV contains multiple scan timestamps')
                self.scans = [(p, rs) for p, rs in self.scans if p.resolve() != path.resolve()]
                self.scans.insert(0, (path, rows))
            except (OSError, UnicodeError, csv.Error, ValueError) as exc:
                self.scans = []
                self.status.setText(f'Latest scan snapshot unavailable: {exc}')
        self.latest=self.scans[0][1] if self.scans else []
        self.filter_castles()
        summary=[]
        for path,rows in self.scans:
            counts=_snapshot_counts(rows)
            summary.append([path.name,rows[0]['kingdom'] if rows else path.name.split('_')[0],len(rows),*counts])
        self.fill_table(self.scan_table,summary)
        live_ids = {id(rows) for _p, rows in self.scans}
        for stale in [k for k in _COUNT_CACHE if k not in live_ids]:
            _COUNT_CACHE.pop(stale, None)  # drop row lists that are no longer loaded
        # Key on file identity (path, size, mtime), not on the identity of freshly re-read lists.
        def _file_key(p):
            try:
                st = p.stat()
                return (str(p), st.st_size, st.st_mtime_ns)
            except OSError:
                return (str(p), None, None)
        key=tuple(_file_key(p) for p, _rows in self.scans)
        if self._movement_cache is None or self._movement_cache[0] != key:
            self._movement_cache = (key, None, movements(self.scans))
        self.fill_table(self.move_table,self._movement_cache[2])

    def explorer_rows(self):
        """Yield (snapshot path, record) pairs without collapsing historical observations."""
        snapshots = self.scans if self.explorer_history.currentIndex() == 1 else self.scans[:1]
        return [(path, record) for path, records in snapshots for record in records]

    def filter_castles(self,*_):
        term = self.search.text().casefold().strip()
        status = self.status_filter.currentText()
        if status == "Fervor":
            status = "UNKNOWN"
        source = self.explorer_rows()
        filtered = [(path, r) for path, r in source
                    if (status == 'ALL' or r['status'] == status)
                    and (not term or any(term in str(v).casefold() for v in r.values())
                         or term in self.gui_player_name(r.get('kingdom'), r.get('x'), r.get('y'),
                                                         r.get('name', '')).casefold())]
        shown_total = len(filtered)
        if shown_total > EXPLORER_MAX_ROWS:
            filtered = filtered[:EXPLORER_MAX_ROWS]  # newest snapshots come first
        self.explorer_visible = filtered
        self.fill_table(self.castles_table,
                        [[self.gui_player_name(r.get('kingdom'), r.get('x'), r.get('y'), r.get('name', '')),
                          r.get('x', ''), r.get('y', ''),
                          ('Fervor' if str(r.get('status', 'UNKNOWN')).upper() == 'UNKNOWN' else r.get('status', 'UNKNOWN')), r.get('status_source', ''),
                          r.get('last_seen_utc', '')] for _, r in filtered],
                        [str(r.get('status', 'UNKNOWN')).upper() for _, r in filtered])
        row_at = {}
        for j in range(self.castles_table.rowCount()):
            try:
                key = (int(self.castles_table.item(j, 1).text()), int(self.castles_table.item(j, 2).text()),
                       self.castles_table.item(j, 5).text())
            except (AttributeError, ValueError):
                continue
            row_at.setdefault(key, j)
        for _path, record in filtered:
            try:
                j = row_at.get((int(record['x']), int(record['y']), str(record['last_seen_utc'])))
            except (KeyError, TypeError, ValueError):
                continue
            if j is not None:
                self.castles_table.item(j, 0).setData(Qt.ItemDataRole.UserRole, record['name'])
        all_rows = [r for _, r in source]
        mode = self.explorer_history.currentText()
        self.summary.setText(
            f'{mode}: {len(all_rows)} castle observations | Displayed: {len(filtered)}'
            + (f' (newest {EXPLORER_MAX_ROWS:,} of {shown_total:,}; search to narrow)' if shown_total > len(filtered) else '')
            + ' | '
            f'Unprotected: {sum(r.get("status") == "UNPROTECTED" for r in all_rows)} | '
            f'Snapshots: {len(self.scans) if self.explorer_history.currentIndex() == 1 else min(len(self.scans), 1)}')

    def start_scan(self):
        if self._busy():
            return
        if not SCANNER.exists():
            QMessageBox.critical(self, 'Missing scanner', str(SCANNER)); return
        maskfile = BASE / 'gotwic_kingdom542_production_mask.json'
        if self.mode.currentText() == 'auto' and not maskfile.exists():
            QMessageBox.warning(self, 'Missing mask', str(maskfile)); return
        range_text = self.kingdom_range.text().strip()
        if range_text:
            try:
                kingdoms = parse_kingdom_selection(range_text)
            except ValueError as exc:
                QMessageBox.warning(self, 'Invalid kingdom selection', str(exc)); return
            if self.mode.currentText() != 'auto':
                QMessageBox.warning(self, 'Batch mode', 'Multiple kingdom scanning requires automatic scan mode.'); return
            self.batch_kingdoms = kingdoms
        else:
            self.batch_kingdoms = [self.kingdom.value()]
        self.batch_completed = 0
        self.scan_config = {
            'mode': self.mode.currentText(), 'port': self.port.value(),
            'show': self.show_filter.currentText(), 'buffer': self.buffer.value(),
            'mask': str(maskfile), 'sql': self.enable_sql.isChecked(),
            'verbose': self.verbose_power.isChecked(),
            'timing': self.power_timing_profile.currentData(),
            'discreet': self.discreet.isChecked(),
        }
        self.pending_scan_csv = None
        self.active_scan_csv = None
        self.player.clear()
        self.console.clear()
        self._reset_live_results()
        self.refresh_live_table()
        self.start_btn.setEnabled(False)
        self.sweep_start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._start_next_kingdom()

    def _start_next_kingdom(self):
        if not self.batch_kingdoms:
            self.scan_config = None
            self.proc = None
            self.start_btn.setEnabled(True)
            self.sweep_start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self.status.setText(f'Scan finished — {self.batch_completed} kingdom(s) completed')
            return
        kingdom = self.batch_kingdoms.pop(0)
        self.current_scan_kingdom = kingdom
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d_%H%M%S_%f')
        report = BASE / f'gui_scan_{stamp}_report.json'
        cfg = self.scan_config
        cmd = [sys.executable, '-u', str(SCANNER), '--kingdom', str(kingdom),
               '--mode', cfg['mode'], '--port', str(cfg['port']), '--show', cfg['show'],
               '--buf', str(cfg['buffer']), '--report', str(report), '--mask', cfg['mask'],
               '--auto-start', '--power-timing-profile', cfg['timing']]
        if not cfg['sql']: cmd.append('--no-db')
        if cfg['verbose']: cmd.append('--verbose')
        if cfg.get('discreet') and cfg['mode'] == 'auto':
            cmd += ['--route', 'random', '--pace', 'discreet']
        self.pending_scan_csv = None
        self.console.append(f'Kingdom {kingdom}: selecting kingdom, then scanning...')
        if self.sweep is None:
            self.status.setText(f'Scanning kingdom {kingdom} — {len(self.batch_kingdoms)} remaining')
        try:
            flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
            env = os.environ.copy()
            env['PYTHONIOENCODING'] = 'utf-8'
            env['PYTHONUTF8'] = '1'
            self.proc = subprocess.Popen(cmd, cwd=str(BASE), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1, text=True,
                encoding='utf-8', errors='replace', creationflags=flags, env=env)
        except OSError as exc:
            self.batch_kingdoms = []
            self.proc = None
            self.start_btn.setEnabled(True)
            self.sweep_start_btn.setEnabled(self.sweep is None)
            self.stop_btn.setEnabled(False)
            QMessageBox.critical(self, 'Launch failed', str(exc))
            return
        self.report_path = report
        self.ready_sent = True
        def read_output(proc):
            try:
                for line in proc.stdout:
                    self.messages.put(('line', line.rstrip()))
            finally:
                self.messages.put(('exit', proc.wait()))
        threading.Thread(target=read_output, args=(self.proc,), daemon=True).start()

    def confirm_ready(self):
        if self.proc and self.proc.poll() is None and not self.ready_sent:
            try:
                self.proc.stdin.write('\n');self.proc.stdin.flush()
                self.ready_sent=True
                self.status.setText('Scanning — '+self.mode.currentText())
            except (OSError,ValueError) as exc:self.console.append(f'Cannot confirm: {exc}')

    def stop_scan(self):
        if self.sweep is not None:
            if self.sweep['stopping']:
                return
            self.sweep['stopping'] = True
            if self.proc is None:
                # Between two kingdoms: cancel the countdown right away.
                self._finish_sweep(stopped=True, reason='Stopped by you')
                return
            self.sweep_state.setText(f"Stopping kingdom {self.sweep['current']}…")
            if self.proc.poll() is not None:
                return  # already exited; tick() will process the exit and finish the sweep
        if not self.proc or self.proc.poll() is not None:return
        if not self.ready_sent:
            self.console.append('Cancelling before scan start (no CSV will be created).')
            self.proc.terminate()
            self.stop_btn.setEnabled(False)
            return
        if self.sweep is None and self.mode.currentText()=='manual' and self.ready_sent:
            self.console.append('Finishing manual scan (requesting Ctrl+C)...')
        else:
            self.console.append('Requesting scanner stop...')
        try:
            if os.name=='nt':self.proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:self.proc.send_signal(signal.SIGINT)
        except (OSError,ValueError) as exc:self.console.append(f'Stop failed: {exc}')
        self.batch_kingdoms = []  # Stop also cancels all remaining kingdoms.
        self.stop_btn.setEnabled(False)

    def tick(self):
        self.poll_power()
        self.poll_bulk_power()
        for _ in range(300):
            try:kind,value=self.messages.get_nowait()
            except queue.Empty:break
            if kind == 'discord_test':
                self.discord_test.setEnabled(True)
                self.discord_status.setText(value[1])
                continue
            if kind == 'discord_report':
                ok, kingdom, count, messages = value
                note = (f'Discord: kingdom {kingdom} sent ({count} castles, {messages} messages)'
                        if ok else f'Discord: kingdom {kingdom} report failed; scanning unaffected')
                self.discord_status.setText(note)
                self.console.append(note)
                continue
            if kind == 'history_import':
                files = value[0] if value else 0
                if files:
                    self.console.append(f'History: imported {files} earlier scan(s) for the Targets tab')
                self.targets_dirty = True
                self._targets_maybe_refresh()
                continue
            if kind == 'sql_check':
                connected, note = value
                self.enable_sql.setChecked(connected)
                self.enable_sql.setEnabled(connected)
                self.enable_sql.setToolTip(note)
                self.console.append(note)
                continue
            if kind == 'upload_ok':
                self.upload_pending = max(0, self.upload_pending - 1)
                continue
            if kind == 'upload_error':
                self.upload_pending = max(0, self.upload_pending - 1)
                continue
            if kind=='line':
                clean_line = re.sub(r'\x1b\[[0-9;]*m', '', value).strip()
                # Scanner status lines are rendered as structured, color-coded table rows.
                castle_match = re.match(
                    r'^(.*?)\s+X=(\d+)\s+Y=(\d+):\s*(PROTECTED|UNPROTECTED|UNKNOWN)\s*$',
                    clean_line, re.IGNORECASE
                )
                if castle_match:
                    name, x, y, status = castle_match.groups()
                    status = status.upper()
                    if ignored_castle(name, x, y):
                        continue
                    self._add_live_castle(self.current_scan_kingdom, name.strip(), int(x), int(y), status)
                    continue
                lowered = clean_line.casefold()
                # Do not show raw tracebacks, encoding errors, or scanner error details in the GUI.
                if any(token in lowered for token in (
                    'traceback', 'exception', 'error:', 'report errors', 'warning:',
                    'failed with', 'connectionerror', 'timeouterror', 'modulenotfounderror',
                    'filenotfounderror', 'charmap', "codec can't encode", 'stop: runtimeerror',
                    'could not encode', 'unicodeencodeerror'
                )):
                    continue
                if clean_line.startswith('CSV:'):
                    raw_path = clean_line.partition(':')[2].strip().strip('"')
                    if raw_path:
                        self.pending_scan_csv = Path(raw_path)
                if clean_line:
                    if clean_line.startswith('GOTWiC Scanner v2.2 |'):
                        clean_line = f'GOTWiC Scanner v{APP_VERSION} |' + clean_line.split('|', 1)[1]
                    self.console.append(clean_line)
            else:
                try:
                    self.proc = None
                    completed_csv = self.pending_scan_csv
                    successful = value == 0
                    report = None
                    if self.report_path.exists():
                        try:
                            report = json.loads(self.report_path.read_text(encoding='utf-8'))
                            if self.sweep is not None:
                                try:
                                    self.report_path.unlink()  # 550 per sweep would pile up in the app folder
                                except OSError:
                                    pass
                            successful = successful and not report.get('errors') and (report.get('csv_export') or {}).get('status') == 'success'
                        except (OSError, ValueError) as exc:
                            successful = False
                            self.console.append(f'Could not load kingdom report: {exc}')
                    else:
                        successful = False
                    if successful and completed_csv is not None and completed_csv.is_file():
                        self.active_scan_csv = completed_csv
                        self.batch_completed += 1
                        # Persist only a genuinely completed kingdom, including the last batch member.
                        self.web_settings.setValue('last_successful_kingdom', int(self.current_scan_kingdom))
                        self.kingdom.setValue(int(self.current_scan_kingdom))
                        # v1.0.9.16: scan completion does not trigger a website upload.
                        # Upload only after an individual or bulk power lookup saves power.
                        self.cleanup_expired_csv()
                        for info in report.get('gui_alliances', []):
                            try:
                                self.remember_gui_alliance(info['kingdom'], info['x'], info['y'],
                                                           info['name'], info.get('alliance'))
                            except (KeyError, TypeError, ValueError):
                                pass
                        if self.sweep is None:
                            self.refresh_data()  # Each completed kingdom appears immediately in Explorer.
                        else:
                            self.data_dirty = True  # Explorer reloads when you open it (or every minute).
                        lookup = {(str(c.get('name')), int(c.get('x')), int(c.get('y'))): c.get('power_millions', 0)
                                  for c in report.get('castles', [])}
                        for castle in self.live_results:
                            if castle.get('kingdom') == self.current_scan_kingdom:
                                castle['power'] = _as_float(lookup.get((castle['name'], castle['x'], castle['y']), 0))
                                castle['display'] = self.gui_player_name(castle['kingdom'], castle['x'], castle['y'], castle['name'])
                        self.refresh_live_table()
                        self._archive_kingdom_totals(self.current_scan_kingdom)
                        if self.sweep is not None:
                            self.console.append(f'Kingdom {self.current_scan_kingdom} complete')
                        else:
                            self.console.append(f'Kingdom {self.current_scan_kingdom} complete — {len(self.batch_kingdoms)} remaining')
                        self._record_history(completed_csv, self.current_scan_kingdom, report)
                        self.queue_discord_report(self.current_scan_kingdom, report.get('castles', []))
                        if self.sweep is not None:
                            self._sweep_kingdom_finished(True)
                            continue
                        if self.batch_kingdoms:
                            self._start_next_kingdom()
                            continue
                    else:
                        if self.sweep is not None:
                            if not self.sweep['stopping']:
                                self.console.append(f'Kingdom {self.current_scan_kingdom} did not complete successfully.')
                            self._sweep_kingdom_finished(False)
                            continue
                        self.console.append(f'Kingdom {self.current_scan_kingdom} did not complete successfully; batch stopped.')
                        self.batch_kingdoms = []
                    self.scan_config = None
                    self.start_btn.setEnabled(True)
                    self.sweep_start_btn.setEnabled(True)
                    self.stop_btn.setEnabled(False)
                    self.status.setText(f'Scan finished — {self.batch_completed} kingdom(s) completed')

                except Exception as exc:  # never leave a sweep hanging on 'Scanning kingdom N'
                    self.console.append(f'Unexpected error after kingdom {self.current_scan_kingdom}: {exc}')
                    if self.sweep is not None and self.proc is None and self.sweep.get('current') == self.current_scan_kingdom:
                        try:
                            self._sweep_kingdom_finished(False)
                        except Exception as exc2:
                            self.console.append(f'Sweep error: {exc2}')
                            self._finish_sweep(stopped=True, reason='Internal error',
                                               resume_at=self.current_scan_kingdom)

    def request_power_at_live_row(self, row, column):
        self.live_table.selectRow(row)
        self.request_power_from_live(keep_open=True)

    def show_live_context_menu(self, position):
        index = self.live_table.indexAt(position)
        if not index.isValid():
            return
        self.live_table.selectRow(index.row())
        menu = QMenu(self.live_table)
        action = menu.addAction('Get Power for selected castle')
        action.setEnabled(not self._busy())
        if menu.exec(self.live_table.viewport().mapToGlobal(position)) == action:
            self.request_power_from_live()

    def request_power_from_live(self, keep_open=False):
        if self._busy():
            QMessageBox.warning(self, 'Busy', 'Finish the active scan, sweep or power lookup first.')
            return
        record = selected_record(self.live_table)
        if record is None:
            QMessageBox.information(self, 'Select castle', 'Select a castle in Live castle results first.')
            return
        try:
            kingdom = int(record['kingdom'])
            name = record['name']
            x = int(record['x'])
            y = int(record['y'])
        except (KeyError, ValueError, TypeError):
            QMessageBox.warning(self, 'Invalid castle', 'Selected castle has invalid coordinates.')
            return
        # The live table has no player ID or snapshot ID. Resolve against the
        # latest persisted snapshot and reject missing/ambiguous matches.
        snapshot = next(((path, rows) for path, rows in self.scans if rows and str(rows[0].get('kingdom')) == str(kingdom)), None)
        matches = [r for r in (snapshot[1] if snapshot else []) if r.get('name') == name
                   and str(r.get('x')) == str(x) and str(r.get('y')) == str(y)
                   and str(r.get('kingdom')) == str(kingdom)]
        if len(matches) != 1 or snapshot is None:
            QMessageBox.warning(self, 'Snapshot unavailable',
                'Could not uniquely match this castle to the latest saved scan. '
                'Finish the scan and reload its snapshot before requesting power.')
            return
        record = matches[0]
        target = {'kingdom':int(record['kingdom']), 'x':x, 'y':y,
                  'name':name, 'player_id':record.get('player_id',''),
                  'stamp':record['last_seen_utc'], 'csv':str(snapshot[0]),
                  'sql':False}
        self.launch_power_lookup(target, keep_open=keep_open)

    def request_power_at_row(self, row, column):
        self.castles_table.selectRow(row)
        self.request_power(keep_open=True)

    def show_castle_context_menu(self, position):
        item = self.castles_table.itemAt(position)
        if item is None:
            return
        self.castles_table.selectRow(item.row())
        menu = QMenu(self.castles_table)
        action = menu.addAction('Get Power for selected castle')
        action.setEnabled(not self._busy())
        if menu.exec(self.castles_table.viewport().mapToGlobal(position)) == action:
            self.request_power()

    def request_power(self, keep_open=False):
        if self._busy():
            QMessageBox.warning(self, 'Busy', 'Finish the active scan, sweep or power lookup first.')
            return
        row = self.castles_table.currentRow()
        if row < 0 or not self.scans:
            QMessageBox.information(self, 'Select castle', 'Select a castle in Castle Explorer first.')
            return
        def cell(column):
            return self.castles_table.item(row, column).text()
        try:
            x, y = int(float(cell(1))), int(float(cell(2)))
        except (TypeError, ValueError):
            QMessageBox.warning(self, 'Invalid castle', 'Selected castle has invalid coordinates.')
            return
        name = self.castles_table.item(row, 0).data(Qt.ItemDataRole.UserRole) or cell(0)
        stamp = cell(5)
        # Sorting changes visible row indices; identify the original snapshot by
        # the displayed record and its scan timestamp, not the latest CSV path.
        matches = [(path, r) for path, r in self.explorer_rows()
                   if str(r.get('x')) == str(x) and str(r.get('y')) == str(y)
                   and str(r.get('name', '')) == name
                   and str(r.get('last_seen_utc')) == stamp]
        if len(matches) != 1:
            QMessageBox.warning(self, 'Ambiguous selection', 'Could not uniquely identify selected snapshot row.')
            return
        snapshot, record = matches[0]
        kingdom = int(record['kingdom'])
        player_id = record.get('player_id', '')
        target = {'kingdom':kingdom,'x':x,'y':y,'name':name,'player_id':player_id,
                  'stamp':stamp,'csv':str(snapshot),'sql':False}
        self.launch_power_lookup(target, keep_open=keep_open)

    @staticmethod
    def has_power(record):
        """Power is persisted in status_source; zero/missing is not a known value."""
        try:
            return float(record.get('status_source') or 0) > 0
        except (TypeError, ValueError):
            return False

    def start_bulk_power(self, status):
        if self._busy():
            QMessageBox.warning(self, 'Busy', 'Finish the active scan, sweep or power lookup first.')
            return
        if not self.scans:
            QMessageBox.information(self, 'No scan', 'No completed scan snapshot is available.')
            return
        snapshot, rows = self.scans[0]
        if not snapshot.is_file():
            QMessageBox.warning(self, 'Snapshot unavailable', 'The latest scan CSV is missing.')
            return
        eligible = [r for r in rows if (str(r.get('status', '')).upper() in ('UNPROTECTED', 'UNKNOWN') if status == 'UNPROTECTED' else str(r.get('status', '')).upper() == status)
                    and not self.has_power(r)
                    and not ignored_castle(r.get('name'), r.get('x'), r.get('y'))]
        targets = []
        for r in eligible:
            try:
                x, y = int(r['x']), int(r['y'])
                kingdom = int(r['kingdom'])
                if not (0 <= x <= 700 and 0 <= y <= 700):
                    raise ValueError('Out-of-range coordinates')
                targets.append({'kingdom': kingdom, 'x': x, 'y': y,
                    'name': str(r['name']), 'player_id': r.get('player_id', ''),
                    'stamp': r['last_seen_utc'], 'csv': str(snapshot),
                    'sql': False})
            except (ValueError, KeyError, TypeError):
                self.console.append(f"Bulk power: skipped invalid record {r.get('name', '?')}")
        if not targets:
            self.status.setText(f'Bulk {status}: no castles missing power in latest scan')
            return
        self.manual_power_reports = []
        self.bulk_power_status = status
        self.bulk_power_queue = targets
        self.bulk_power_total = len(targets)
        self.bulk_power_done = 0
        self.bulk_power_failed = 0
        self.bulk_power_saved = 0
        self.bulk_power_csv = Path(snapshot)
        self.bulk_protected_btn.setEnabled(False)
        self.bulk_unprotected_btn.setEnabled(False)
        self.bulk_unknown_btn.setEnabled(False)
        self.console.append(f'Bulk {status}: checking {len(targets)} castles missing power from {snapshot.name}')
        self.launch_bulk_worker()

    def launch_bulk_worker(self):
        cmd = [sys.executable, str(BASE/'castle_power_detector.py'), '--batch', '--port', str(self.port.value()),
               '--power-timing-profile', self.power_timing_profile.currentData()]
        env = os.environ.copy()
        env['PYTHONIOENCODING'] = 'utf-8'
        env['PYTHONUTF8'] = '1'
        try:
            worker = subprocess.Popen(cmd, cwd=str(BASE), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
                                      bufsize=1, env=env)
            self.bulk_worker = worker
            targets = list(self.bulk_power_queue)
            self.bulk_power_queue = targets
            def read_results():
                try:
                    for line in worker.stdout:
                        try:
                            self.bulk_messages.put(json.loads(line))
                        except json.JSONDecodeError:
                            self.bulk_messages.put({'success':False,'error':'Invalid worker response'})
                finally:
                    self.bulk_messages.put({'_finished':True})
            threading.Thread(target=read_results, daemon=True).start()
            try:
                for target in targets:
                    worker.stdin.write(json.dumps({k:target[k] for k in ('x','y','name')}, ensure_ascii=False)+'\n')
                worker.stdin.flush()
                worker.stdin.close()
            except (BrokenPipeError, OSError) as exc:
                self.console.append(f'Bulk worker input failed: {exc}')
        except OSError as exc:
            self.console.append(f'Bulk worker startup failed: {exc}')
            self.bulk_worker = None
            self.bulk_power_failed = self.bulk_power_total
            self.bulk_power_done = self.bulk_power_total
            self.finish_bulk_power()

    def finish_bulk_power(self):
        self.flush_manual_power_reports()
        status = self.bulk_power_status
        # All per-castle CSV writes have completed before queuing the upload.
        saved = self.bulk_power_saved
        snapshot = self.bulk_power_csv
        self.bulk_power_saved = 0
        self.bulk_power_csv = None
        self.bulk_power_status = None
        self.bulk_worker = None
        self.bulk_protected_btn.setEnabled(True)
        self.bulk_unprotected_btn.setEnabled(True)
        self.bulk_unknown_btn.setEnabled(True)
        message = (f'Bulk {status} complete: {self.bulk_power_done - self.bulk_power_failed} saved, '
                   f'{self.bulk_power_failed} failed, {self.bulk_power_total} attempted')
        self.console.append(message)
        self.status.setText(message)
        if saved > 0 and snapshot is not None:
            self.start_web_upload(snapshot)

    def poll_bulk_power(self):
        if self.bulk_worker is None:
            return
        while True:
            try:
                result = self.bulk_messages.get_nowait()
            except queue.Empty:
                break
            if result.get('_finished'):
                if self.bulk_power_done < self.bulk_power_total:
                    remaining = self.bulk_power_total - self.bulk_power_done
                    self.bulk_power_failed += remaining
                    self.bulk_power_done += remaining
                    self.console.append(f'Bulk worker ended early: {remaining} unprocessed castles')
                self.finish_bulk_power()
                break
            if result.get('fatal'):
                self.console.append(f"Bulk CDP initialization failed: {result['fatal']}")
                continue
            if self.bulk_power_done >= len(self.bulk_power_queue):
                continue
            target = self.bulk_power_queue[self.bulk_power_done]
            self.power_target = target
            self.handle_power_result(result)
            self.power_target = None
            self.bulk_power_done += 1
            self.status.setText(f'Bulk {self.bulk_power_status}: {self.bulk_power_done}/{self.bulk_power_total}')

    def flush_manual_power_reports(self):
        # Only successfully retrieved castles are reported, once per operation.
        by_kingdom = {}
        for castle in self.manual_power_reports:
            by_kingdom.setdefault(castle['kingdom'], []).append(castle)
        self.manual_power_reports = []
        for kingdom, castles in by_kingdom.items():
            self.queue_discord_report(kingdom, castles, manual=True)

    def next_bulk_power(self):
        if self.bulk_power_status is None:
            return
        if not self.bulk_power_queue:
            self.finish_bulk_power()
            return
        target = self.bulk_power_queue.pop(0)
        self.status.setText(f'Bulk {self.bulk_power_status}: '
                            f'{self.bulk_power_done + 1}/{self.bulk_power_total} — {target["name"]}')
        self.launch_power_lookup(target)
        if self.power_proc is None:
            self.bulk_power_done += 1
            self.bulk_power_failed += 1
            self.console.append(f'Bulk power: failed to launch for {target["name"]}')
            QTimer.singleShot(0, self.next_bulk_power)

    def launch_power_lookup(self, target, keep_open=False):
        x, y, name = target['x'], target['y'], target['name']
        cmd = [sys.executable, str(BASE/'castle_power_detector.py'), '--x',str(x),'--y',str(y),
               '--name',name,'--port',str(self.port.value()),
               '--power-timing-profile',self.power_timing_profile.currentData()]
        if keep_open:
            cmd.extend(['--keep-open', '--kingdom', str(target['kingdom'])])
        self.get_power_btn.setEnabled(False)
        self.live_power_btn.setEnabled(False)
        self.status.setText(f'Looking up power for {name} at X={x}, Y={y}...')
        try:
            power_env = os.environ.copy()
            power_env['PYTHONIOENCODING'] = 'utf-8'
            power_env['PYTHONUTF8'] = '1'
            self.power_proc = subprocess.Popen(cmd, cwd=str(BASE), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
                         env=power_env)
            self.power_target = target
        except OSError as exc:
            self.power_proc = None
            self.get_power_btn.setEnabled(True)
            self.live_power_btn.setEnabled(True)
            if self.bulk_power_status is None:
                QMessageBox.critical(self, 'Power lookup failed', str(exc))
            else:
                self.console.append(f'Bulk power launch failed: {exc}')

    def poll_power(self):
        if self.power_proc is None or self.power_proc.poll() is None:
            return
        proc = self.power_proc
        self.power_proc = None
        self.get_power_btn.setEnabled(self.sweep is None)
        self.live_power_btn.setEnabled(self.sweep is None)
        out, err = proc.communicate()
        try:
            result = json.loads(out)
        except (ValueError, TypeError):
            result = {'success':False,'error':err or out or 'Invalid power lookup output'}
        if proc.returncode and result.get('success'):
            result = {'success':False,'error':err or 'Power detector exited unsuccessfully'}
        self.handle_power_result(result)
        self.power_target = None

    def lookup_castle_status(self, target):
        # Match the exact CSV snapshot and castle identity; never infer status from GUI filters.
        try:
            with Path(target['csv']).open(encoding='utf-8-sig', newline='') as handle:
                for row in csv.DictReader(handle):
                    if (str(row.get('kingdom')) == str(target['kingdom'])
                            and str(row.get('x')) == str(target['x'])
                            and str(row.get('y')) == str(target['y'])
                            and str(row.get('name')) == str(target['name'])
                            and str(row.get('last_seen_utc')) == str(target['stamp'])):
                        return str(row.get('status') or 'UNKNOWN').upper()
        except (OSError, csv.Error, UnicodeError, KeyError):
            pass
        return 'UNKNOWN'

    def handle_power_result(self, result):
        try:
            if not result.get('success'):
                raise ValueError(result.get('error') or 'Power extraction failed')
            target = self.power_target
            if result.get('player') != target['name']:
                raise ValueError('Returned player name does not match the selected castle')
            from power_storage import save_power
            value = save_power(target, result['power_millions'])
            self.remember_gui_alliance(target['kingdom'], target['x'], target['y'],
                                       target['name'], result.get('alliance'))
            if self.history is not None:
                try:
                    self.history.update_power(Path(target['csv']).name, target['kingdom'], target['x'], target['y'],
                                              target['name'], float(value), result.get('kills'), result.get('merit'))
                    self.targets_dirty = True
                except Exception:
                    pass
            self.refresh_data()
            for castle in self.live_results:
                if (castle['name'] == target['name'] and castle['x'] == target['x']
                        and castle['y'] == target['y']):
                    castle['power'] = float(value)
            self.refresh_live_table()
            report_castle = {'kingdom': int(target['kingdom']), 'name': target['name'],
                             'x': target['x'], 'y': target['y'],
                             'status': self.lookup_castle_status(target),
                             'power_millions': float(value)}
            if self.bulk_power_status is not None:
                self.bulk_power_saved += 1
                self.manual_power_reports.append(report_castle)
            else:
                self.queue_discord_report(target['kingdom'], [report_castle], manual=True)
                # The atomic CSV save has finished; upload the updated snapshot.
                self.start_web_upload(target['csv'])
            self.status.setText(f"Power saved: {target['name']} — {value:.3f} M")
            if self.bulk_power_status is not None:
                self.console.append(f'Bulk power {self.bulk_power_done + 1}/{self.bulk_power_total}: {target["name"]} — {value:.3f} M')
        except Exception as exc:
            self.status.setText('Power lookup failed — snapshot unchanged')
            if self.bulk_power_status is None:
                QMessageBox.warning(self, 'Power lookup', str(exc))
            else:
                self.bulk_power_failed += 1
                self.console.append(f'Bulk power failed ({self.bulk_power_done + 1}/{self.bulk_power_total}): {self.power_target["name"]} — {exc}')

    def closeEvent(self,event):
        if self.upload_pending:
            event.ignore()
            return
        if self.sweep is not None:
            QMessageBox.warning(self, 'Sweep running', 'Stop the kingdom sweep before closing. You can resume it later.')
            event.ignore(); return
        if (self.proc is not None and self.proc.poll() is None) or self.power_proc is not None or self.bulk_power_status is not None:
            QMessageBox.warning(self,'Scanner active','Finish or stop the scanner before closing the GUI.')
            event.ignore();return
        event.accept()


# Multi-file, changed-only updater. Release manifest lists every distributable file.
APP_VERSION = '1.0.9.22'
UPDATE_BASE_URL = "https://raw.githubusercontent.com/gotwic-scanner/castle_scanner/main/"
UPDATE_CHECK_TIMEOUT_SECONDS = 5
UPDATE_DOWNLOAD_TIMEOUT_SECONDS = 15

def _version_tuple(value):
    try:
        return tuple(int(part) for part in str(value).strip().split("."))
    except (TypeError, ValueError):
        return ()

def _safe_update_name(name):
    return (isinstance(name, str) and bool(name) and name not in (".", "..")
            and "/" not in name and "\\" not in name
            and Path(name).name == name)

def _update_file_hash(path):
    import hashlib
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()

def _locally_modified_files(root):
    """Files that differ from the installed release manifest (version.json): your local improvements."""
    try:
        manifest = json.loads((root / "version.json").read_text(encoding="utf-8"))
        entries = manifest.get("files") or []
    except (OSError, ValueError, AttributeError):
        return []
    modified = []
    for item in entries:
        name = item.get("path") if isinstance(item, dict) else None
        if not _safe_update_name(name) or not (root / name).is_file():
            continue
        try:
            if _update_file_hash(root / name) != str(item.get("sha256", "")).lower():
                modified.append(name)
        except OSError:
            continue
    return sorted(modified)


def _ask_about_update_with_local_changes(remote, local_files):
    """Returns 'install', 'later' or 'skip'."""
    listed = "\n".join("  • " + n for n in local_files[:12]) + ("\n  • …" if len(local_files) > 12 else "")
    box = QMessageBox(QMessageBox.Icon.Information, "GOTWiC Scanner update",
        f"Version {remote} is available (you have {APP_VERSION}).\n\n"
        f"Your copy has local improvements in {len(local_files)} file(s):\n{listed}\n\n"
        "Installing the update replaces these files with the official versions "
        "(for example the kingdom sweep, the redesign and the game-update fixes would be undone). "
        f"A copy of your current files is saved first in backups/before-{remote}/.")
    install = box.addButton("Install update", QMessageBox.ButtonRole.AcceptRole)
    skip = box.addButton("Skip this version", QMessageBox.ButtonRole.DestructiveRole)
    later = box.addButton("Not now", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(later)
    box.exec()
    clicked = box.clickedButton()
    return 'install' if clicked is install else 'skip' if clicked is skip else 'later'


def _backup_local_files(root, remote, files):
    import shutil
    folder = root / "backups" / f"before-{remote}"
    folder.mkdir(parents=True, exist_ok=True)
    for name in files:
        shutil.copy2(root / name, folder / name)
    return folder


def check_for_gui_update():
    import hashlib
    import shutil
    import tempfile
    import time
    import urllib.request

    try:
        req = urllib.request.Request(UPDATE_BASE_URL + "version.json",
                                     headers={"User-Agent": "GOTWiC-Scanner-Updater"})
        with urllib.request.urlopen(req, timeout=UPDATE_CHECK_TIMEOUT_SECONDS) as response:
            raw = response.read(131073)
        if len(raw) > 131072:
            raise ValueError("Update manifest exceeds 128 KiB")
        if raw.lstrip().startswith((b"<", b"<!")):
            raise ValueError("Update server returned HTML instead of version.json")
        manifest = json.loads(raw.decode("utf-8"))
        remote = manifest.get("version")
        if not _version_tuple(remote) or _version_tuple(remote) <= _version_tuple(APP_VERSION):
            return False
        entries = manifest.get("files")
        if not isinstance(entries, list) or not entries or len(entries) > 100:
            QMessageBox.critical(None, "Update unavailable",
                "The server does not provide a valid multi-file manifest. The application will close.")
            return True
        seen = set()
        for item in entries:
            if not isinstance(item, dict):
                raise ValueError("Invalid file record")
            name = item.get("path")
            digest = item.get("sha256")
            if (not _safe_update_name(name) or name in seen or
                not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest) or
                not isinstance(item.get("size"), int) or item["size"] < 0 or item["size"] > 30_000_000):
                raise ValueError("Invalid update file metadata")
            seen.add(name)
    except Exception as exc:
        QMessageBox.warning(None, "GOTWiC Scanner update check failed",
            f"Could not check for updates at GitHub:\n{exc}\n\n"
            "Select OK to continue with the installed version.")
        return False

    root = Path(__file__).resolve().parent
    changed = [item for item in entries
               if not (root/item["path"]).is_file()
               or _update_file_hash(root/item["path"]) != item["sha256"].lower()]
    local_files = _locally_modified_files(root)
    if local_files:
        # A customised copy: never overwrite it without asking, and keep a backup.
        settings = QSettings('GOTWiC', 'Scanner')
        if str(settings.value('update_skip_version', '') or '') == str(remote):
            return False
        choice = _ask_about_update_with_local_changes(remote, local_files)
        if choice == 'skip':
            settings.setValue('update_skip_version', str(remote))
            return False
        if choice != 'install':
            return False
        try:
            _backup_local_files(root, remote, local_files)
        except OSError as exc:
            QMessageBox.critical(None, "Update cancelled",
                f"Could not back up your local files:\n{exc}\n\nNothing was changed.")
            return False
    else:
        answer = QMessageBox.question(None, "GOTWiC Scanner update",
            f"Version {remote} is available (installed: {APP_VERSION}).\n"
            f"{len(changed)} changed file(s) need downloading.\n\nThis update is required. Declining will close the application. Install now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if answer != QMessageBox.StandardButton.Yes:
            QMessageBox.information(None, "Update required", "The update was declined. The application will now close.")
            return True

    stage = Path(tempfile.mkdtemp(prefix="gotwic_update_"))
    started = time.monotonic()
    try:
        for item in changed:
            name = item["path"]
            remaining = UPDATE_DOWNLOAD_TIMEOUT_SECONDS - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("The 15-second overall download timeout was exceeded")
            req = urllib.request.Request(UPDATE_BASE_URL + name,
                                        headers={"User-Agent": "GOTWiC-Scanner-Updater"})
            with urllib.request.urlopen(req, timeout=remaining) as response, (stage/name).open("wb") as target:
                while True:
                    remaining = UPDATE_DOWNLOAD_TIMEOUT_SECONDS - (time.monotonic() - started)
                    if remaining <= 0:
                        raise TimeoutError("The 15-second overall download timeout was exceeded")
                    block = response.read(min(65536, item["size"] + 1 - target.tell()))
                    if not block:
                        break
                    target.write(block)
                    if target.tell() > item["size"]:
                        raise ValueError("Downloaded file exceeds declared size: " + name)
            if (stage/name).stat().st_size != item["size"] or _update_file_hash(stage/name) != item["sha256"].lower():
                raise ValueError("Downloaded file failed verification: " + name)
            if name.endswith(".py"):
                compile((stage/name).read_text(encoding="utf-8"), name, "exec")
        if time.monotonic() - started > UPDATE_DOWNLOAD_TIMEOUT_SECONDS:
            raise TimeoutError("The 15-second overall download timeout was exceeded")
        (stage/"plan.json").write_text(json.dumps({"files": changed}), encoding="utf-8")
        # Copy the helper outside the installation folder, so updater.py itself may be replaced.
        helper = root/"updater.py"
        if not helper.is_file():
            raise OSError("updater.py is missing")
        helper_copy = stage/"run_updater.py"
        shutil.copy2(helper, helper_copy)
        subprocess.Popen([sys.executable, str(helper_copy), "--pid", str(os.getpid()),
                          "--root", str(root), "--stage", str(stage)],
                         cwd=str(root), close_fds=True)
        return True
    except Exception as exc:
        shutil.rmtree(stage, ignore_errors=True)
        QMessageBox.critical(None, "Update failed",
            f"Could not download or stage the complete update:\n{exc}\n\n"
            "Select OK to close the application. The update is required before continuing.")
        return True


if __name__=='__main__':
    app=QApplication(sys.argv)
    if check_for_gui_update():
        sys.exit(0)
    from license_client import authorize
    if not authorize():
        sys.exit(1)
    parser=argparse.ArgumentParser(add_help=False)
    parser.add_argument('-v','--verbose',action='store_true')
    cli_args,_=parser.parse_known_args(sys.argv[1:])
    window=MainWindow()
    if cli_args.verbose:window.verbose_power.setChecked(True)
    window.show()
    sys.exit(app.exec())
