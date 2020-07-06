# Copyright (C) 2006-2011 Canonical Ltd
# Copyright (C) 2020 Breezy Developers
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

from __future__ import absolute_import

import errno
import os
from stat import S_IEXEC, S_ISREG
import time

from .. import (
    annotate,
    errors,
    lock,
    multiparent,
    osutils,
    revision as _mod_revision,
    trace,
    tree,
    ui,
    urlutils,
    )

from ..i18n import gettext
from ..mutabletree import MutableTree
from ..sixish import text_type, viewvalues, viewitems
from ..transform import (
    ROOT_PARENT,
    _FileMover,
    _TransformResults,
    joinpath,
    NoFinalPath,
    FinalPaths,
    unique_add,
    TreeTransform,
    TransformRenameFailed,
    ImmortalLimbo,
    ReusingTransform,
    MalformedTransform,
    )
from ..tree import TreeChange
from . import (
    inventory,
    inventorytree,
    )


class TreeTransformBase(TreeTransform):
    """The base class for TreeTransform and its kin."""

    def __init__(self, tree, pb=None, case_sensitive=True):
        """Constructor.

        :param tree: The tree that will be transformed, but not necessarily
            the output tree.
        :param pb: ignored
        :param case_sensitive: If True, the target of the transform is
            case sensitive, not just case preserving.
        """
        super(TreeTransformBase, self).__init__(tree, pb=pb)
        # mapping of trans_id => (sha1 of content, stat_value)
        self._observed_sha1s = {}
        # Mapping of trans_id -> new file_id
        self._new_id = {}
        # Mapping of old file-id -> trans_id
        self._non_present_ids = {}
        # Mapping of new file_id -> trans_id
        self._r_new_id = {}
        # The trans_id that will be used as the tree root
        if tree.is_versioned(''):
            self._new_root = self.trans_id_tree_path('')
        else:
            self._new_root = None
        # Whether the target is case sensitive
        self._case_sensitive_target = case_sensitive

    def finalize(self):
        """Release the working tree lock, if held.

        This is required if apply has not been invoked, but can be invoked
        even after apply.
        """
        if self._tree is None:
            return
        for hook in MutableTree.hooks['post_transform']:
            hook(self._tree, self)
        self._tree.unlock()
        self._tree = None

    def __get_root(self):
        return self._new_root

    root = property(__get_root)

    def create_path(self, name, parent):
        """Assign a transaction id to a new path"""
        trans_id = self._assign_id()
        unique_add(self._new_name, trans_id, name)
        unique_add(self._new_parent, trans_id, parent)
        return trans_id

    def adjust_root_path(self, name, parent):
        """Emulate moving the root by moving all children, instead.

        We do this by undoing the association of root's transaction id with the
        current tree.  This allows us to create a new directory with that
        transaction id.  We unversion the root directory and version the
        physically new directory, and hope someone versions the tree root
        later.
        """
        old_root = self._new_root
        old_root_file_id = self.final_file_id(old_root)
        # force moving all children of root
        for child_id in self.iter_tree_children(old_root):
            if child_id != parent:
                self.adjust_path(self.final_name(child_id),
                                 self.final_parent(child_id), child_id)
            file_id = self.final_file_id(child_id)
            if file_id is not None:
                self.unversion_file(child_id)
            self.version_file(child_id, file_id=file_id)

        # the physical root needs a new transaction id
        self._tree_path_ids.pop("")
        self._tree_id_paths.pop(old_root)
        self._new_root = self.trans_id_tree_path('')
        if parent == old_root:
            parent = self._new_root
        self.adjust_path(name, parent, old_root)
        self.create_directory(old_root)
        self.version_file(old_root, file_id=old_root_file_id)
        self.unversion_file(self._new_root)

    def fixup_new_roots(self):
        """Reinterpret requests to change the root directory

        Instead of creating a root directory, or moving an existing directory,
        all the attributes and children of the new root are applied to the
        existing root directory.

        This means that the old root trans-id becomes obsolete, so it is
        recommended only to invoke this after the root trans-id has become
        irrelevant.

        """
        new_roots = [k for k, v in viewitems(self._new_parent)
                     if v == ROOT_PARENT]
        if len(new_roots) < 1:
            return
        if len(new_roots) != 1:
            raise ValueError('A tree cannot have two roots!')
        if self._new_root is None:
            self._new_root = new_roots[0]
            return
        old_new_root = new_roots[0]
        # unversion the new root's directory.
        if self.final_kind(self._new_root) is None:
            file_id = self.final_file_id(old_new_root)
        else:
            file_id = self.final_file_id(self._new_root)
        if old_new_root in self._new_id:
            self.cancel_versioning(old_new_root)
        else:
            self.unversion_file(old_new_root)
        # if, at this stage, root still has an old file_id, zap it so we can
        # stick a new one in.
        if (self.tree_file_id(self._new_root) is not None
                and self._new_root not in self._removed_id):
            self.unversion_file(self._new_root)
        if file_id is not None:
            self.version_file(self._new_root, file_id=file_id)

        # Now move children of new root into old root directory.
        # Ensure all children are registered with the transaction, but don't
        # use directly-- some tree children have new parents
        list(self.iter_tree_children(old_new_root))
        # Move all children of new root into old root directory.
        for child in self.by_parent().get(old_new_root, []):
            self.adjust_path(self.final_name(child), self._new_root, child)

        # Ensure old_new_root has no directory.
        if old_new_root in self._new_contents:
            self.cancel_creation(old_new_root)
        else:
            self.delete_contents(old_new_root)

        # prevent deletion of root directory.
        if self._new_root in self._removed_contents:
            self.cancel_deletion(self._new_root)

        # destroy path info for old_new_root.
        del self._new_parent[old_new_root]
        del self._new_name[old_new_root]

    def trans_id_file_id(self, file_id):
        """Determine or set the transaction id associated with a file ID.
        A new id is only created for file_ids that were never present.  If
        a transaction has been unversioned, it is deliberately still returned.
        (this will likely lead to an unversioned parent conflict.)
        """
        if file_id is None:
            raise ValueError('None is not a valid file id')
        if file_id in self._r_new_id and self._r_new_id[file_id] is not None:
            return self._r_new_id[file_id]
        else:
            try:
                path = self._tree.id2path(file_id)
            except errors.NoSuchId:
                if file_id in self._non_present_ids:
                    return self._non_present_ids[file_id]
                else:
                    trans_id = self._assign_id()
                    self._non_present_ids[file_id] = trans_id
                    return trans_id
            else:
                return self.trans_id_tree_path(path)

    def version_file(self, trans_id, file_id=None):
        """Schedule a file to become versioned."""
        raise NotImplementedError(self.version_file)

    def cancel_versioning(self, trans_id):
        """Undo a previous versioning of a file"""
        raise NotImplementedError(self.cancel_versioning)

    def new_paths(self, filesystem_only=False):
        """Determine the paths of all new and changed files.

        :param filesystem_only: if True, only calculate values for files
            that require renames or execute bit changes.
        """
        new_ids = set()
        if filesystem_only:
            stale_ids = self._needs_rename.difference(self._new_name)
            stale_ids.difference_update(self._new_parent)
            stale_ids.difference_update(self._new_contents)
            stale_ids.difference_update(self._new_id)
            needs_rename = self._needs_rename.difference(stale_ids)
            id_sets = (needs_rename, self._new_executability)
        else:
            id_sets = (self._new_name, self._new_parent, self._new_contents,
                       self._new_id, self._new_executability)
        for id_set in id_sets:
            new_ids.update(id_set)
        return sorted(FinalPaths(self).get_paths(new_ids))

    def tree_file_id(self, trans_id):
        """Determine the file id associated with the trans_id in the tree"""
        path = self.tree_path(trans_id)
        if path is None:
            return None
        # the file is old; the old id is still valid
        if self._new_root == trans_id:
            return self._tree.path2id('')
        return self._tree.path2id(path)

    def final_is_versioned(self, trans_id):
        return self.final_file_id(trans_id) is not None

    def final_file_id(self, trans_id):
        """Determine the file id after any changes are applied, or None.

        None indicates that the file will not be versioned after changes are
        applied.
        """
        try:
            return self._new_id[trans_id]
        except KeyError:
            if trans_id in self._removed_id:
                return None
        return self.tree_file_id(trans_id)

    def inactive_file_id(self, trans_id):
        """Return the inactive file_id associated with a transaction id.
        That is, the one in the tree or in non_present_ids.
        The file_id may actually be active, too.
        """
        file_id = self.tree_file_id(trans_id)
        if file_id is not None:
            return file_id
        for key, value in viewitems(self._non_present_ids):
            if value == trans_id:
                return key

    def find_conflicts(self):
        """Find any violations of inventory or filesystem invariants"""
        if self._done is True:
            raise ReusingTransform()
        conflicts = []
        # ensure all children of all existent parents are known
        # all children of non-existent parents are known, by definition.
        self._add_tree_children()
        by_parent = self.by_parent()
        conflicts.extend(self._unversioned_parents(by_parent))
        conflicts.extend(self._parent_loops())
        conflicts.extend(self._duplicate_entries(by_parent))
        conflicts.extend(self._parent_type_conflicts(by_parent))
        conflicts.extend(self._improper_versioning())
        conflicts.extend(self._executability_conflicts())
        conflicts.extend(self._overwrite_conflicts())
        return conflicts

    def _check_malformed(self):
        conflicts = self.find_conflicts()
        if len(conflicts) != 0:
            raise MalformedTransform(conflicts=conflicts)

    def _add_tree_children(self):
        """Add all the children of all active parents to the known paths.

        Active parents are those which gain children, and those which are
        removed.  This is a necessary first step in detecting conflicts.
        """
        parents = list(self.by_parent())
        parents.extend([t for t in self._removed_contents if
                        self.tree_kind(t) == 'directory'])
        for trans_id in self._removed_id:
            path = self.tree_path(trans_id)
            if path is not None:
                if self._tree.stored_kind(path) == 'directory':
                    parents.append(trans_id)
            elif self.tree_kind(trans_id) == 'directory':
                parents.append(trans_id)

        for parent_id in parents:
            # ensure that all children are registered with the transaction
            list(self.iter_tree_children(parent_id))

    def _has_named_child(self, name, parent_id, known_children):
        """Does a parent already have a name child.

        :param name: The searched for name.

        :param parent_id: The parent for which the check is made.

        :param known_children: The already known children. This should have
            been recently obtained from `self.by_parent.get(parent_id)`
            (or will be if None is passed).
        """
        if known_children is None:
            known_children = self.by_parent().get(parent_id, [])
        for child in known_children:
            if self.final_name(child) == name:
                return True
        parent_path = self._tree_id_paths.get(parent_id, None)
        if parent_path is None:
            # No parent... no children
            return False
        child_path = joinpath(parent_path, name)
        child_id = self._tree_path_ids.get(child_path, None)
        if child_id is None:
            # Not known by the tree transform yet, check the filesystem
            return osutils.lexists(self._tree.abspath(child_path))
        else:
            raise AssertionError('child_id is missing: %s, %s, %s'
                                 % (name, parent_id, child_id))

    def _available_backup_name(self, name, target_id):
        """Find an available backup name.

        :param name: The basename of the file.

        :param target_id: The directory trans_id where the backup should
            be placed.
        """
        known_children = self.by_parent().get(target_id, [])
        return osutils.available_backup_name(
            name,
            lambda base: self._has_named_child(
                base, target_id, known_children))

    def _parent_loops(self):
        """No entry should be its own ancestor"""
        conflicts = []
        for trans_id in self._new_parent:
            seen = set()
            parent_id = trans_id
            while parent_id != ROOT_PARENT:
                seen.add(parent_id)
                try:
                    parent_id = self.final_parent(parent_id)
                except KeyError:
                    break
                if parent_id == trans_id:
                    conflicts.append(('parent loop', trans_id))
                if parent_id in seen:
                    break
        return conflicts

    def _unversioned_parents(self, by_parent):
        """If parent directories are versioned, children must be versioned."""
        conflicts = []
        for parent_id, children in viewitems(by_parent):
            if parent_id == ROOT_PARENT:
                continue
            if self.final_is_versioned(parent_id):
                continue
            for child_id in children:
                if self.final_is_versioned(child_id):
                    conflicts.append(('unversioned parent', parent_id))
                    break
        return conflicts

    def _improper_versioning(self):
        """Cannot version a file with no contents, or a bad type.

        However, existing entries with no contents are okay.
        """
        conflicts = []
        for trans_id in self._new_id:
            kind = self.final_kind(trans_id)
            if kind == 'symlink' and not self._tree.supports_symlinks():
                # Ignore symlinks as they are not supported on this platform
                continue
            if kind is None:
                conflicts.append(('versioning no contents', trans_id))
                continue
            if not self._tree.versionable_kind(kind):
                conflicts.append(('versioning bad kind', trans_id, kind))
        return conflicts

    def _executability_conflicts(self):
        """Check for bad executability changes.

        Only versioned files may have their executability set, because
        1. only versioned entries can have executability under windows
        2. only files can be executable.  (The execute bit on a directory
           does not indicate searchability)
        """
        conflicts = []
        for trans_id in self._new_executability:
            if not self.final_is_versioned(trans_id):
                conflicts.append(('unversioned executability', trans_id))
            else:
                if self.final_kind(trans_id) != "file":
                    conflicts.append(('non-file executability', trans_id))
        return conflicts

    def _overwrite_conflicts(self):
        """Check for overwrites (not permitted on Win32)"""
        conflicts = []
        for trans_id in self._new_contents:
            if self.tree_kind(trans_id) is None:
                continue
            if trans_id not in self._removed_contents:
                conflicts.append(('overwrite', trans_id,
                                  self.final_name(trans_id)))
        return conflicts

    def _duplicate_entries(self, by_parent):
        """No directory may have two entries with the same name."""
        conflicts = []
        if (self._new_name, self._new_parent) == ({}, {}):
            return conflicts
        for children in viewvalues(by_parent):
            name_ids = []
            for child_tid in children:
                name = self.final_name(child_tid)
                if name is not None:
                    # Keep children only if they still exist in the end
                    if not self._case_sensitive_target:
                        name = name.lower()
                    name_ids.append((name, child_tid))
            name_ids.sort()
            last_name = None
            last_trans_id = None
            for name, trans_id in name_ids:
                kind = self.final_kind(trans_id)
                if kind is None and not self.final_is_versioned(trans_id):
                    continue
                if name == last_name:
                    conflicts.append(('duplicate', last_trans_id, trans_id,
                                      name))
                last_name = name
                last_trans_id = trans_id
        return conflicts

    def _parent_type_conflicts(self, by_parent):
        """Children must have a directory parent"""
        conflicts = []
        for parent_id, children in viewitems(by_parent):
            if parent_id == ROOT_PARENT:
                continue
            no_children = True
            for child_id in children:
                if self.final_kind(child_id) is not None:
                    no_children = False
                    break
            if no_children:
                continue
            # There is at least a child, so we need an existing directory to
            # contain it.
            kind = self.final_kind(parent_id)
            if kind is None:
                # The directory will be deleted
                conflicts.append(('missing parent', parent_id))
            elif kind != "directory":
                # Meh, we need a *directory* to put something in it
                conflicts.append(('non-directory parent', parent_id))
        return conflicts

    def _set_executability(self, path, trans_id):
        """Set the executability of versioned files """
        if self._tree._supports_executable():
            new_executability = self._new_executability[trans_id]
            abspath = self._tree.abspath(path)
            current_mode = os.stat(abspath).st_mode
            if new_executability:
                umask = os.umask(0)
                os.umask(umask)
                to_mode = current_mode | (0o100 & ~umask)
                # Enable x-bit for others only if they can read it.
                if current_mode & 0o004:
                    to_mode |= 0o001 & ~umask
                if current_mode & 0o040:
                    to_mode |= 0o010 & ~umask
            else:
                to_mode = current_mode & ~0o111
            osutils.chmod_if_possible(abspath, to_mode)

    def _new_entry(self, name, parent_id, file_id):
        """Helper function to create a new filesystem entry."""
        trans_id = self.create_path(name, parent_id)
        if file_id is not None:
            self.version_file(trans_id, file_id=file_id)
        return trans_id

    def new_file(self, name, parent_id, contents, file_id=None,
                 executable=None, sha1=None):
        """Convenience method to create files.

        name is the name of the file to create.
        parent_id is the transaction id of the parent directory of the file.
        contents is an iterator of bytestrings, which will be used to produce
        the file.
        :param file_id: The inventory ID of the file, if it is to be versioned.
        :param executable: Only valid when a file_id has been supplied.
        """
        trans_id = self._new_entry(name, parent_id, file_id)
        # TODO: rather than scheduling a set_executable call,
        # have create_file create the file with the right mode.
        self.create_file(contents, trans_id, sha1=sha1)
        if executable is not None:
            self.set_executability(executable, trans_id)
        return trans_id

    def new_directory(self, name, parent_id, file_id=None):
        """Convenience method to create directories.

        name is the name of the directory to create.
        parent_id is the transaction id of the parent directory of the
        directory.
        file_id is the inventory ID of the directory, if it is to be versioned.
        """
        trans_id = self._new_entry(name, parent_id, file_id)
        self.create_directory(trans_id)
        return trans_id

    def new_symlink(self, name, parent_id, target, file_id=None):
        """Convenience method to create symbolic link.

        name is the name of the symlink to create.
        parent_id is the transaction id of the parent directory of the symlink.
        target is a bytestring of the target of the symlink.
        file_id is the inventory ID of the file, if it is to be versioned.
        """
        trans_id = self._new_entry(name, parent_id, file_id)
        self.create_symlink(target, trans_id)
        return trans_id

    def new_orphan(self, trans_id, parent_id):
        """Schedule an item to be orphaned.

        When a directory is about to be removed, its children, if they are not
        versioned are moved out of the way: they don't have a parent anymore.

        :param trans_id: The trans_id of the existing item.
        :param parent_id: The parent trans_id of the item.
        """
        raise NotImplementedError(self.new_orphan)

    def _get_potential_orphans(self, dir_id):
        """Find the potential orphans in a directory.

        A directory can't be safely deleted if there are versioned files in it.
        If all the contained files are unversioned then they can be orphaned.

        The 'None' return value means that the directory contains at least one
        versioned file and should not be deleted.

        :param dir_id: The directory trans id.

        :return: A list of the orphan trans ids or None if at least one
             versioned file is present.
        """
        orphans = []
        # Find the potential orphans, stop if one item should be kept
        for child_tid in self.by_parent()[dir_id]:
            if child_tid in self._removed_contents:
                # The child is removed as part of the transform. Since it was
                # versioned before, it's not an orphan
                continue
            if not self.final_is_versioned(child_tid):
                # The child is not versioned
                orphans.append(child_tid)
            else:
                # We have a versioned file here, searching for orphans is
                # meaningless.
                orphans = None
                break
        return orphans

    def _affected_ids(self):
        """Return the set of transform ids affected by the transform"""
        trans_ids = set(self._removed_id)
        trans_ids.update(self._new_id)
        trans_ids.update(self._removed_contents)
        trans_ids.update(self._new_contents)
        trans_ids.update(self._new_executability)
        trans_ids.update(self._new_name)
        trans_ids.update(self._new_parent)
        return trans_ids

    def _get_file_id_maps(self):
        """Return mapping of file_ids to trans_ids in the to and from states"""
        trans_ids = self._affected_ids()
        from_trans_ids = {}
        to_trans_ids = {}
        # Build up two dicts: trans_ids associated with file ids in the
        # FROM state, vs the TO state.
        for trans_id in trans_ids:
            from_file_id = self.tree_file_id(trans_id)
            if from_file_id is not None:
                from_trans_ids[from_file_id] = trans_id
            to_file_id = self.final_file_id(trans_id)
            if to_file_id is not None:
                to_trans_ids[to_file_id] = trans_id
        return from_trans_ids, to_trans_ids

    def _from_file_data(self, from_trans_id, from_versioned, from_path):
        """Get data about a file in the from (tree) state

        Return a (name, parent, kind, executable) tuple
        """
        from_path = self._tree_id_paths.get(from_trans_id)
        if from_versioned:
            # get data from working tree if versioned
            from_entry = next(self._tree.iter_entries_by_dir(
                specific_files=[from_path]))[1]
            from_name = from_entry.name
            from_parent = from_entry.parent_id
        else:
            from_entry = None
            if from_path is None:
                # File does not exist in FROM state
                from_name = None
                from_parent = None
            else:
                # File exists, but is not versioned.  Have to use path-
                # splitting stuff
                from_name = os.path.basename(from_path)
                tree_parent = self.get_tree_parent(from_trans_id)
                from_parent = self.tree_file_id(tree_parent)
        if from_path is not None:
            from_kind, from_executable, from_stats = \
                self._tree._comparison_data(from_entry, from_path)
        else:
            from_kind = None
            from_executable = False
        return from_name, from_parent, from_kind, from_executable

    def _to_file_data(self, to_trans_id, from_trans_id, from_executable):
        """Get data about a file in the to (target) state

        Return a (name, parent, kind, executable) tuple
        """
        to_name = self.final_name(to_trans_id)
        to_kind = self.final_kind(to_trans_id)
        to_parent = self.final_file_id(self.final_parent(to_trans_id))
        if to_trans_id in self._new_executability:
            to_executable = self._new_executability[to_trans_id]
        elif to_trans_id == from_trans_id:
            to_executable = from_executable
        else:
            to_executable = False
        return to_name, to_parent, to_kind, to_executable

    def iter_changes(self):
        """Produce output in the same format as Tree.iter_changes.

        Will produce nonsensical results if invoked while inventory/filesystem
        conflicts (as reported by TreeTransform.find_conflicts()) are present.

        This reads the Transform, but only reproduces changes involving a
        file_id.  Files that are not versioned in either of the FROM or TO
        states are not reflected.
        """
        final_paths = FinalPaths(self)
        from_trans_ids, to_trans_ids = self._get_file_id_maps()
        results = []
        # Now iterate through all active file_ids
        for file_id in set(from_trans_ids).union(to_trans_ids):
            modified = False
            from_trans_id = from_trans_ids.get(file_id)
            # find file ids, and determine versioning state
            if from_trans_id is None:
                from_versioned = False
                from_trans_id = to_trans_ids[file_id]
            else:
                from_versioned = True
            to_trans_id = to_trans_ids.get(file_id)
            if to_trans_id is None:
                to_versioned = False
                to_trans_id = from_trans_id
            else:
                to_versioned = True

            if not from_versioned:
                from_path = None
            else:
                from_path = self._tree_id_paths.get(from_trans_id)
            if not to_versioned:
                to_path = None
            else:
                to_path = final_paths.get_path(to_trans_id)

            from_name, from_parent, from_kind, from_executable = \
                self._from_file_data(from_trans_id, from_versioned, from_path)

            to_name, to_parent, to_kind, to_executable = \
                self._to_file_data(to_trans_id, from_trans_id, from_executable)

            if from_kind != to_kind:
                modified = True
            elif to_kind in ('file', 'symlink') and (
                    to_trans_id != from_trans_id
                    or to_trans_id in self._new_contents):
                modified = True
            if (not modified and from_versioned == to_versioned
                and from_parent == to_parent and from_name == to_name
                    and from_executable == to_executable):
                continue
            results.append(
                TreeChange(
                    file_id, (from_path, to_path), modified,
                    (from_versioned, to_versioned),
                    (from_parent, to_parent),
                    (from_name, to_name),
                    (from_kind, to_kind),
                    (from_executable, to_executable)))

        def path_key(c):
            return (c.path[0] or '', c.path[1] or '')
        return iter(sorted(results, key=path_key))

    def get_preview_tree(self):
        """Return a tree representing the result of the transform.

        The tree is a snapshot, and altering the TreeTransform will invalidate
        it.
        """
        raise NotImplementedError(self.get_preview)

    def commit(self, branch, message, merge_parents=None, strict=False,
               timestamp=None, timezone=None, committer=None, authors=None,
               revprops=None, revision_id=None):
        """Commit the result of this TreeTransform to a branch.

        :param branch: The branch to commit to.
        :param message: The message to attach to the commit.
        :param merge_parents: Additional parent revision-ids specified by
            pending merges.
        :param strict: If True, abort the commit if there are unversioned
            files.
        :param timestamp: if not None, seconds-since-epoch for the time and
            date.  (May be a float.)
        :param timezone: Optional timezone for timestamp, as an offset in
            seconds.
        :param committer: Optional committer in email-id format.
            (e.g. "J Random Hacker <jrandom@example.com>")
        :param authors: Optional list of authors in email-id format.
        :param revprops: Optional dictionary of revision properties.
        :param revision_id: Optional revision id.  (Specifying a revision-id
            may reduce performance for some non-native formats.)
        :return: The revision_id of the revision committed.
        """
        self._check_malformed()
        if strict:
            unversioned = set(self._new_contents).difference(set(self._new_id))
            for trans_id in unversioned:
                if not self.final_is_versioned(trans_id):
                    raise errors.StrictCommitFailed()

        revno, last_rev_id = branch.last_revision_info()
        if last_rev_id == _mod_revision.NULL_REVISION:
            if merge_parents is not None:
                raise ValueError('Cannot supply merge parents for first'
                                 ' commit.')
            parent_ids = []
        else:
            parent_ids = [last_rev_id]
            if merge_parents is not None:
                parent_ids.extend(merge_parents)
        if self._tree.get_revision_id() != last_rev_id:
            raise ValueError('TreeTransform not based on branch basis: %s' %
                             self._tree.get_revision_id().decode('utf-8'))
        from .. import commit
        revprops = commit.Commit.update_revprops(revprops, branch, authors)
        builder = branch.get_commit_builder(parent_ids,
                                            timestamp=timestamp,
                                            timezone=timezone,
                                            committer=committer,
                                            revprops=revprops,
                                            revision_id=revision_id)
        preview = self.get_preview_tree()
        list(builder.record_iter_changes(preview, last_rev_id,
                                         self.iter_changes()))
        builder.finish_inventory()
        revision_id = builder.commit(message)
        branch.set_last_revision_info(revno + 1, revision_id)
        return revision_id

    def _text_parent(self, trans_id):
        path = self.tree_path(trans_id)
        try:
            if path is None or self._tree.kind(path) != 'file':
                return None
        except errors.NoSuchFile:
            return None
        return path

    def _get_parents_texts(self, trans_id):
        """Get texts for compression parents of this file."""
        path = self._text_parent(trans_id)
        if path is None:
            return ()
        return (self._tree.get_file_text(path),)

    def _get_parents_lines(self, trans_id):
        """Get lines for compression parents of this file."""
        path = self._text_parent(trans_id)
        if path is None:
            return ()
        return (self._tree.get_file_lines(path),)

    def serialize(self, serializer):
        """Serialize this TreeTransform.

        :param serializer: A Serialiser like pack.ContainerSerializer.
        """
        from .. import bencode
        new_name = {k.encode('utf-8'): v.encode('utf-8')
                    for k, v in viewitems(self._new_name)}
        new_parent = {k.encode('utf-8'): v.encode('utf-8')
                      for k, v in viewitems(self._new_parent)}
        new_id = {k.encode('utf-8'): v
                  for k, v in viewitems(self._new_id)}
        new_executability = {k.encode('utf-8'): int(v)
                             for k, v in viewitems(self._new_executability)}
        tree_path_ids = {k.encode('utf-8'): v.encode('utf-8')
                         for k, v in viewitems(self._tree_path_ids)}
        non_present_ids = {k: v.encode('utf-8')
                           for k, v in viewitems(self._non_present_ids)}
        removed_contents = [trans_id.encode('utf-8')
                            for trans_id in self._removed_contents]
        removed_id = [trans_id.encode('utf-8')
                      for trans_id in self._removed_id]
        attribs = {
            b'_id_number': self._id_number,
            b'_new_name': new_name,
            b'_new_parent': new_parent,
            b'_new_executability': new_executability,
            b'_new_id': new_id,
            b'_tree_path_ids': tree_path_ids,
            b'_removed_id': removed_id,
            b'_removed_contents': removed_contents,
            b'_non_present_ids': non_present_ids,
            }
        yield serializer.bytes_record(bencode.bencode(attribs),
                                      ((b'attribs',),))
        for trans_id, kind in sorted(viewitems(self._new_contents)):
            if kind == 'file':
                with open(self._limbo_name(trans_id), 'rb') as cur_file:
                    lines = cur_file.readlines()
                parents = self._get_parents_lines(trans_id)
                mpdiff = multiparent.MultiParent.from_lines(lines, parents)
                content = b''.join(mpdiff.to_patch())
            if kind == 'directory':
                content = b''
            if kind == 'symlink':
                content = self._read_symlink_target(trans_id)
                if not isinstance(content, bytes):
                    content = content.encode('utf-8')
            yield serializer.bytes_record(
                content, ((trans_id.encode('utf-8'), kind.encode('ascii')),))

    def deserialize(self, records):
        """Deserialize a stored TreeTransform.

        :param records: An iterable of (names, content) tuples, as per
            pack.ContainerPushParser.
        """
        from .. import bencode
        names, content = next(records)
        attribs = bencode.bdecode(content)
        self._id_number = attribs[b'_id_number']
        self._new_name = {k.decode('utf-8'): v.decode('utf-8')
                          for k, v in viewitems(attribs[b'_new_name'])}
        self._new_parent = {k.decode('utf-8'): v.decode('utf-8')
                            for k, v in viewitems(attribs[b'_new_parent'])}
        self._new_executability = {
            k.decode('utf-8'): bool(v)
            for k, v in viewitems(attribs[b'_new_executability'])}
        self._new_id = {k.decode('utf-8'): v
                        for k, v in viewitems(attribs[b'_new_id'])}
        self._r_new_id = {v: k for k, v in viewitems(self._new_id)}
        self._tree_path_ids = {}
        self._tree_id_paths = {}
        for bytepath, trans_id in viewitems(attribs[b'_tree_path_ids']):
            path = bytepath.decode('utf-8')
            trans_id = trans_id.decode('utf-8')
            self._tree_path_ids[path] = trans_id
            self._tree_id_paths[trans_id] = path
        self._removed_id = {trans_id.decode('utf-8')
                            for trans_id in attribs[b'_removed_id']}
        self._removed_contents = set(
            trans_id.decode('utf-8')
            for trans_id in attribs[b'_removed_contents'])
        self._non_present_ids = {
            k: v.decode('utf-8')
            for k, v in viewitems(attribs[b'_non_present_ids'])}
        for ((trans_id, kind),), content in records:
            trans_id = trans_id.decode('utf-8')
            kind = kind.decode('ascii')
            if kind == 'file':
                mpdiff = multiparent.MultiParent.from_patch(content)
                lines = mpdiff.to_lines(self._get_parents_texts(trans_id))
                self.create_file(lines, trans_id)
            if kind == 'directory':
                self.create_directory(trans_id)
            if kind == 'symlink':
                self.create_symlink(content.decode('utf-8'), trans_id)

    def create_file(self, contents, trans_id, mode_id=None, sha1=None):
        """Schedule creation of a new file.

        :seealso: new_file.

        :param contents: an iterator of strings, all of which will be written
            to the target destination.
        :param trans_id: TreeTransform handle
        :param mode_id: If not None, force the mode of the target file to match
            the mode of the object referenced by mode_id.
            Otherwise, we will try to preserve mode bits of an existing file.
        :param sha1: If the sha1 of this content is already known, pass it in.
            We can use it to prevent future sha1 computations.
        """
        raise NotImplementedError(self.create_file)

    def create_directory(self, trans_id):
        """Schedule creation of a new directory.

        See also new_directory.
        """
        raise NotImplementedError(self.create_directory)

    def create_symlink(self, target, trans_id):
        """Schedule creation of a new symbolic link.

        target is a bytestring.
        See also new_symlink.
        """
        raise NotImplementedError(self.create_symlink)

    def create_hardlink(self, path, trans_id):
        """Schedule creation of a hard link"""
        raise NotImplementedError(self.create_hardlink)

    def cancel_creation(self, trans_id):
        """Cancel the creation of new file contents."""
        raise NotImplementedError(self.cancel_creation)

    def apply(self, no_conflicts=False, precomputed_delta=None, _mover=None):
        """Apply all changes to the inventory and filesystem.

        If filesystem or inventory conflicts are present, MalformedTransform
        will be thrown.

        If apply succeeds, finalize is not necessary.

        :param no_conflicts: if True, the caller guarantees there are no
            conflicts, so no check is made.
        :param precomputed_delta: An inventory delta to use instead of
            calculating one.
        :param _mover: Supply an alternate FileMover, for testing
        """
        raise NotImplementedError(self.apply)


