# Copyright (C) 2005, 2006 Canonical Ltd
#
# Authors:
#   Johan Rydberg <jrydberg@gnu.org>
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

"""Versioned text file storage api."""

from bzrlib.lazy_import import lazy_import
lazy_import(globals(), """

from bzrlib import (
    errors,
    osutils,
    multiparent,
    tsort,
    revision,
    ui,
    )
from bzrlib.graph import Graph
from bzrlib.transport.memory import MemoryTransport
""")

from cStringIO import StringIO

from bzrlib.inter import InterObject
from bzrlib.registry import Registry
from bzrlib.symbol_versioning import *
from bzrlib.textmerge import TextMerge


adapter_registry = Registry()
adapter_registry.register_lazy(('knit-delta-gz', 'fulltext'), 'bzrlib.knit',
    'DeltaPlainToFullText')
adapter_registry.register_lazy(('knit-ft-gz', 'fulltext'), 'bzrlib.knit',
    'FTPlainToFullText')
adapter_registry.register_lazy(('knit-annotated-delta-gz', 'knit-delta-gz'),
    'bzrlib.knit', 'DeltaAnnotatedToUnannotated')
adapter_registry.register_lazy(('knit-annotated-delta-gz', 'fulltext'),
    'bzrlib.knit', 'DeltaAnnotatedToFullText')
adapter_registry.register_lazy(('knit-annotated-ft-gz', 'knit-ft-gz'),
    'bzrlib.knit', 'FTAnnotatedToUnannotated')
adapter_registry.register_lazy(('knit-annotated-ft-gz', 'fulltext'),
    'bzrlib.knit', 'FTAnnotatedToFullText')


class ContentFactory(object):
    """Abstract interface for insertion and retrieval from a VersionedFile.
    
    :ivar sha1: None, or the sha1 of the content fulltext.
    :ivar storage_kind: The native storage kind of this factory. One of
        'mpdiff', 'knit-annotated-ft', 'knit-annotated-delta', 'knit-ft',
        'knit-delta', 'fulltext', 'knit-annotated-ft-gz',
        'knit-annotated-delta-gz', 'knit-ft-gz', 'knit-delta-gz'.
    :ivar key: The key of this content. Each key is a tuple with a single
        string in it.
    :ivar parents: A tuple of parent keys for self.key. If the object has
        no parent information, None (as opposed to () for an empty list of
        parents).
        """

    def __init__(self):
        """Create a ContentFactory."""
        self.sha1 = None
        self.storage_kind = None
        self.key = None
        self.parents = None


class AbsentContentFactory(object):
    """A placeholder content factory for unavailable texts.
    
    :ivar sha1: None.
    :ivar storage_kind: 'absent'.
    :ivar key: The key of this content. Each key is a tuple with a single
        string in it.
    :ivar parents: None.
    """

    def __init__(self, key):
        """Create a ContentFactory."""
        self.sha1 = None
        self.storage_kind = 'absent'
        self.key = key
        self.parents = None


def filter_absent(record_stream):
    """Adapt a record stream to remove absent records."""
    for record in record_stream:
        if record.storage_kind != 'absent':
            yield record


