# Copyright (C) 2012-2013 Itay Brandes

import os
import urllib2
import copy
import threading
import time
import math
import hashlib
import logging
from urlparse import urlparse
from StringIO import StringIO
import multiprocessing.dummy as multiprocessing
from ctypes import c_int

import utils
import threadpool

__all__ = ['SmartDL', 'utils']
__version_mjaor__ = 1
__version_minor__ = 0
__version_micro__ = 0
__version__ = "%d.%d.%d" % (__version_mjaor__, __version_minor__, __version_micro__)

class HashFailedException(Exception):
    "Raised when hash check fails."
    def __init__(self, fn, calc_hash, needed_hash):
        self.filename = fn
        self.calculated_hash = calc_hash
        self.needed_hash = needed_hash
    def __str__(self):
        return 'HashFailedException(%s, got %s, expected %s)' % (self.filename, self.calculated_hash, self.needed_hash)
    def __repr__(self):
        return "<HashFailedException %s, got %s, expected %s>" % (self.filename, self.calculated_hash, self.needed_hash)
        
class CanceledException(Exception):
    "Raised when the job is canceled."
    def __init__(self):
        pass
    def __str__(self):
        return 'CanceledException'
    def __repr__(self):
        return "<CanceledException>"

class SmartDL:
    '''
    The main SmartDL class
    
    :param urls: Download url. You can also pass a list of urls, and those will be used as mirrors.
    :type urls: string or list of strings
    :param dest: Destination path. Default is `%TEMP%/pySmartDL/`.
    :type dest: string
    :param progress_bar: If True, prints a progress bar to the `stdout stream <http://docs.python.org/2/library/sys.html#sys.stdout>`_. Default is `True`.
    :type progress_bar: bool
    :param logger: An optional logger.
    :type logger: `logging.Logger` instance
    :param connect_default_logger: If true, connects a default logger to the class.
    :type connect_default_logger: bool
    :rtype: `SmartDL` instance
    
    .. NOTE::
            The provided dest may be a folder or a full path name (including filename). The algorithm works as follows:
            
            * If the path exists, and it's an existing folder, the file will be downloaded to there with the original filename.
            * If the past does not exist, it will create the folders, if needed, and refer to the last section of the path as the filename.
            * If you want to download to folder that does not exist at the moment, and want the module to fill in the filename, make sure the path ends with `os.sep`.
            * If no path is provided, `%TEMP%/pySmartDL/` will be used.
    
    '''
    
    def __init__(self, urls, dest=None, progress_bar=True, logger=None, connect_default_logger=False):
        self.mirrors = [urls] if isinstance(urls, basestring) else urls
        for i, url in enumerate(self.mirrors):
            if " " in url:
                self.mirrors[i] = utils.url_fix(url)
        self.url = self.mirrors.pop(0)
        
        fn = urlparse(self.url).path.split('/')[-1]
        self.dest = dest or os.path.join(os.environ["Temp"], 'pySmartDL', fn)
        if self.dest[-1] == os.sep:
            self.dest += fn
        if os.path.isdir(self.dest):
            self.dest = os.path.join(self.dest, fn)
        if not os.path.exists(os.path.dirname(self.dest)):
            os.makedirs(os.path.dirname(self.dest))
        self.progress_bar = progress_bar
        if logger:
            self.logger = logger
        elif connect_default_logger:
            self.logger = utils.create_debugging_logger()
        else:
            self.logger = utils.DummyLogger()
        
        self.headers = {'User-Agent': utils.get_random_useragent()}
        self.threads_count = 5
        self.timeout = 4
        self.current_attemp = 1 
        self.attemps_limit = 4
        self.minChunkFile = 1024**2 # 1MB
        self.filesize = 0
        self.shared_var = multiprocessing.Value(c_int, 0) # a ctypes var that counts the bytes already downloaded
        self.thread_shared_cmds = {}
        self.status = "ready"
        self.verify_hash = False
        self._killed = False
        self._failed = False
        self._start_func_blocking = True
        self.errors = []
        
        self.post_threadpool_thread = None
        self.control_thread = None
        
        if not utils.is_HTTPRange_supported(self.url):
            self.logger.warning("Server does not support HTTPRange. threads_count is set to 1.")
            self.threads_count = 1
        if os.path.exists(self.dest):
            self.logger.warning("Destination '%s' already exists. Existing file will be removed." % self.dest)
        if not os.path.exists(os.path.dirname(self.dest)):
            self.logger.warning("Directory '%s' does not exist. Creating it..." % os.path.dirname(self.dest))
            os.makedirs(os.path.dirname(self.dest))
            
        self.pool = threadpool.ThreadPool(self.threads_count)
        
    def __str__(self):
        return 'SmartDL(url=r"%s", dest=r"%s", progress_bar=%s)' % (self.url, self.dest, self.progress_bar)
    def __repr__(self):
        return "<SmartDL %s>" % (self.url)
        
    def add_hash_verification(self, algorithm, hash):
        '''
        Adds hash verification to the download.
        
        If hash is not correct, will try different mirrors. If all mirrors aren't
        passing hash verification, HashFailedException() Exception will be raised.
        
        .. NOTE::
            If downloaded file already exist on the destination, and hash matches, pySmartDL will not download it again.
            
        .. WARNING::
            The hashing algorithm must be supported on your system, as documented at `hashlib documentation page <http://docs.python.org/2/library/hashlib.html>`_.
        
        :param algorithm: Hashing algorithm.
        :type algorithm: string
        :param hash: Hash code.
        :type hash: string
        '''
        
        self.verify_hash = True
        self.hash_algorithm = algorithm
        self.hash_code = hash
        
    def start(self, blocking=None):
        '''
        Starts the download task. Will raise `RuntimeError` if it's the object's already downloading.
        
        .. warning::
            If you're using the non-blocking mode, Exceptions won't be raised. In that case, call
            `isSuccessful()` after the task is finished, to make sure the download succeeded. Call
            `get_errors()` to get the the exceptions.
        
        :param blocking: If true, calling this function will block the thread until the download finished. Default is *True*.
        :type blocking: bool
        '''
        if not self.status == "ready":
            raise RuntimeError("cannot start (current status is %s)" % self.status)
        
        if blocking is None:
            blocking = self._start_func_blocking
        else:
            self._start_func_blocking = blocking
            
        self.logger.debug('1 link and %d mirrors are loaded.' % len(self.mirrors))
            
        if self.verify_hash and os.path.exists(self.dest):
            with open(self.dest, 'rb') as f:
                hash = hashlib.new(self.hash_algorithm, f.read()).hexdigest()
                if hash == self.hash_code:
                    self.logger.debug("Destination '%s' already exists, and the hash matches. No need to download." % self.dest)
                    self.status = 'finished'
                    return
        
        self.logger.debug("Downloading '%s' to '%s'..." % (self.url, self.dest))
        req = urllib2.Request(self.url, headers=self.headers)
        try:
            urlObj = urllib2.urlopen(req, timeout=self.timeout)
        except (urllib2.HTTPError, urllib2.URLError), e:
            self.errors.append(e)
            if self.mirrors:
                self.logger.debug("%s. Trying next mirror..." % unicode(e))
                self.url = self.mirrors.pop(0)
                self.start(blocking)
                return
            else:
                self.logger.warning(unicode(e))
                self.errors.append(e)
                self._failed = True
                self.status = "finished"
                raise
                
        meta = urlObj.info()
        try:
            self.filesize = int(meta.getheaders("Content-Length")[0])
            self.logger.debug("Content-Length is %d (%.2fMB)." % (self.filesize, self.filesize/1024.0**2))
        except IndexError:
            self.logger.warning("Server did not send Content-Length.")
            self.filesize = 0
            
        args = calc_args(self.filesize, self.threads_count, self.minChunkFile)
        bytes_per_thread = args[0][1]-args[0][0]
        if len(args)>1:
            self.logger.debug("Launching %d threads (downloads %sKB/Thread)." % (len(args),  "{:,}".format(bytes_per_thread/1024)))
        else:
            self.logger.debug("Launching 1 thread.")
        
        self.status = "downloading"
        for i, arg in enumerate(args):
            x = [self.url, self.dest+".%.3d" % i, arg[0],
                    arg[1], copy.deepcopy(self.headers), self.timeout, self.shared_var]
                    
        t_args = [[ self.url,
                    self.dest+".%.3d" % i,
                    arg[0],
                    arg[1],
                    copy.deepcopy(self.headers),
                    self.timeout,
                    self.shared_var] for i, arg in enumerate(args)]
                    
        for i, arg in enumerate(args):
            req = threadpool.WorkRequest(download,
                                        [   self.url,
                                            self.dest+".%.3d" % i,
                                            arg[0],
                                            arg[1],
                                            copy.deepcopy(self.headers),
                                            self.timeout,
                                            self.shared_var,
                                            self.thread_shared_cmds],
                                        exc_callback=self._exc_callback)
            self.pool.putRequest(req)
        
        self.post_threadpool_thread = threading.Thread(target=post_threadpool_actions, args=(self.pool, [[(self.dest+".%.3d" % i) for i in range(len(args))], self.dest], self.filesize, self))
        self.post_threadpool_thread.daemon = True
        self.post_threadpool_thread.start()
        
        self.control_thread = ControlThread(self)
        
        if blocking:
            self.wait(raise_exceptions=True)
            
    def _exc_callback(self, req, e):
        self.errors.append(e[0])
        self.logger.exception(e[1])
        
    def retry(self, eStr=""):
        if self.current_attemp < self.attemps_limit:
            self.current_attemp += 1
            self.status = "ready"
            self.shared_var.value = 0
            self.thread_shared_cmds = {}
            self.start()
             
        else:
            s = 'The maximum retry attempts reached'
            if eStr:
                s += " (%s)" % eStr
            self.errors.append(urllib2.HTTPError(self.url, "0", s, {}, StringIO()))
            self._failed = True
            
    def try_next_mirror(self, e=None):
        if self.mirrors:
            if e:
                self.errors.append(e)
            self.status = "ready"
            self.shared_var.value = 0
            self.url = self.mirrors.pop(0)
            self.start()
        else:
            self._failed = True
            self.errors.append(e)
    
    def get_eta(self):
        '''
        Get estimated time of download completion, in seconds. Returns `0` if there is
        no enough data to calculate the estimated time (this will happen on the approx.
        first 5 seconds of each download).
        
        :rtype: int
        '''
        return self.control_thread.get_eta()
    def get_speed(self):
        '''
        Get current transfer speed in bytes per second.
        
        :rtype: int
        '''
        return self.control_thread.get_speed()
    def get_progress(self):
        '''
        Returns the current progress of the download, as a float between `0` and `1`.
        
        :rtype: float
        '''
        if not self.filesize:
            return 0
        if self.control_thread.get_dl_size() <= self.filesize:
            return 1.0*self.control_thread.get_dl_size()/self.filesize
        return 1.0
    def get_progress_bar(self):
        '''
        Returns the current progress of the download as a string containing a progress bar.
        
        .. NOTE::
            That's an alias for pySmartDL.utils.progress_bar(obj.get_progress()).
        
        :rtype: string
        '''
        return utils.progress_bar(self.get_progress())
    def isFinished(self):
        '''
        Returns if the task is finished.
        
        :rtype: bool
        '''
        if self.status == "ready":
            return False
        if self.status == "finished":
            return True
        return not self.post_threadpool_thread.is_alive()
    def isSuccessful(self):
        '''
        Returns if the download is successfull. It may fail in the following scenarios:
        
        - Hash check is enabled and fails.
        - All mirrors are down.
        - Any local I/O problems (such as `no disk space available`).
        
        .. NOTE::
            Call `get_errors()` to get the exceptions, if any.
        
        Will raise `RuntimeError` if it's called when the download task is not finished yet.
        
        :rtype: bool
        '''
        
        if self._killed:
            return False
        
        n = 0
        while self.status != 'finished':
            n += 1
            time.sleep(0.1)
            if n >= 15:
                raise RuntimeError("The download task must be finished in order to see if it's successful. (current status is %s)" % self.status)
            
        return not self._failed
        
    def get_errors(self):
        '''
        Get errors happened while downloading.
        
        :rtype: list of `Exception` instances
        '''
        return self.errors
        
    def get_status(self):
        '''
        Returns the current status of the task. Possible values: *ready*,
        *downloading*, *paused*, *combining*, *finished*.
        
        :rtype: string
        '''
        return self.status
    def wait(self, raise_exceptions=False):
        '''
        Blocks until the download is finished.
        
        :param raise_exceptions: If true, this function will raise exceptions. Default is *False*.
        :type raise_exceptions: bool
        '''
        if self.status == "finished":
            return
            
        while not self.isFinished():
            time.sleep(0.1)
        self.post_threadpool_thread.join()
        self.control_thread.join()
        
        if self._failed and raise_exceptions:
            raise self.errors[-1]
    def stop(self):
        '''
        Stops the download.
        '''
        if self.status == "downloading":
            self.thread_shared_cmds['stop'] = ""
            self._killed = True
    def pause(self):
        '''
        Pauses the download.
        '''
        if self.status == "downloading":
            self.status = "paused"
            self.thread_shared_cmds['pause'] = ""
    def unpause(self):
        '''
        Pauses the download.
        '''
        if self.status == "paused" and 'pause' in self.thread_shared_cmds:
            self.status = "downloading"
            del self.thread_shared_cmds['pause']
    # def limit_speed(self, kbytes=-1):
        # '''
        # Limits the download transfer speed.
        
        # :param kbytes: Number of Kilobytes to download per second. Negative values will not limit the speed. Default is `-1`.
        # :type kbytes: int
        # '''
        # if kbytes == 0:
            # self.pause()
        # if kbytes > 0 and self.status == "downloading":
            # self.thread_shared_cmds['limit'] = 1.0*kbytes/self.threads_count
        
    def get_dest(self):
        '''
        Get the destination path of the downloaded file. Needed when no
        destination is provided to the class, and exists on a temp folder.
        
        :rtype: string
        '''
        return self.dest
    def get_dl_time(self):
        '''
        Returns how much time did the download take, in seconds. Returns
        `-1` if the download task is not finished yet.
        
        :rtype: int
        '''
        return self.control_thread.get_dl_time()
        
    def get_dl_size(self):
        '''
        Get downloaded bytes counter in bytes.
        
        :rtype: int
        '''
        return self.control_thread.get_dl_size()
    
    def get_data(self, binary=False, bytes=-1):
        '''
        Returns the downloaded data. Will raise `RuntimeError` if it's
        called when the download task is not finished yet.
        
        :param binary: If true, will read the data as binary. Else, will read it as text.
        :type binary: bool
        :param bytes: Number of bytes to read. Negative values will read until EOF. Default is `-1`.
        :type bytes: int
        :rtype: string
        '''
        if self.status != 'finished':
            raise RuntimeError("The download task must be finished in order to read the data. (current status is %s)" % self.status)
            
        flags = 'rb' if binary else 'r'
        with open(self.get_dest(), flags) as f:
            data = f.read(bytes) if bytes>0 else f.read()
        return data
        
    def get_data_hash(self, algorithm):
        '''
        Returns the downloaded data's hash. Will raise `RuntimeError` if it's
        called when the download task is not finished yet.
        
        :param algorithm: Hashing algorithm.
        :type algorithm: bool
        :rtype: string
        
        .. WARNING::
            The hashing algorithm must be supported on your system, as documented at `hashlib documentation page <http://docs.python.org/2/library/hashlib.html>`_.
        '''
        return hashlib.new(algorithm, self.get_data(binary=True)).hexdigest()

