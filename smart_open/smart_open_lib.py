#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (C) 2015 Radim Rehurek <me@radimrehurek.com>
#
# This code is distributed under the terms and conditions
# from the MIT License (MIT).


"""
Utilities for streaming from several file-like data storages: S3 / HDFS / standard
filesystem / compressed files..., using a single, Pythonic API.

The streaming makes heavy use of generators and pipes, to avoid loading
full file contents into memory, allowing work with arbitrarily large files.

The main methods are:

* `smart_open()`, which opens the given file for reading/writing
* `s3_iter_bucket()`, which goes over all keys in an S3 bucket in parallel

"""

import logging
import multiprocessing.pool
import os
import subprocess
import sys

from boto.compat import BytesIO, urlsplit, six
import boto.s3.key

logger = logging.getLogger(__name__)

S3_MIN_PART_SIZE = 50 * 1024**2  # minimum part size for S3 multipart uploads


def smart_open(uri, mode="rb"):
    """
    Open the given S3 / HDFS / filesystem file pointed to by `uri` for reading or writing.

    The only supported modes for now are 'rb' (read, default) and 'wb' (replace & write).

    The reads/writes are memory efficient (streamed) and therefore suitable for
    arbitrarily large files.

    The `uri` can be either:

    1. local filesystem (compressed ``.gz`` or ``.bz2`` files handled automatically):
       `./lines.txt`, `/home/joe/lines.txt.gz`, `file:///home/joe/lines.txt.bz2`
    2. Amazon's S3 (can also supply credentials inside the URI):
       `s3://my_bucket/lines.txt`, `s3://my_aws_key_id:key_secret@my_bucket/lines.txt`
    3. HDFS: `hdfs:///some/path/lines.txt`

    Examples::

      >>> # stream lines from S3; you can use context managers too:
      >>> with smart_open.smart_open('s3://mybucket/mykey.txt') as fin:
      ...     for line in fin:
      ...         print line

      >>> # stream line-by-line from an HDFS file
      >>> for line in smart_open.smart_open('hdfs:///user/hadoop/my_file.txt'):
      ...    print line

      >>> # stream content *into* S3:
      >>> with smart_open.smart_open('s3://mybucket/mykey.txt', 'wb') as fout:
      ...     for line in ['first line', 'second line', 'third line']:
      ...          fout.write(line + '\n')

      >>> # stream from/to (compressed) local files:
      >>> for line in smart_open.smart_open('/home/radim/my_file.txt'):
      ...    print line
      >>> for line in smart_open.smart_open('/home/radim/my_file.txt.gz'):
      ...    print line
      >>> with smart_open.smart_open('/home/radim/my_file.txt.gz', 'wb') as fout:
      ...    fout.write("hello world!\n")
      >>> with smart_open.smart_open('/home/radim/another.txt.bz2', 'wb') as fout:
      ...    fout.write("good bye!\n")

    """
    # this method just routes the request to classes handling the specific storage
    # schemes, depending on the URI protocol in `uri`
    parsed_uri = ParseUri(uri)

    if parsed_uri.scheme in ("file", ):
        # local files -- both read & write supported
        # compression, if any, is determined by the filename extension (.gz, .bz2)
        return file_smart_open(parsed_uri.uri_path, mode)

    if mode in ('r', 'rb'):
        if parsed_uri.scheme in ("s3", "s3n"):
            return S3OpenRead(parsed_uri)
        elif parsed_uri.scheme in ("hdfs", ):
            return HdfsOpenRead(parsed_uri)
        else:
            raise NotImplementedError("read mode not supported for %r scheme", parsed_uri.scheme)
    elif mode in ('w', 'wb'):
        if parsed_uri.scheme in ("s3", "s3n"):
            s3_connection = boto.connect_s3(aws_access_key_id=parsed_uri.access_id, aws_secret_access_key=parsed_uri.access_secret)
            outbucket = s3_connection.get_bucket(parsed_uri.bucket_id)
            outkey = boto.s3.key.Key(outbucket)
            outkey.key = parsed_uri.key_id
            return S3OpenWrite(outbucket, outkey)
        else:
            raise NotImplementedError("write mode not supported for %r scheme", parsed_uri.scheme)
    else:
        raise NotImplementedError("unknown file mode %s" % mode)