class DiskTreeTransform(TreeTransformBase):
    """Tree transform storing its contents on disk."""

    def __init__(self, tree, limbodir, pb=None, case_sensitive=True):
        """Constructor.
        :param tree: The tree that will be transformed, but not necessarily
            the output tree.
        :param limbodir: A directory where new files can be stored until
            they are installed in their proper places
        :param pb: ignored
        :param case_sensitive: If True, the target of the transform is
            case sensitive, not just case preserving.
        """
        TreeTransformBase.__init__(self, tree, pb, case_sensitive)
        self._limbodir = limbodir
        self._deletiondir = None
        # A mapping of transform ids to their limbo filename
        self._limbo_files = {}
        self._possibly_stale_limbo_files = set()
        # A mapping of transform ids to a set of the transform ids of children
        # that their limbo directory has
        self._limbo_children = {}
        # Map transform ids to maps of child filename to child transform id
        self._limbo_children_names = {}
        # List of transform ids that need to be renamed from limbo into place
        self._needs_rename = set()
        self._creation_mtime = None
        self._create_symlinks = osutils.supports_symlinks(self._limbodir)

    def finalize(self):
        """Release the working tree lock, if held, clean up limbo dir.

        This is required if apply has not been invoked, but can be invoked
        even after apply.
        """
        if self._tree is None:
            return
        try:
            limbo_paths = list(viewvalues(self._limbo_files))
            limbo_paths.extend(self._possibly_stale_limbo_files)
            limbo_paths.sort(reverse=True)
            for path in limbo_paths:
                try:
                    osutils.delete_any(path)
                except OSError as e:
                    if e.errno != errno.ENOENT:
                        raise
                    # XXX: warn? perhaps we just got interrupted at an
                    # inconvenient moment, but perhaps files are disappearing
                    # from under us?
            try:
                osutils.delete_any(self._limbodir)
            except OSError:
                # We don't especially care *why* the dir is immortal.
                raise ImmortalLimbo(self._limbodir)
            try:
                if self._deletiondir is not None:
                    osutils.delete_any(self._deletiondir)
            except OSError:
                raise errors.ImmortalPendingDeletion(self._deletiondir)
        finally:
            TreeTransformBase.finalize(self)

    def _limbo_supports_executable(self):
        """Check if the limbo path supports the executable bit."""
        return osutils.supports_executable(self._limbodir)

    def _limbo_name(self, trans_id):
        """Generate the limbo name of a file"""
        limbo_name = self._limbo_files.get(trans_id)
        if limbo_name is None:
            limbo_name = self._generate_limbo_path(trans_id)
            self._limbo_files[trans_id] = limbo_name
        return limbo_name

    def _generate_limbo_path(self, trans_id):
        """Generate a limbo path using the trans_id as the relative path.

        This is suitable as a fallback, and when the transform should not be
        sensitive to the path encoding of the limbo directory.
        """
        self._needs_rename.add(trans_id)
        return osutils.pathjoin(self._limbodir, trans_id)

    def adjust_path(self, name, parent, trans_id):
        previous_parent = self._new_parent.get(trans_id)
        previous_name = self._new_name.get(trans_id)
        super(DiskTreeTransform, self).adjust_path(name, parent, trans_id)
        if (trans_id in self._limbo_files
                and trans_id not in self._needs_rename):
            self._rename_in_limbo([trans_id])
            if previous_parent != parent:
                self._limbo_children[previous_parent].remove(trans_id)
            if previous_parent != parent or previous_name != name:
                del self._limbo_children_names[previous_parent][previous_name]

    def _rename_in_limbo(self, trans_ids):
        """Fix limbo names so that the right final path is produced.

        This means we outsmarted ourselves-- we tried to avoid renaming
        these files later by creating them with their final names in their
        final parents.  But now the previous name or parent is no longer
        suitable, so we have to rename them.

        Even for trans_ids that have no new contents, we must remove their
        entries from _limbo_files, because they are now stale.
        """
        for trans_id in trans_ids:
            old_path = self._limbo_files[trans_id]
            self._possibly_stale_limbo_files.add(old_path)
            del self._limbo_files[trans_id]
            if trans_id not in self._new_contents:
                continue
            new_path = self._limbo_name(trans_id)
            os.rename(old_path, new_path)
            self._possibly_stale_limbo_files.remove(old_path)
            for descendant in self._limbo_descendants(trans_id):
                desc_path = self._limbo_files[descendant]
                desc_path = new_path + desc_path[len(old_path):]
                self._limbo_files[descendant] = desc_path

    def _limbo_descendants(self, trans_id):
        """Return the set of trans_ids whose limbo paths descend from this."""
        descendants = set(self._limbo_children.get(trans_id, []))
        for descendant in list(descendants):
            descendants.update(self._limbo_descendants(descendant))
        return descendants

    def _set_mode(self, trans_id, mode_id, typefunc):
        raise NotImplementedError(self._set_mode)

    def create_file(self, contents, trans_id, mode_id=None, sha1=None):
        """Schedule creation of a new file.

        :seealso: new_file.

        :param contents: an iterator of strings, all of which will be written
            to the target destination.
        :param trans_id: TreeTransform handle
        :param mode_id: If not None, force the mode of the target file to match
            the mode of the object referenced by mode_id.
            Otherwise, we will try to preserve mode bits of an existing file.
        :param sha1: If the sha1 of this content is already known, pass it in.
            We can use it to prevent future sha1 computations.
        """
        name = self._limbo_name(trans_id)
        with open(name, 'wb') as f:
            unique_add(self._new_contents, trans_id, 'file')
            f.writelines(contents)
        self._set_mtime(name)
        self._set_mode(trans_id, mode_id, S_ISREG)
        # It is unfortunate we have to use lstat instead of fstat, but we just
        # used utime and chmod on the file, so we need the accurate final
        # details.
        if sha1 is not None:
            self._observed_sha1s[trans_id] = (sha1, osutils.lstat(name))

    def _read_symlink_target(self, trans_id):
        return os.readlink(self._limbo_name(trans_id))

    def _set_mtime(self, path):
        """All files that are created get the same mtime.

        This time is set by the first object to be created.
        """
        if self._creation_mtime is None:
            self._creation_mtime = time.time()
        os.utime(path, (self._creation_mtime, self._creation_mtime))

    def create_hardlink(self, path, trans_id):
        """Schedule creation of a hard link"""
        name = self._limbo_name(trans_id)
        try:
            os.link(path, name)
        except OSError as e:
            if e.errno != errno.EPERM:
                raise
            raise errors.HardLinkNotSupported(path)
        try:
            unique_add(self._new_contents, trans_id, 'file')
        except BaseException:
            # Clean up the file, it never got registered so
            # TreeTransform.finalize() won't clean it up.
            os.unlink(name)
            raise

    def create_directory(self, trans_id):
        """Schedule creation of a new directory.

        See also new_directory.
        """
        os.mkdir(self._limbo_name(trans_id))
        unique_add(self._new_contents, trans_id, 'directory')

    def create_symlink(self, target, trans_id):
        """Schedule creation of a new symbolic link.

        target is a bytestring.
        See also new_symlink.
        """
        if self._create_symlinks:
            os.symlink(target, self._limbo_name(trans_id))
        else:
            try:
                path = FinalPaths(self).get_path(trans_id)
            except KeyError:
                path = None
            trace.warning(
                'Unable to create symlink "%s" on this filesystem.' % (path,))
        # We add symlink to _new_contents even if they are unsupported
        # and not created. These entries are subsequently used to avoid
        # conflicts on platforms that don't support symlink
        unique_add(self._new_contents, trans_id, 'symlink')

    def cancel_creation(self, trans_id):
        """Cancel the creation of new file contents."""
        del self._new_contents[trans_id]
        if trans_id in self._observed_sha1s:
            del self._observed_sha1s[trans_id]
        children = self._limbo_children.get(trans_id)
        # if this is a limbo directory with children, move them before removing
        # the directory
        if children is not None:
            self._rename_in_limbo(children)
            del self._limbo_children[trans_id]
            del self._limbo_children_names[trans_id]
        osutils.delete_any(self._limbo_name(trans_id))

    def new_orphan(self, trans_id, parent_id):
        conf = self._tree.get_config_stack()
        handle_orphan = conf.get('transform.orphan_policy')
        handle_orphan(self, trans_id, parent_id)


