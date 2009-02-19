#    config.py -- Configuration of bzr-builddeb from files
#    Copyright (C) 2006 James Westby <jw+debian@jameswestby.net>
#    
#    This file is part of bzr-builddeb.
#
#    bzr-builddeb is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 2 of the License, or
#    (at your option) any later version.
#
#    bzr-builddeb is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with bzr-builddeb; if not, write to the Free Software
#    Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA
#

from bzrlib.config import ConfigObj, TreeConfig
from bzrlib.trace import mutter


class SvnBuildPackageMappedConfig(object):
  """Config object that provides a bzr-builddeb configuration 
  based on a svn-buildpackage configuration.
  """

  def __init__(self, bp_config):
    self.bp_config = bp_config

  def get_option(self, option, section=None):
    """Retrieve the contents of an option, mapped from the equivalent 
    svn-buildpackage option."""
    if section == "BUILDDEB":
      if option == "merge":
        return self.bp_config.get_merge_with_upstream()
      elif option == "orig-dir":
        return self.bp_config.get("origDir")
      elif option == "build-dir":
        return self.bp_config.get("buildArea")
    return None


class DebBuildConfig(object):
  """Holds the configuration settings for builddeb. These are taken from
  a hierarchy of config files. .bzr-builddeb/local.conf then 
  ~/.bazaar/builddeb.conf, finally .bzr-builddeb/default.conf. The value is 
  taken from the first file in which it is specified."""

  section = 'BUILDDEB'

  def __init__(self, files, branch=None, tree=None, version=None):
    """ 
    Creates a config to read from config files in a hierarchy.

    Pass it a list of tuples (file, secure) where file is the location of
    a config file (that doesn't have to exist, and trusted is True or false,
    and states whether the file can be trusted for sensitive values.

    The value will be returned from the first in the list that has it,
    unless that key is marked as needing a trusted file and the file isn't
    trusted.

    If branch is not None then it will be used in preference to all others.
    It will not be considered trusted.

    The sample files used in this test are included in the builddeb source tree.

    >>> import os
    >>> import bzrlib.plugins.builddeb
    >>> d = os.path.dirname(bzrlib.plugins.builddeb.__file__) + '/'
    >>> c = DebBuildConfig([
    ...      (d + 'local.conf', False),
    ...      (d + 'user.conf', True), 
    ...      (d + 'default.conf', False)])
    >>> print c.orig_dir
    None
    >>> print c.merge
    True
    >>> print c.build_dir
    defaultbuild
    >>> print c.result_dir
    userresult
    >>> print c.builder
    userbuild
    """
    self._config_files = []
    self.version = version
    for input in files:
      config = ConfigObj(input[0])
      self._config_files.append((config, input[1]))
    if branch is not None:
      self._branch_config = TreeConfig(branch)
    else:
      self._branch_config = None
    self._tree_config = None
    if tree is not None:
      try:
        # Imported here, since not everybody will have bzr-svn installed
        from bzrlib.plugins.svn.config import SubversionBuildPackageConfig, NoSubversionBuildPackageConfig
        try:
          self._tree_config = SvnBuildPackageMappedConfig(SubversionBuildPackageConfig(tree))
        except NoSubversionBuildPackageConfig:
          pass # Not a svn tree
      except ImportError:
        pass # No svn, apparently
    self.user_config = None

  def set_user_config(self, user_conf):
    if user_conf is not None:
      self.user_config = ConfigObj(user_conf)

  def _user_config_value(self, key):
    if self.user_config is not None:
      try:
        return self.user_config.get_value(self.section, key)
      except KeyError:
        pass
    return None

  def set_version(self, version):
    """Set the version used for substitution."""
    self.version = version

  def _get_opt(self, config, key, section=None):
    """Returns the value for key from config, of None if it is not defined in 
    the file"""
    if section is None:
      section = self.section
    try:
      return config.get_value(section, key)
    except KeyError:
      return None

  def _get_best_opt(self, key, trusted=False, section=None):
    """Returns the value for key, obeying precedence.
    
    Returns the value for the key from the first file in which it is defined,
    or None if none of the files define it.
    
    If trusted is True then the the value will only be taken from a file
    marked as trusted.
    
    """
    if section is None:
      section = self.section
    if not trusted:
      if self._branch_config is not None:
        value = self._branch_config.get_option(key, section=self.section)
        if value is not None:
          mutter("Using %s for %s, taken from the branch", value, key)
          return value
      if self._tree_config is not None:
        value = self._tree_config.get_option(key, section=self.section)
        if value is not None:
          mutter("Using %s for %s, taken from the tree", value, key)
          return value
    for config_file in self._config_files:
      if not trusted or config_file[1]:
        value = self._get_opt(config_file[0], key, section=section)
        if value is not None:
          mutter("Using %s for %s, taken from %s", value, key,
                 config_file[0].filename)
          return value
    return None

  def get_hook(self, hook_name):
    return self._get_best_opt(hook_name, section='HOOKS')

  def _get_bool(self, config, key):
    try:
      return True, config.get_bool('BUILDDEB', key)
    except KeyError:
      return False, False

  def _get_best_bool(self, key, trusted=False, default=False):
    """Returns the value of key, obeying precedence.

    Returns the value for the key from the first file in which it is defined,
    or default if none of the files define it.
    
    If trusted is True then the the value will only be taken from a file
    marked as trusted.
    
    """
    if not trusted:
      if self._branch_config is not None:
        value = self._branch_config.get_option(key, section=self.section)
        if value is not None:
          mutter("Using %s for %s, taken from the branch", value, key)
          return value
      if self._tree_config is not None:
        value = self._tree_config.get_option(key, section=self.section)
        if value is not None:
          mutter("Using %s for %s, taken from the tree", value, key)
          return value
    for config_file in self._config_files:
      if not trusted or config_file[1]:
        (found, value) = self._get_bool(config_file[0], key)
        if found:
          mutter("Using %s for %s, taken from %s", value, key,
                 config_file[0].filename)
          return value
    return default

  def _opt_property(name, help=None, trusted=False):
    return property(lambda self: self._get_best_opt(name, trusted), None,
                    None, help)

  def _bool_property(name, help=None, trusted=False, default=False):
    return property(lambda self: self._get_best_bool(name, trusted, default),
                    None, None, help)

  build_dir = _opt_property('build-dir', "The dir to build in")

  user_build_dir = property(
          lambda self: self._user_config_value('build-dir'))

  orig_dir = _opt_property('orig-dir', "The dir to get upstream tarballs from")

  user_orig_dir = property(
          lambda self: self._user_config_value('orig-dir'))

  builder = _opt_property('builder', "The command to build with", True)

  result_dir = _opt_property('result-dir', "The dir to put the results in")

  user_result_dir = property(
          lambda self: self._user_config_value('result-dir'))

  merge = _bool_property('merge', "Run in merge mode")

  quick_builder = _opt_property('quick-builder',
                          "A quick command to build with", True)

  native = _bool_property('native', "Build a native package")

  split = _bool_property('split', "Split a full source package")

  upstream_branch = _opt_property('upstream-branch',
          "The upstream branch to merge from")

def _test():
  import doctest
  doctest.testmod()

if __name__ == '__main__':
  _test()

# vim: ts=2 sts=2 sw=2
