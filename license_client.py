#!/usr/bin/env python3
"""Online-only GOTWiC license gate; no database access from the desktop client."""
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request

from PySide6.QtWidgets import QInputDialog, QLineEdit, QMessageBox

API_URL = 'https://scannerapp.serveminecraft.net/api/license.php'
KEY_PATTERN = re.compile(r'^GOTWIC-[A-F0-9]{8}-[A-F0-9]{8}-[A-F0-9]{8}$')
TIMEOUT_SECONDS = 10


def _license_path():
    base = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
    return base / 'GOTWiC-Scanner' / 'license.json'


def _machine_id():
    """Hash the stable Windows machine GUID; never transmit the raw GUID."""
    if os.name != 'nt':
        raise RuntimeError('Device activation requires Windows.')
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                        r'SOFTWARE\Microsoft\Cryptography', 0,
                        winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
        guid, _ = winreg.QueryValueEx(key, 'MachineGuid')
    if not isinstance(guid, str) or not guid.strip():
        raise RuntimeError('Windows device identifier is unavailable.')
    return hashlib.sha256(('GOTWiC-Scanner-device-v1:' + guid.strip().lower()).encode('utf-8')).hexdigest()


def _load_key():
    try:
        data = json.loads(_license_path().read_text(encoding='utf-8'))
        key = data.get('license_key')
        return key if isinstance(key, str) and KEY_PATTERN.fullmatch(key) else None
    except (OSError, ValueError, TypeError):
        return None


def _save_key(key):
    path = _license_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    try:
        tmp.write_text(json.dumps({'license_key': key}), encoding='utf-8')
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def validate(key, device_id):
    payload = json.dumps({'license_key': key, 'device_id': device_id}).encode('utf-8')
    request = urllib.request.Request(API_URL, data=payload,
        headers={'Content-Type': 'application/json', 'Accept': 'application/json',
                 'User-Agent': 'GOTWiC-Scanner-License/1.0.9.16'}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read(4097)
    except urllib.error.HTTPError as exc:
        # The API may return JSON error reasons with 4xx/5xx HTTP status.
        raw = exc.read(4097)
    if len(raw) > 4096:
        raise ValueError('License server response is too large.')
    result = json.loads(raw.decode('utf-8'))
    if not isinstance(result, dict) or not isinstance(result.get('valid'), bool):
        raise ValueError('Unexpected license server response.')
    return result


REASONS = {
    'invalid_license': 'The license key was not recognized.',
    'invalid_request': 'The license server rejected the request format.',
    'revoked': 'This license has been revoked.',
    'expired': 'This license has expired.',
    'different_device': 'This license is already assigned to another computer. Request a new license.',
    'service_unavailable': 'The licensing service is temporarily unavailable.',
    'https_required': 'The licensing service requires HTTPS.',
}


def authorize():
    """Allow local use without the retired online licensing server.

    This offline mode is intended for the project owner's recovered local copy.
    It does not contact the licensing API or activate licenses for other users.
    """
    return True
