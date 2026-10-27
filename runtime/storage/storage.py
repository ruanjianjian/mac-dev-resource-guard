"""Bounded cache maintenance. Never deletes source, Products, devices or app data."""
from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import time
import uuid
from pathlib import Path

GIB = 2 ** 30
HOME = Path.home()
DEV = HOME / 'Library/Caches/dev-resource-guard'
LEAVES = {'Intermediates.noindex', 'Index.noindex', 'ModuleCache.noindex', 'SDKStatCaches.noindex'}
SKIP = {'.git', 'node_modules', '.dart_tool', 'Pods', '.gradle', '.build', 'Products', 'Artifacts', '.next'}
PROTECTED_CACHE_NAMES = {'products', 'reports', 'report', 'test-results', 'test_results',
                         'artifacts', '.git', 'node_modules', 'pods', '.env', 'coverage'}
PROTECTED_CACHE_SUFFIXES = ('.xcresult', '.xctestrun', '.dsym', '.apk', '.ipa', '.mobileprovision',
                            '.p12', '.pfx', '.pem', '.key', '.cer', '.log')
FD_OPERATIONS_AVAILABLE = ({os.open, os.stat, os.rename, os.unlink, os.rmdir}.issubset(os.supports_dir_fd)
                           and hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY'))


class ProtectedCacheContent(ValueError):
    """Known preserved content makes its entire enclosing cache leaf ineligible."""


@dataclasses.dataclass
class Settings:
    cache_roots: list = dataclasses.field(default_factory=lambda: [DEV])
    work_roots: list = dataclasses.field(default_factory=lambda: [])
    state_root: Path = HOME / '.local/state/dev-storage'
    lease_root: Path = HOME / '.device-leases/leases'
    volume: Path = HOME
    target_free_gib: int = 100
    warning_free_gib: int = 75
    critical_free_gib: int = 50
    intermediate_budget_gib: int = 220
    normal_days: int = 7
    pressure_days: int = 3
    critical_days: int = 2

    def __post_init__(self):
        # macOS /var and /private/var aliases must not bypass an explicit pin.
        self.cache_roots = [Path(p).resolve() for p in self.cache_roots]
        self.work_roots = [Path(p).resolve() for p in self.work_roots]
        self.state_root = Path(self.state_root).resolve()
        self.lease_root = Path(self.lease_root).resolve()
        self.volume = Path(self.volume).resolve()


def beneath(path, parent):
    try:
        Path(path).relative_to(Path(parent))
        return True
    except ValueError:
        return False


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def scan_tree(path):
    """Count cache use; reject preserved namespaces and special filesystem nodes."""
    path = Path(path)
    root = path.lstat()
    if path.is_symlink() or not path.is_dir():
        raise ValueError('cache root must be a real directory')
    count = 0
    allocated = 0
    latest = root.st_mtime
    stack = [path]
    while stack:
        folder = stack.pop()
        with os.scandir(str(folder)) as entries:
            for entry in entries:
                stat = entry.stat(follow_symlinks=False)
                name = entry.name.lower()
                relative = str(Path(entry.path).relative_to(path))
                if name in PROTECTED_CACHE_NAMES or name.endswith(PROTECTED_CACHE_SUFFIXES):
                    raise ProtectedCacheContent('protected cache content: ' + relative)
                count += 1
                allocated += stat.st_blocks * 512
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    latest = max(latest, stat.st_mtime)
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    latest = max(latest, stat.st_mtime, stat.st_atime)
                else:
                    raise ProtectedCacheContent('special filesystem node: ' + relative)
    return {'device': root.st_dev, 'inode': root.st_ino, 'allocated_bytes': allocated,
            'entry_count': count, 'latest_use': latest}


def open_directory_fd(path):
    """Anchor every ancestor without traversing a symlink, including rename races."""
    if not FD_OPERATIONS_AVAILABLE:
        raise RuntimeError('safe directory-fd operations unavailable')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open('/', flags)
    try:
        for component in Path(path).parts[1:]:
            following = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = following
        return fd
    except BaseException:
        os.close(fd)
        raise


def remove_tree_at(parent_fd, name, expected=None):
    """Remove only descendants of a held directory descriptor; links are unlinked."""
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        identity = os.fstat(fd)
        if expected is not None and (identity.st_dev, identity.st_ino) != expected:
            raise RuntimeError('cache identity changed during removal')
        with os.scandir(fd) as entries:
            for entry in entries:
                value = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(value.st_mode):
                    remove_tree_at(fd, entry.name, (value.st_dev, value.st_ino))
                else:
                    os.unlink(entry.name, dir_fd=fd)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
            raise RuntimeError('quarantine identity changed')
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)


