import sys
import os
import re
import json
from os import path
import zipfile
import zipimport
import shutil
from textwrap import dedent
from threading import Lock

try:
    str_cls = unicode
except (NameError):
    str_cls = str

import sublime

from .sys_path import st_dir
from .console_write import console_write
from .package_disabler import PackageDisabler
from .settings import pc_settings_filename, load_list_setting, save_list_setting


loader_lock = Lock()
# This variable should only be touched while loader_lock is acquired.
non_local = {
    'swap_queued': False,
    'loaders': None
}


packages_dir = path.join(st_dir, u'Packages')
installed_packages_dir = path.join(st_dir, u'Installed Packages')


loader_package_name = u'0_package_control_loader'
if sys.version_info < (3,):
    loader_package_path = path.join(packages_dir, loader_package_name)
else:
    loader_package_path = path.join(installed_packages_dir, u'%s.sublime-package' % loader_package_name)

# With the zipfile module there is no way to delete a file from a zip, so we
# must instead copy the other files to a new zipfile and swap the filenames.
# These files are used in that process.
new_loader_package_path = loader_package_path + u'-new'
intermediate_loader_package_path = loader_package_path + u'-intermediate'


def __update_loaders(z):
    """
    Updates the cached list of loaders from a zipfile. The loader_lock MUST
    be held when calling this function.

    :param z:
        The zipfile.ZipFile object to list the files in
    """

    non_local['loaders'] = []
    for filename in z.namelist():
        if not isinstance(filename, str_cls):
            filename = filename.decode('utf-8')
        non_local['loaders'].append(filename)


def is_swapping():
    """
    If the loader is currently being swapped

    :return:
        Boolean
    """

    loader_lock.acquire()
    queued = non_local['swap_queued']
    loader_lock.release()
    return queued


def exists(name):
    """
    If a loader for the specified dependency is installed

    :param name:
        The dependency to check for a loader for
    """

    if not path.exists(loader_package_path):
        return False

    loader_filename_regex = u'^\\d\\d-%s.pyc?$' % re.escape(name)

    if sys.version_info < (3,):
        for filename in os.listdir(loader_package_path):
            if re.match(loader_filename_regex, filename):
                return True
        return False

    # We acquire a lock so that multiple removals don't stomp on each other
    loader_lock.acquire()
    found = False

    try:
        # We cache the list of loaders for performance
        if non_local['loaders'] is None:
            # This means we have a new loader waiting to be installed, so we want
            # the source loader zip to be that new one instead of the original
            if os.path.exists(new_loader_package_path):
                loader_path_to_check = new_loader_package_path
            else:
                loader_path_to_check = loader_package_path

            with zipfile.ZipFile(loader_path_to_check, 'r') as z:
                __update_loaders(z)

        for filename in non_local['loaders']:
            if re.match(loader_filename_regex, filename):
                found = True
                break
    finally:
        loader_lock.release()

    return found


def add(priority, name, code=None):
    """
    Adds a dependency to the loader

    :param priority:
        A two-digit string. If a dep has no dependencies, this can be
        something like '01'. If it does, use something like '10' leaving
        space for other entries

    :param name:
        The name of the dependency as a unicode string

    :param code:
        Any special loader code, otherwise the default will be used
    """

    if not code:
        code = """
            from package_control import sys_path
            sys_path.add_dependency(%s)
        """ % repr(name)
        code = dedent(code).lstrip()

    loader_filename = '%s-%s.py' % (priority, name)

    just_created_loader = False

    loader_metadata = {
        "version": "1.0.0",
        "sublime_text": "*",
        # Tie the loader to the platform so we can detect
        # people syncing packages incorrectly.
        "platforms": [sublime.platform()],
        "url": "https://github.com/wbond/package_control/issues",
        "description": "Package Control dependency loader"
    }
    loader_metadata_enc = json.dumps(loader_metadata).encode('utf-8')

    if sys.version_info < (3,):
        if not path.exists(loader_package_path):
            just_created_loader = True
            os.mkdir(loader_package_path, 0o755)
            with open(path.join(loader_package_path, 'dependency-metadata.json'), 'wb') as f:
                f.write(loader_metadata_enc)

        loader_path = path.join(loader_package_path, loader_filename)
        with open(loader_path, 'wb') as f:
            f.write(code.encode('utf-8'))

    else:
        # Make sure Python doesn't use the old file listing for the loader
        # when trying to import modules
        if loader_package_path in zipimport._zip_directory_cache:
            del zipimport._zip_directory_cache[loader_package_path]

        try:
            loader_lock.acquire()

            # If a swap of the loader .sublime-package was queued because of a
            # file being removed, we need to add the new loader code the the
            # .sublime-package that will be swapped into place shortly.
            if not non_local['swap_queued']:
                package_to_update = loader_package_path
            else:
                package_to_update = new_loader_package_path

            mode = 'a' if os.path.exists(package_to_update) else 'w'
            with zipfile.ZipFile(package_to_update, mode) as z:
                if mode == 'w':
                    just_created_loader = True
                    z.writestr('dependency-metadata.json', loader_metadata_enc)
                z.writestr(loader_filename, code.encode('utf-8'))
                __update_loaders(z)

        finally:
            loader_lock.release()

        if not just_created_loader and not non_local['swap_queued']:
            # Manually execute the loader code because Sublime Text does not
            # detect changes to the zip archive, only if the file is new.
            importer = zipimport.zipimporter(loader_package_path)
            importer.load_module(loader_filename[0:-3])

    # Clean things up for people who were tracking the master branch
    if just_created_loader:
        old_loader_sp = path.join(installed_packages_dir, '0-package_control_loader.sublime-package')
        old_loader_dir = path.join(packages_dir, '0-package_control_loader')

        removed_old_loader = False

        if path.exists(old_loader_sp):
            removed_old_loader = True
            os.remove(old_loader_sp)

        if path.exists(old_loader_dir):
            removed_old_loader = True
            try:
                shutil.rmtree(old_loader_dir)
            except (OSError):
                open(os.path.join(old_loader_dir, 'package-control.cleanup'), 'w').close()

        if removed_old_loader:
            console_write(u'Cleaning up remenants of old loaders', True)

            pc_settings = sublime.load_settings(pc_settings_filename())
            orig_installed_packages = load_list_setting(pc_settings, 'installed_packages')
            installed_packages = list(orig_installed_packages)

            if '0-package_control_loader' in installed_packages:
                installed_packages.remove('0-package_control_loader')

            for name in ['bz2', 'ssl-linux', 'ssl-windows']:
                dep_dir = path.join(packages_dir, name)
                if path.exists(dep_dir):
                    try:
                        shutil.rmtree(dep_dir)
                    except (OSError):
                        open(os.path.join(dep_dir, 'package-control.cleanup'), 'w').close()
                if name in installed_packages:
                    installed_packages.remove(name)

            save_list_setting(pc_settings, pc_settings_filename(),
                'installed_packages', installed_packages, orig_installed_packages)


