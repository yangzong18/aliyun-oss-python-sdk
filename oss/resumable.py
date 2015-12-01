# -*- coding: utf-8 -*-

"""
oss.resumable
~~~~~~~~~~~~~

该模块包含了断点续传相关的函数和类。
"""

import os

from . import utils
from . import iterators
from . import exceptions
from . import defaults

from .models import PartInfo
from .compat import json, stringify

import logging


_MAX_PART_COUNT = 10000
_MIN_PART_SIZE = 100 * 1024


def resumable_upload(bucket, key, filename,
                     store=None,
                     headers=None,
                     multipart_threshold=defaults.multipart_threshold,
                     part_size=defaults.part_size,
                     progress_callback=None):
    """断点上传本地文件。

    缺省条件下，该函数会在用户HOME目录下保存断点续传的信息。当待上传的本地文件没有发生变化，
    且目标对象名没有变化时，会根据本地保存的信息，从断点开始上传。

    :param bucket: :class:`Bucket <oss.Bucket>` 对象
    :param key: 上传到用户空间的对象名
    :param filename: 待上传本地文件名
    :param store: 用来保存断点信息的持久存储，参见 :class:`FileStore` 的接口。如不指定，则使用 `FileStore` 。
    :param headers: 传给 `put_object` 或 `init_multipart_upload` 的HTTP头部
    :param multipart_threshold: 文件长度大于该值时，则用分片上传。
    :param part_size: 指定分片上传的每个分片的大小。如不指定，则自动计算。
    :param progress_callback: 上传进度回调函数。参见 :ref:`progress_callback` 。
    """
    size = os.path.getsize(filename)

    if size >= multipart_threshold:
        uploader = ResumableUploader(bucket, key, filename, size, store,
                                     part_size=part_size,
                                     headers=headers,
                                     progress_callback=progress_callback)
        uploader.upload()
    else:
        with open(filename, 'rb') as f:
            bucket.put_object(key, f,
                              headers=headers,
                              progress_callback=progress_callback)


def determine_part_size(total_size,
                        preferred_size=defaults.part_size):
    if total_size < preferred_size:
        return total_size

    if preferred_size * _MAX_PART_COUNT < total_size:
        return total_size // _MAX_PART_COUNT + 1
    else:
        return preferred_size


_UPLOAD_TEMP_DIR = '.py-oss-upload'


class ResumableUploader(object):
    """以断点续传方式上传文件。

    :param bucket: :class:`Bucket <oss.Bucket>` 对象
    :param key: 对象名
    :param filename: 待上传的文件名
    :param size: 文件总长度
    :param store: 用来保存进度的持久化存储
    :param headers: 传给 `init_multipart_upload` 的HTTP头部
    :param part_size: 分片大小。优先使用用户提供的值。如果用户没有指定，那么对于新上传，计算出一个合理值；对于老的上传，采用第一个
        分片的大小。
    :param progress_callback: 上传进度回调函数。参见 :ref:`progress_callback` 。
    """
    def __init__(self, bucket, key, filename, size,
                 store=None,
                 headers=None,
                 part_size=defaults.part_size,
                 progress_callback=None):
        self.bucket = bucket
        self.key = key
        self.filename = filename
        self.size = size

        self.store = store or FileStore(dir=_UPLOAD_TEMP_DIR)
        self.headers = headers
        self.part_size = part_size

        self.abspath = os.path.abspath(filename)
        self.mtime = os.path.getmtime(filename)

        self.progress_callback = progress_callback

        self.store_key = self.store.make_store_key(bucket.bucket_name, key, self.abspath)

    def upload(self):
        record = self.__load_record()

        parts_uploaded = self.__recorded_parts(record)
        upload_id = record['upload_id']

        with open(self.filename, 'rb') as f:
            parts_to_upload, kept_parts = self.__get_parts_to_upload(f, parts_uploaded)
            parts_to_upload = sorted(parts_to_upload, key=lambda p: p.part_number)

            size_uploaded = sum(p.size for p in kept_parts)

            for part in parts_to_upload:
                f.seek(part.start, os.SEEK_SET)
                result = self.bucket.upload_part(self.key, upload_id, part.part_number,
                                                 utils.SizedStreamReader(f, part.size))
                kept_parts.append(PartInfo(part.part_number, result.etag))

                if self.progress_callback:
                    self.progress_callback(size_uploaded, self.size, part.size)

                size_uploaded += part.size

                record['parts'].append({'part_number': part.part_number, 'etag': result.etag})
                self.__store_put(record)

            if self.progress_callback:
                self.progress_callback(self.size, self.size, 0)

            self.bucket.complete_multipart_upload(self.key, upload_id, kept_parts)
            self.__store_delete()

    def __store_get(self):
        return self.store.get(self.store_key)

    def __store_put(self, record):
        return self.store.put(self.store_key, record)

    def __store_delete(self):
        return self.store.delete(self.store_key)

    def __load_record(self):
        record = self.__store_get()

        if record and not is_record_sane(record):
            self.__store_delete()
            record = None

        if record and self.__file_changed(record):
            logging.debug('{0} was changed, clear the record.'.format(self.filename))
            self.__store_delete()
            record = None

        if record and not self.__upload_exists(record['upload_id']):
            self.__store_delete()
            record = None

        if record:
            self.part_size = record['part_size']
        else:
            self.part_size = self.part_size or determine_part_size(self.size)
            upload_id = self.bucket.init_multipart_upload(self.key, headers=self.headers).upload_id
            record = {'upload_id': upload_id, 'mtime': self.mtime, 'size': self.size, 'parts': [],
                      'abspath': self.abspath, 'key': self.key,
                      'part_size': self.part_size}

            self.__store_put(record)

        return record

    def __recorded_parts(self, record):
        last_part_number = utils.how_many(self.size, self.part_size)

        parts_uploaded = []

        for p in record['parts']:
            part_info = PartInfo(int(p['part_number']), p['etag'])
            if part_info.part_number == last_part_number:
                part_info.size = self.size % self.part_size
            else:
                part_info.size = self.part_size

            parts_uploaded.append(part_info)

        return parts_uploaded

    def __upload_exists(self, upload_id):
        try:
            list(iterators.PartIterator(self.bucket, self.key, upload_id, '0', max_parts=1))
        except exceptions.NoSuchUpload:
            return False
        else:
            return True

    def __file_changed(self, record):
        return record['mtime'] != self.mtime or record['size'] != self.size

    def __get_parts_to_upload(self, f, parts_uploaded):
        num_parts = utils.how_many(self.size, self.part_size)
        uploaded_map = {}
        to_upload_map = {}

        for uploaded in parts_uploaded:
            uploaded_map[uploaded.part_number] = uploaded

        for i in range(num_parts):
            if i == num_parts - 1:
                start = i * self.part_size
                end = self.size
            else:
                start = i * self.part_size
                end = self.part_size + start

            to_upload_map[i + 1] = _PartToUpload(i + 1, start, end)

        if not parts_uploaded:
            return to_upload_map.values(), []

        kept_parts = []

        for uploaded in parts_uploaded:
            if uploaded.part_number in to_upload_map:
                del to_upload_map[uploaded.part_number]
                kept_parts.append(uploaded)

        return to_upload_map.values(), kept_parts