class InventoryTreeTransform(DiskTreeTransform):
    """Represent a tree transformation.

    This object is designed to support incremental generation of the transform,
    in any order.

    However, it gives optimum performance when parent directories are created
    before their contents.  The transform is then able to put child files
    directly in their parent directory, avoiding later renames.

    It is easy to produce malformed transforms, but they are generally
    harmless.  Attempting to apply a malformed transform will cause an
    exception to be raised before any modifications are made to the tree.

    Many kinds of malformed transforms can be corrected with the
    resolve_conflicts function.  The remaining ones indicate programming error,
    such as trying to create a file with no path.

    Two sets of file creation methods are supplied.  Convenience methods are:
     * new_file
     * new_directory
     * new_symlink

    These are composed of the low-level methods:
     * create_path
     * create_file or create_directory or create_symlink
     * version_file
     * set_executability

    Transform/Transaction ids
    -------------------------
    trans_ids are temporary ids assigned to all files involved in a transform.
    It's possible, even common, that not all files in the Tree have trans_ids.

    trans_ids are used because filenames and file_ids are not good enough
    identifiers; filenames change, and not all files have file_ids.  File-ids
    are also associated with trans-ids, so that moving a file moves its
    file-id.

    trans_ids are only valid for the TreeTransform that generated them.

    Limbo
    -----
    Limbo is a temporary directory use to hold new versions of files.
    Files are added to limbo by create_file, create_directory, create_symlink,
    and their convenience variants (new_*).  Files may be removed from limbo
    using cancel_creation.  Files are renamed from limbo into their final
    location as part of TreeTransform.apply

    Limbo must be cleaned up, by either calling TreeTransform.apply or
    calling TreeTransform.finalize.

    Files are placed into limbo inside their parent directories, where
    possible.  This reduces subsequent renames, and makes operations involving
    lots of files faster.  This optimization is only possible if the parent
    directory is created *before* creating any of its children, so avoid
    creating children before parents, where possible.

    Pending-deletion
    ----------------
    This temporary directory is used by _FileMover for storing files that are
    about to be deleted.  In case of rollback, the files will be restored.
    FileMover does not delete files until it is sure that a rollback will not
    happen.
    """

    def __init__(self, tree, pb=None):
        """Note: a tree_write lock is taken on the tree.

        Use TreeTransform.finalize() to release the lock (can be omitted if
        TreeTransform.apply() called).
        """
        tree.lock_tree_write()
        try:
            limbodir = urlutils.local_path_from_url(
                tree._transport.abspath('limbo'))
            osutils.ensure_empty_directory_exists(
                limbodir,
                errors.ExistingLimbo)
            deletiondir = urlutils.local_path_from_url(
                tree._transport.abspath('pending-deletion'))
            osutils.ensure_empty_directory_exists(
                deletiondir,
                errors.ExistingPendingDeletion)
        except BaseException:
            tree.unlock()
            raise

        # Cache of realpath results, to speed up canonical_path
        self._realpaths = {}
        # Cache of relpath results, to speed up canonical_path
        self._relpaths = {}
        DiskTreeTransform.__init__(self, tree, limbodir, pb,
                                   tree.case_sensitive)
        self._deletiondir = deletiondir

    def canonical_path(self, path):
        """Get the canonical tree-relative path"""
        # don't follow final symlinks
        abs = self._tree.abspath(path)
        if abs in self._relpaths:
            return self._relpaths[abs]
        dirname, basename = os.path.split(abs)
        if dirname not in self._realpaths:
            self._realpaths[dirname] = os.path.realpath(dirname)
        dirname = self._realpaths[dirname]
        abs = osutils.pathjoin(dirname, basename)
        if dirname in self._relpaths:
            relpath = osutils.pathjoin(self._relpaths[dirname], basename)
            relpath = relpath.rstrip('/\\')
        else:
            relpath = self._tree.relpath(abs)
        self._relpaths[abs] = relpath
        return relpath

    def tree_kind(self, trans_id):
        """Determine the file kind in the working tree.

        :returns: The file kind or None if the file does not exist
        """
        path = self._tree_id_paths.get(trans_id)
        if path is None:
            return None
        try:
            return osutils.file_kind(self._tree.abspath(path))
        except errors.NoSuchFile:
            return None

    def _set_mode(self, trans_id, mode_id, typefunc):
        """Set the mode of new file contents.
        The mode_id is the existing file to get the mode from (often the same
        as trans_id).  The operation is only performed if there's a mode match
        according to typefunc.
        """
        if mode_id is None:
            mode_id = trans_id
        try:
            old_path = self._tree_id_paths[mode_id]
        except KeyError:
            return
        try:
            mode = os.stat(self._tree.abspath(old_path)).st_mode
        except OSError as e:
            if e.errno in (errno.ENOENT, errno.ENOTDIR):
                # Either old_path doesn't exist, or the parent of the
                # target is not a directory (but will be one eventually)
                # Either way, we know it doesn't exist *right now*
                # See also bug #248448
                return
            else:
                raise
        if typefunc(mode):
            osutils.chmod_if_possible(self._limbo_name(trans_id), mode)

    def iter_tree_children(self, parent_id):
        """Iterate through the entry's tree children, if any"""
        try:
            path = self._tree_id_paths[parent_id]
        except KeyError:
            return
        try:
            children = os.listdir(self._tree.abspath(path))
        except OSError as e:
            if not (osutils._is_error_enotdir(e) or
                    e.errno in (errno.ENOENT, errno.ESRCH)):
                raise
            return

        for child in children:
            childpath = joinpath(path, child)
            if self._tree.is_control_filename(childpath):
                continue
            yield self.trans_id_tree_path(childpath)

    def _generate_limbo_path(self, trans_id):
        """Generate a limbo path using the final path if possible.

        This optimizes the performance of applying the tree transform by
        avoiding renames.  These renames can be avoided only when the parent
        directory is already scheduled for creation.

        If the final path cannot be used, falls back to using the trans_id as
        the relpath.
        """
        parent = self._new_parent.get(trans_id)
        # if the parent directory is already in limbo (e.g. when building a
        # tree), choose a limbo name inside the parent, to reduce further
        # renames.
        use_direct_path = False
        if self._new_contents.get(parent) == 'directory':
            filename = self._new_name.get(trans_id)
            if filename is not None:
                if parent not in self._limbo_children:
                    self._limbo_children[parent] = set()
                    self._limbo_children_names[parent] = {}
                    use_direct_path = True
                # the direct path can only be used if no other file has
                # already taken this pathname, i.e. if the name is unused, or
                # if it is already associated with this trans_id.
                elif self._case_sensitive_target:
                    if (self._limbo_children_names[parent].get(filename)
                            in (trans_id, None)):
                        use_direct_path = True
                else:
                    for l_filename, l_trans_id in viewitems(
                            self._limbo_children_names[parent]):
                        if l_trans_id == trans_id:
                            continue
                        if l_filename.lower() == filename.lower():
                            break
                    else:
                        use_direct_path = True

        if not use_direct_path:
            return DiskTreeTransform._generate_limbo_path(self, trans_id)

        limbo_name = osutils.pathjoin(self._limbo_files[parent], filename)
        self._limbo_children[parent].add(trans_id)
        self._limbo_children_names[parent][filename] = trans_id
        return limbo_name

    def version_file(self, trans_id, file_id=None):
        """Schedule a file to become versioned."""
        if file_id is None:
            raise ValueError()
        unique_add(self._new_id, trans_id, file_id)
        unique_add(self._r_new_id, file_id, trans_id)

    def cancel_versioning(self, trans_id):
        """Undo a previous versioning of a file"""
        file_id = self._new_id[trans_id]
        del self._new_id[trans_id]
        del self._r_new_id[file_id]

    def _duplicate_ids(self):
        """Each inventory id may only be used once"""
        conflicts = []
        try:
            all_ids = self._tree.all_file_ids()
        except errors.UnsupportedOperation:
            # it's okay for non-file-id trees to raise UnsupportedOperation.
            return []
        removed_tree_ids = set((self.tree_file_id(trans_id) for trans_id in
                                self._removed_id))
        active_tree_ids = all_ids.difference(removed_tree_ids)
        for trans_id, file_id in viewitems(self._new_id):
            if file_id in active_tree_ids:
                path = self._tree.id2path(file_id)
                old_trans_id = self.trans_id_tree_path(path)
                conflicts.append(('duplicate id', old_trans_id, trans_id))
        return conflicts

    def find_conflicts(self):
        conflicts = super(InventoryTreeTransform, self).find_conflicts()
        conflicts.extend(self._duplicate_ids())
        return conflicts

    def apply(self, no_conflicts=False, precomputed_delta=None, _mover=None):
        """Apply all changes to the inventory and filesystem.

        If filesystem or inventory conflicts are present, MalformedTransform
        will be thrown.

        If apply succeeds, finalize is not necessary.

        :param no_conflicts: if True, the caller guarantees there are no
            conflicts, so no check is made.
        :param precomputed_delta: An inventory delta to use instead of
            calculating one.
        :param _mover: Supply an alternate FileMover, for testing
        """
        for hook in MutableTree.hooks['pre_transform']:
            hook(self._tree, self)
        if not no_conflicts:
            self._check_malformed()
        self.rename_count = 0
        with ui.ui_factory.nested_progress_bar() as child_pb:
            if precomputed_delta is None:
                child_pb.update(gettext('Apply phase'), 0, 2)
                inventory_delta = self._generate_inventory_delta()
                offset = 1
            else:
                inventory_delta = precomputed_delta
                offset = 0
            if _mover is None:
                mover = _FileMover()
            else:
                mover = _mover
            try:
                child_pb.update(gettext('Apply phase'), 0 + offset, 2 + offset)
                self._apply_removals(mover)
                child_pb.update(gettext('Apply phase'), 1 + offset, 2 + offset)
                modified_paths = self._apply_insertions(mover)
            except BaseException:
                mover.rollback()
                raise
            else:
                mover.apply_deletions()
        if self.final_file_id(self.root) is None:
            inventory_delta = [e for e in inventory_delta if e[0] != '']
        self._tree.apply_inventory_delta(inventory_delta)
        self._apply_observed_sha1s()
        self._done = True
        self.finalize()
        return _TransformResults(modified_paths, self.rename_count)

    def _apply_removals(self, mover):
        """Perform tree operations that remove directory/inventory names.

        That is, delete files that are to be deleted, and put any files that
        need renaming into limbo.  This must be done in strict child-to-parent
        order.

        If inventory_delta is None, no inventory delta generation is performed.
        """
        tree_paths = sorted(viewitems(self._tree_path_ids), reverse=True)
        with ui.ui_factory.nested_progress_bar() as child_pb:
            for num, (path, trans_id) in enumerate(tree_paths):
                # do not attempt to move root into a subdirectory of itself.
                if path == '':
                    continue
                child_pb.update(gettext('removing file'), num, len(tree_paths))
                full_path = self._tree.abspath(path)
                if trans_id in self._removed_contents:
                    delete_path = os.path.join(self._deletiondir, trans_id)
                    mover.pre_delete(full_path, delete_path)
                elif (trans_id in self._new_name or
                      trans_id in self._new_parent):
                    try:
                        mover.rename(full_path, self._limbo_name(trans_id))
                    except TransformRenameFailed as e:
                        if e.errno != errno.ENOENT:
                            raise
                    else:
                        self.rename_count += 1

    def _apply_insertions(self, mover):
        """Perform tree operations that insert directory/inventory names.

        That is, create any files that need to be created, and restore from
        limbo any files that needed renaming.  This must be done in strict
        parent-to-child order.

        If inventory_delta is None, no inventory delta is calculated, and
        no list of modified paths is returned.
        """
        new_paths = self.new_paths(filesystem_only=True)
        modified_paths = []
        with ui.ui_factory.nested_progress_bar() as child_pb:
            for num, (path, trans_id) in enumerate(new_paths):
                if (num % 10) == 0:
                    child_pb.update(gettext('adding file'),
                                    num, len(new_paths))
                full_path = self._tree.abspath(path)
                if trans_id in self._needs_rename:
                    try:
                        mover.rename(self._limbo_name(trans_id), full_path)
                    except TransformRenameFailed as e:
                        # We may be renaming a dangling inventory id
                        if e.errno != errno.ENOENT:
                            raise
                    else:
                        self.rename_count += 1
                    # TODO: if trans_id in self._observed_sha1s, we should
                    #       re-stat the final target, since ctime will be
                    #       updated by the change.
                if (trans_id in self._new_contents
                        or self.path_changed(trans_id)):
                    if trans_id in self._new_contents:
                        modified_paths.append(full_path)
                if trans_id in self._new_executability:
                    self._set_executability(path, trans_id)
                if trans_id in self._observed_sha1s:
                    o_sha1, o_st_val = self._observed_sha1s[trans_id]
                    st = osutils.lstat(full_path)
                    self._observed_sha1s[trans_id] = (o_sha1, st)
        for path, trans_id in new_paths:
            # new_paths includes stuff like workingtree conflicts. Only the
            # stuff in new_contents actually comes from limbo.
            if trans_id in self._limbo_files:
                del self._limbo_files[trans_id]
        self._new_contents.clear()
        return modified_paths

    def _apply_observed_sha1s(self):
        """After we have finished renaming everything, update observed sha1s

        This has to be done after self._tree.apply_inventory_delta, otherwise
        it doesn't know anything about the files we are updating. Also, we want
        to do this as late as possible, so that most entries end up cached.
        """
        # TODO: this doesn't update the stat information for directories. So
        #       the first 'bzr status' will still need to rewrite
        #       .bzr/checkout/dirstate. However, we at least don't need to
        #       re-read all of the files.
        # TODO: If the operation took a while, we could do a time.sleep(3) here
        #       to allow the clock to tick over and ensure we won't have any
        #       problems. (we could observe start time, and finish time, and if
        #       it is less than eg 10% overhead, add a sleep call.)
        paths = FinalPaths(self)
        for trans_id, observed in viewitems(self._observed_sha1s):
            path = paths.get_path(trans_id)
            self._tree._observed_sha1(path, observed)

    def get_preview_tree(self):
        """Return a tree representing the result of the transform.

        The tree is a snapshot, and altering the TreeTransform will invalidate
        it.
        """
        return _PreviewTree(self)

    def _inventory_altered(self):
        """Determine which trans_ids need new Inventory entries.

        An new entry is needed when anything that would be reflected by an
        inventory entry changes, including file name, file_id, parent file_id,
        file kind, and the execute bit.

        Some care is taken to return entries with real changes, not cases
        where the value is deleted and then restored to its original value,
        but some actually unchanged values may be returned.

        :returns: A list of (path, trans_id) for all items requiring an
            inventory change. Ordered by path.
        """
        changed_ids = set()
        # Find entries whose file_ids are new (or changed).
        new_file_id = set(t for t in self._new_id
                          if self._new_id[t] != self.tree_file_id(t))
        for id_set in [self._new_name, self._new_parent, new_file_id,
                       self._new_executability]:
            changed_ids.update(id_set)
        # removing implies a kind change
        changed_kind = set(self._removed_contents)
        # so does adding
        changed_kind.intersection_update(self._new_contents)
        # Ignore entries that are already known to have changed.
        changed_kind.difference_update(changed_ids)
        #  to keep only the truly changed ones
        changed_kind = (t for t in changed_kind
                        if self.tree_kind(t) != self.final_kind(t))
        # all kind changes will alter the inventory
        changed_ids.update(changed_kind)
        # To find entries with changed parent_ids, find parents which existed,
        # but changed file_id.
        # Now add all their children to the set.
        for parent_trans_id in new_file_id:
            changed_ids.update(self.iter_tree_children(parent_trans_id))
        return sorted(FinalPaths(self).get_paths(changed_ids))

    def _generate_inventory_delta(self):
        """Generate an inventory delta for the current transform."""
        inventory_delta = []
        new_paths = self._inventory_altered()
        total_entries = len(new_paths) + len(self._removed_id)
        with ui.ui_factory.nested_progress_bar() as child_pb:
            for num, trans_id in enumerate(self._removed_id):
                if (num % 10) == 0:
                    child_pb.update(gettext('removing file'),
                                    num, total_entries)
                if trans_id == self._new_root:
                    file_id = self._tree.path2id('')
                else:
                    file_id = self.tree_file_id(trans_id)
                # File-id isn't really being deleted, just moved
                if file_id in self._r_new_id:
                    continue
                path = self._tree_id_paths[trans_id]
                inventory_delta.append((path, None, file_id, None))
            new_path_file_ids = dict((t, self.final_file_id(t)) for p, t in
                                     new_paths)
            for num, (path, trans_id) in enumerate(new_paths):
                if (num % 10) == 0:
                    child_pb.update(gettext('adding file'),
                                    num + len(self._removed_id), total_entries)
                file_id = new_path_file_ids[trans_id]
                if file_id is None:
                    continue
                kind = self.final_kind(trans_id)
                if kind is None:
                    kind = self._tree.stored_kind(self._tree.id2path(file_id))
                parent_trans_id = self.final_parent(trans_id)
                parent_file_id = new_path_file_ids.get(parent_trans_id)
                if parent_file_id is None:
                    parent_file_id = self.final_file_id(parent_trans_id)
                if trans_id in self._new_reference_revision:
                    new_entry = inventory.TreeReference(
                        file_id,
                        self._new_name[trans_id],
                        self.final_file_id(self._new_parent[trans_id]),
                        None, self._new_reference_revision[trans_id])
                else:
                    new_entry = inventory.make_entry(kind,
                                                     self.final_name(trans_id),
                                                     parent_file_id, file_id)
                try:
                    old_path = self._tree.id2path(new_entry.file_id)
                except errors.NoSuchId:
                    old_path = None
                new_executability = self._new_executability.get(trans_id)
                if new_executability is not None:
                    new_entry.executable = new_executability
                inventory_delta.append(
                    (old_path, path, new_entry.file_id, new_entry))
        return inventory_delta


