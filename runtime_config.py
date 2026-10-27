"""Paths and resident-service utilities shared by development and installed builds."""
import fcntl
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sqlite3
import sys
import shutil
import threading
import time

CODE_ROOT = Path(__file__).resolve().parent
APP_HOME = Path(os.environ.get('SELECTION_HOME', Path.home() / 'Library/Application Support/FashionStudioSelection')).expanduser().resolve()
DEPLOYED = bool(os.environ.get('SELECTION_HOME')) or CODE_ROOT == APP_HOME / 'app'
try:
    SETTINGS = json.loads((APP_HOME / 'runtime.json').read_text()) if DEPLOYED else {}
except (OSError, ValueError):
    SETTINGS = {}

def _path(env, key, default):
    return Path(os.environ.get(env, SETTINGS.get(key, str(default)))).expanduser().resolve()

PROJECT_DIR = _path('SELECTION_PROJECT_DIR', 'project_dir', APP_HOME / 'project' if DEPLOYED else CODE_ROOT)
DATA_DIR = _path('SELECTION_DATA_DIR', 'data_dir', APP_HOME / 'data' if DEPLOYED else CODE_ROOT / 'data')
OUTPUT_DIR = _path('SELECTION_OUTPUT_DIR', 'output_dir', APP_HOME / 'exports' if DEPLOYED else CODE_ROOT / 'exports')
STYLE_PATH = _path('SELECTION_STYLE_PATH', 'style_path', CODE_ROOT / 'examples/style-rules.json')
LEGACY_DIR = _path('SELECTION_LEGACY_DIR', 'legacy_dir', CODE_ROOT / 'data/legacy-import')
LOG_DIR = _path('SELECTION_LOG_DIR', 'log_dir', APP_HOME / 'logs' if DEPLOYED else CODE_ROOT / 'logs')
BACKUP_DIR = _path('SELECTION_BACKUP_DIR', 'backup_dir', APP_HOME / 'backups' if DEPLOYED else DATA_DIR / 'backups')
HOST = os.environ.get('SELECTION_HOST', SETTINGS.get('host', '0.0.0.0' if DEPLOYED else '127.0.0.1'))
PORT = int(os.environ.get('SELECTION_PORT', SETTINGS.get('port', 8770)))
NODE = _path('SELECTION_NODE', 'node', shutil.which('node') or '/unconfigured/node')
PYTHON = _path('SELECTION_PYTHON', 'python', sys.executable)
SERVICE_LABEL = 'com.fashionstudio.selectionworkbench'
_LOCK_HANDLE = None


def ensure_private_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def acquire_instance_lock():
    """Hold for process lifetime. A direct second server cannot reset the queue."""
    global _LOCK_HANDLE
    if _LOCK_HANDLE is not None:
        return _LOCK_HANDLE
    ensure_private_dir(DATA_DIR)
    handle = open(DATA_DIR / 'server.lock', 'a+')
    os.chmod(handle.name, 0o600)
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError('Fashion Studio选款后台已在运行，请使用现有工作台；本次不会启动第二个服务。')
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    _LOCK_HANDLE = handle
    return handle


class _LogStream(io.TextIOBase):
    def __init__(self, logger, level):
        self.logger, self.level, self.local = logger, level, threading.local()
    def write(self, text):
        if not text:
            return 0
        buffer = getattr(self.local, 'buffer', '') + str(text)
        lines = buffer.split('\n')
        self.local.buffer = lines.pop()
        for line in lines:
            if line.strip():
                self.logger.log(self.level, line.rstrip())
        return len(text)
    def flush(self):
        buffer = getattr(self.local, 'buffer', '')
        if buffer.strip():
            self.logger.log(self.level, buffer.rstrip())
        self.local.buffer = ''


def configure_logging():
    """Bound service logs to approximately 30 MB, including redirected tracebacks."""
    ensure_private_dir(LOG_DIR)
    logger = logging.getLogger('fashionstudio.selection')
    if not logger.handlers:
        handler = RotatingFileHandler(LOG_DIR / 'service.log', maxBytes=5*1024*1024, backupCount=5, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    sys.stdout = _LogStream(logger, logging.INFO)
    sys.stderr = _LogStream(logger, logging.ERROR)
    return logger


def backup_database(database=None, backup_dir=None, force=False, keep=14):
    """Use SQLite's online backup API, never copy a live WAL database file."""
    database = Path(database or DATA_DIR / 'selection.sqlite3')
    backup_dir = ensure_private_dir(Path(backup_dir or BACKUP_DIR))
    if not database.is_file():
        return None
    stamp = time.strftime('%Y-%m-%d')
    target = backup_dir / ('selection-' + stamp + '.sqlite3')
    if target.exists() and not force:
        return target
    lock = open(backup_dir / '.backup.lock', 'a+')
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return None
    temp = target.with_suffix('.tmp')
    try:
        if temp.exists():
            temp.unlink()
        with sqlite3.connect(database) as source, sqlite3.connect(temp) as dest:
            source.backup(dest, pages=256, sleep=0.05)
            if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('数据库备份完整性校验失败')
        temp.chmod(0o600)
        os.replace(temp, target)
        for old in sorted(backup_dir.glob('selection-????-??-??.sqlite3'), reverse=True)[keep:]:
            old.unlink()
        return target
    finally:
        if temp.exists():
            temp.unlink()
        lock.close()


def start_backup_worker():
    def maintain():
        while True:
            try:
                path = backup_database()
                if path:
                    logging.getLogger('fashionstudio.selection').info('数据库每日备份已就绪: %s', path.name)
            except Exception:
                logging.getLogger('fashionstudio.selection').exception('数据库每日备份失败')
            time.sleep(3600)
    thread = threading.Thread(target=maintain, name='selection-daily-backup', daemon=True)
    thread.start()
    return thread