class VersionedFile(object):
    """Versioned text file storage.
    
    A versioned file manages versions of line-based text files,
    keeping track of the originating version for each line.

    To clients the "lines" of the file are represented as a list of
    strings. These strings will typically have terminal newline
    characters, but this is not required.  In particular files commonly
    do not have a newline at the end of the file.

    Texts are identified by a version-id string.
    """

    @staticmethod
    def check_not_reserved_id(version_id):
        revision.check_not_reserved_id(version_id)

    def copy_to(self, name, transport):
        """Copy this versioned file to name on transport."""
        raise NotImplementedError(self.copy_to)

    def get_record_stream(self, versions, ordering, include_delta_closure):
        """Get a stream of records for versions.

        :param versions: The versions to include. Each version is a tuple
            (version,).
        :param ordering: Either 'unordered' or 'topological'. A topologically
            sorted stream has compression parents strictly before their
            children.
        :param include_delta_closure: If True then the closure across any
            compression parents will be included (in the data content of the
            stream, not in the emitted records). This guarantees that
            'fulltext' can be used successfully on every record.
        :return: An iterator of ContentFactory objects, each of which is only
            valid until the iterator is advanced.
        """
        raise NotImplementedError(self.get_record_stream)

    def has_version(self, version_id):
        """Returns whether version is present."""
        raise NotImplementedError(self.has_version)

    def insert_record_stream(self, stream):
        """Insert a record stream into this versioned file.

        :param stream: A stream of records to insert. 
        :return: None
        :seealso VersionedFile.get_record_stream:
        """
        raise NotImplementedError

    def add_lines(self, version_id, parents, lines, parent_texts=None,
        left_matching_blocks=None, nostore_sha=None, random_id=False,
        check_content=True):
        """Add a single text on top of the versioned file.

        Must raise RevisionAlreadyPresent if the new version is
        already present in file history.

        Must raise RevisionNotPresent if any of the given parents are
        not present in file history.

        :param lines: A list of lines. Each line must be a bytestring. And all
            of them except the last must be terminated with \n and contain no
            other \n's. The last line may either contain no \n's or a single
            terminated \n. If the lines list does meet this constraint the add
            routine may error or may succeed - but you will be unable to read
            the data back accurately. (Checking the lines have been split
            correctly is expensive and extremely unlikely to catch bugs so it
            is not done at runtime unless check_content is True.)
        :param parent_texts: An optional dictionary containing the opaque 
            representations of some or all of the parents of version_id to
            allow delta optimisations.  VERY IMPORTANT: the texts must be those
            returned by add_lines or data corruption can be caused.
        :param left_matching_blocks: a hint about which areas are common
            between the text and its left-hand-parent.  The format is
            the SequenceMatcher.get_matching_blocks format.
        :param nostore_sha: Raise ExistingContent and do not add the lines to
            the versioned file if the digest of the lines matches this.
        :param random_id: If True a random id has been selected rather than
            an id determined by some deterministic process such as a converter
            from a foreign VCS. When True the backend may choose not to check
            for uniqueness of the resulting key within the versioned file, so
            this should only be done when the result is expected to be unique
            anyway.
        :param check_content: If True, the lines supplied are verified to be
            bytestrings that are correctly formed lines.
        :return: The text sha1, the number of bytes in the text, and an opaque
                 representation of the inserted version which can be provided
                 back to future add_lines calls in the parent_texts dictionary.
        """
        self._check_write_ok()
        return self._add_lines(version_id, parents, lines, parent_texts,
            left_matching_blocks, nostore_sha, random_id, check_content)

    def _add_lines(self, version_id, parents, lines, parent_texts,
        left_matching_blocks, nostore_sha, random_id, check_content):
        """Helper to do the class specific add_lines."""
        raise NotImplementedError(self.add_lines)

    def add_lines_with_ghosts(self, version_id, parents, lines,
        parent_texts=None, nostore_sha=None, random_id=False,
        check_content=True, left_matching_blocks=None):
        """Add lines to the versioned file, allowing ghosts to be present.
        
        This takes the same parameters as add_lines and returns the same.
        """
        self._check_write_ok()
        return self._add_lines_with_ghosts(version_id, parents, lines,
            parent_texts, nostore_sha, random_id, check_content, left_matching_blocks)

    def _add_lines_with_ghosts(self, version_id, parents, lines, parent_texts,
        nostore_sha, random_id, check_content, left_matching_blocks):
        """Helper to do class specific add_lines_with_ghosts."""
        raise NotImplementedError(self.add_lines_with_ghosts)

    def check(self, progress_bar=None):
        """Check the versioned file for integrity."""
        raise NotImplementedError(self.check)

    def _check_lines_not_unicode(self, lines):
        """Check that lines being added to a versioned file are not unicode."""
        for line in lines:
            if line.__class__ is not str:
                raise errors.BzrBadParameterUnicode("lines")

    def _check_lines_are_lines(self, lines):
        """Check that the lines really are full lines without inline EOL."""
        for line in lines:
            if '\n' in line[:-1]:
                raise errors.BzrBadParameterContainsNewline("lines")

    def get_format_signature(self):
        """Get a text description of the data encoding in this file.
        
        :since: 0.90
        """
        raise NotImplementedError(self.get_format_signature)

    def make_mpdiffs(self, version_ids):
        """Create multiparent diffs for specified versions."""
        knit_versions = set()
        knit_versions.update(version_ids)
        parent_map = self.get_parent_map(version_ids)
        for version_id in version_ids:
            try:
                knit_versions.update(parent_map[version_id])
            except KeyError:
                raise RevisionNotPresent(version_id, self)
        # We need to filter out ghosts, because we can't diff against them.
        knit_versions = set(self.get_parent_map(knit_versions).keys())
        lines = dict(zip(knit_versions,
            self._get_lf_split_line_list(knit_versions)))
        diffs = []
        for version_id in version_ids:
            target = lines[version_id]
            try:
                parents = [lines[p] for p in parent_map[version_id] if p in
                    knit_versions]
            except KeyError:
                raise RevisionNotPresent(version_id, self)
            if len(parents) > 0:
                left_parent_blocks = self._extract_blocks(version_id,
                                                          parents[0], target)
            else:
                left_parent_blocks = None
            diffs.append(multiparent.MultiParent.from_lines(target, parents,
                         left_parent_blocks))
        return diffs

    def _extract_blocks(self, version_id, source, target):
        return None

    def add_mpdiffs(self, records):
        """Add mpdiffs to this VersionedFile.

        Records should be iterables of version, parents, expected_sha1,
        mpdiff. mpdiff should be a MultiParent instance.
        """
        # Does this need to call self._check_write_ok()? (IanC 20070919)
        vf_parents = {}
        mpvf = multiparent.MultiMemoryVersionedFile()
        versions = []
        for version, parent_ids, expected_sha1, mpdiff in records:
            versions.append(version)
            mpvf.add_diff(mpdiff, version, parent_ids)
        needed_parents = set()
        for version, parent_ids, expected_sha1, mpdiff in records:
            needed_parents.update(p for p in parent_ids
                                  if not mpvf.has_version(p))
        present_parents = set(self.get_parent_map(needed_parents).keys())
        for parent_id, lines in zip(present_parents,
                                 self._get_lf_split_line_list(present_parents)):
            mpvf.add_version(lines, parent_id, [])
        for (version, parent_ids, expected_sha1, mpdiff), lines in\
            zip(records, mpvf.get_line_list(versions)):
            if len(parent_ids) == 1:
                left_matching_blocks = list(mpdiff.get_matching_blocks(0,
                    mpvf.get_diff(parent_ids[0]).num_lines()))
            else:
                left_matching_blocks = None
            try:
                _, _, version_text = self.add_lines_with_ghosts(version,
                    parent_ids, lines, vf_parents,
                    left_matching_blocks=left_matching_blocks)
            except NotImplementedError:
                # The vf can't handle ghosts, so add lines normally, which will
                # (reasonably) fail if there are ghosts in the data.
                _, _, version_text = self.add_lines(version,
                    parent_ids, lines, vf_parents,
                    left_matching_blocks=left_matching_blocks)
            vf_parents[version] = version_text
        for (version, parent_ids, expected_sha1, mpdiff), sha1 in\
             zip(records, self.get_sha1s(versions)):
            if expected_sha1 != sha1:
                raise errors.VersionedFileInvalidChecksum(version)

    def get_sha1s(self, version_ids):
        """Get the stored sha1 sums for the given revisions.

        :param version_ids: The names of the versions to lookup
        :return: a list of sha1s in order according to the version_ids
        """
        raise NotImplementedError(self.get_sha1s)

    def get_text(self, version_id):
        """Return version contents as a text string.

        Raises RevisionNotPresent if version is not present in
        file history.
        """
        return ''.join(self.get_lines(version_id))
    get_string = get_text

    def get_texts(self, version_ids):
        """Return the texts of listed versions as a list of strings.

        Raises RevisionNotPresent if version is not present in
        file history.
        """
        return [''.join(self.get_lines(v)) for v in version_ids]

    def get_lines(self, version_id):
        """Return version contents as a sequence of lines.

        Raises RevisionNotPresent if version is not present in
        file history.
        """
        raise NotImplementedError(self.get_lines)

    def _get_lf_split_line_list(self, version_ids):
        return [StringIO(t).readlines() for t in self.get_texts(version_ids)]

    def get_ancestry(self, version_ids, topo_sorted=True):
        """Return a list of all ancestors of given version(s). This
        will not include the null revision.

        This list will not be topologically sorted if topo_sorted=False is
        passed.

        Must raise RevisionNotPresent if any of the given versions are
        not present in file history."""
        if isinstance(version_ids, basestring):
            version_ids = [version_ids]
        raise NotImplementedError(self.get_ancestry)
        
    def get_ancestry_with_ghosts(self, version_ids):
        """Return a list of all ancestors of given version(s). This
        will not include the null revision.

        Must raise RevisionNotPresent if any of the given versions are
        not present in file history.
        
        Ghosts that are known about will be included in ancestry list,
        but are not explicitly marked.
        """
        raise NotImplementedError(self.get_ancestry_with_ghosts)
    
    def get_parent_map(self, version_ids):
        """Get a map of the parents of version_ids.

        :param version_ids: The version ids to look up parents for.
        :return: A mapping from version id to parents.
        """
        raise NotImplementedError(self.get_parent_map)

    def get_parents_with_ghosts(self, version_id):
        """Return version names for parents of version_id.

        Will raise RevisionNotPresent if version_id is not present
        in the history.

        Ghosts that are known about will be included in the parent list,
        but are not explicitly marked.
        """
        try:
            return list(self.get_parent_map([version_id])[version_id])
        except KeyError:
            raise errors.RevisionNotPresent(version_id, self)

    def annotate(self, version_id):
        """Return a list of (version-id, line) tuples for version_id.

        :raise RevisionNotPresent: If the given version is
        not present in file history.
        """
        raise NotImplementedError(self.annotate)

    @deprecated_method(one_five)
    def join(self, other, pb=None, msg=None, version_ids=None,
             ignore_missing=False):
        """Integrate versions from other into this versioned file.

        If version_ids is None all versions from other should be
        incorporated into this versioned file.

        Must raise RevisionNotPresent if any of the specified versions
        are not present in the other file's history unless ignore_missing
        is supplied in which case they are silently skipped.
        """
        self._check_write_ok()
        return InterVersionedFile.get(other, self).join(
            pb,
            msg,
            version_ids,
            ignore_missing)

    def iter_lines_added_or_present_in_versions(self, version_ids=None,
                                                pb=None):
        """Iterate over the lines in the versioned file from version_ids.

        This may return lines from other versions. Each item the returned
        iterator yields is a tuple of a line and a text version that that line
        is present in (not introduced in).

        Ordering of results is in whatever order is most suitable for the
        underlying storage format.

        If a progress bar is supplied, it may be used to indicate progress.
        The caller is responsible for cleaning up progress bars (because this
        is an iterator).

        NOTES: Lines are normalised: they will all have \n terminators.
               Lines are returned in arbitrary order.

        :return: An iterator over (line, version_id).
        """
        raise NotImplementedError(self.iter_lines_added_or_present_in_versions)

    def plan_merge(self, ver_a, ver_b):
        """Return pseudo-annotation indicating how the two versions merge.

        This is computed between versions a and b and their common
        base.

        Weave lines present in none of them are skipped entirely.

        Legend:
        killed-base Dead in base revision
        killed-both Killed in each revision
        killed-a    Killed in a
        killed-b    Killed in b
        unchanged   Alive in both a and b (possibly created in both)
        new-a       Created in a
        new-b       Created in b
        ghost-a     Killed in a, unborn in b    
        ghost-b     Killed in b, unborn in a
        irrelevant  Not in either revision
        """
        raise NotImplementedError(VersionedFile.plan_merge)
        
    def weave_merge(self, plan, a_marker=TextMerge.A_MARKER,
                    b_marker=TextMerge.B_MARKER):
        return PlanWeaveMerge(plan, a_marker, b_marker).merge_lines()[0]