class ControlThread(threading.Thread):
    "A class that shows information about a running SmartDL object."
    def __init__(self, obj):
        threading.Thread.__init__(self)
        self.obj = obj
        self.progress_bar = obj.progress_bar
        self.logger = obj.logger
        self.shared_var = obj.shared_var
        
        self.dl_speed = 0
        self.eta = 0
        self.lastBytesSamples = [] # list with last 50 Bytes Samples.
        self.last_calculated_totalBytes = 0
        self.calcETA_queue = []
        self.calcETA_i = 0
        self.calcETA_val = 0
        self.dl_time = -1.00
        
        self.daemon = True
        self.start()
        
    def run(self):
        t1 = time.time()
        
        while self.obj.pool.workRequests:
            self.dl_speed = self.calcDownloadSpeed(self.shared_var.value)
            if self.dl_speed > 0:
                self.eta = self.calcETA((self.obj.filesize-self.shared_var.value)/self.dl_speed)
                
            if self.progress_bar:
                if self.obj.filesize:
                    status = r"[*] %.2f / %.2f MB @ %.2fKB/s %s [%3.2f%%, %ds left]   " % (self.shared_var.value / 1024.0**2, self.obj.filesize / 1024.0**2, self.dl_speed/1024.0, utils.progress_bar(1.0*self.shared_var.value/self.obj.filesize), self.shared_var.value * 100.0 / self.obj.filesize, self.eta)
                else:
                    status = r"[*] %.2f / ??? MB @ %.2fKB/s   " % (self.shared_var.value / 1024.0**2, self.dl_speed/1024.0)
                status = status + chr(8)*(len(status)+1)
                print status,
            try:
                self.obj.pool.poll()
            except threadpool.NoResultsPending:
                break
            time.sleep(0.1)
            
        if self.obj._killed:
            self.logger.debug("File download process has been stopped.")
            return
            
        if self.progress_bar:
            if self.obj.filesize:
                print r"[*] %.2f / %.2f MB @ %.2fKB/s %s [100%%, 0s left]    " % (self.obj.filesize / 1024.0**2, self.obj.filesize / 1024.0**2, self.dl_speed/1024.0, utils.progress_bar(1.0))
            else:
                print r"[*] %.2f / %.2f MB @ %.2fKB/s    " % (self.shared_var.value / 1024.0**2, self.shared_var.value / 1024.0**2, self.dl_speed/1024.0)
                
        t2 = time.time()
        self.dl_time = float(t2-t1)
        
        # self.logger.debug("Combining files...") # actually happens on post_threadpool_thread
        # self.obj.status = "combining" # actually happens on post_threadpool_thread
        while self.obj.post_threadpool_thread.is_alive():
            time.sleep(0.1)
            
        self.obj.status = "finished"
        if not self.obj.errors:
            self.logger.debug("File downloaded within %.2f seconds." % self.dl_time)
            
    def get_eta(self):
        if self.eta <= 0 or self.obj.status == 'paused':
            return 0
        return self.eta
    def get_speed(self):
        if self.obj.status == 'paused':
            return 0
        return self.dl_speed
    def get_dl_size(self):
        if self.shared_var.value > self.obj.filesize:
            return self.obj.filesize
        return self.shared_var.value
    def get_final_filesize(self):
        return self.obj.filesize
    def get_progress(self):
        if not self.obj.filesize:
            return 0
        return 1.0*self.shared_var.value/self.obj.filesize
    def get_dl_time(self):
        return self.dl_time
        
    def calcDownloadSpeed(self, totalBytes, sampleCount=30, sampleDuration=0.1):
        '''
        Function calculates the download rate.
        @param totalBytes: The total amount of bytes.
        @param sampleCount: How much samples should the function take into consideration.
        @param sampleDuration: Duration of a sample in seconds.
        '''
        l = self.lastBytesSamples
        newBytes = totalBytes - self.last_calculated_totalBytes
        self.last_calculated_totalBytes = totalBytes
        if newBytes >= 0: # newBytes may be negetive, will happen
                          # if a thread has crushed and the totalBytes counter got decreased.
            if len(l) == sampleCount: # calc download for last 3 seconds (30 * 100ms per signal emit)
                l.pop(0)
                
            l.append(newBytes)
            
        dlRate = sum(l)/len(l)/sampleDuration
        return dlRate
        
    def calcETA(self, eta):
        self.calcETA_i += 1
        l = self.calcETA_queue
        l.append(eta)
        
        if self.calcETA_i % 10 == 0:
            self.calcETA_val = sum(l)/len(l)
        if len(l) == 30:
            l.pop(0)

        if self.calcETA_i < 50:
            return 0
        return self.calcETA_val