def remove(name):
    """
    Removes a loader by name

    :param name:
        The name of the dependency
    """

    if not path.exists(loader_package_path):
        return

    loader_filename_regex = u'^\\d\\d-%s.pyc?$' % re.escape(name)

    if sys.version_info < (3,):
        for filename in os.listdir(loader_package_path):
            if re.match(loader_filename_regex, filename):
                os.remove(path.join(loader_package_path, filename))
        return

    removed = False

    # We acquire a lock so that multiple removals don't stomp on each other
    loader_lock.acquire()

    try:
        # This means we have a new loader waiting to be installed, so we want
        # the source loader zip to be that new one instead of the original
        if os.path.exists(new_loader_package_path):
            if os.path.exists(intermediate_loader_package_path):
                os.remove(intermediate_loader_package_path)
            os.rename(new_loader_package_path, intermediate_loader_package_path)
            old_loader_z = zipfile.ZipFile(intermediate_loader_package_path, 'r')

        # Under normal circumstances the source loader zip should be the
        # loader_package_path
        else:
            old_loader_z = zipfile.ZipFile(loader_package_path, 'r')

        new_loader_z = zipfile.ZipFile(new_loader_package_path, 'w')
        for enc_filename in old_loader_z.namelist():
            if not isinstance(enc_filename, str_cls):
                filename = enc_filename.decode('utf-8')
            else:
                filename = enc_filename
            if re.match(loader_filename_regex, filename):
                removed = True
                continue
            new_loader_z.writestr(enc_filename, old_loader_z.read(enc_filename))

        __update_loaders(new_loader_z)

    finally:
        old_loader_z.close()
        new_loader_z.close()
        if os.path.exists(intermediate_loader_package_path):
            os.remove(intermediate_loader_package_path)

    # If we did not remove any files and there isn't already a swap queued, that
    # means that nothing in the zip changed, so we do not need to disable the
    # loader package and then re-enable it
    if not removed and not non_local['swap_queued']:
        os.remove(new_loader_package_path)
        loader_lock.release()
        return

    disabler = PackageDisabler()
    disabler.disable_packages(loader_package_name, 'loader')

    # Note: If we "manually" loaded the dependency loader before it will not
    # be unloaded automatically when the package is disabled. Since it is
    # highly doubtful that anyone would define `plugin_unloaded` in his
    # `loader.py`, we don't necessarily have to implement it, but this is just
    # a note.

    # It is possible multiple dependencies will be removed in quick succession,
    # however we pause to let the loader file system lock to be released on
    # Windows by Sublime Text. The non_local['swap_queued'] variable makes sure
    # we don't have multiple timeouts set to replace the loader zip with the
    # new version, thus hitting a race condition where files are overwritten
    # and rename operations fail because the source file doesn't exist.
    if not non_local['swap_queued']:
        def do_swap():
            loader_lock.acquire()

            os.remove(loader_package_path)
            os.rename(new_loader_package_path, loader_package_path)

            def do_reenable():
                disabler.reenable_package(loader_package_name, 'loader')
                non_local['swap_queued'] = False
                loader_lock.release()
            sublime.set_timeout(do_reenable, 10)

        sublime.set_timeout(do_swap, 700)
        non_local['swap_queued'] = True

    loader_lock.release()
