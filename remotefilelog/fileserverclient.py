# fileserverclient.py - client for communicating with the cache process
#
# Copyright 2013 Facebook, Inc.
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2 or any later version.

from mercurial.i18n import _
from mercurial import util, sshpeer, hg, error
import os, socket, lz4, time, grp

# Statistics for debugging
fetchcost = 0
fetches = 0
fetched = 0
fetchmisses = 0

_downloading = _('downloading')

client = None

def makedirs(root, path, owner):
    os.makedirs(path)

    while path != root:
        stat = os.stat(path)
        if stat.st_uid == owner:
            os.chmod(path, 0o2775)
        path = os.path.dirname(path)

def getcachekey(reponame, file, id):
    pathhash = util.sha1(file).hexdigest()
    return os.path.join(reponame, pathhash[:2], pathhash[2:], id)

def getlocalkey(file, id):
    pathhash = util.sha1(file).hexdigest()
    return os.path.join(pathhash, id)

class fileserverclient(object):
    """A client for requesting files from the remote file server.
    """
    def __init__(self, ui):
        self.ui = ui
        self.cachepath = ui.config("remotefilelog", "cachepath")
        self.cacheprocess = ui.config("remotefilelog", "cacheprocess")
        self.debugoutput = ui.configbool("remotefilelog", "debug")

        self.pipeo = self.pipei = self.pipee = None

        if not os.path.exists(self.cachepath):
            oldumask = os.umask(0o002)
            try:
                os.makedirs(self.cachepath)

                groupname = ui.config("remotefilelog", "cachegroup")
                if groupname:
                    gid = grp.getgrnam(groupname).gr_gid
                    if gid:
                        os.chown(self.cachepath, os.getuid(), gid)
                        os.chmod(self.cachepath, 0o2775)
            finally:
                os.umask(oldumask)

    def request(self, repo, fileids):
        """Takes a list of filename/node pairs and fetches them from the
        server. Files are stored in the self.cachepath.
        A list of nodes that the server couldn't find is returned.
        If the connection fails, an exception is raised.
        """
        if not self.pipeo:
            self.connect()

        count = len(fileids)
        request = "get\n%d\n" % count
        idmap = {}
        reponame = repo.name
        for file, id in fileids:
            fullid = getcachekey(reponame, file, id)
            request += fullid + "\n"
            idmap[fullid] = file

        self.pipei.write(request)
        self.pipei.flush()

        missing = []
        total = count
        self.ui.progress(_downloading, 0, total=count)

        fallbackrepo = repo.ui.config("remotefilelog", "fallbackrepo",
                         repo.ui.config("paths", "default"))

        missed = []
        count = 0
        while True:
            missingid = self.pipeo.readline()[:-1]
            if not missingid:
                raise error.ResponseError(_("error downloading cached file:" +
                    " connection closed early\n"))
            if missingid == "0":
                break
            if missingid.startswith("_hits_"):
                # receive progress reports
                parts = missingid.split("_")
                count += int(parts[2])
                self.ui.progress(_downloading, count, total=total)
                continue

            missed.append(missingid)

        global fetchmisses
        fetchmisses += len(missed)

        count = total - len(missed)
        self.ui.progress(_downloading, count, total=total)

        uid = os.getuid()
        oldumask = os.umask(0o002)
        try:
            # receive cache misses from master
            if missed:
                verbose = self.ui.verbose
                try:
                    # When verbose is true, sshpeer prints 'running ssh...'
                    # to stdout, which can interfere with some command
                    # outputs
                    self.ui.verbose = False

                    remote = hg.peer(self.ui, {}, fallbackrepo)
                    remote._callstream("getfiles")
                finally:
                    self.ui.verbose = verbose

                i = 0
                while i < len(missed):
                    # issue a batch of requests
                    start = i
                    end = min(len(missed), start + 10000)
                    i = end
                    for missingid in missed[start:end]:
                        # issue new request
                        versionid = missingid[-40:]
                        file = idmap[missingid]
                        sshrequest = "%s%s\n" % (versionid, file)
                        remote.pipeo.write(sshrequest)
                    remote.pipeo.flush()

                    # receive batch results
                    for j in range(start, end):
                        self.receivemissing(remote.pipei, missed[j], uid)
                        count += 1
                        self.ui.progress(_downloading, count, total=total)

                remote.cleanup()
                remote = None

                # send to memcache
                count = len(missed)
                request = "set\n%d\n%s\n" % (count, "\n".join(missed))

                self.pipei.write(request)
                self.pipei.flush()

            self.ui.progress(_downloading, None)

            # mark ourselves as a user of this cache
            repospath = os.path.join(self.cachepath, "repos")
            reposfile = open(repospath, 'a')
            reposfile.write(os.path.dirname(repo.path) + "\n")
            reposfile.close()
            stat = os.stat(repospath)
            if stat.st_uid == uid:
                os.chmod(repospath, 0o0664)
        finally:
            os.umask(oldumask)

        return missing

    def receivemissing(self, pipe, missingid, uid):
        line = pipe.readline()[:-1]
        if not line:
            raise error.ResponseError(_("error downloading file " +
                "contents: connection closed early\n"))
        size = int(line)
        data = pipe.read(size)

        idcachepath = os.path.join(self.cachepath, missingid)
        dirpath = os.path.dirname(idcachepath)
        if not os.path.exists(dirpath):
            makedirs(self.cachepath, dirpath, uid)
        f = open(idcachepath, "w")
        try:
            f.write(lz4.decompress(data))
        finally:
            f.close()

        stat = os.stat(idcachepath)
        if stat.st_uid == uid:
            os.chmod(idcachepath, 0o0664)

    def connect(self):
        if self.cacheprocess:
            cmd = "%s %s" % (self.cacheprocess, self.cachepath)
            self.pipei, self.pipeo, self.pipee, self.subprocess = \
                util.popen4(cmd)
        else:
            # If no cache process is specified, we fake one that always
            # returns cache misses.  This enables tests to run easily
            # and may eventually allow us to be a drop in replacement
            # for the largefiles extension.
            class simplepipe(object):
                def __init__(self):
                    self.data = ""
                    self.missingids = []
                def flush(self):
                    lines = self.data.split("\n")
                    if lines[0] == "get":
                        for line in lines[2:-1]:
                            self.missingids.append(line)
                        self.missingids.append('0')
                    self.data = ""
                def close(self):
                    pass
                def readline(self):
                    return self.missingids.pop(0) + "\n"
                def write(self, data):
                    self.data += data
            self.pipei = simplepipe()
            self.pipeo = self.pipei
            self.pipee = simplepipe()

            class simpleprocess(object):
                def poll(self):
                    return None
                def wait(self):
                    return
            self.subprocess = simpleprocess()

    def close(self):
        if fetches and self.debugoutput:
            self.ui.warn(("%s files fetched over %d fetches - " +
                "(%d misses, %0.2f%% hit ratio) over %0.2fs\n") % (
                    fetched,
                    fetches,
                    fetchmisses,
                    float(fetched - fetchmisses) / float(fetched) * 100.0,
                    fetchcost))

        # if the process is still open, close the pipes
        if self.pipeo and self.subprocess.poll() == None:
            self.pipei.write("exit\n")
            self.pipei.close()
            self.pipeo.close()
            self.pipee.close()
            self.subprocess.wait()
            del self.subprocess
            self.pipeo = None
            self.pipei = None
            self.pipee = None

    def prefetch(self, repo, fileids, force=False):
        """downloads the given file versions to the cache
        """
        storepath = repo.sopener.vfs.base
        reponame = repo.name
        missingids = []
        for file, id in fileids:
            # hack
            # - we don't use .hgtags
            # - workingctx produces ids with length 42,
            #   which we skip since they aren't in any cache
            if file == '.hgtags' or len(id) == 42 or not repo.shallowmatch(file):
                continue

            cachekey = getcachekey(reponame, file, id)
            localkey = getlocalkey(file, id)
            idcachepath = os.path.join(self.cachepath, cachekey)
            idlocalpath = os.path.join(storepath, 'data', localkey)
            if os.path.exists(idcachepath):
                continue
            if not force and os.path.exists(idlocalpath):
                continue

            missingids.append((file, id))

        if missingids:
            global fetches, fetched, fetchcost
            fetches += 1
            fetched += len(missingids)
            start = time.time()
            missingids = self.request(repo, missingids)
            if missingids:
                raise util.Abort(_("unable to download %d files") % len(missingids))
            fetchcost += time.time() - start
