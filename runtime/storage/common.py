"""Explicit manual maintenance of existing Xcode caches; never used by tick/maintain."""
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


def manager(home):
    settings = configure(home)
    home = Path(home).resolve()
    root = home / 'Library/Developer/Xcode/DerivedData'
    if root.resolve() != root:
        raise ValueError('Xcode 缓存路径包含符号链接；请核实路径，未启用清理')
    settings.cache_roots = [root]
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