def post_threadpool_actions(pool, args, expected_filesize, SmartDL_obj):
    "Run function after thread pool is done. Run this in a thread."
    while pool.workRequests:
        time.sleep(0.1)
        
    if SmartDL_obj._killed:
        return
        
    if SmartDL_obj._failed:
        SmartDL_obj.logger.warning("Task has errors. Exiting...")
        return
        
    if expected_filesize: # if not zero, etc expected filesize is not known
        threads = len(args[0])
        total_filesize = sum([os.path.getsize(x) for x in args[0]])
        diff = math.fabs(expected_filesize - total_filesize)
        
        # if the difference is more than 4*thread numbers (because a thread may download 4KB more per thread because of NTFS's block size)
        if diff > 4*threads:
            SmartDL_obj.logger.warning('Diff between downloaded files and expected filesizes is %dKB. Retrying...' % diff)
            SmartDL_obj.retry('Diff between downloaded files and expected filesizes is %dKB.' % diff)
            return
    
    SmartDL_obj.status = "combining"
    utils.combine_files(*args)
    
    if SmartDL_obj.verify_hash:
        dest_path = args[-1]
        with open(dest_path, 'rb') as f:
            hash = hashlib.new(SmartDL_obj.hash_algorithm, f.read()).hexdigest()
            
        if hash == SmartDL_obj.hash_code:
            SmartDL_obj.logger.debug('Hash verification succeeded.')
        else:
            SmartDL_obj.logger.debug('Hash verification failed.')
            SmartDL_obj.try_next_mirror(HashFailedException(os.path.basename(dest_path), hash, SmartDL_obj.hash_code))
    