class FileStore(object):
    def __init__(self, root=None, dir=None):
        root = root or os.path.expanduser('~')
        if dir:
            self.dir = os.path.join(root, dir)
        else:
            self.dir = root

        if not os.path.isdir(self.dir):
            os.makedirs(self.dir)

    @staticmethod
    def make_store_key(bucket_name, key, filename):
        oss_pathname = 'oss://{0}/{1}'.format(bucket_name, key)
        return utils.md5_string(oss_pathname) + '-' + utils.md5_string(filename)

    def get(self, key):
        pathname = self.__path(key)

        logging.debug('get key={0}, pathname={1}'.format(key, pathname))

        if not os.path.exists(pathname):
            return None

        # json.load()返回的总是unicode，对于Python2，我们将其转换
        # 为str。
        with open(pathname, 'r') as f:
            return stringify(json.load(f))

    def put(self, key, value):
        pathname = self.__path(key)

        with open(pathname, 'w') as f:
            json.dump(value, f)

        logging.debug('put key={0}, pathname={1}'.format(key, pathname))

    def delete(self, key):
        pathname = self.__path(key)
        os.remove(pathname)

        logging.debug('del key={0}, pathname={1}'.format(key, pathname))

    def __path(self, key):
        return os.path.join(self.dir, key)


def make_upload_store():
    return FileStore(dir=_UPLOAD_TEMP_DIR)


def rebuild_record(filename, store, bucket, key, upload_id, part_size=None):
    abspath = os.path.abspath(filename)
    mtime = os.path.getmtime(filename)
    size = os.path.getsize(filename)

    store_key = store.make_store_key(bucket.bucket_name, key, abspath)
    record = {'upload_id': upload_id, 'mtime': mtime, 'size': size, 'parts': [],
              'abspath': abspath, 'key': key}

    for p in iterators.PartIterator(bucket, key, upload_id):
        record['parts'].append({'part_number': p.part_number,
                                'etag': p.etag})

        if not part_size:
            part_size = p.size

    record['part_size'] = part_size

    store.put(store_key, record)


def is_record_sane(record):
    try:
        for key in ('upload_id', 'abspath', 'key'):
            if not isinstance(record[key], str):
                logging.error('{0} is not a string: {1}, but {2}'.format(key, record[key], record[key].__class__))
                return False

        for key in ('size', 'part_size'):
            if not isinstance(record[key], int):
                logging.error('{0} is not an integer: {1}'.format(key, record[key]))
                return False

        if not isinstance(record['mtime'], int) and not isinstance(record['mtime'], float):
            logging.error('mtime is not a float or an integer: {0}'.format(record['mtime']))
            return False

        if not isinstance(record['parts'], list):
            logging.error('parts is not a list: {0}'.format(record['parts'].__class__.__name__))
            return False
    except KeyError as e:
        logging.error('Key not found: {0}'.format(e.args))
        return False

    return True


class _PartToUpload(object):
    def __init__(self, part_number, start, end):
        self.part_number = part_number
        self.start = start
        self.end = end

    @property
    def size(self):
        return self.end - self.start