class ParseUri(object):
    """
    Parse the given URI.

    Supported URI schemes are "file", "s3", "s3n" and "hdfs".

    Valid URI examples::

      * s3://my_bucket/my_key
      * s3://my_key:my_secret@my_bucket/my_key
      * hdfs:///path/file
      * ./local/path/file
      * ./local/path/file.gz
      * file:///home/user/file
      * file:///home/user/file.bz2

    """
    def __init__(self, uri, default_scheme="file"):
        """
        Assume `default_scheme` if no scheme given in `uri`.

        """
        if os.name == 'nt':
            # urlsplit doesn't work on Windows -- it parses the drive as the scheme...
            if '://' not in uri:
                # no protocol given => assume a local file
                uri = 'file://' + uri
        parsed_uri = urlsplit(uri)
        self.scheme = parsed_uri.scheme if parsed_uri.scheme else default_scheme

        if self.scheme == "hdfs":
            self.uri_path = parsed_uri.netloc + parsed_uri.path

            if not self.uri_path:
                raise RuntimeError("invalid HDFS URI: %s" % uri)
        elif self.scheme in ("s3", "s3n"):
            self.bucket_id = (parsed_uri.netloc + parsed_uri.path).split('@')
            self.key_id = None

            if len(self.bucket_id) == 1:
                # URI without credentials: s3://bucket/object
                self.bucket_id, self.key_id = self.bucket_id[0].split('/', 1)
                # "None" credentials are interpreted as "look for credentials in other locations" by boto
                self.access_id, self.access_secret = None, None
            elif len(self.bucket_id) == 2 and len(self.bucket_id[0].split(':')) == 2:
                # URI in full format: s3://key:secret@bucket/object
                # access key id: [A-Z0-9]{20}
                # secret access key: [A-Za-z0-9/+=]{40}
                acc, self.bucket_id = self.bucket_id
                self.access_id, self.access_secret = acc.split(':')
                self.bucket_id, self.key_id = self.bucket_id.split('/', 1)
            else:
                # more than 1 '@' means invalid uri
                # Bucket names must be at least 3 and no more than 63 characters long.
                # Bucket names must be a series of one or more labels.
                # Adjacent labels are separated by a single period (.).
                # Bucket names can contain lowercase letters, numbers, and hyphens.
                # Each label must start and end with a lowercase letter or a number.
                raise RuntimeError("invalid S3 URI: %s" % uri)
        elif self.scheme == 'file':
            self.uri_path = parsed_uri.netloc + parsed_uri.path

            if not self.uri_path:
                raise RuntimeError("invalid file URI: %s" % uri)
        else:
            raise NotImplementedError("unknown URI scheme %r in %r" % (self.scheme, uri))


class S3OpenRead(object):
    """
    Implement streamed reader from S3, as an iterable & context manager.

    """
    def __init__(self, parsed_uri):
        if parsed_uri.scheme not in ("s3", "s3n"):
            raise TypeError("can only process S3 files")
        self.parsed_uri = parsed_uri
        s3_connection = boto.connect_s3(
            aws_access_key_id=parsed_uri.access_id,
            aws_secret_access_key=parsed_uri.access_secret)
        self.read_key = s3_connection.get_bucket(parsed_uri.bucket_id).lookup(parsed_uri.key_id)
        if self.read_key is None:
            raise KeyError(parsed_uri.key_id)

    def __iter__(self):
        s3_connection = boto.connect_s3(
            aws_access_key_id=self.parsed_uri.access_id,
            aws_secret_access_key=self.parsed_uri.access_secret)
        key = s3_connection.get_bucket(self.parsed_uri.bucket_id).lookup(self.parsed_uri.key_id)
        if key is None:
            raise KeyError(self.parsed_uri.key_id)

        return s3_iter_lines(key)

    def read(self, size=None):
        """
        Read a specified number of bytes from the key.

        Note read() and line iteration (`for line in self: ...`) each have their
        own file position, so they are independent. Doing a `read` will not affect
        the line iteration, and vice versa.

        """
        if not size or size < 0:
            # For compatibility with standard Python, `read(negative)` = read the rest of the file.
            # Otherwise, boto would read *from the start* if given size=-1.
            size = 0
        return self.read_key.read(size)

    def seek(self, offset, whence=0):
        """
        Seek to the specified position.

        Only seeking to the beginning (offset=0) supported for now.

        """
        if whence != 0 or offset != 0:
            raise NotImplementedError("seek other than offset=0 not implemented yet")
        self.read_key.close(fast=True)

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.read_key.close()


