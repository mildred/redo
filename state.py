import sys, os, errno, glob, stat, fcntl
import vars
from helpers import unlink, join, close_on_exec
from log import warn, err, debug, debug2, debug3

ALWAYS = '//ALWAYS'   # an invalid filename that is always marked as dirty
STAMP_DIR = 'dir'     # the stamp of a directory; mtime is unhelpful
STAMP_OLD = 'old'     # the .deps file is from and old redo
STAMP_MISSING = '0'   # the stamp of a nonexistent file

DEPSFILE_TAG = "redo.0"

def fix_chdir(targets):
    """Undo any chdir() done by the .do script that called us.

    When we run a .do script, we do it from the directory containing that .do
    script, which is represented by STARTDIR/PWD (ie. the redo start directory
    plus any relative path of the current script).  However, the .do script
    is allowed to do chdir() and then run various redo commands.  We need
    to be running in well-defined conditions, so we chdir() to the original
    STARTDIR/PWD and paraphrase all the command-line arguments (targets) into
    paths relative to that directory.

    Args:
      targets: almost always sys.argv[1:]; paths relative to os.getcwd().
    Returns:
      targets, but relative to the (newly changed) os.getcwd().
    """
    abs_pwd = os.path.join(vars.STARTDIR, vars.PWD)
    if os.path.samefile('.', abs_pwd):
        return targets  # nothing to change
    rel_orig_dir = os.path.relpath('.', abs_pwd)
    os.chdir(abs_pwd)
    return [os.path.join(rel_orig_dir, t) for t in targets]


def _files(target, seen):
    dir = os.path.dirname(target)
    f = File(target)
    if f.name not in seen:
        seen[f.name] = 1
        yield f
    for stamp, dep in f.deps:
        fullname = os.path.join(dir, dep)
        for i in _files(fullname, seen):
            yield i


def files():
    """Return a list of files known to redo, starting in os.getcwd()."""
    seen = {}
    for depfile in glob.glob('*.deps.redo'):
        for i in _files(depfile[:-10], seen):
            yield i

# FIXME: I really want to use fcntl F_SETLK, F_SETLKW, etc here.  But python
# doesn't do the lockdata structure in a portable way, so we have to use
# fcntl.lockf() instead.  Usually this is just a wrapper for fcntl, so it's
# ok, but it doesn't have F_GETLK, so we can't report which pid owns the lock.
# The makes debugging a bit harder.  When we someday port to C, we can do that.
class LockHelper:
    def __init__(self, lock, kind):
        self.lock = lock
        self.kind = kind

    def __enter__(self):
        self.oldkind = self.lock.owned
        if self.kind != self.oldkind:
            self.lock.waitlock(self.kind)

    def __exit__(self, type, value, traceback):
        if self.kind == self.oldkind:
            pass
        elif self.oldkind:
            self.lock.waitlock(self.oldkind)
        else:
            self.lock.unlock()

LOCK_EX = fcntl.LOCK_EX
LOCK_SH = fcntl.LOCK_SH