@dataclasses.dataclass
class Activity:
    commands: list = dataclasses.field(default_factory=list)
    open_paths: list = dataclasses.field(default_factory=list)
    lease_refs: list = dataclasses.field(default_factory=list)
    errors: list = dataclasses.field(default_factory=list)

    @classmethod
    def collect(cls, lease_root=None):
        result = cls()
        own = os.getpid()
        try:
            p = subprocess.run(['ps', '-axww', '-o', 'pid=,ppid=,command='], capture_output=True, text=True, timeout=20)
            if p.returncode:
                raise RuntimeError('ps failed')
            for line in p.stdout.splitlines():
                fields = line.strip().split(None, 2)
                if len(fields) == 3 and fields[0].isdigit() and int(fields[0]) != own:
                    result.commands.append((int(fields[0]), fields[2]))
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as e:
            result.errors.append(type(e).__name__ + ': process inventory unavailable')
        try:
            p = subprocess.run(['/usr/sbin/lsof', '-nP', '-F', 'pn'], capture_output=True, text=True, timeout=40)
            if p.returncode or re.search(r'incomplete|permission denied|operation not permitted|can.t stat', p.stderr or '', re.I):
                raise RuntimeError('lsof incomplete')
            pid = None
            for line in p.stdout.splitlines():
                if line.startswith('p') and line[1:].isdigit():
                    pid = int(line[1:])
                elif line.startswith('n/') and pid is not None and pid != own:
                    result.open_paths.append((pid, line[1:].removesuffix(' (deleted)') if hasattr(str, 'removesuffix') else line[1:]))
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as e:
            result.errors.append(type(e).__name__ + ': open-file inventory unavailable')
        root = Path(lease_root or HOME / '.device-leases/leases')
        if root.exists():
            for file in root.glob('*.json'):
                try:
                    value = json.loads(file.read_text())
                    if not isinstance(value, dict):
                        raise ValueError('invalid lease')
                    result.lease_refs.append(str(value.get('cwd', '')) + ' ' + str(value.get('task', '')))
                except (OSError, ValueError):
                    result.errors.append('unreadable device lease')
        return result

    def reasons(self, scope, worktree=None):
        reasons = list(self.errors)
        scope = Path(scope)
        scopes = [scope] + ([Path(worktree)] if worktree else [])
        prefixes = [(str(p), str(p).rstrip('/') + '/') for p in scopes]
        for pid, path in self.open_paths:
            if any(path == exact or path.startswith(prefix) for exact, prefix in prefixes):
                reasons.append('open file/cwd held by PID ' + str(pid))
        for pid, cmd in self.commands:
            if any(str(p) in cmd for p in scopes):
                reasons.append('task referenced by PID ' + str(pid))
        task = scope.name
        # A repository basename identifies many independent worktrees. For a
        # known shared basename, use its dated task ancestor for lease text;
        # process/open-file protection above remains scoped to the exact tree.
        dated_task = next((p.name for p in scope.parents
                           if re.match(r'^\d{8}-.+', p.name)), None)
        if dated_task:
            task = dated_task
        suffix = re.sub(r'^\d{8}-', '', task)
        tokens = {task, suffix}
        # Long specific task fragments also cover leases recorded with human task names.
        if not dated_task:
            tokens.update(x for x in re.split('[-_]', suffix) if len(x) >= 5 and x not in {'memory', 'social', 'followups', 'entry', 'native', 'simulator'})
        for reference in self.lease_refs:
            if (any(t and t in reference for t in tokens) or str(scope) in reference or
                    (worktree and str(worktree) in reference)):
                reasons.append('devlease task is still held')
        return sorted(set(reasons))