class HdfsOpenRead(object):
    """
    Implement streamed reader from HDFS, as an iterable & context manager.

    """
    def __init__(self, parsed_uri):
        if parsed_uri.scheme not in ("hdfs"):
            raise TypeError("can only process HDFS files")
        self.parsed_uri = parsed_uri

    def __iter__(self):
        hdfs = subprocess.Popen(["hadoop", "fs", "-cat", self.parsed_uri.uri_path], stdout=subprocess.PIPE)
        return hdfs.stdout

    def read(self, size=None):
        raise NotImplementedError("read() not implemented yet")

    def seek(self, offset, whence=None):
        raise NotImplementedError("seek() not implemented yet")

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        pass


def make_closing(base, **attrs):
    """
    Add support for `with Base(attrs) as fout:` to the base class if it's missing.
    The base class' `close()` method will be called on context exit, to always close the file properly.

    This is needed for gzip.GzipFile, bz2.BZ2File etc in older Pythons (<=2.6), which otherwise
    raise "AttributeError: GzipFile instance has no attribute '__exit__'".

    """
    if not hasattr(base, '__enter__'):
        attrs['__enter__'] = lambda self: self
    if not hasattr(base, '__exit__'):
        attrs['__exit__'] = lambda self, type, value, traceback: self.close()
    return type('Closing' + base.__name__, (base, object), attrs)


def file_smart_open(fname, mode='rb'):
    """
    Stream from/to local filesystem, transparently (de)compressing gzip and bz2
    files if necessary.

    """
    _, ext = os.path.splitext(fname)

    if ext == '.bz2':
        PY2 = sys.version_info[0] == 2
        if PY2:
            from bz2file import BZ2File
        else:
            from bz2 import BZ2File
        return make_closing(BZ2File)(fname, mode)

    if ext == '.gz':
        from gzip import GzipFile
        return make_closing(GzipFile)(fname, mode)

    return open(fname, mode)


class S3OpenWrite(object):
    """
    Context manager for writing into S3 files.

    """
    def __init__(self, outbucket, outkey, min_part_size=S3_MIN_PART_SIZE):
        """
        Streamed input is uploaded in chunks, as soon as `min_part_size` bytes are
        accumulated (50MB by default). The minimum chunk size allowed by AWS S3
        is 5MB.

        """
        self.outbucket = outbucket
        self.outkey = outkey
        self.min_part_size = min_part_size

        if min_part_size < 5 * 1024 ** 2:
            logger.warning("S3 requires minimum part size >= 5MB; multipart upload may fail")

        # initialize mulitpart upload
        self.mp = self.outbucket.initiate_multipart_upload(self.outkey)

        # initialize stats
        self.lines = []
        self.total_size = 0
        self.chunk_bytes = 0
        self.parts = 0

    def write(self, b):
        """
        Write the given bytes (binary string) into the S3 file from constructor.

        Note there's buffering happening under the covers, so this may not actually
        do any HTTP transfer right away.

        """
        if isinstance(b, six.text_type):
            # not part of API: also accept unicode => encode it as utf8
            b = b.encode('utf8')

        if not isinstance(b, six.binary_type):
            raise TypeError("input must be a binary string")

        self.lines.append(b)
        self.chunk_bytes += len(b)
        self.total_size += len(b)

        if self.chunk_bytes >= self.min_part_size:
            buff = b"".join(self.lines)
            logger.info("uploading part #%i, %i bytes (total %.3fGB)" % (self.parts, len(buff), self.total_size / 1024.0 ** 3))
            self.mp.upload_part_from_file(BytesIO(buff), part_num=self.parts + 1)
            logger.debug("upload of part #%i finished" % self.parts)
            self.parts += 1
            self.lines, self.chunk_bytes = [], 0

    def seek(self, offset, whence=None):
        raise NotImplementedError("seek() not implemented yet")

    def close(self):
        buff = b"".join(self.lines)
        if buff:
            logger.info("uploading last part #%i, %i bytes (total %.3fGB)" % (self.parts, len(buff), self.total_size / 1024.0 ** 3))
            self.mp.upload_part_from_file(BytesIO(buff), part_num=self.parts + 1)
            logger.debug("upload of last part #%i finished" % self.parts)

        if self.total_size:
            self.mp.complete_upload()
        else:
            # AWS complains with "The XML you provided was not well-formed or did not validate against our published schema"
            # when the input is completely empty => abort the upload, no file created
            # TODO: or create the empty file some other way?
            logger.info("empty input, ignoring multipart upload")
            self.outbucket.cancel_multipart_upload(self.mp.key_name, self.mp.id)

    def __enter__(self):
        return self

    def _termination_error(self):
        logger.exception("encountered error while terminating multipart upload; attempting cancel")
        self.outbucket.cancel_multipart_upload(self.mp.key_name, self.mp.id)
        logger.info("cancel completed")

    def __exit__(self, type, value, traceback):
        if type is not None:
            self._termination_error()
            return False
        
        try:
            self.close()
        except:
            self._termination_error()
            raise


