# Copyright (C) 2007 Canonical Ltd
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA

"""Tests for Tree.get_root_id()"""

from bzrlib.tests.tree_implementations import TestCaseWithTree


class TestGetRootID(TestCaseWithTree):

    def get_tree_with_default_root_id(self):
        tree = self.make_branch_and_tree('tree')
        return self._convert_tree(tree)

    def get_tree_with_fixed_root_id(self):
        tree = self.make_branch_and_tree('tree')
        tree.set_root_id('custom-tree-root-id')
        return self._convert_tree(tree)

    def test_get_root_id_default(self):
        tree = self.get_tree_with_default_root_id()
        tree.lock_read()
        self.addCleanup(tree.unlock)
        self.assertIsNot(None, tree.get_root_id())

    def test_get_root_id_fixed(self):
        tree = self.get_tree_with_fixed_root_id()
        tree.lock_read()
        self.addCleanup(tree.unlock)
        self.assertEqual('custom-tree-root-id', tree.get_root_id())