class RecordingVersionedFileDecorator(object):
    """A minimal versioned file that records calls made on it.
    
    Only enough methods have been added to support tests using it to date.

    :ivar calls: A list of the calls made; can be reset at any time by
        assigning [] to it.
    """

    def __init__(self, backing_vf):
        """Create a RecordingVersionedFileDecorator decorating backing_vf.
        
        :param backing_vf: The versioned file to answer all methods.
        """
        self._backing_vf = backing_vf
        self.calls = []

    def get_lines(self, version_ids):
        self.calls.append(("get_lines", version_ids))
        return self._backing_vf.get_lines(version_ids)


class _PlanMergeVersionedFile(object):
    """A VersionedFile for uncommitted and committed texts.

    It is intended to allow merges to be planned with working tree texts.
    It implements only the small part of the VersionedFile interface used by
    PlanMerge.  It falls back to multiple versionedfiles for data not stored in
    _PlanMergeVersionedFile itself.
    """

    def __init__(self, file_id, fallback_versionedfiles=None):
        """Constuctor

        :param file_id: Used when raising exceptions.
        :param fallback_versionedfiles: If supplied, the set of fallbacks to
            use.  Otherwise, _PlanMergeVersionedFile.fallback_versionedfiles
            can be appended to later.
        """
        self._file_id = file_id
        if fallback_versionedfiles is None:
            self.fallback_versionedfiles = []
        else:
            self.fallback_versionedfiles = fallback_versionedfiles
        self._parents = {}
        self._lines = {}

    def plan_merge(self, ver_a, ver_b, base=None):
        """See VersionedFile.plan_merge"""
        from bzrlib.merge import _PlanMerge
        if base is None:
            return _PlanMerge(ver_a, ver_b, self).plan_merge()
        old_plan = list(_PlanMerge(ver_a, base, self).plan_merge())
        new_plan = list(_PlanMerge(ver_a, ver_b, self).plan_merge())
        return _PlanMerge._subtract_plans(old_plan, new_plan)

    def plan_lca_merge(self, ver_a, ver_b, base=None):
        from bzrlib.merge import _PlanLCAMerge
        graph = self._get_graph()
        new_plan = _PlanLCAMerge(ver_a, ver_b, self, graph).plan_merge()
        if base is None:
            return new_plan
        old_plan = _PlanLCAMerge(ver_a, base, self, graph).plan_merge()
        return _PlanLCAMerge._subtract_plans(list(old_plan), list(new_plan))

    def add_lines(self, version_id, parents, lines):
        """See VersionedFile.add_lines

        Lines are added locally, not fallback versionedfiles.  Also, ghosts are
        permitted.  Only reserved ids are permitted.
        """
        if not revision.is_reserved_id(version_id):
            raise ValueError('Only reserved ids may be used')
        if parents is None:
            raise ValueError('Parents may not be None')
        if lines is None:
            raise ValueError('Lines may not be None')
        self._parents[version_id] = tuple(parents)
        self._lines[version_id] = lines

    def get_lines(self, version_id):
        """See VersionedFile.get_ancestry"""
        lines = self._lines.get(version_id)
        if lines is not None:
            return lines
        for versionedfile in self.fallback_versionedfiles:
            try:
                return versionedfile.get_lines(version_id)
            except errors.RevisionNotPresent:
                continue
        else:
            raise errors.RevisionNotPresent(version_id, self._file_id)

    def get_ancestry(self, version_id, topo_sorted=False):
        """See VersionedFile.get_ancestry.

        Note that this implementation assumes that if a VersionedFile can
        answer get_ancestry at all, it can give an authoritative answer.  In
        fact, ghosts can invalidate this assumption.  But it's good enough
        99% of the time, and far cheaper/simpler.

        Also note that the results of this version are never topologically
        sorted, and are a set.
        """
        if topo_sorted:
            raise ValueError('This implementation does not provide sorting')
        parents = self._parents.get(version_id)
        if parents is None:
            for vf in self.fallback_versionedfiles:
                try:
                    return vf.get_ancestry(version_id, topo_sorted=False)
                except errors.RevisionNotPresent:
                    continue
            else:
                raise errors.RevisionNotPresent(version_id, self._file_id)
        ancestry = set([version_id])
        for parent in parents:
            ancestry.update(self.get_ancestry(parent, topo_sorted=False))
        return ancestry

    def get_parent_map(self, version_ids):
        """See VersionedFile.get_parent_map"""
        result = {}
        pending = set(version_ids)
        for key in version_ids:
            try:
                result[key] = self._parents[key]
            except KeyError:
                pass
        pending = pending - set(result.keys())
        for versionedfile in self.fallback_versionedfiles:
            parents = versionedfile.get_parent_map(pending)
            result.update(parents)
            pending = pending - set(parents.keys())
            if not pending:
                return result
        return result

    def _get_graph(self):
        from bzrlib.graph import (
            DictParentsProvider,
            Graph,
            _StackedParentsProvider,
            )
        from bzrlib.repofmt.knitrepo import _KnitParentsProvider
        parent_providers = [DictParentsProvider(self._parents)]
        for vf in self.fallback_versionedfiles:
            parent_providers.append(_KnitParentsProvider(vf))
        return Graph(_StackedParentsProvider(parent_providers))