class Lock:
    def __init__(self, name=None, f=None):
        self.owned = False
        self.name  = name
        self.close_on_del = False
        self.lockfile = f
        self.shared = fcntl.LOCK_SH
        self.exclusive = fcntl.LOCK_EX
        self._open_lock()

    def __del__(self):
        if self.owned:
            self.unlock()
        if self.close_on_del:
            os.close(self.lockfile)

    def _open_lock(self):
        if not self.lockfile:
            try: os.makedirs(os.path.dirname(self.name))
            except: pass
            self.lockfile = os.open(self.name, os.O_RDWR | os.O_CREAT, 0666)
            self.close_on_del = True
            close_on_exec(self.lockfile, True)

    def read(self):
        return LockHelper(self, fcntl.LOCK_SH)

    def write(self):
        return LockHelper(self, fcntl.LOCK_EX)

    def trylock(self, kind=fcntl.LOCK_EX):
        assert(self.owned != kind)
        try:
            self._open_lock()
            fcntl.lockf(self.lockfile, kind|fcntl.LOCK_NB, 0, 0)
        except IOError, e:
            if e.errno in (errno.EAGAIN, errno.EACCES):
                if vars.DEBUG_LOCKS: debug("%s lock failed\n", self.name)
                pass  # someone else has it locked
            else:
                raise
        else:
            if vars.DEBUG_LOCKS: debug("%s lock (try)\n", self.name)
            self.owned = kind

    def waitlock(self, kind=fcntl.LOCK_EX):
        assert(self.owned != kind)
        if vars.DEBUG_LOCKS: debug("%s lock (wait)\n", self.name)
        self._open_lock()
        fcntl.lockf(self.lockfile, kind, 0, 0)
        self.owned = kind

    def unlock(self):
        if not self.owned:
            raise Exception("can't unlock %r - we don't own it" % self.name)
        self._open_lock()
        fcntl.lockf(self.lockfile, fcntl.LOCK_UN, 0, 0)
        if vars.DEBUG_LOCKS: debug("%s unlock\n", self.name)
        self.owned = False