class StorageManager:
    def __init__(self, settings=None):
        self.settings = settings or Settings()
        self.state_root = Path(self.settings.state_root)
        self.lock_path = self.state_root / 'maintenance.lock'
        self.pins_path = self.state_root / 'pins.json'

    def free_bytes(self):
        return shutil.disk_usage(str(self.settings.volume)).free

    def retention_days(self, free):
        if free < self.settings.critical_free_gib * GIB:
            return self.settings.critical_days
        if free < self.settings.warning_free_gib * GIB:
            return self.settings.pressure_days
        return self.settings.normal_days

    def pin(self, path, reason, owner, days=None):
        path = Path(path).resolve()
        if not reason.strip():
            raise ValueError('a pin needs a reason')
        if days is not None and days <= 0:
            raise ValueError('pin days must be positive')
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(self.state_root / 'pins.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            pins = json.loads(self.pins_path.read_text()) if self.pins_path.exists() else {}
            if str(path) in pins and pins[str(path)]['owner'] != owner:
                raise ValueError('pin belongs to another session; keep it')
            pins[str(path)] = {'reason': reason, 'owner': owner, 'created': time.time(),
                               'expires': time.time() + days * 86400 if days is not None else None}
            atomic_json(self.pins_path, pins)

    def unpin(self, path, owner):
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(self.state_root / 'pins.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            pins = json.loads(self.pins_path.read_text()) if self.pins_path.exists() else {}
            key = str(Path(path).resolve())
            if key in pins and pins[key]['owner'] != owner:
                raise ValueError('pin belongs to another session; keep it')
            pins.pop(key, None)
            atomic_json(self.pins_path, pins)

    def _pins(self, now):
        try:
            pins = json.loads(self.pins_path.read_text()) if self.pins_path.exists() else {}
            if not isinstance(pins, dict):
                raise ValueError('invalid pins')
            return {p: v for p, v in pins.items() if v.get('expires') is None or v['expires'] > now}, []
        except (OSError, ValueError, TypeError, AttributeError):
            return {}, ['pin file is unreadable; cleanup disabled']

    def _path_safe(self, path):
        path = Path(path)
        if not path.is_absolute() or '..' in path.parts or path.name not in LEAVES:
            return False
        if path.name == 'Intermediates.noindex' and path.parent.name != 'Build':
            return False
        for root in self.settings.cache_roots + self.settings.work_roots:
            root = Path(root)
            if not beneath(path, root) or path == root:
                continue
            # Worktree cleanup is allowed only under a generated build directory.
            if root in self.settings.work_roots and 'build' not in path.relative_to(root).parts:
                continue
            current = path
            while current != root.parent:
                if current.is_symlink():
                    return False
                current = current.parent
            return True
        return False

    def discover(self):
        result = []
        visited = set()
        for root in self.settings.cache_roots + self.settings.work_roots:
            root = Path(root)
            if not root.is_dir() or root.is_symlink():
                continue
            stack = [(root, 0)]
            while stack:
                folder, depth = stack.pop()
                if depth > 12 or folder in visited:
                    continue
                visited.add(folder)
                try:
                    with os.scandir(str(folder)) as entries:
                        for entry in entries:
                            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                                continue
                            path = Path(entry.path)
                            if entry.name in LEAVES:
                                if self._path_safe(path):
                                    result.append(path)
                                continue
                            if entry.name in SKIP or entry.name.startswith('.dev-storage-purge-'):
                                continue
                            # Only look for cache signatures; do not enumerate each build product.
                            stack.append((path, depth + 1))
                except OSError:
                    continue  # Missing roots are not candidates; failed candidate scans fail closed.
        return sorted(set(result), key=str)

    def _context(self, path):
        path = Path(path)
        for root in self.settings.cache_roots:
            root = Path(root)
            if not beneath(path, root):
                continue
            rel = path.relative_to(root).parts
            if rel and rel[0] == 'tasks' and len(rel) > 1:
                return root / 'tasks' / rel[1], self._workspace(path), None
            if len(rel) > 2 and rel[:2] == ('shared', 'xcode'):
                key = rel[2]
                manifest = self.state_root / 'slots' / (key + '.json')
                try:
                    data = json.loads(manifest.read_text())
                    if data.get('key') != key or not beneath(path, data['cache_path']):
                        raise ValueError('bad slot manifest')
                    return root / 'shared/xcode' / key, data['worktree'], key
                except (OSError, ValueError, KeyError, TypeError):
                    return root / 'shared/xcode' / key, None, 'INVALID'
            return root / rel[0] if rel else root, self._workspace(path), None
        # A worktree's generated output must be protected by uses of its project tree.
        parts = path.parts
        i = parts.index('build') if 'build' in parts else len(parts) - 1
        scope = Path(*parts[:i])
        return scope, scope, None

    def _workspace(self, path):
        for parent in list(Path(path).parents)[:5]:
            info = parent / 'info.plist'
            if not info.is_file():
                continue
            try:
                value = plistlib.loads(info.read_bytes()).get('WorkspacePath')
                if not isinstance(value, str) or not value.startswith('/'):
                    continue
                workspace = Path(value).resolve()
                for folder in workspace.parents:
                    if (folder / '.git').exists():
                        return folder
                return workspace.parent
            except (OSError, ValueError, plistlib.InvalidFileException):
                continue
        return None

    def _stats(self, path, slot):
        value = scan_tree(path)
        if slot and slot != 'INVALID':
            manifest = json.loads((self.state_root / 'slots' / (slot + '.json')).read_text())
            last = max(manifest.get('last_used') or 0, manifest.get('last_finished') or 0)
            value['slot_last_used'] = last
            value['latest_use'] = max(value['latest_use'], last)
        return value

    @staticmethod
    def _pin_reasons(pins, path, scope, worktree):
        for pin in pins:
            if (beneath(path, pin) or beneath(pin, scope) or
                    (worktree and (beneath(worktree, pin) or beneath(pin, worktree)))):
                return ['explicitly pinned for acceptance/use']
        return []

    def plan(self, activity=None, free_bytes=None, now=None):
        now = time.time() if now is None else now
        activity = activity if activity is not None else Activity.collect(self.settings.lease_root)
        free = self.free_bytes() if free_bytes is None else free_bytes
        days = self.retention_days(free)
        pins, pin_errors = self._pins(now)
        candidates = []
        for path in self.discover():
            scope, worktree, slot = self._context(path)
            reasons = activity.reasons(scope, worktree) + pin_errors
            if slot == 'INVALID':
                reasons.append('managed slot manifest missing or invalid')
            reasons += self._pin_reasons(pins, path, scope, worktree)
            try:
                stats = self._stats(path, slot)
                if now - stats['latest_use'] < days * 86400:
                    reasons.append('used within retention window')
            except ProtectedCacheContent as e:
                stats = {}
                reasons.append(str(e))
            except (OSError, ValueError) as e:
                stats = {}
                reasons.append('incomplete cache scan: ' + type(e).__name__)
            candidates.append({'path': str(path), 'scope': str(scope), 'worktree': str(worktree) if worktree else None,
                               'slot_key': slot, 'stats': stats, 'decision': 'keep' if reasons else 'eligible',
                               'reasons': sorted(set(reasons))})
        return {'schema_version': 1, 'created': now, 'free_bytes': free, 'retention_days': days,
                'target_free_bytes': self.settings.target_free_gib * GIB,
                'intermediate_budget_bytes': self.settings.intermediate_budget_gib * GIB,
                'candidate_accounting_bytes': sum(c['stats'].get('allocated_bytes', 0) for c in candidates),
                'eligible_accounting_bytes': sum(c['stats'].get('allocated_bytes', 0) for c in candidates if c['decision'] == 'eligible'),
                'activity_errors': activity.errors, 'candidates': candidates}

    @contextlib.contextmanager
    def _slot_lock(self, key):
        if key is None:
            yield True
            return
        if key == 'INVALID' or not re.fullmatch(r'[a-f0-9]{16,64}', key):
            yield False
            return
        root = self.state_root / 'slot-locks'
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(root / (key + '.lock'), 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def apply(self, plan, activity_factory=None, now=None, manual=False):
        now = time.time() if now is None else now
        collect = activity_factory or (lambda: Activity.collect(self.settings.lease_root))
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        result = {'schema_version': 1, 'time': now, 'status': 'ok', 'deleted': [], 'kept': [], 'errors': []}
        with open(self.lock_path, 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                result['status'] = 'busy'
                return result
            before = self.free_bytes()
            accounting_remaining = plan.get('candidate_accounting_bytes', 0)
            for candidate in sorted(plan['candidates'], key=lambda c: c.get('stats', {}).get('latest_use', float('inf'))):
                if candidate['decision'] != 'eligible':
                    continue
                path = Path(candidate['path'])
                if not self._path_safe(path):
                    result['kept'].append({'path': str(path), 'reason': 'path outside cache deletion contract'})
                    continue
                if (not manual and self.free_bytes() >= self.settings.target_free_gib * GIB and
                        accounting_remaining <= self.settings.intermediate_budget_gib * GIB):
                    break
                scope, worktree, slot = self._context(path)
                with self._slot_lock(slot) as acquired:
                    if not acquired:
                        result['kept'].append({'path': str(path), 'reason': 'build slot is locked'})
                        continue
                    try:
                        reasons = []
                        current = self._stats(path, slot)
                        if current != candidate['stats']:
                            reasons.append('cache changed since preview')
                        days = max(plan['retention_days'], self.retention_days(self.free_bytes()))
                        if now - current['latest_use'] < days * 86400:
                            reasons.append('retention window')
                        # Pins may be created while the potentially long tree scan runs.
                        # Recheck under the same lock as pin/unpin, through the rename.
                        with open(self.state_root / 'pins.lock', 'a') as pin_lock:
                            fcntl.flock(pin_lock, fcntl.LOCK_EX)
                            pins, pin_errors = self._pins(now)
                            reasons += pin_errors + self._pin_reasons(pins, path, scope, worktree)
                            reasons += collect().reasons(scope, worktree)
                            if reasons:
                                result['kept'].append({'path': str(path), 'reason': '; '.join(sorted(set(reasons)))})
                                continue
                            if not self._path_safe(path):
                                raise RuntimeError('cache path changed')
                            parent_fd = open_directory_fd(path.parent)
                            try:
                                identity = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
                                if (identity.st_dev, identity.st_ino) != (current['device'], current['inode']):
                                    raise RuntimeError('cache identity changed')
                                quarantine = path.with_name('.dev-storage-purge-' + path.name + '-' + uuid.uuid4().hex)
                                os.rename(path.name, quarantine.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                            except BaseException:
                                os.close(parent_fd)
                                raise
                        entry = {'path': str(path), 'quarantine': str(quarantine), 'directory_accounting_bytes': current['allocated_bytes']}
                        self._log({'event': 'delete_started', 'time': time.time(), **entry})
                        try:
                            remove_tree_at(parent_fd, quarantine.name, (current['device'], current['inode']))
                        finally:
                            os.close(parent_fd)
                        result['deleted'].append(entry)
                        accounting_remaining -= current['allocated_bytes']
                        self._log({'event': 'delete_complete', 'time': time.time(), **entry})
                    except ProtectedCacheContent as e:
                        result['kept'].append({'path': str(path), 'reason': str(e)})
                    except (OSError, ValueError, RuntimeError) as e:
                        result['errors'].append({'path': str(path), 'reason': str(e)})
                        # A partial or unknown deletion stops the run, never blindly retry.
                        break
            result['before_free_bytes'] = before
            result['after_free_bytes'] = self.free_bytes()
            result['observed_free_delta_bytes'] = result['after_free_bytes'] - before
            if result['errors']:
                result['status'] = 'error'
            elif result['after_free_bytes'] < self.settings.warning_free_gib * GIB:
                result['status'] = 'attention'
            atomic_json(self.state_root / 'last-maintenance.json', result)
        return result

    def _log(self, value):
        with open(self.state_root / 'actions.jsonl', 'a') as f:
            f.write(json.dumps(value, ensure_ascii=False) + '\n')


def summarize(plan):
    return {'free_gib': round(plan['free_bytes'] / GIB, 2), 'retention_days': plan['retention_days'],
            'rebuildable_accounting_gib': round(plan['candidate_accounting_bytes'] / GIB, 2),
            'eligible_accounting_gib': round(plan['eligible_accounting_bytes'] / GIB, 2),
            'eligible_count': sum(c['decision'] == 'eligible' for c in plan['candidates']),
            'kept_count': sum(c['decision'] == 'keep' for c in plan['candidates']),
            'activity_errors': plan['activity_errors']}