class PlanWeaveMerge(TextMerge):
    """Weave merge that takes a plan as its input.
    
    This exists so that VersionedFile.plan_merge is implementable.
    Most callers will want to use WeaveMerge instead.
    """

    def __init__(self, plan, a_marker=TextMerge.A_MARKER,
                 b_marker=TextMerge.B_MARKER):
        TextMerge.__init__(self, a_marker, b_marker)
        self.plan = plan

    def _merge_struct(self):
        lines_a = []
        lines_b = []
        ch_a = ch_b = False

        def outstanding_struct():
            if not lines_a and not lines_b:
                return
            elif ch_a and not ch_b:
                # one-sided change:
                yield(lines_a,)
            elif ch_b and not ch_a:
                yield (lines_b,)
            elif lines_a == lines_b:
                yield(lines_a,)
            else:
                yield (lines_a, lines_b)
       
        # We previously considered either 'unchanged' or 'killed-both' lines
        # to be possible places to resynchronize.  However, assuming agreement
        # on killed-both lines may be too aggressive. -- mbp 20060324
        for state, line in self.plan:
            if state == 'unchanged':
                # resync and flush queued conflicts changes if any
                for struct in outstanding_struct():
                    yield struct
                lines_a = []
                lines_b = []
                ch_a = ch_b = False
                
            if state == 'unchanged':
                if line:
                    yield ([line],)
            elif state == 'killed-a':
                ch_a = True
                lines_b.append(line)
            elif state == 'killed-b':
                ch_b = True
                lines_a.append(line)
            elif state == 'new-a':
                ch_a = True
                lines_a.append(line)
            elif state == 'new-b':
                ch_b = True
                lines_b.append(line)
            elif state == 'conflicted-a':
                ch_b = ch_a = True
                lines_a.append(line)
            elif state == 'conflicted-b':
                ch_b = ch_a = True
                lines_b.append(line)
            else:
                if state not in ('irrelevant', 'ghost-a', 'ghost-b',
                        'killed-base', 'killed-both'):
                    raise AssertionError(state)
        for struct in outstanding_struct():
            yield struct


