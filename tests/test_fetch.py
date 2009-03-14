# Copyright (C) 2009 Jelmer Vernooij <jelmer@samba.org>
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

import os

from bzrlib.bzrdir import BzrDir
from bzrlib.repository import Repository
from bzrlib.tests import TestCaseWithTransport

from bzrlib.plugins.git import (
    get_rich_root_format,
    )
from bzrlib.plugins.git.fetch import BzrFetchGraphWalker
from bzrlib.plugins.git.mapping import default_mapping
from bzrlib.plugins.git.tests import (
    GitBranchBuilder,
    run_git,
    )


class FetchGraphWalkerTests(TestCaseWithTransport):

    def setUp(self):
        TestCaseWithTransport.setUp(self)
        self.mapping = default_mapping

    def test_empty(self):
        tree = self.make_branch_and_tree("wt")
        graphwalker = BzrFetchGraphWalker(tree.branch.repository, self.mapping)
        self.assertEquals(None, graphwalker.next())


class RepositoryFetchTests(TestCaseWithTransport):

    def make_git_repo(self, path):
        os.mkdir(path)
        os.chdir(path)
        run_git("init")
        os.chdir("..")

    def clone_git_repo(self, from_url, to_url):
        oldrepos = Repository.open(from_url)
        dir = BzrDir.create(to_url, get_rich_root_format())
        newrepos = dir.create_repository()
        oldrepos.copy_content_into(newrepos)
        return newrepos

    def test_empty(self):
        self.make_git_repo("d")
        newrepos = self.clone_git_repo("d", "f")
        self.assertEquals([], newrepos.all_revision_ids())

    def test_single_rev(self):
        self.make_git_repo("d")
        os.chdir("d")
        bb = GitBranchBuilder()
        bb.set_file("foobar", "foo\nbar\n", False)
        mark = bb.commit("Somebody <somebody@someorg.org>", "mymsg")
        gitsha = bb.finish()[mark]
        os.chdir("..")
        oldrepo = Repository.open("d")
        newrepo = self.clone_git_repo("d", "f")
        self.assertEquals([oldrepo.get_mapping().revision_id_foreign_to_bzr(gitsha)], newrepo.all_revision_ids())