def calc_args(filesize, threads, minChunkFile):
    if not filesize:
        return [(0, 0)]
        
    while filesize/threads < minChunkFile and threads > 1:
        threads -= 1
        
    args = []
    pos = 0
    chunk = filesize/threads
    for i in range(threads):
        startByte = pos
        endByte = pos + chunk
        if endByte > filesize-1:
            endByte = filesize-1
        args.append((startByte, endByte))
        pos += chunk+1
        
    return args

def download(url, dest, startByte=0, endByte=None, headers=None, timeout=4, shared_var=None, thread_shared_cmds=None, logger=None, retries=3):
    "The basic download function that runs at each thread."
    logger = logger or utils.DummyLogger()
    if not headers:
        headers = {}
    if endByte:
        headers['Range'] = 'bytes=%d-%d' % (startByte, endByte)
        
    logger.debug("Downloading '%s' to '%s'..." % (url, dest))
    req = urllib2.Request(url, headers=headers)
    try:
        urlObj = urllib2.urlopen(req, timeout=timeout)
    except urllib2.HTTPError, e:
        if e.code == 416:
            '''
            HTTP 416 Error: Requested Range Not Satisfiable. Happens when we ask
            for a range that is not available on the server. It will happen when
            the server will try to send us a .html page that means something like
            "you opened too many connections to our server". If this happens, we
            will wait for the other threads to finish their connections and try again.
            '''
            
            if retries > 0:
                logger.warning("Thread didn't got the file it was expecting. Retrying (%d times left)..." % (retries-1))
                time.sleep(5)
                return download(url, dest, startByte, endByte, headers, timeout, shared_var, logger, retries-1)
            else:
                raise
        else:
            raise
    
    with open(dest, 'wb') as f:
        if endByte:
            filesize = endByte-startByte
        else:
            try:
                meta = urlObj.info()
                filesize = int(meta.getheaders("Content-Length")[0])
                logger.debug("Content-Length is %d." % filesize)
            except IndexError:
                logger.warning("Server did not send Content-Length.")
        
        filesize_dl = 0 # total downloaded size
        # limitspeed_timestamp = 0
        # limitspeed_filesize = 0
        block_sz = 8192
        while True:
            if thread_shared_cmds:
                if 'stop' in thread_shared_cmds:
                    logger.error('stop command issued.')
                    raise CanceledException()
                if 'pause' in thread_shared_cmds:
                    time.sleep(0.2)
                    continue
                # if 'limit' in thread_shared_cmds:
                    # currect_time = int(time.time())
                    # if limitspeed_timestamp == currect_time:
                        # if limitspeed_filesize >= thread_shared_cmds['limit']:
                            # time.sleep(0.05)
                            # continue
                    # else:
                        # limitspeed_timestamp = currect_time
                        # limitspeed_filesize = 0
                
            try:
                buff = urlObj.read(block_sz)
            except Exception, e:
                logger.error(unicode(e))
                if shared_var:
                    shared_var.value -= filesize_dl
                raise
                
            if not buff:
                break

            filesize_dl += len(buff)
            # limitspeed_filesize += len(buff)
            if shared_var:
                shared_var.value += len(buff)
            f.write(buff)
            
    urlObj.close()