class WeaveMerge(PlanWeaveMerge):
    """Weave merge that takes a VersionedFile and two versions as its input."""

    def __init__(self, versionedfile, ver_a, ver_b, 
        a_marker=PlanWeaveMerge.A_MARKER, b_marker=PlanWeaveMerge.B_MARKER):
        plan = versionedfile.plan_merge(ver_a, ver_b)
        PlanWeaveMerge.__init__(self, plan, a_marker, b_marker)


class InterVersionedFile(InterObject):
    """This class represents operations taking place between two VersionedFiles.

    Its instances have methods like join, and contain
    references to the source and target versionedfiles these operations can be 
    carried out on.

    Often we will provide convenience methods on 'versionedfile' which carry out
    operations with another versionedfile - they will always forward to
    InterVersionedFile.get(other).method_name(parameters).
    """

    _optimisers = []
    """The available optimised InterVersionedFile types."""

    def join(self, pb=None, msg=None, version_ids=None, ignore_missing=False):
        """Integrate versions from self.source into self.target.

        If version_ids is None all versions from source should be
        incorporated into this versioned file.

        Must raise RevisionNotPresent if any of the specified versions
        are not present in the other file's history unless ignore_missing is 
        supplied in which case they are silently skipped.
        """
        target = self.target
        version_ids = self._get_source_version_ids(version_ids, ignore_missing)
        graph = Graph(self.source)
        search = graph._make_breadth_first_searcher(version_ids)
        transitive_ids = set()
        map(transitive_ids.update, list(search))
        parent_map = self.source.get_parent_map(transitive_ids)
        order = tsort.topo_sort(parent_map.items())
        pb = ui.ui_factory.nested_progress_bar()
        parent_texts = {}
        try:
            # TODO for incremental cross-format work:
            # make a versioned file with the following content:
            # all revisions we have been asked to join
            # all their ancestors that are *not* in target already.
            # the immediate parents of the above two sets, with 
            # empty parent lists - these versions are in target already
            # and the incorrect version data will be ignored.
            # TODO: for all ancestors that are present in target already,
            # check them for consistent data, this requires moving sha1 from
            # 
            # TODO: remove parent texts when they are not relevant any more for 
            # memory pressure reduction. RBC 20060313
            # pb.update('Converting versioned data', 0, len(order))
            total = len(order)
            for index, version in enumerate(order):
                pb.update('Converting versioned data', index, total)
                if version in target:
                    continue
                _, _, parent_text = target.add_lines(version,
                                               parent_map[version],
                                               self.source.get_lines(version),
                                               parent_texts=parent_texts)
                parent_texts[version] = parent_text
            return total
        finally:
            pb.finished()

    def _get_source_version_ids(self, version_ids, ignore_missing):
        """Determine the version ids to be used from self.source.

        :param version_ids: The caller-supplied version ids to check. (None 
                            for all). If None is in version_ids, it is stripped.
        :param ignore_missing: if True, remove missing ids from the version 
                               list. If False, raise RevisionNotPresent on
                               a missing version id.
        :return: A set of version ids.
        """
        if version_ids is None:
            # None cannot be in source.versions
            return set(self.source.versions())
        else:
            if ignore_missing:
                return set(self.source.versions()).intersection(set(version_ids))
            else:
                new_version_ids = set()
                for version in version_ids:
                    if version is None:
                        continue
                    if not self.source.has_version(version):
                        raise errors.RevisionNotPresent(version, str(self.source))
                    else:
                        new_version_ids.add(version)
                return new_version_ids
