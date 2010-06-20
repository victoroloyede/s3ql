'''
__init__.py - this file is part of S3QL (http://s3ql.googlecode.com)

Copyright (C) 2010 Nikolaus Rath <Nikolaus@rath.org>
Copyright (C) 2010 Ron Knapp <ron.siesta@gmail.com>

This program can be distributed under the terms of the GNU LGPL.
'''

from __future__ import division, print_function, absolute_import

from .common import AbstractConnection, AbstractBucket
import logging
import errno
import shutil
import cPickle as pickle
import os
import stat
import paramiko

log = logging.getLogger("backend.sftp")


class Connection(AbstractConnection):

    def __init__(self, host, port, login, password):
        super(Connection, self).__init__()

        self.port = port or 22
        self.host = host
        self.login = login
        self.password = password

        self._client = None
        self.sftp = None
        self.setup_ssh_connection()

    def setup_ssh_connection(self):

        self._client = paramiko.SSHClient()
        # Probably not a good idea to do this by default
        #self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._client.load_host_keys(os.path.expanduser('~/.ssh/known_hosts'))
        self._client.connect(self.host, port=self.port, username=self.login, password=self.password)
        self.sftp = self._client.open_sftp()

        # We don't want the connection to time out
        self._client.get_transport().set_keepalive(300)

    def __contains__(self, entry):
        try:
            self.sftp.stat(entry)
        except IOError as exc:
            if exc.errno == errno.ENOENT:
                return False
            else:
                raise
        else:
            return True

    def delete_bucket(self, name, recursive=False):
        """Delete bucket"""

        if name not in self:
            raise KeyError('Bucket directory does not exist on remote host')

        if recursive:
            self._rmtree(name)
  
        self.sftp.rmdir(name)
        
    def _rmtree(self, path):
        '''Recursively delete contents of remote path'''
        
        for attr in self.sftp.listdir_attr(path):
            fullname = '%s/%s' % (path, attr.filename)
            if stat.S_ISDIR(attr.st_mode):
                self._rmtree(fullname)
                self.sftp.rmdir(fullname)
            else:
                self.sftp.remove(fullname)                

        
    def create_bucket(self, name, passphrase=None, compression='lzma'):
        """Create and return bucket"""

        self.sftp.mkdir(name)
        return self.get_bucket(name, passphrase, compression)

    def get_bucket(self, name, passphrase=None, compression='lzma'):
        """Return Bucket instance for the bucket `name`
        
        Raises `KeyError` if the bucket does not exist.
        """
        if name not in self:
            raise KeyError('Bucket %s does not exist' % name)

        return Bucket(self, name, passphrase, compression)

    def close(self):
        self._client.close()

    def prepare_fork(self):
        self._client.close()

    def finish_fork(self):
        self.setup_ssh_connection()

