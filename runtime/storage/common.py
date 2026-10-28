"""Existing Xcode caches: manual cleanup and the periodic service tick, never dev-storage maintain."""
import os
import re
from pathlib import Path

from storage import Activity, StorageManager
from portable_config import configure


class CommonActivity(Activity):
    def reasons(self, scope, worktree=None):
        reasons = super().reasons(scope, worktree)
        # Unwrapped builds cannot share our slot lock. Keep all standard caches
        # while any build tool is alive, even before it opens its first output.
        if any(re.match(r'^(?:/[^\n]*?/)?(?:xcodebuild|clang(?:\+\+)?|swift-frontend|swiftc)(?:\s|$)', command)
               for _, command in self.commands):
            reasons.append('检测到编译进程；等待构建结束再清理已有缓存')
        return sorted(set(reasons))


def activity(lease_root):
    base = Activity.collect(lease_root)
    return CommonActivity(**vars(base))


class CacheUnavailable(ValueError):
    """The standard cache links to a location that is absent now, e.g. an unplugged drive."""


def standard_root(home, settings):
    root = home / 'Library/Developer/Xcode/DerivedData'
    target = root.resolve()
    if target == root:
        return root
    # A relocated cache (often on an external drive) is cleaned where it really lives,
    # unless the link widens the scope to a home, a whole volume or our own roots.
    owned = [Path(p) for p in settings.cache_roots] + [Path(settings.state_root)]
    if (target == Path(target.anchor) or target == home or target in home.parents or os.path.ismount(target) or
            any(target == p or target in p.parents or p in target.parents for p in owned)):
        raise ValueError('Xcode 缓存链接指向的范围过大：{}；这一类不清理'.format(target))
    if not target.is_dir():
        raise CacheUnavailable('Xcode 缓存链接指向的位置不存在：{}（外接盘未连接？）；本轮跳过'.format(target))
    try:
        os.listdir(target)
    except PermissionError:
        raise ValueError('没有读取 {} 的权限：请在系统提示里允许「资源看板」访问可移除宗卷；这一类本轮未清理'.format(target))
    return target


def manager(home):
    settings = configure(home)
    home = Path(home).resolve()
    settings.cache_roots = [standard_root(home, settings)]
    settings.work_roots = []
    # Native caches stay conservative even if the disk is critically low.
    settings.normal_days = settings.pressure_days = settings.critical_days = 7
    return StorageManager(settings)


def preview(home):
    storage = manager(home)
    return storage.plan(activity=activity(storage.settings.lease_root))


def apply(home, plan):
    storage = manager(home)
    return storage.apply(plan, activity_factory=lambda: activity(storage.settings.lease_root), manual=True)