class File(object):
    def __init__(self, name, context=None):
        if name != ALWAYS and context:
            name = os.path.join(context, name)
        if name != ALWAYS and name.startswith('/'):
            name = os.path.relpath(name, os.getcwd())
        self.name = name
        self.dir = os.path.split(self.name)[0]
        if name != ALWAYS:
            self.redo_dir = self._get_redodir(name)
        self._dolock = None
        self.refresh()
        assert(isinstance(self.stamp, Stamp))

    def __repr__(self):
        return 'state.File(%s)' % self.name

    def _get_redodir(self, name):
        d = os.path.dirname(name)
        return os.path.join(d, ".redo")

    def dolock(self):
        if self._dolock == None:
            try:
                self._dolock = Lock(self.tmpfilename("do.lock"))
            except:
                self._dolock = False
        return self._dolock

    def check_deadlocks(self, check_with=None):
        #if check_with:
        #    debug("%s: check deadlock with %r\n",
        #          self.printable_name(), check_with.printable_name())
        #else:
        #    debug("%s: check deadlock\n", self.printable_name())
        if not check_with:
            parent = File(vars.TARGET)
            return parent.check_deadlocks(check_with = self)
        elif self == check_with:
            return True
        else:
            try:
                with open(self.tmpfilename('parent'), "r") as f:
                    parent = f.read()
                    parent = File(parent, self.dir)
            except IOError:
                return False
            else:
                return parent.check_deadlocks(check_with = check_with)

    def tmpfilename(self, filetype):
        return '%s.%s' % (os.path.join(self.redo_dir, self.basename()), filetype)

    def basename(self):
        return os.path.basename(self.name)

    def dirname(self):
        return os.path.dirname(self.name)

    def printable_name(self):
        """Return the name relative to vars.STARTDIR, normalized.

        "normalized" means we use os.path.normpath(), but only if that doesn't
        change the meaning of the filename.  (If there are symlinks,
        simplifying a/b/../c into a/c might not be correct.)

        The result is suitable for printing in the output, where all filenames
        will be relative to the user's starting directory, regardless of
        which .do file we're in or the getcwd() of the moment.
        """
        base = os.path.join(vars.PWD, self.name)
        base_full_dir = os.path.dirname(os.path.join(vars.STARTDIR, base))
        norm = os.path.normpath(base)
        norm_full_dir = os.path.dirname(os.path.join(vars.STARTDIR, norm))
        try:
            if os.path.samefile(base_full_dir, norm_full_dir):
                return norm
        except OSError:
            pass
        return base

    def refresh(self):
        if self.name == ALWAYS:
            self.stamp_mtime = str(vars.RUNID)
            self.exitcode = 0
            self.deps = []
            self.is_generated = True
            self.stamp = Stamp(str(vars.RUNID))
            return
        assert(not self.name.startswith('/'))
        try:
            # read the state file
            f = open(self.tmpfilename('deps'))
        except IOError:
            try:
                # okay, check for the file itself
                st = os.stat(self.name)
            except OSError:
                # it doesn't exist at all yet
                self.stamp_mtime = 0  # no stamp file
                self.exitcode = 0
                self.deps = []
                self.stamp = Stamp(STAMP_MISSING)
                self.runid = None
                self.is_generated = True
            else:
                # it's a source file (without a .deps file)
                self.stamp_mtime = 0  # no stamp file
                self.exitcode = 0
                self.deps = []
                self.is_generated = False
                self.stamp = self.read_stamp(st=st)
                self.runid = self.stamp.runid()
        else:
            # it's a target (with a .deps file)
            st = os.fstat(f.fileno())
            lines = f.read().strip().split('\n')
            version = None
            device  = None
            inode   = None
            try:
                version = lines.pop(0)
                device, inode = [int(i) for i in lines.pop(0).split(" ")]
            except: pass
            if version != DEPSFILE_TAG or device != st.st_dev or inode != st.st_ino:
                # It is an old .deps file, consider it missing
                self.stamp_mtime = 0  # no stamp file
                self.exitcode = 0
                self.deps = []
                self.stamp = Stamp(STAMP_OLD)
                self.runid = None
                self.is_generated = True
            else:
                # Read .deps file
                self.stamp_mtime = int(st.st_mtime)
                self.exitcode = int(lines.pop(-1))
                self.is_generated = True
                self.stamp = Stamp(lines.pop(-1))
                if not self.exists() and not self.stamp.is_missing():
                    unlink(self.tmpfilename('deps'))
                    return self.refresh()
                self.runid = self.stamp.runid()
                self.deps = [line.split(' ', 1) for line in lines]
                # if the next line fails, it means that the .dep file is not
                # correctly formatted
                while self.deps and self.deps[-1][1] == '.':
                    # a line added by redo-stamp
                    self.stamp.csum = self.deps.pop(-1)[0]
                for i in range(len(self.deps)):
                    self.deps[i][0] = Stamp(auto_detect=self.deps[i][0])

    def exists(self):
        return os.path.exists(self.name)

    def exists_not_dir(self):
        return os.path.exists(self.name) and not os.path.isdir(self.name)

    def forget(self):
        """Turn a 'target' file back into a 'source' file."""
        debug3('forget(%s)\n', self.name)
        unlink(self.tmpfilename('deps'))

    def _add(self, line):
        depsname = self.tmpfilename('deps2')
        debug3('_add(%s) to %r\n', line, depsname)
        #assert os.path.exists(depsname)
        line = str(line)
        assert('\n' not in line)
        with open(depsname, 'a') as f:
            f.write(line + '\n')

    def build_starting(self, write_parent=True):
        """Call this when you're about to start building this target."""
        if vars.TARGET and write_parent:
            with open(self.tmpfilename('parent'), "w") as f:
                f.write(os.path.relpath(vars.TARGET, self.dir))
        depsname = self.tmpfilename('deps2')
        debug3('build starting: %r\n', depsname)
        unlink(depsname)
        with open(depsname, 'a') as f:
            f.write(DEPSFILE_TAG + '\n')
            st = os.fstat(f.fileno())
            f.write('%d %d\n' % (st.st_dev, st.st_ino))

    def build_done(self, exitcode, runid=vars.RUNID):
        """Call this when you're done building this target."""
        depsname = self.tmpfilename('deps2')
        debug3('build ending: %r\n', depsname)
        self._add(self.read_stamp(runid=runid).stamp)
        self._add(exitcode)
        os.utime(depsname, (runid, runid))
        os.rename(depsname, self.tmpfilename('deps'))
        unlink(self.tmpfilename('parent'))

    def add_dep(self, file):
        """Mark the given File() object as a dependency of this target.

        The filesystem file it refers to may or may not exist.  If it doesn't
        exist, creating the file is considered a "modified" event and will
        result in this target being rebuilt.
        """
        if file.name == ALWAYS:
            relname = file.name
        else:
            relname = os.path.relpath(file.name, self.dir)
        debug3('add-dep: %r < %r %r\n', self.name, file.stamp, relname)
        assert('\n' not in file.name)
        assert(isinstance(file.stamp, Stamp))
        assert(file.stamp.csum_or_stamp() is not None)
        self._add('%s %s' % (file.stamp.csum_or_stamp(), relname))

    def copy_deps_from(self, other):
        for dep in other.deps:
            assert(dep[0].csum_or_stamp() is not None)
            self._add('%s %s' % (dep[0].csum_or_stamp(), dep[1]))

    def read_stamp(self, runid=None, st=None, st_deps=None):
        # FIXME: make this formula more well-defined
        if runid == None and st_deps == None:
            try: st_deps = os.stat(self.tmpfilename('deps'))
            except OSError: st_deps = False
        if st == None:
            try: st = os.stat(self.name)
            except OSError: st = False

        if runid == None and st_deps:
            runid = int(st_deps.st_mtime)

        return Stamp(st = st, runid = runid)

    def __eq__(self, other):
        try:
            return os.path.realpath(self.name) == os.path.realpath(other.name)
        except:
            return False
    
    def __ne__(self, other):
        return not self.__eq__(other)

