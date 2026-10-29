"""One signing identity per Mac, so macOS keeps the permissions the user granted across upgrades.

An ad-hoc signature is identified by a hash of that exact build: every reinstall looked like a new app to macOS
privacy (external drive, Downloads, Finder), so it asked again. A self-signed certificate made once on this Mac
and kept in a private keychain makes the app's identity "this bundle id + this certificate" instead.
The key never leaves the Mac, the login keychain and trust settings are not touched, and any failure falls back
to ad-hoc signing, which only means one more permission question.
"""
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile

NAME = 'Resource Guard Local Signing'
# LibreSSL: its .p12 files import into a keychain; a Homebrew OpenSSL 3 earlier on PATH may write ones that do not.
OPENSSL = '/usr/bin/openssl'
EXTENSIONS = """[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = {name}
[ext]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
""".format(name=NAME)


def folder(home):
    return Path(home) / '.local/share/dev-resource-guard/signing'


def _run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=120)


def _create(base):
    """A 20-year certificate in a keychain of its own; the password file is written last, so a half-made folder is redone."""
    keychain = base / 'signing.keychain-db'
    if keychain.exists():
        subprocess.run(['security', 'delete-keychain', str(keychain)], capture_output=True, timeout=60)
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, mode=0o700)
    password = secrets.token_hex(24)
    with tempfile.TemporaryDirectory() as scratch:
        scratch = Path(scratch)
        (scratch / 'cert.cnf').write_text(EXTENSIONS)
        _run(OPENSSL, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '7300', '-config', str(scratch / 'cert.cnf'),
             '-keyout', str(scratch / 'key.pem'), '-out', str(scratch / 'cert.pem'))
        _run(OPENSSL, 'pkcs12', '-export', '-inkey', str(scratch / 'key.pem'), '-in', str(scratch / 'cert.pem'),
             '-out', str(scratch / 'id.p12'), '-passout', 'pass:' + password)
        _run('security', 'create-keychain', '-p', password, str(keychain))
        _run('security', 'set-keychain-settings', str(keychain))   # no auto-lock
        _run('security', 'unlock-keychain', '-p', password, str(keychain))
        _run('security', 'import', str(scratch / 'id.p12'), '-k', str(keychain), '-P', password, '-T', '/usr/bin/codesign')
        # Lets codesign use the key without a keychain dialog.
        _run('security', 'set-key-partition-list', '-S', 'apple-tool:,apple:,codesign:', '-s', '-k', password, str(keychain))
        shutil.copy2(scratch / 'cert.pem', base / 'cert.pem')
    secret = base / 'password'
    secret.write_text(password + '\n')
    os.chmod(secret, 0o600)


def identity(home):
    """(SHA-1, keychain) of this Mac's signing identity, made on first use."""
    base = folder(home)
    if not all((base / name).is_file() for name in ('signing.keychain-db', 'cert.pem', 'password')):
        _create(base)
    keychain = base / 'signing.keychain-db'
    _run('security', 'unlock-keychain', '-p', (base / 'password').read_text().strip(), str(keychain))
    sha1 = _run(OPENSSL, 'x509', '-in', str(base / 'cert.pem'), '-noout', '-fingerprint', '-sha1').stdout
    sha1 = sha1.strip().split('=', 1)[1].replace(':', '').upper()
    if sha1 not in _run('security', 'find-identity', '-p', 'codesigning', str(keychain)).stdout:
        raise RuntimeError('签名证书不在钥匙串里')
    return sha1, keychain


def _search_list():
    return [line.strip().strip('"') for line in _run('security', 'list-keychains', '-d', 'user').stdout.splitlines() if line.strip()]


def sign(app, home):
    """Signs the app with this Mac's identity and returns 'local'; falls back to ad-hoc and returns 'ad-hoc'."""
    try:
        sha1, keychain = identity(home)
        # Before macOS 27, codesign finds an identity only in a keychain on the search list (--keychain alone gives
        # "no identity found"), so ours is listed just while signing and the user's list is put back right after.
        listed = _search_list()
        added = str(keychain) not in listed
        try:
            if added:
                _run('security', 'list-keychains', '-d', 'user', '-s', str(keychain), *listed)
            _run('codesign', '--force', '--sign', sha1, '--keychain', str(keychain), str(app))
        finally:
            if added:
                _run('security', 'list-keychains', '-d', 'user', '-s', *listed)
        _run('codesign', '--verify', '--strict', str(app))
        return 'local'
    except (OSError, IndexError, RuntimeError, subprocess.SubprocessError) as error:
        detail = getattr(error, 'stderr', '') or error
        print('没用上本机固定签名，先用临时签名（以后升级时 macOS 会再问一次权限）：{}'.format(str(detail).strip()))
        subprocess.run(['codesign', '--force', '--sign', '-', str(app)], check=True)
        return 'ad-hoc'


def requirement(app):
    """The designated requirement macOS privacy keys its grants on; '' when the app is missing or unsigned."""
    out = subprocess.run(['codesign', '-d', '-r-', str(app)], capture_output=True, text=True)
    lines = [line.lstrip('# ') for line in ((out.stdout or '') + (out.stderr or '')).splitlines()]   # ad-hoc: '# designated => cdhash …'
    return next((line.split('=>', 1)[1].strip() for line in lines if line.startswith('designated =>')), '')