class TransformPreview(InventoryTreeTransform):
    """A TreeTransform for generating preview trees.

    Unlike TreeTransform, this version works when the input tree is a
    RevisionTree, rather than a WorkingTree.  As a result, it tends to ignore
    unversioned files in the input tree.
    """

    def __init__(self, tree, pb=None, case_sensitive=True):
        tree.lock_read()
        limbodir = osutils.mkdtemp(prefix='bzr-limbo-')
        DiskTreeTransform.__init__(self, tree, limbodir, pb, case_sensitive)

    def canonical_path(self, path):
        return path

    def tree_kind(self, trans_id):
        path = self._tree_id_paths.get(trans_id)
        if path is None:
            return None
        kind = self._tree.path_content_summary(path)[0]
        if kind == 'missing':
            kind = None
        return kind

    def _set_mode(self, trans_id, mode_id, typefunc):
        """Set the mode of new file contents.
        The mode_id is the existing file to get the mode from (often the same
        as trans_id).  The operation is only performed if there's a mode match
        according to typefunc.
        """
        # is it ok to ignore this?  probably
        pass

    def iter_tree_children(self, parent_id):
        """Iterate through the entry's tree children, if any"""
        try:
            path = self._tree_id_paths[parent_id]
        except KeyError:
            return
        try:
            entry = next(self._tree.iter_entries_by_dir(
                specific_files=[path]))[1]
        except StopIteration:
            return
        children = getattr(entry, 'children', {})
        for child in children:
            childpath = joinpath(path, child)
            yield self.trans_id_tree_path(childpath)

    def new_orphan(self, trans_id, parent_id):
        raise NotImplementedError(self.new_orphan)


