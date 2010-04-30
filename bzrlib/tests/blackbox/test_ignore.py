# Copyright (C) 2006-2010 Canonical Ltd
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

"""UI tests for bzr ignore."""


from cStringIO import StringIO
import os
import re
import sys

from bzrlib import (
    ignores,
    osutils,
    )
import bzrlib
from bzrlib.branch import Branch
import bzrlib.bzrdir as bzrdir
from bzrlib.errors import BzrCommandError
from bzrlib.osutils import (
    pathjoin,
    )
from bzrlib.tests.test_sftp_transport import TestCaseWithSFTPServer
from bzrlib.tests.blackbox import ExternalBase
from bzrlib.workingtree import WorkingTree


class TestCommands(ExternalBase):

    def test_ignore_absolutes(self):
        """'ignore' with an absolute path returns an error"""
        self.make_branch_and_tree('.')
        self.run_bzr_error(('bzr: ERROR: NAME_PATTERN should not '
                            'be an absolute path\n',),
                           'ignore /crud')

    def test_ignore_directories(self):
        """ignoring a directory should ignore directory tree.

        Also check that trailing slashes on directories are stripped.
        """
        self.run_bzr('init')
        self.build_tree(['dir1/', 'dir1/foo',
                         'dir2/', 'dir2/bar',
                         'dir3/', 'dir3/baz'])
        self.run_bzr(['ignore', 'dir1', 'dir2/', 'dir4\\'])
        self.check_file_contents('.bzrignore', 'dir1\ndir2\ndir4\n')
        self.assertEquals(self.run_bzr('unknowns')[0], 'dir3\n')

    def test_ignore_patterns(self):
        tree = self.make_branch_and_tree('.')

        self.assertEquals(list(tree.unknowns()), [])

        # is_ignored() will now create the user global ignore file
        # if it doesn't exist, so make sure we ignore it in our tests
        ignores._set_user_ignores(['*.tmp'])

        self.build_tree_contents(
            [('foo.tmp', '.tmp files are ignored by default')])
        self.assertEquals(list(tree.unknowns()), [])

        self.build_tree_contents([('foo.c', 'int main() {}')])
        self.assertEquals(list(tree.unknowns()), ['foo.c'])

        tree.add('foo.c')
        self.assertEquals(list(tree.unknowns()), [])

        # 'ignore' works when creating the .bzrignore file
        self.build_tree_contents([('foo.blah', 'blah')])
        self.assertEquals(list(tree.unknowns()), ['foo.blah'])
        self.run_bzr('ignore *.blah')
        self.assertEquals(list(tree.unknowns()), [])
        self.check_file_contents('.bzrignore', '*.blah\n')

        # 'ignore' works when then .bzrignore file already exists
        self.build_tree_contents([('garh', 'garh')])
        self.assertEquals(list(tree.unknowns()), ['garh'])
        self.run_bzr('ignore garh')
        self.assertEquals(list(tree.unknowns()), [])
        self.check_file_contents('.bzrignore', '*.blah\ngarh\n')

    def test_ignore_multiple_arguments(self):
        """'ignore' works with multiple arguments"""
        tree = self.make_branch_and_tree('.')
        self.build_tree(['a','b','c','d'])
        self.assertEquals(list(tree.unknowns()), ['a', 'b', 'c', 'd'])
        self.run_bzr('ignore a b c')
        self.assertEquals(list(tree.unknowns()), ['d'])
        self.check_file_contents('.bzrignore', 'a\nb\nc\n')

    def test_ignore_no_arguments(self):
        """'ignore' with no arguments returns an error"""
        self.make_branch_and_tree('.')
        self.run_bzr_error(('bzr: ERROR: ignore requires at least one '
                            'NAME_PATTERN or --default-rules.\n',),
                           'ignore')

    def test_ignore_default_rules(self):
        out, err = self.run_bzr(['ignore', '--default-rules'])
        reference_set = set(ignores.USER_DEFAULTS)
        output_set = set(out.rstrip().split('\n'))
        self.assertEqual(reference_set, output_set)
        self.assertEqual('', err)

    def test_ignore_versioned_file(self):
        tree = self.make_branch_and_tree('.')
        self.build_tree(['a','b'])
        tree.add('a')

        # test a single versioned file
        out, err = self.run_bzr('ignore a')
        self.assertEqual(out,
                         "Warning: the following files are version controlled"\
                         " and match your ignore pattern:\na\n"\
                         "These files will continue to be version controlled"\
                         " unless you 'bzr remove' them.\n")

        # test a single unversioned file
        out, err = self.run_bzr('ignore b')
        self.assertEqual(out, '')

        # test wildcards
        tree.add('b')
        out, err = self.run_bzr('ignore *')
        self.assertEqual(out,
                         "Warning: the following files are version controlled"\
                         " and match your ignore pattern:\n.bzrignore\na\nb\n"\
                         "These files will continue to be version controlled"\
                         " unless you 'bzr remove' them.\n")

    def test_ignored_versioned_file_matching_new_pattern(self):
        tree = self.make_branch_and_tree('.')
        self.build_tree(['a', 'b'])
        tree.add(['a', 'b'])
        self.run_bzr('ignore *')

        # If only the given pattern is used then only 'b' should match in
        # this case.
        out, err = self.run_bzr('ignore b')
        self.assertEqual(out,
                         "Warning: the following files are version controlled"\
                         " and match your ignore pattern:\nb\n"\
                         "These files will continue to be version controlled"\
                         " unless you 'bzr remove' them.\n")