class Stamp:
    "either a checksum or a stamp"

    def __init__(self, stamp=None, csum=None, auto_detect=None, st=None, runid=None):
        assert(stamp == None or isinstance(stamp, str))
        assert(csum == None or isinstance(csum, str))
        self.stamp = stamp
        self.csum  = csum
        if auto_detect:
            if len(auto_detect) == 40 and auto_detect.isalnum():
                self.csum  = auto_detect
            else:
                self.stamp = auto_detect
        elif st != None:
            if st == False:
                self.stamp = STAMP_MISSING
            elif stat.S_ISDIR(st.st_mode):
                self.stamp = STAMP_DIR
            else:
                self.stamp = join('-', (st.st_ctime, st.st_mtime,
                                        st.st_size, st.st_dev, st.st_ino))
            if runid:
                self.stamp = self.stamp + '+' + str(int(runid))

    def __eq__(self, other):
        assert(False)

    def __ne__(self, other):
        assert(False)

    def is_missing(self):
        if not self.stamp:
            return False
        return self.stamp == STAMP_MISSING or self.stamp.startswith(STAMP_MISSING + '+')

    def is_old(self):
        return self.stamp == STAMP_OLD

    def is_stamp(self):
        return self.stamp != None

    def is_csum(self):
        return self.csum != None

    def is_none(self):
        return self.stamp == None and self.csum == None

    def runid(self):
        try:
            _, _, runid = self.stamp.rpartition('+')
            return int(runid)
        except: return None

    def __str__(self):
        assert(False)

    def __repr__(self):
        return "%r %r" % (self.stamp, self.csum)

    def csum_or_stamp(self):
        assert(self.csum or self.stamp)
        return self.csum or self.stamp

    def is_override_or_missing(self, f):
        """check the file is overriden by the user or if it is missing, given
        that self is a newly computed stamp (no checksum) and other is the File
        object"""
        return f.is_generated and f.stamp.stamp != self.stamp and not f.stamp.is_old()

    def is_stamp_dirty(self, f):
        "is the information in the self stamp (not csum) dirty compared to file f"
        return self.stamp != f.stamp.stamp

    def is_dirty(self, f):
        "is the information in the self stamp or csum dirty compared to file f"
        return self.csum and self.csum != f.stamp.csum or self.stamp and self.is_stamp_dirty(f)