class Bucket(AbstractBucket):

    def __init__(self, conn, name, passphrase, compression):
        super(Bucket, self).__init__(passphrase, compression)
        self.conn = conn
        self.name = name

    def key_to_path(self, key):
        '''Return path for given key'''
        
        key = escape(key)
        
        if not key.startswith('s3ql_data_'):
            return os.path.join(self.name, key)
        
        no = key[10:]
        path = [ self.name, 's3ql_data']
        for i in range(0, len(no), 3):
            path.append(no[:i])
        path.append(key)
        
        return os.path.join(*path)
    
    def __str__(self):
        return '<sftp bucket, name=%r>' % self.name

    def clear(self):
        self.conn._rmtree(self.name)

    def contains(self, key):
        return (self.key_to_path(key) + '.dat') in self.conn

    def raw_lookup(self, key):
        path = self.key_to_path(key)
        try:
            src = self.conn.sftp.open(path + '.meta', 'rb')
            return pickle.load(src)
        except IOError as exc:
            if exc.errno == errno.ENOENT:
                raise KeyError('Key %r not in bucket' % key)
            else:
                raise

    def delete(self, key, force=False):
        path = self.key_to_path(key)

        try:
            self.conn.sftp.remove(path + '.dat')
            self.conn.sftp.remove(path + '.meta')
        except IOError as exc:
            if exc.errno == errno.ENOENT:
                if force:
                    pass
                else:
                    raise KeyError('Key %r not in bucket' % key)
            else:
                raise

    def list(self, prefix=''):
        if prefix:
            base = os.path.dirname(self.key_to_path(prefix))
        else:
            base = self.name
            
        for (_, _, names) in self._walk(base):
            for name in names:
                if not name.endswith('.dat'):
                    continue
                key = unescape(name[:-4])
                
                if not prefix or key.startswith(prefix):
                    yield key
           
    def _walk(self, base):
        '''Iterate recursively over directories, like os.walk'''
                         
        to_visit = [ base ]
        while to_visit: 
            base = to_visit.pop()
            files = list()
            for attr in self.conn.sftp.listdir_attr(base):
                if stat.S_ISDIR(attr.st_mode):
                    to_visit.append('%s/%s' % (base, attr.filename))
                else:
                    files.append(attr.filename) 
            yield (base, to_visit, files)
    
    def _makedirs(self, path):
        '''Like os.makedirs, but over sftp'''
        
        cur = '/'
        done = False
        for el in path.split('/'):
            cur = '%s/%s' % (cur, el)
            if cur not in self.conn:
                self.conn.sftp.mkdir(cur)
                done = True
        
        if not done:
            err = OSError('Entry already exists: %s' % cur)
            err.errno = errno.EEXIST
            raise err
            
        
    def get_size(self):
        size = 0
        to_visit = [ self.name ]
        while to_visit: 
            base = to_visit.pop()
            for attr in self.conn.sftp.listdir_attr(base):
                if stat.S_ISDIR(attr.st_mode):
                    to_visit.append('%s/%s' % (base, attr.filename))
                elif attr.filename.endswith('.dat'):
                    size += attr.st_size            
            
        return size

    def raw_fetch(self, key, fh):
        path = self.key_to_path(key)
        try:
            src = self.conn.sftp.open(path + '.dat', 'r')
            fh.seek(0)
            shutil.copyfileobj(src, fh)

            src = self.conn.sftp.open(path + '.meta', 'r')
            metadata = pickle.load(src)

        except IOError as exc:
            if exc.errno == errno.ENOENT:
                raise KeyError('Key %r not in bucket' % key)
            else:
                raise

        return metadata

    def raw_store(self, key, fh, metadata):
        path = self.key_to_path(key)
        fh.seek(0)
        try:
            dest = self.conn.sftp.open(path + '.dat', 'w')
        except IOError as exc:
            if exc.errno != errno.ENOENT:
                raise
            self._makedirs(os.path.dirname(path))
            dest = self.conn.sftp.open(path + '.dat', 'w')
            
        shutil.copyfileobj(fh, dest)
        dest.close()
                
        dest = self.conn.sftp.open(path + '.meta', 'w')
        pickle.dump(metadata, dest, 2)
        dest.close()

    def rename(self, src, dest):
        src_path = self.key_to_path(src)
        dest_path = self.key_to_path(dest)
        if not os.path.exists(src_path + '.dat'):
            raise KeyError('Key %r not in bucket' % src)
           
        try: 
            self.conn.sftp.rename(src_path, dest_path)
        except IOError as exc:
            if exc.errno != errno.ENOENT:
                raise
            self._makedirs(os.path.dirname(dest_path))
            self.conn.sftp.rename(src_path, dest_path)          


def escape(s):
    '''Escape '/', '=' and '\0' in s'''

    s = s.replace('=', '=3D')
    s = s.replace('/', '=2F')
    s = s.replace('\0', '=00')

    return s

def unescape(s):
    '''Un-Escape '/', '=' and '\0' in s'''

    s = s.replace('=2F', '/')
    s = s.replace('=00', '\0')
    s = s.replace('=3D', '=')

    return s
