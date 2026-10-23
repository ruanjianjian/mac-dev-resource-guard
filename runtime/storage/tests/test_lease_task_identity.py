import unittest
from pathlib import Path

from storage import Activity


class LeaseTaskIdentityTests(unittest.TestCase):
    old = Path('/DevData/work/worktrees/20260101-task-a/SampleApp-minteg')

    def test_same_repository_basename_in_other_task_is_not_an_occupancy(self):
        activity = Activity(lease_refs=['/DevData/work/20260102-task-b/SampleApp mobile acceptance'])
        self.assertEqual(activity.reasons(self.old, self.old), [])

    def test_exact_tree_reference_still_protects(self):
        activity = Activity(lease_refs=[str(self.old) + ' acceptance'])
        self.assertIn('devlease task is still held', activity.reasons(self.old, self.old))

    def test_task_name_without_repository_path_still_protects(self):
        activity = Activity(lease_refs=['20260101-task-a awaiting device acceptance'])
        self.assertIn('devlease task is still held', activity.reasons(self.old, self.old))

    def test_unknown_repository_context_remains_conservative(self):
        repo = Path('/unclassified/SampleApp')
        activity = Activity(lease_refs=['SampleApp acceptance'])
        self.assertIn('devlease task is still held', activity.reasons(repo, repo))

    def test_open_file_in_exact_repository_still_protects(self):
        activity = Activity(open_paths=[(42, str(self.old / 'build' / 'object.o'))])
        self.assertIn('open file/cwd held by PID 42', activity.reasons(self.old, self.old))