class _PreviewTree(inventorytree.InventoryTree):
    """Partial implementation of Tree to support show_diff_trees"""

    def __init__(self, transform):
        self._transform = transform
        self._final_paths = FinalPaths(transform)
        self.__by_parent = None
        self._parent_ids = []
        self._all_children_cache = {}
        self._path2trans_id_cache = {}
        self._final_name_cache = {}
        self._iter_changes_cache = {
            c.file_id: c for c in self._transform.iter_changes()}

    def supports_tree_reference(self):
        # TODO(jelmer): Support tree references in _PreviewTree.
        # return self._transform._tree.supports_tree_reference()
        return False

    def _content_change(self, file_id):
        """Return True if the content of this file changed"""
        changes = self._iter_changes_cache.get(file_id)
        return (changes is not None and changes.changed_content)

    def _get_repository(self):
        repo = getattr(self._transform._tree, '_repository', None)
        if repo is None:
            repo = self._transform._tree.branch.repository
        return repo

    def _iter_parent_trees(self):
        for revision_id in self.get_parent_ids():
            try:
                yield self.revision_tree(revision_id)
            except errors.NoSuchRevisionInTree:
                yield self._get_repository().revision_tree(revision_id)

    def _get_file_revision(self, path, file_id, vf, tree_revision):
        parent_keys = [
            (file_id, t.get_file_revision(t.id2path(file_id)))
            for t in self._iter_parent_trees()]
        vf.add_lines((file_id, tree_revision), parent_keys,
                     self.get_file_lines(path))
        repo = self._get_repository()
        base_vf = repo.texts
        if base_vf not in vf.fallback_versionedfiles:
            vf.fallback_versionedfiles.append(base_vf)
        return tree_revision

    def _stat_limbo_file(self, trans_id):
        name = self._transform._limbo_name(trans_id)
        return os.lstat(name)

    @property
    def _by_parent(self):
        if self.__by_parent is None:
            self.__by_parent = self._transform.by_parent()
        return self.__by_parent

    def _comparison_data(self, entry, path):
        kind, size, executable, link_or_sha1 = self.path_content_summary(path)
        if kind == 'missing':
            kind = None
            executable = False
        else:
            file_id = self._transform.final_file_id(self._path2trans_id(path))
            executable = self.is_executable(path)
        return kind, executable, None

    def is_locked(self):
        return False

    def lock_read(self):
        # Perhaps in theory, this should lock the TreeTransform?
        return lock.LogicalLockResult(self.unlock)

    def unlock(self):
        pass

    @property
    def root_inventory(self):
        """This Tree does not use inventory as its backing data."""
        raise NotImplementedError(_PreviewTree.root_inventory)

    def all_file_ids(self):
        tree_ids = set(self._transform._tree.all_file_ids())
        tree_ids.difference_update(self._transform.tree_file_id(t)
                                   for t in self._transform._removed_id)
        tree_ids.update(viewvalues(self._transform._new_id))
        return tree_ids

    def all_versioned_paths(self):
        tree_paths = set(self._transform._tree.all_versioned_paths())

        tree_paths.difference_update(
            self._transform.trans_id_tree_path(t)
            for t in self._transform._removed_id)

        tree_paths.update(
            self._final_paths._determine_path(t)
            for t in self._transform._new_id)

        return tree_paths

    def _path2trans_id(self, path):
        # We must not use None here, because that is a valid value to store.
        trans_id = self._path2trans_id_cache.get(path, object)
        if trans_id is not object:
            return trans_id
        segments = osutils.splitpath(path)
        cur_parent = self._transform.root
        for cur_segment in segments:
            for child in self._all_children(cur_parent):
                final_name = self._final_name_cache.get(child)
                if final_name is None:
                    final_name = self._transform.final_name(child)
                    self._final_name_cache[child] = final_name
                if final_name == cur_segment:
                    cur_parent = child
                    break
            else:
                self._path2trans_id_cache[path] = None
                return None
        self._path2trans_id_cache[path] = cur_parent
        return cur_parent

    def path2id(self, path):
        if isinstance(path, list):
            if path == []:
                path = [""]
            path = osutils.pathjoin(*path)
        return self._transform.final_file_id(self._path2trans_id(path))

    def id2path(self, file_id, recurse='down'):
        trans_id = self._transform.trans_id_file_id(file_id)
        try:
            return self._final_paths._determine_path(trans_id)
        except NoFinalPath:
            raise errors.NoSuchId(self, file_id)

    def _all_children(self, trans_id):
        children = self._all_children_cache.get(trans_id)
        if children is not None:
            return children
        children = set(self._transform.iter_tree_children(trans_id))
        # children in the _new_parent set are provided by _by_parent.
        children.difference_update(self._transform._new_parent)
        children.update(self._by_parent.get(trans_id, []))
        self._all_children_cache[trans_id] = children
        return children

    def extras(self):
        possible_extras = set(self._transform.trans_id_tree_path(p) for p
                              in self._transform._tree.extras())
        possible_extras.update(self._transform._new_contents)
        possible_extras.update(self._transform._removed_id)
        for trans_id in possible_extras:
            if self._transform.final_file_id(trans_id) is None:
                yield self._final_paths._determine_path(trans_id)

    def _make_inv_entries(self, ordered_entries, specific_files=None):
        for trans_id, parent_file_id in ordered_entries:
            file_id = self._transform.final_file_id(trans_id)
            if file_id is None:
                continue
            if (specific_files is not None
                    and self._final_paths.get_path(trans_id) not in specific_files):
                continue
            kind = self._transform.final_kind(trans_id)
            if kind is None:
                kind = self._transform._tree.stored_kind(
                    self._transform._tree.id2path(file_id))
            new_entry = inventory.make_entry(
                kind,
                self._transform.final_name(trans_id),
                parent_file_id, file_id)
            yield new_entry, trans_id

    def _list_files_by_dir(self):
        todo = [ROOT_PARENT]
        ordered_ids = []
        while len(todo) > 0:
            parent = todo.pop()
            parent_file_id = self._transform.final_file_id(parent)
            children = list(self._all_children(parent))
            paths = dict(zip(children, self._final_paths.get_paths(children)))
            children.sort(key=paths.get)
            todo.extend(reversed(children))
            for trans_id in children:
                ordered_ids.append((trans_id, parent_file_id))
        return ordered_ids

    def iter_child_entries(self, path):
        trans_id = self._path2trans_id(path)
        if trans_id is None:
            raise errors.NoSuchFile(path)
        todo = [(child_trans_id, trans_id) for child_trans_id in
                self._all_children(trans_id)]
        for entry, trans_id in self._make_inv_entries(todo):
            yield entry

    def iter_entries_by_dir(self, specific_files=None, recurse_nested=False):
        if recurse_nested:
            raise NotImplementedError(
                'follow tree references not yet supported')

        # This may not be a maximally efficient implementation, but it is
        # reasonably straightforward.  An implementation that grafts the
        # TreeTransform changes onto the tree's iter_entries_by_dir results
        # might be more efficient, but requires tricky inferences about stack
        # position.
        ordered_ids = self._list_files_by_dir()
        for entry, trans_id in self._make_inv_entries(ordered_ids,
                                                      specific_files):
            yield self._final_paths.get_path(trans_id), entry

    def _iter_entries_for_dir(self, dir_path):
        """Return path, entry for items in a directory without recursing down."""
        ordered_ids = []
        dir_trans_id = self._path2trans_id(dir_path)
        dir_id = self._transform.final_file_id(dir_trans_id)
        for child_trans_id in self._all_children(dir_trans_id):
            ordered_ids.append((child_trans_id, dir_id))
        path_entries = []
        for entry, trans_id in self._make_inv_entries(ordered_ids):
            path_entries.append((self._final_paths.get_path(trans_id), entry))
        path_entries.sort()
        return path_entries

    def list_files(self, include_root=False, from_dir=None, recursive=True,
                   recurse_nested=False):
        """See WorkingTree.list_files."""
        if recurse_nested:
            raise NotImplementedError(
                'follow tree references not yet supported')

        # XXX This should behave like WorkingTree.list_files, but is really
        # more like RevisionTree.list_files.
        if from_dir == '.':
            from_dir = None
        if recursive:
            prefix = None
            if from_dir:
                prefix = from_dir + '/'
            entries = self.iter_entries_by_dir()
            for path, entry in entries:
                if entry.name == '' and not include_root:
                    continue
                if prefix:
                    if not path.startswith(prefix):
                        continue
                    path = path[len(prefix):]
                yield path, 'V', entry.kind, entry
        else:
            if from_dir is None and include_root is True:
                root_entry = inventory.make_entry(
                    'directory', '', ROOT_PARENT, self.path2id(''))
                yield '', 'V', 'directory', root_entry
            entries = self._iter_entries_for_dir(from_dir or '')
            for path, entry in entries:
                yield path, 'V', entry.kind, entry

    def kind(self, path):
        trans_id = self._path2trans_id(path)
        if trans_id is None:
            raise errors.NoSuchFile(path)
        return self._transform.final_kind(trans_id)

    def stored_kind(self, path):
        trans_id = self._path2trans_id(path)
        if trans_id is None:
            raise errors.NoSuchFile(path)
        try:
            return self._transform._new_contents[trans_id]
        except KeyError:
            return self._transform._tree.stored_kind(path)

    def get_file_mtime(self, path):
        """See Tree.get_file_mtime"""
        file_id = self.path2id(path)
        if file_id is None:
            raise errors.NoSuchFile(path)
        if not self._content_change(file_id):
            return self._transform._tree.get_file_mtime(
                self._transform._tree.id2path(file_id))
        trans_id = self._path2trans_id(path)
        return self._stat_limbo_file(trans_id).st_mtime

    def get_file_size(self, path):
        """See Tree.get_file_size"""
        trans_id = self._path2trans_id(path)
        if trans_id is None:
            raise errors.NoSuchFile(path)
        kind = self._transform.final_kind(trans_id)
        if kind != 'file':
            return None
        if trans_id in self._transform._new_contents:
            return self._stat_limbo_file(trans_id).st_size
        if self.kind(path) == 'file':
            return self._transform._tree.get_file_size(path)
        else:
            return None

    def get_file_verifier(self, path, stat_value=None):
        trans_id = self._path2trans_id(path)
        if trans_id is None:
            raise errors.NoSuchFile(path)
        kind = self._transform._new_contents.get(trans_id)
        if kind is None:
            return self._transform._tree.get_file_verifier(path)
        if kind == 'file':
            with self.get_file(path) as fileobj:
                return ("SHA1", osutils.sha_file(fileobj))

    def get_file_sha1(self, path, stat_value=None):
        trans_id = self._path2trans_id(path)
        if trans_id is None:
            raise errors.NoSuchFile(path)
        kind = self._transform._new_contents.get(trans_id)
        if kind is None:
            return self._transform._tree.get_file_sha1(path)
        if kind == 'file':
            with self.get_file(path) as fileobj:
                return osutils.sha_file(fileobj)

    def get_reference_revision(self, path):
        trans_id = self._path2trans_id(path)
        if trans_id is None:
            raise errors.NoSuchFile(path)
        reference_revision = self._transform._new_reference_revision.get(trans_id)
        if reference_revision is None:
            return self._transform._tree.get_reference_revision(path)
        return reference_revision

    def is_executable(self, path):
        trans_id = self._path2trans_id(path)
        if trans_id is None:
            return False
        try:
            return self._transform._new_executability[trans_id]
        except KeyError:
            try:
                return self._transform._tree.is_executable(path)
            except OSError as e:
                if e.errno == errno.ENOENT:
                    return False
                raise
            except errors.NoSuchFile:
                return False

    def has_filename(self, path):
        trans_id = self._path2trans_id(path)
        if trans_id in self._transform._new_contents:
            return True
        elif trans_id in self._transform._removed_contents:
            return False
        else:
            return self._transform._tree.has_filename(path)

    def path_content_summary(self, path):
        trans_id = self._path2trans_id(path)
        tt = self._transform
        tree_path = tt._tree_id_paths.get(trans_id)
        kind = tt._new_contents.get(trans_id)
        if kind is None:
            if tree_path is None or trans_id in tt._removed_contents:
                return 'missing', None, None, None
            summary = tt._tree.path_content_summary(tree_path)
            kind, size, executable, link_or_sha1 = summary
        else:
            link_or_sha1 = None
            limbo_name = tt._limbo_name(trans_id)
            if trans_id in tt._new_reference_revision:
                kind = 'tree-reference'
            if kind == 'file':
                statval = os.lstat(limbo_name)
                size = statval.st_size
                if not tt._limbo_supports_executable():
                    executable = False
                else:
                    executable = statval.st_mode & S_IEXEC
            else:
                size = None
                executable = None
            if kind == 'symlink':
                link_or_sha1 = os.readlink(limbo_name)
                if not isinstance(link_or_sha1, text_type):
                    link_or_sha1 = link_or_sha1.decode(osutils._fs_enc)
        executable = tt._new_executability.get(trans_id, executable)
        return kind, size, executable, link_or_sha1

    def iter_changes(self, from_tree, include_unchanged=False,
                     specific_files=None, pb=None, extra_trees=None,
                     require_versioned=True, want_unversioned=False):
        """See InterTree.iter_changes.

        This has a fast path that is only used when the from_tree matches
        the transform tree, and no fancy options are supplied.
        """
        if (from_tree is not self._transform._tree or include_unchanged
                or specific_files or want_unversioned):
            return tree.InterTree.get(from_tree, self).iter_changes(
                include_unchanged=include_unchanged,
                specific_files=specific_files,
                pb=pb,
                extra_trees=extra_trees,
                require_versioned=require_versioned,
                want_unversioned=want_unversioned)
        if want_unversioned:
            raise ValueError('want_unversioned is not supported')
        return self._transform.iter_changes()

    def get_file(self, path):
        """See Tree.get_file"""
        file_id = self.path2id(path)
        if not self._content_change(file_id):
            return self._transform._tree.get_file(path)
        trans_id = self._path2trans_id(path)
        name = self._transform._limbo_name(trans_id)
        return open(name, 'rb')

    def get_file_with_stat(self, path):
        return self.get_file(path), None

    def annotate_iter(self, path,
                      default_revision=_mod_revision.CURRENT_REVISION):
        file_id = self.path2id(path)
        changes = self._iter_changes_cache.get(file_id)
        if changes is None:
            get_old = True
        else:
            changed_content, versioned, kind = (
                changes.changed_content, changes.versioned, changes.kind)
            if kind[1] is None:
                return None
            get_old = (kind[0] == 'file' and versioned[0])
        if get_old:
            old_annotation = self._transform._tree.annotate_iter(
                path, default_revision=default_revision)
        else:
            old_annotation = []
        if changes is None:
            return old_annotation
        if not changed_content:
            return old_annotation
        # TODO: This is doing something similar to what WT.annotate_iter is
        #       doing, however it fails slightly because it doesn't know what
        #       the *other* revision_id is, so it doesn't know how to give the
        #       other as the origin for some lines, they all get
        #       'default_revision'
        #       It would be nice to be able to use the new Annotator based
        #       approach, as well.
        return annotate.reannotate([old_annotation],
                                   self.get_file(path).readlines(),
                                   default_revision)

    def get_symlink_target(self, path):
        """See Tree.get_symlink_target"""
        file_id = self.path2id(path)
        if not self._content_change(file_id):
            return self._transform._tree.get_symlink_target(path)
        trans_id = self._path2trans_id(path)
        name = self._transform._limbo_name(trans_id)
        return osutils.readlink(name)

    def walkdirs(self, prefix=''):
        pending = [self._transform.root]
        while len(pending) > 0:
            parent_id = pending.pop()
            children = []
            subdirs = []
            prefix = prefix.rstrip('/')
            parent_path = self._final_paths.get_path(parent_id)
            parent_file_id = self._transform.final_file_id(parent_id)
            for child_id in self._all_children(parent_id):
                path_from_root = self._final_paths.get_path(child_id)
                basename = self._transform.final_name(child_id)
                file_id = self._transform.final_file_id(child_id)
                kind = self._transform.final_kind(child_id)
                if kind is not None:
                    versioned_kind = kind
                else:
                    kind = 'unknown'
                    versioned_kind = self._transform._tree.stored_kind(
                        self._transform._tree.id2path(file_id))
                if versioned_kind == 'directory':
                    subdirs.append(child_id)
                children.append((path_from_root, basename, kind, None,
                                 file_id, versioned_kind))
            children.sort()
            if parent_path.startswith(prefix):
                yield (parent_path, parent_file_id), children
            pending.extend(sorted(subdirs, key=self._final_paths.get_path,
                                  reverse=True))

    def get_parent_ids(self):
        return self._parent_ids

    def set_parent_ids(self, parent_ids):
        self._parent_ids = parent_ids

    def get_revision_tree(self, revision_id):
        return self._transform._tree.get_revision_tree(revision_id)



