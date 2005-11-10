# Copyright (C) 2005 by Canonical Development Ltd

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

"""
A store that keeps the full text of every version.

This store keeps uncompressed versions of the full text. It does not
do any sort of delta compression.
"""

import bzrlib.store
from bzrlib.trace import mutter
from bzrlib.errors import BzrError

import gzip
from cStringIO import StringIO


class TextStore(bzrlib.store.TransportStore):
    """Store that holds files indexed by unique names.

    Files can be added, but not modified once they are in.  Typically
    the hash is used as the name, or something else known to be unique,
    such as a UUID.

    Files are stored uncompressed, with no delta compression.
    """

    def _add_compressed(self, fn, f):
        from cStringIO import StringIO
        from bzrlib.osutils import pumpfile
        
        if isinstance(f, basestring):
            f = StringIO(f)
            
        sio = StringIO()
        gf = gzip.GzipFile(mode='wb', fileobj=sio)
        # if pumpfile handles files that don't fit in ram,
        # so will this function
        pumpfile(f, gf)
        gf.close()
        sio.seek(0)
        self._transport.put(fn, sio)

    def _add(self, fn, f):
        if self._compressed:
            self._add_compressed(fn, f)
        else:
            self._transport.put(fn, f)

    def _get(self, fn):
        if fn.endswith('.gz'):
            return self._get_compressed(fn)
        else:
            return self._transport.get(fn)

    def _copy_one(self, fileid, suffix, other, pb):
        # TODO: Once the copy_to interface is improved to allow a source
        #       and destination targets, then we can always do the copy
        #       as long as other is a TextStore
        if not (isinstance(other, TextStore)
            and other._prefixed == self._prefixed):
            return super(TextStore, self)._copy_one(fileid, suffix, other, pb)
        path = other._get_name(fileid, suffix)
        result = other._transport.copy_to([path], self._transport, pb=pb)
        if result != 1:
            raise BzrError('Unable to copy file: %r' % (path,))
        assert result == 1      # or what???

    def _get_compressed(self, filename):
        """Returns a file reading from a particular entry."""
        f = self._transport.get(filename)
        # gzip.GzipFile.read() requires a tell() function
        # but some transports return objects that cannot seek
        # so buffer them in a StringIO instead
        if hasattr(f, 'tell'):
            return gzip.GzipFile(mode='rb', fileobj=f)
        else:
            from cStringIO import StringIO
            sio = StringIO(f.read())
            return gzip.GzipFile(mode='rb', fileobj=sio)


def ScratchTextStore():
    return TextStore(ScratchTransport())
