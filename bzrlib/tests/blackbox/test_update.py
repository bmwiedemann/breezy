# Copyright (C) 2006, 2009 Canonical Ltd
# -*- coding: utf-8 -*-
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


"""Tests for the update command of bzr."""

import os
import re

from bzrlib import (
    branch,
    bzrdir,
    osutils,
    tests,
    urlutils,
    workingtree,
    )
from bzrlib.tests.script import ScriptRunner


class TestUpdate(tests.TestCaseWithTransport):

    def test_update_standalone_trivial(self):
        self.make_branch_and_tree('.')
        out, err = self.run_bzr('update')
        self.assertEqual(
            'Tree is up to date at revision 0 of branch %s\n' % self.test_dir,
            err)
        self.assertEqual('', out)

    def test_update_quiet(self):
        self.make_branch_and_tree('.')
        out, err = self.run_bzr('update --quiet')
        self.assertEqual('', err)
        self.assertEqual('', out)

    def test_update_standalone_trivial_with_alias_up(self):
        self.make_branch_and_tree('.')
        out, err = self.run_bzr('up')
        self.assertEqual('Tree is up to date at revision 0 of branch %s\n'
                         % self.test_dir,
                         err)
        self.assertEqual('', out)

    def test_update_up_to_date_light_checkout(self):
        self.make_branch_and_tree('branch')
        self.run_bzr('checkout --lightweight branch checkout')
        out, err = self.run_bzr('update checkout')
        self.assertEqual('Tree is up to date at revision 0 of branch %s\n'
                         % osutils.pathjoin(self.test_dir, 'branch'),
                         err)
        self.assertEqual('', out)

    def test_update_up_to_date_checkout(self):
        self.make_branch_and_tree('branch')
        self.run_bzr('checkout branch checkout')
        sr = ScriptRunner()
        sr.run_script(self, '''
$ bzr update checkout
2>Tree is up to date at revision 0 of branch .../branch
''')

    def test_update_out_of_date_standalone_tree(self):
        # FIXME the default format has to change for this to pass
        # because it currently uses the branch last-revision marker.
        self.make_branch_and_tree('branch')
        # make a checkout
        self.run_bzr('checkout --lightweight branch checkout')
        self.build_tree(['checkout/file'])
        self.run_bzr('add checkout/file')
        self.run_bzr('commit -m add-file checkout')
        # now branch should be out of date
        out,err = self.run_bzr('update branch')
        self.assertEqual('', out)
        self.assertEqualDiff("""+N  file
All changes applied successfully.
Updated to revision 1 of branch %s
""" % osutils.pathjoin(self.test_dir, 'branch',),
                         err)
        self.failUnlessExists('branch/file')

    def test_update_out_of_date_light_checkout(self):
        self.make_branch_and_tree('branch')
        # make two checkouts
        self.run_bzr('checkout --lightweight branch checkout')
        self.run_bzr('checkout --lightweight branch checkout2')
        self.build_tree(['checkout/file'])
        self.run_bzr('add checkout/file')
        self.run_bzr('commit -m add-file checkout')
        # now checkout2 should be out of date
        out,err = self.run_bzr('update checkout2')
        self.assertEqualDiff('''+N  file
All changes applied successfully.
Updated to revision 1 of branch %s
''' % osutils.pathjoin(self.test_dir, 'branch',),
                         err)
        self.assertEqual('', out)

    def test_update_conflicts_returns_2(self):
        self.make_branch_and_tree('branch')
        # make two checkouts
        self.run_bzr('checkout --lightweight branch checkout')
        self.build_tree(['checkout/file'])
        self.run_bzr('add checkout/file')
        self.run_bzr('commit -m add-file checkout')
        self.run_bzr('checkout --lightweight branch checkout2')
        # now alter file in checkout
        a_file = file('checkout/file', 'wt')
        a_file.write('Foo')
        a_file.close()
        self.run_bzr('commit -m checnge-file checkout')
        # now checkout2 should be out of date
        # make a local change to file
        a_file = file('checkout2/file', 'wt')
        a_file.write('Bar')
        a_file.close()
        out,err = self.run_bzr('update checkout2', retcode=1)
        self.assertEqualDiff(''' M  file
Text conflict in file
1 conflicts encountered.
Updated to revision 2 of branch %s
''' % osutils.pathjoin(self.test_dir, 'branch',),
                         err)
        self.assertEqual('', out)

    def test_smoke_update_checkout_bound_branch_local_commits(self):
        # smoke test for doing an update of a checkout of a bound
        # branch with local commits.
        master = self.make_branch_and_tree('master')
        # make a bound branch
        self.run_bzr('checkout master child')
        # get an object form of child
        child = workingtree.WorkingTree.open('child')
        # check that out
        self.run_bzr('checkout --lightweight child checkout')
        # get an object form of the checkout to manipulate
        wt = workingtree.WorkingTree.open('checkout')
        # change master
        a_file = file('master/file', 'wt')
        a_file.write('Foo')
        a_file.close()
        master.add(['file'])
        master_tip = master.commit('add file')
        # change child
        a_file = file('child/file_b', 'wt')
        a_file.write('Foo')
        a_file.close()
        child.add(['file_b'])
        child_tip = child.commit('add file_b', local=True)
        # check checkout
        a_file = file('checkout/file_c', 'wt')
        a_file.write('Foo')
        a_file.close()
        wt.add(['file_c'])

        # now, update checkout ->
        # get all three files and a pending merge.
        out, err = self.run_bzr('update checkout')
        self.assertEqual('', out)
        self.assertEqualDiff("""+N  file_b
All changes applied successfully.
+N  file
All changes applied successfully.
Updated to revision 1 of branch %s
Your local commits will now show as pending merges with 'bzr status', and can be committed with 'bzr commit'.
""" % osutils.pathjoin(self.test_dir, 'master',),
                         err)
        self.assertEqual([master_tip, child_tip], wt.get_parent_ids())
        self.failUnlessExists('checkout/file')
        self.failUnlessExists('checkout/file_b')
        self.failUnlessExists('checkout/file_c')
        self.assertTrue(wt.has_filename('file_c'))

    def test_update_with_merges(self):
        # Test that 'bzr update' works correctly when you have
        # an update in the master tree, and a lightweight checkout
        # which has merged another branch
        master = self.make_branch_and_tree('master')
        self.build_tree(['master/file'])
        master.add(['file'])
        master.commit('one', rev_id='m1')

        self.build_tree(['checkout1/'])
        checkout_dir = bzrdir.BzrDirMetaFormat1().initialize('checkout1')
        branch.BranchReferenceFormat().initialize(checkout_dir, master.branch)
        checkout1 = checkout_dir.create_workingtree('m1')

        # Create a second branch, with an extra commit
        other = master.bzrdir.sprout('other').open_workingtree()
        self.build_tree(['other/file2'])
        other.add(['file2'])
        other.commit('other2', rev_id='o2')

        # Create a new commit in the master branch
        self.build_tree(['master/file3'])
        master.add(['file3'])
        master.commit('f3', rev_id='m2')

        # Merge the other branch into checkout
        os.chdir('checkout1')
        self.run_bzr('merge ../other')

        self.assertEqual(['o2'], checkout1.get_parent_ids()[1:])

        # At this point, 'commit' should fail, because we are out of date
        self.run_bzr_error(["please run 'bzr update'"],
                           'commit -m merged')

        # This should not report about local commits being pending
        # merges, because they were real merges
        out, err = self.run_bzr('update')
        self.assertEqual('', out)
        self.assertEqualDiff('''+N  file3
All changes applied successfully.
Updated to revision 2 of branch %s
''' % osutils.pathjoin(self.test_dir, 'master',),
                         err)
        # The pending merges should still be there
        self.assertEqual(['o2'], checkout1.get_parent_ids()[1:])

    def test_readonly_lightweight_update(self):
        """Update a light checkout of a readonly branch"""
        tree = self.make_branch_and_tree('branch')
        readonly_branch = branch.Branch.open(self.get_readonly_url('branch'))
        checkout = readonly_branch.create_checkout('checkout',
                                                   lightweight=True)
        tree.commit('empty commit')
        self.run_bzr('update checkout')

    def test_update_dash_r(self):
        master = self.make_branch_and_tree('master')
        os.chdir('master')
        self.build_tree(['./file1'])
        master.add(['file1'])
        master.commit('one', rev_id='m1')
        self.build_tree(['./file2'])
        master.add(['file2'])
        master.commit('two', rev_id='m2')

        sr = ScriptRunner()
        sr.run_script(self, '''
$ bzr update -r 1
2>-D  file2
2>All changes applied successfully.
2>Updated to revision 1 of .../master
''')
        self.failUnlessExists('./file1')
        self.failIfExists('./file2')
        self.assertEquals(['m1'], master.get_parent_ids())

    def test_update_dash_r_outside_history(self):
        master = self.make_branch_and_tree('master')
        self.build_tree(['master/file1'])
        master.add(['file1'])
        master.commit('one', rev_id='m1')

        # Create a second branch, with an extra commit
        other = master.bzrdir.sprout('other').open_workingtree()
        self.build_tree(['other/file2'])
        other.add(['file2'])
        other.commit('other2', rev_id='o2')

        os.chdir('master')
        self.run_bzr('merge ../other')
        master.commit('merge', rev_id='merge')

        out, err = self.run_bzr('update -r revid:o2',
                                retcode=3)
        self.assertEqual('', out)
        self.assertEqual('bzr: ERROR: branch has no revision o2\n'
                         'bzr update --revision only works'
                         ' for a revision in the branch history\n',
                         err)

    def test_update_dash_r_in_master(self):
        # Test that 'bzr update' works correctly when you have
        # an update in the master tree,
        master = self.make_branch_and_tree('master')
        self.build_tree(['master/file1'])
        master.add(['file1'])
        master.commit('one', rev_id='m1')

        self.run_bzr('checkout master checkout')

        # add a revision in the master.
        self.build_tree(['master/file2'])
        master.add(['file2'])
        master.commit('two', rev_id='m2')

        os.chdir('checkout')
        sr = ScriptRunner()
        sr.run_script(self, '''
$ bzr update -r revid:m2
2>+N  file2
2>All changes applied successfully.
2>Updated to revision 2 of branch .../master
''')

    def test_update_checkout_prevent_double_merge(self):
        """"Launchpad bug 113809 in bzr "update performs two merges"
        https://launchpad.net/bugs/113809"""
        master = self.make_branch_and_tree('master')
        self.build_tree_contents([('master/file', 'initial contents\n')])
        master.add(['file'])
        master.commit('one', rev_id='m1')

        checkout = master.branch.create_checkout('checkout')
        lightweight = checkout.branch.create_checkout('lightweight',
                                                      lightweight=True)

        # time to create a mess
        # add a commit to the master
        self.build_tree_contents([('master/file', 'master\n')])
        master.commit('two', rev_id='m2')
        self.build_tree_contents([('master/file', 'master local changes\n')])

        # local commit on the checkout
        self.build_tree_contents([('checkout/file', 'checkout\n')])
        checkout.commit('tree', rev_id='c2', local=True)
        self.build_tree_contents([('checkout/file',
                                   'checkout local changes\n')])

        # lightweight 
        self.build_tree_contents([('lightweight/file',
                                   'lightweight local changes\n')])

        # now update (and get conflicts)
        out, err = self.run_bzr('update lightweight', retcode=1)
        self.assertEqual('', out)
        self.assertFileEqual('''\
<<<<<<< TREE
lightweight local changes
=======
checkout
>>>>>>> MERGE-SOURCE
''',
                             'lightweight/file')

        # resolve it
        self.build_tree_contents([('lightweight/file',
                                   'lightweight+checkout\n')])
        self.run_bzr('resolve lightweight/file')

        # check we get the second conflict
        out, err = self.run_bzr('update lightweight', retcode=1)
        self.assertEqual('', out)
        self.assertFileEqual('''\
<<<<<<< TREE
lightweight+checkout
=======
master
>>>>>>> MERGE-SOURCE
''',
                             'lightweight/file')

    def test_update_remove_commit(self):
        """Update should remove revisions when the branch has removed
        some commits.

        B: wt.branch.last_revision()
        M: wt.branch.get_master_branch().last_revision()
        W: wt.last_revision()


            1
            |\
          B-2 3
            | |
            4 5-M
            |
            W

        What you want is to revert 4, as in this graph the final result should
        be equivalent to:

           1
           |\
           3 2
           | |\
        MB-5 | 4
           |/
           W

        And the changes in 4 have been removed from the WT.
        """

        # building the picture
        M = self.make_branch_and_tree('M')
        self.build_tree_contents([('M/file1', '1')])
        M.add(['file1'])
        M.commit('one', rev_id='c1')

        B = M.branch.create_checkout('B')
        self.build_tree_contents([('B/file2', '2')])
        B.add(['file2'])
        B.commit('two', rev_id='c2', local=True)
        self.build_tree_contents([('B/file4', '4')])
        B.add(['file4'])
        B.commit('four', rev_id='c4', local=True)

        W = B.branch.create_checkout('W', lightweight=True)
        # move B back to 2
        B.pull(B.branch, local=True, stop_revision='c2', overwrite=True)

        self.build_tree_contents([('M/file3', '3')])
        M.add(['file3'])
        M.commit('three', rev_id='c3')
        self.build_tree_contents([('M/file5', '5')])
        M.add(['file5'])
        M.commit('five', rev_id='c5')

        # now check if everything is as we expect it to be (test the test)
        # M has 1,3,5
        self.failUnlessExists('M/file1')
        self.failIfExists(    'M/file2')
        self.failUnlessExists('M/file3')
        self.failIfExists(    'M/file4')
        self.failUnlessExists('M/file5')
        # B has 1,2
        self.failUnlessExists('B/file1')
        self.failUnlessExists('B/file2')
        self.failIfExists(    'B/file3')
        self.failIfExists(    'B/file4')
        self.failIfExists(    'B/file5')
        # W has 1,2,4
        self.failUnlessExists('W/file1')
        self.failUnlessExists('W/file2')
        self.failIfExists(    'W/file3')
        self.failUnlessExists('W/file4')
        self.failIfExists(    'W/file5')

        out, err = self.run_bzr('update W', retcode=0)
        self.assertEqual('', out)
        self.failUnlessExists('W/file1')
        self.failUnlessExists('W/file2')
        self.failUnlessExists('W/file3')
        self.failIfExists('W/file4')
        self.failUnlessExists('W/file5')
