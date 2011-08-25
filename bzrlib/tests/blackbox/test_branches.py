# Copyright (C) 2011 Canonical Ltd
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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA


"""Black-box tests for bzr branches."""

from bzrlib.tests import TestCaseWithTransport


class TestBranches(TestCaseWithTransport):

    def test_no_colocated_support(self):
        # Listing the branches in a control directory without colocated branch
        # support.
        self.run_bzr('init a')
        out, err = self.run_bzr('branches a')
        self.assertEquals(out, " (default)\n")

    def test_no_branch(self):
        # Listing the branches in a control directory without branches.
        self.run_bzr('init-repo a')
        out, err = self.run_bzr('branches a')
        self.assertEquals(out, "")

    def test_default_current_dir(self):
        # "bzr branches" list the branches in the current directory
        # if no location was specified.
        self.run_bzr('init-repo a')
        out, err = self.run_bzr('branches', working_dir='a')
        self.assertEquals(out, "")