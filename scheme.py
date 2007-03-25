# Copyright (C) 2006 Jelmer Vernooij <jelmer@samba.org>

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

from bzrlib.errors import NotBranchError

class BranchingScheme:
    """ Divides SVN repository data up into branches. Since there
    is no proper way to do this, there are several subclasses of this class
    each of which handles a particular convention that may be in use.
    """
    def is_branch(self, path):
        """Check whether a location refers to a branch.
        
        :param path: Path to check.
        """
        raise NotImplementedError

    def unprefix(self, path):
        """Split up a Subversion path into a branch-path and inside-branch path.

        :param path: Path to split up.
        :return: Tuple with branch-path and inside-branch path.
        """
        raise NotImplementedError

    @staticmethod
    def guess_scheme(relpath):
        """Try to guess the branching scheme based on a partial URL.

        :param relpath: Relative URL to a branch.
        :return: New BranchingScheme instance.
        """
        parts = relpath.strip("/").split("/")
        for i in range(0, len(parts)):
            if parts[i] in ("trunk", "branches", "tags"):
                return TrunkBranchingScheme(level=i)

        return NoBranchingScheme()

    @staticmethod
    def find_scheme(name):
        if name == "trunk":
            return TrunkBranchingScheme()

        if name.startswith("trunk-"):
            try:
                return TrunkBranchingScheme(level=int(name[len("trunk-"):]))
            except ValueError:
                return None

        if name == "none":
            return NoBranchingScheme()

        return None

    def is_branch_parent(self, path):
        raise NotImplementedError
                

class TrunkBranchingScheme(BranchingScheme):
    """Standard Subversion repository layout. Each project contains three 
    directories `trunk', `tags' and `branches'. 
    """
    def __init__(self, level=0):
        self.level = level

    def is_branch(self, path):
        """See BranchingScheme.is_branch()."""
        parts = path.strip("/").split("/")
        if len(parts) == self.level+1 and parts[self.level] == "trunk":
            return True

        if len(parts) == self.level+2 and \
           (parts[self.level] == "branches" or parts[self.level] == "tags"):
            return True

        return False

    def unprefix(self, path):
        """See BranchingScheme.unprefix()."""
        parts = path.strip("/").split("/")
        if len(parts) == 0 or self.level >= len(parts):
            raise NotBranchError(path=path)

        if parts[self.level] == "trunk" or parts[self.level] == "hooks":
            return ("/".join(parts[0:self.level+1]).strip("/"), 
                    "/".join(parts[self.level+1:]).strip("/"))
        elif ((parts[self.level] == "tags" or parts[self.level] == "branches") and 
              len(parts) >= self.level+2):
            return ("/".join(parts[0:self.level+2]).strip("/"), 
                    "/".join(parts[self.level+2:]).strip("/"))
        else:
            raise NotBranchError(path=path)

    def __str__(self):
        return "trunk%d" % self.level

    def is_branch_parent(self, path):
        parts = path.strip("/").split("/")
        if len(parts) <= self.level:
            return True
        return self.is_branch(path+"/trunk")

class NoBranchingScheme(BranchingScheme):
    """Describes a scheme where there is just one branch, the 
    root of the repository."""
    def is_branch(self, path):
        """See BranchingScheme.is_branch()."""
        return path.strip("/") == ""

    def unprefix(self, path):
        """See BranchingScheme.unprefix()."""
        return ("", path.strip("/"))

    def __str__(self):
        return "null"

    def is_branch_parent(self, path):
        return False


class ListBranchingScheme(BranchingScheme):
    def __init__(self, branch_list):
        """Create new ListBranchingScheme instance.

        :param branch_list: List of know branch locations.
        """
        self.branch_list = []
        for p in branch_list:
            self.branch_list.append(p.strip("/"))

    def is_branch(self, path):
        """See BranchingScheme.is_branch()."""
        return path.strip("/") in self.branch_list

    def unprefix(self, path):
        """See BranchingScheme.unprefix()."""
        path = path.strip("/")
        for i in self.branch_list:
            if (path+"/").startswith(i+"/"):
                return (i, path[len(i):].strip("/"))

        raise NotBranchError(path=path)

