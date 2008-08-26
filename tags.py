# Copyright (C) 2005-2007 Jelmer Vernooij <jelmer@samba.org>
 
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from bzrlib import urlutils
from bzrlib.errors import NoSuchRevision, NoSuchTag
from bzrlib.tag import BasicTags
from bzrlib.trace import mutter

from bzrlib.plugins.svn import commit, core, properties

class SubversionTags(BasicTags):
    def __init__(self, branch):
        self.branch = branch
        self.repository = branch.repository

    def _ensure_tag_parent_exists(self, parent):
        assert isinstance(parent, str)
        bp_parts = parent.split("/")
        existing_bp_parts = commit._check_dirs_exist(
                self.repository.transport, 
                bp_parts, self.repository.get_latest_revnum())
        if existing_bp_parts == bp_parts:
            return
        conn = self.repository.transport.get_connection()
        try:
            ci = conn.get_commit_editor({properties.PROP_REVISION_LOG: "Add tags base directory."})
            try:
                root = ci.open_root()
                name = None
                batons = [root]
                for p in existing_bp_parts:
                    if name is None:
                        name = p
                    else:
                        name += "/" + p
                    batons.append(batons[-1].open_directory(name))
                for p in bp_parts[len(existing_bp_parts):]:
                    if name is None:
                        name = p
                    else:
                        name += "/" + p
                    batons.append(batons[-1].add_directory(name))
                for baton in reversed(batons):
                    baton.close()
            except:
                ci.abort()
                raise
            ci.close()
        finally:
            self.repository.transport.add_connection(conn)

    def set_tag(self, tag_name, tag_target):
        path = self.branch.layout.get_tag_path(tag_name, self.branch.project)
        assert isinstance(path, str)
        parent = urlutils.dirname(path)
        try:
            (from_bp, from_revnum, mapping) = self.repository.lookup_revision_id(tag_target)
        except NoSuchRevision:
            mutter("not setting tag %s; unknown revision %s", tag_name, tag_target)
            return
        if from_bp == path:
            return
        self._ensure_tag_parent_exists(parent)
        conn = self.repository.transport.connections.get(urlutils.join(self.repository.base, parent))
        deletefirst = (conn.check_path(urlutils.basename(path), self.repository.get_latest_revnum()) != core.NODE_NONE)
        try:
            ci = conn.get_commit_editor({properties.PROP_REVISION_LOG: "Add tag %s" % tag_name.encode("utf-8")})
            try:
                root = ci.open_root()
                if deletefirst:
                    root.delete_entry(urlutils.basename(path))
                root.add_directory(urlutils.basename(path), urlutils.join(self.repository.base, from_bp), from_revnum)
                root.close()
            except:
                ci.abort()
                raise
            ci.close()
        finally:
            self.repository.transport.add_connection(conn)

    def lookup_tag(self, tag_name):
        try:
            return self.get_tag_dict()[tag_name]
        except KeyError:
            raise NoSuchTag(tag_name)

    def get_tag_dict(self, _from_revnum=0):
        return self.repository.find_tags_between(project=self.branch.project, 
                              layout=self.branch.layout,
                              mapping=self.branch.mapping,
                              from_revnum=_from_revnum, 
                              to_revnum=self.repository.get_latest_revnum())

    def get_reverse_tag_dict(self):
        """Returns a dict with revisions as keys
           and a list of tags for that revision as value"""
        d = self.get_tag_dict()
        rev = {}
        for key in d:
            try:
                rev[d[key]].append(key)
            except KeyError:
                rev[d[key]] = [key]
        return rev

    def delete_tag(self, tag_name):
        path = self.branch.layout.get_tag_path(tag_name, self.branch.project)
        parent = urlutils.dirname(path)
        conn = self.repository.transport.connections.get(urlutils.join(self.repository.base, parent))
        try:
            if conn.check_path(urlutils.basename(path), self.repository.get_latest_revnum()) != core.NODE_DIR:
                raise NoSuchTag(tag_name)
            ci = conn.get_commit_editor({properties.PROP_REVISION_LOG: "Remove tag %s" % tag_name.encode("utf-8")})
            try:
                root = ci.open_root()
                root.delete_entry(urlutils.basename(path))
                root.close()
            except:
                ci.abort()
                raise
            ci.close()
        finally:
            assert not conn.busy
            self.repository.transport.add_connection(conn)

    def _set_tag_dict(self, dest_dict):
        cur_dict = self.get_tag_dict()
        for k, v in dest_dict.iteritems():
            if cur_dict.get(k) != v:
                self.set_tag(k, v)
        for k in cur_dict:
            if k not in dest_dict:
                self.delete_tag(k)