def s3_iter_bucket_process_key(key):
    """
    Conceptually part of `s3_iter_bucket`, but must remain top-level method because
    of pickling visibility.

    """
    return key, key.get_contents_as_string()


def s3_iter_bucket(bucket, prefix='', accept_key=lambda key: True, key_limit=None, workers=16):
    """
    Iterate and download all S3 files under `bucket/prefix`, yielding out
    `(key name, key content)` 2-tuples (generator).

    `accept_key` is a function that accepts a key name (unicode string) and
    returns True/False, signalling whether the given key should be downloaded out or
    not (default: accept all keys).

    If `key_limit` is given, stop after yielding out that many results.

    The keys are processed in parallel, using `workers` processes (default: 16),
    to speed up downloads greatly.

    Example::

      >>> mybucket = boto.connect_s3().get_bucket('mybucket')

      >>> # get all JSON files under "mybucket/foo/"
      >>> for key, content in s3_iter_bucket(mybucket, prefix='foo/', accept_key=lambda key: key.endswith('.json')):
      ...     print key, len(content)

      >>> # limit to 10k files, using 32 parallel workers (default is 16)
      >>> for key, content in s3_iter_bucket(mybucket, key_limit=10000, workers=32):
      ...     print key, len(content)

    """
    logger.info("iterating over keys from %s with %i workers" % (bucket, workers))

    total_size, key_no = 0, -1
    keys = (key for key in bucket.list(prefix=prefix) if accept_key(key.name))

    pool = multiprocessing.pool.Pool(processes=workers)
    for key_no, (key, content) in enumerate(pool.imap_unordered(s3_iter_bucket_process_key, keys)):
        if key_no % 1000 == 0:
            logger.info("yielding key #%i: %s, size %i (total %.1fMB)" %
                (key_no, key, len(content), total_size / 1024.0 ** 2))

        yield key.name, content
        key.close()
        total_size += len(content)

        if key_limit is not None and key_no + 1 >= key_limit:
            # we were asked to output only a limited number of keys => we're done
            break
    pool.terminate()

    logger.info("processed %i keys, total size %i" % (key_no + 1, total_size))


def s3_iter_lines(key):
    """
    Stream an object from S3 line by line (generator).

    `key` must be a `boto.key.Key` object.

    """
    # check valid object on input
    if not isinstance(key, boto.s3.key.Key):
        raise TypeError("expected boto.key.Key object on input")

    buf = b''
    # keep reading chunks of bytes into the buffer
    for chunk in key:
        buf += chunk

        start = 0
        # process all lines within the current buffer
        while True:
            end = buf.find(b'\n', start) + 1
            if end:
                yield buf[start : end]
                start = end
            else:
                # no more newlines => break out to read more data from s3 into the buffer
                buf = buf[start : ]
                break

    # process the last line, too
    if buf:
        yield buf
