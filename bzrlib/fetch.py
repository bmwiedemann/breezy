# Copyright (C) 2005 by Canonical Ltd

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA

from copy import copy
import os
from cStringIO import StringIO

import bzrlib
import bzrlib.errors as errors
from bzrlib.errors import (InstallFailed, NoSuchRevision, WeaveError,
                           MissingText)
from bzrlib.trace import mutter, note, warning
from bzrlib.branch import Branch
from bzrlib.progress import ProgressBar
from bzrlib.xml5 import serializer_v5
from bzrlib.osutils import sha_string, split_lines

"""Copying of history from one branch to another.

The basic plan is that every branch knows the history of everything
that has merged into it.  As the first step of a merge, pull, or
branch operation we copy history from the source into the destination
branch.

The copying is done in a slightly complicated order.  We don't want to
add a revision to the store until everything it refers to is also
stored, so that if a revision is present we can totally recreate it.
However, we can't know what files are included in a revision until we
read its inventory.  Therefore, we first pull the XML and hold it in
memory until we've updated all of the files referenced.
"""

# TODO: Avoid repeatedly opening weaves so many times.

# XXX: This doesn't handle ghost (not present in branch) revisions at
# all yet.  I'm not sure they really should be supported.

# NOTE: This doesn't copy revisions which may be present but not
# merged into the last revision.  I'm not sure we want to do that.

# - get a list of revisions that need to be pulled in
# - for each one, pull in that revision file
#   and get the inventory, and store the inventory with right
#   parents.
# - and get the ancestry, and store that with right parents too
# - and keep a note of all file ids and version seen
# - then go through all files; for each one get the weave,
#   and add in all file versions



def greedy_fetch(to_branch, from_branch, revision=None, pb=None):
    f = Fetcher(to_branch, from_branch, revision, pb)
    return f.count_copied, f.failed_revisions



class Fetcher(object):
    """Pull revisions and texts from one branch to another.

    This doesn't update the destination's history; that can be done
    separately if desired.  

    revision_limit
        If set, pull only up to this revision_id.

    After running:

    last_revision -- if last_revision
        is given it will be that, otherwise the last revision of
        from_branch

    count_copied -- number of revisions copied

    count_weaves -- number of file weaves copied
    """
    def __init__(self, to_branch, from_branch, last_revision=None, pb=None):
        if to_branch == from_branch:
            raise Exception("can't fetch from a branch to itself")
        self.to_branch = to_branch
        self.to_weaves = to_branch.weave_store
        self.to_control = to_branch.control_weaves
        self.from_branch = from_branch
        self.from_weaves = from_branch.weave_store
        self.from_control = from_branch.control_weaves
        self.failed_revisions = []
        self.count_copied = 0
        self.count_total = 0
        self.count_weaves = 0
        self.copied_file_ids = set()
        self.file_ids_names = {}
        if pb is None:
            self.pb = bzrlib.ui.ui_factory.progress_bar()
        else:
            self.pb = pb
        self.from_branch.lock_read()
        try:
            revs = self._revids_to_fetch(last_revision )
            # nothing to do
            if revs: 
                self._fetch_revision_texts( revs )
                self._fetch_weave_texts( revs )
                self._fetch_inventory_weave( revs )
                self.count_copied += len(revs)
        finally:
            self.from_branch.unlock()
            self.pb.clear()

    def _revids_to_fetch(self, last_revision):
        self.last_revision = self._find_last_revision(last_revision)
        mutter('fetch up to rev {%s}', self.last_revision)
        if (self.last_revision is not None and 
            self.to_branch.has_revision(self.last_revision)):
            return
        try:
            branch_from_revs = set(self.from_branch.get_ancestry(self.last_revision))
        except WeaveError:
            raise InstallFailed([self.last_revision])

        self.dest_last_rev = self.to_branch.last_revision()
        branch_to_revs = set(self.to_branch.get_ancestry(self.dest_last_rev))

        return branch_from_revs.difference( branch_to_revs )

    def _fetch_revision_texts( self, revs ):
        self.to_branch.revision_store.copy_multi(
            self.from_branch.revision_store, revs )

    def _fetch_weave_texts( self, revs ):
        file_ids = self.from_branch.file_involved_by_set( revs )
        count = 0
        num_file_ids = len(file_ids)
        for file_id in file_ids:
            self.pb.update( "merge weave merge",count,num_file_ids)
            count +=1
            to_weave = self.to_weaves.get_weave_or_empty(file_id,
                self.to_branch.get_transaction())
            from_weave = self.from_weaves.get_weave(file_id,
                self.from_branch.get_transaction())

            if to_weave.numversions() > 0:
                # destination has contents, must merge
                try:
                    to_weave.join(from_weave)
                except errors.WeaveParentMismatch:
                    to_weave.reweave(from_weave)
            else:
                # destination is empty, just replace it
                to_weave = from_weave.copy( )

            self.to_weaves.put_weave(file_id, to_weave,
                self.to_branch.get_transaction())

        self.pb.clear( )

    def _fetch_inventory_weave( self, revs ):
        self.pb.update( "inventory merge",0,1)

        from_weave = self.from_control.get_weave('inventory',
                self.from_branch.get_transaction())
        to_weave = self.to_control.get_weave('inventory',
                self.to_branch.get_transaction())

        if to_weave.numversions() > 0:
            # destination has contents, must merge
            try:
                to_weave.join(from_weave)
            except errors.WeaveParentMismatch:
                to_weave.reweave(from_weave)
        else:
            # destination is empty, just replace it
            to_weave = from_weave.copy( )

        self.to_control.put_weave('inventory', to_weave,
            self.to_branch.get_transaction())

        self.pb.clear( )

    def _find_last_revision(self, last_revision):
        """Find the limiting source revision.

        Every ancestor of that revision will be merged across.

        Returns the revision_id, or returns None if there's no history
        in the source branch."""
        if last_revision:
            return last_revision
        self.pb.update('get source history')
        from_history = self.from_branch.revision_history()
        self.pb.update('get destination history')
        if from_history:
            return from_history[-1]
        else:
            return None                 # no history in the source branch

fetch = Fetcher
