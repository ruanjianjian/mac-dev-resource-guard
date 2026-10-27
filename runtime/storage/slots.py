"""Stable Xcode build slots; only run_xcode acquires a build lease.

slot_identity and slot_paths are read-only queries. A path returned by either
function does not authorize building in, or cleaning, the corresponding slot.
"""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time


DEFAULT_CACHE_ROOT = Path.home() / "Library/Caches/dev-resource-guard/shared/xcode"
DEFAULT_STATE_ROOT = Path.home() / ".local" / "state" / "dev-storage"


def _canonical(path):
    return Path(path).expanduser().resolve()


def _label(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name + " must be a nonempty string")
    return value.strip()


def _developer_path(value):
    path = _canonical(value)
    # DEVELOPER_DIR accepts either Xcode.app or its Developer directory.
    if path.suffix == ".app" and (path / "Contents/Developer").is_dir():
        path = (path / "Contents/Developer").resolve()
    return str(path)


def _toolchain_identity():
    developer_dir = os.environ.get("DEVELOPER_DIR", "")
    developer_identity = _developer_path(developer_dir) if developer_dir else ""
    if developer_dir:
        environment = dict(os.environ)
        environment.pop("DEVELOPER_DIR", None)
        try:
            selected = subprocess.run(
                ["xcode-select", "--print-path"], capture_output=True, text=True,
                check=True, timeout=5, env=environment,
            ).stdout.strip()
            if Path(selected).is_absolute() and _developer_path(selected) == developer_identity:
                # Keep the existing implicit-default identity and all its slot
                # keys. Setting the same Xcode explicitly must not fork it.
                developer_identity = ""
        except (OSError, subprocess.SubprocessError):
            # Identity lookup must not add a build gate. Retain the explicit
            # path when the system selection cannot be established.
            pass
    version = subprocess.run(
        ["xcodebuild", "-version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if not version:
        raise ValueError("xcodebuild -version returned an empty identity")
    return {
        "developer_dir": developer_identity,
        "xcodebuild_version": version,
    }


def slot_identity(worktree, platform, configuration, toolchain=None, component=None):
    """Return the canonical build identity and its SHA-256 key, without a lease."""
    worktree_path = _canonical(worktree)
    if not worktree_path.is_dir():
        raise ValueError("worktree must be an existing directory")
    # Round-tripping also prevents callers mutating a supplied mapping after the
    # hash was calculated. Only JSON values form part of a toolchain identity.
    toolchain_value = _toolchain_identity() if toolchain is None else toolchain
    toolchain_value = json.loads(json.dumps(toolchain_value, sort_keys=True, allow_nan=False))
    if not toolchain_value:
        raise ValueError("toolchain identity must not be empty")
    dimensions = {
        "worktree": str(worktree_path),
        "toolchain": toolchain_value,
        "platform": _label(platform, "platform"),
        "configuration": _label(configuration, "configuration"),
    }
    if component is not None:
        dimensions["component"] = _label(component, "component")
    encoded = json.dumps(dimensions, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {"schema_version": 1, "key": hashlib.sha256(encoded.encode("utf-8")).hexdigest(), **dimensions}


def slot_paths(identity, cache_root=None, state_root=None):
    """Return paths without creating directories or acquiring the slot lock."""
    key = identity.get("key", "")
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
        raise ValueError("invalid slot key")
    cache = _canonical(DEFAULT_CACHE_ROOT if cache_root is None else cache_root)
    state = _canonical(DEFAULT_STATE_ROOT if state_root is None else state_root)
    parts = cache.parts
    # A cache on an unplugged drive must fail loudly, never fall back to the internal disk.
    if len(parts) > 2 and parts[1] == "Volumes" and not os.path.ismount(os.path.join("/", parts[1], parts[2])):
        raise ValueError("缓存所在的盘未连接：" + str(cache))
    lock = state / "slot-locks" / (key + ".lock")
    manifest = state / "slots" / (key + ".json")
    cache_path = cache / key / "DerivedData"
    if cache_path.resolve() != cache_path:
        raise ValueError("managed slot paths must not contain symlinks")
    if lock.resolve().is_relative_to(cache) or manifest.resolve().is_relative_to(cache):
        raise ValueError("slot locks and manifests must be outside the cache root")
    return {
        "key": key,
        "cache_path": str(cache_path),
        "lock_path": str(lock),
        "manifest_path": str(manifest),
    }


def _build_command(command, cache_path, worktree):
    if isinstance(command, (str, bytes)):
        raise ValueError("command must be an argv sequence")
    try:
        arguments = list(command)
    except TypeError as error:
        raise ValueError("command must be an argv sequence") from error
    if not arguments or any(not isinstance(value, str) or not value or "\0" in value for value in arguments):
        raise ValueError("command must contain nonempty string arguments")
    executable = Path(arguments[0]).name
    direct = executable == "xcodebuild"
    through_xcrun = executable == "xcrun" and len(arguments) > 1 and Path(arguments[1]).name == "xcodebuild"
    if not direct and not through_xcrun:
        raise ValueError("only xcodebuild or xcrun xcodebuild is supported")
    normalized = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        supplied = None
        if argument == "-derivedDataPath":
            index += 1
            if index >= len(arguments):
                raise ValueError("-derivedDataPath requires a value")
            supplied = arguments[index]
        elif argument.startswith("-derivedDataPath="):
            supplied = argument.split("=", 1)[1]
        if supplied is not None:
            supplied_path = Path(supplied).expanduser()
            if not supplied_path.is_absolute():
                supplied_path = worktree / supplied_path
            if not supplied or str(supplied_path.resolve()) != cache_path:
                raise ValueError("-derivedDataPath must match the managed slot")
        else:
            normalized.append(argument)
        index += 1
    return [*normalized, "-derivedDataPath", cache_path]


def _write_manifest(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="." + path.stem + "-", suffix=".tmp", dir=path.parent, delete=False
        ) as output:
            temporary = Path(output.name)
            json.dump(value, output, sort_keys=True, ensure_ascii=False, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _previous_finish(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("last_finished")
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    except (OSError, ValueError, AttributeError):
        return None


def _stop_child(child):
    if child.poll() is not None:
        return
    try:
        child.terminate()
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            child.kill()
        except ProcessLookupError:
            pass
        child.wait()


def run_xcode(worktree, platform, configuration, command, cache_root=None, state_root=None, toolchain=None):
    """Run one Xcode build while holding its stable, cross-process slot lock.

    The manifest contains build identity, paths, timestamps and the current child
    PID only. Build argv and environment values are never copied into it.
    Nonzero child status is returned; startup and execution exceptions propagate
    after the child is stopped, the PID is cleared, and the lock is released.
    """
    identity = slot_identity(worktree, platform, configuration, toolchain)
    paths = slot_paths(identity, cache_root, state_root)
    arguments = _build_command(command, paths["cache_path"], Path(identity["worktree"]))
    return run_in_slot(identity, arguments, cache_root, state_root)


def run_script(worktree, platform, configuration, command, env_name, cache_root=None, state_root=None, toolchain=None):
    """Run a whole build script in the slot; the script learns the cache path from env_name."""
    if not isinstance(env_name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
        raise ValueError("环境变量名不合法：" + str(env_name))
    identity = slot_identity(worktree, platform, configuration, toolchain)
    paths = slot_paths(identity, cache_root, state_root)
    arguments = list(command)
    if not arguments or any(not isinstance(value, str) or not value or "\0" in value for value in arguments):
        raise ValueError("command must contain nonempty string arguments")
    env = dict(os.environ, **{env_name: paths["cache_path"]})
    return run_in_slot(identity, arguments, cache_root, state_root, env=env, script=True)


def run_in_slot(identity, arguments, cache_root=None, state_root=None, env=None, cache_path=None, script=False):
    """Hold the existing managed lease for an entire build command or script."""
    paths = slot_paths(identity, cache_root, state_root)
    if cache_path is not None:
        paths["cache_path"] = str(_canonical(cache_path))
    lock_path = Path(paths["lock_path"])
    manifest_path = Path(paths["manifest_path"])
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(lock_fd, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            successes = int(json.loads(manifest_path.read_text()).get('successful_builds', 0))
        except (OSError, ValueError, TypeError):
            successes = 0
        manifest = {
            **identity,
            "cache_path": paths["cache_path"],
            "last_used": time.time(),
            "last_finished": _previous_finish(manifest_path),
            "pid": None,
            "successful_builds": successes,
        }
        child = None
        try:
            Path(paths["cache_path"]).mkdir(parents=True, exist_ok=True)
            _write_manifest(manifest_path, manifest)
            child = subprocess.Popen(
                arguments,
                cwd=identity["worktree"],
                shell=False,
                env=env,
                # Keep the lease alive if the wrapper is abruptly terminated
                # while the actual build process is still running.
                pass_fds=(lock.fileno(),),
            )
            manifest["pid"] = child.pid
            _write_manifest(manifest_path, manifest)
            code = child.wait()
            build_actions = {'build', 'build-for-testing', 'test', 'archive', 'analyze', 'install'}
            query_flags = {'-list', '-showBuildSettings', '-showdestinations', '-showsdks', '-version', '-help', '-resolvePackageDependencies'}
            # A whole script has no xcodebuild verb to inspect: its success is the build's success.
            if code == 0 and (script or build_actions.intersection(arguments) and not query_flags.intersection(arguments)):
                manifest['successful_builds'] += 1
            return code
        finally:
            try:
                if child is not None:
                    _stop_child(child)
            finally:
                manifest["pid"] = None
                manifest["last_finished"] = time.time()
                _write_manifest(manifest_path, manifest)
