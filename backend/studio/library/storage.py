"""Where original files live: the data folder (default) or any S3-compatible bucket, chosen on the Settings page.
Keys look like `<collection id>/<source id>/<file name>`; the first part is what ties a file to its collection, so deleting a collection
deletes everything under its prefix. Callers get a Store from `store()` and never touch paths or buckets themselves."""
import hashlib
import os
import re
import shutil
import tempfile
from .. import config, settings

CHUNK = 1024 * 1024


class StorageError(RuntimeError):
    pass


def clean_name(name):
    """A file name that is safe as part of a key: no folders, no odd characters, bounded length."""
    base = os.path.basename((name or '').replace('\\', '/')) or 'file'
    base = re.sub(r'[^A-Za-z0-9._ -]+', '_', base).strip(' .') or 'file'
    return base[:120]


def spool(fileobj, max_bytes):
    """Copy an upload to a temporary file while counting and hashing it. Returns (file positioned at 0, size, sha256). Raises ValueError when too big."""
    tmp = tempfile.SpooledTemporaryFile(max_size=8 * CHUNK)
    h, n = hashlib.sha256(), 0
    while True:
        b = fileobj.read(CHUNK)
        if not b:
            break
        n += len(b)
        if n > max_bytes:
            tmp.close()
            raise ValueError(f'the file is larger than {max_bytes // (1024 * 1024)} MB')
        h.update(b)
        tmp.write(b)
    tmp.seek(0)
    return tmp, n, h.hexdigest()


class LocalStore:
    kind = 'local'

    def __init__(self, root):
        self.root = os.path.realpath(root)
        os.makedirs(self.root, exist_ok=True)

    def _path(self, key):
        p = os.path.realpath(os.path.join(self.root, key))
        if not p.startswith(self.root + os.sep):
            raise StorageError('bad storage key')
        return p

    def put(self, key, fh):
        p = self._path(key)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix='.part-')
        try:
            with os.fdopen(fd, 'wb') as out:
                shutil.copyfileobj(fh, out, CHUNK)
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def open(self, key):
        try:
            return open(self._path(key), 'rb')
        except FileNotFoundError:
            raise StorageError('the stored file is missing')

    def exists(self, key):
        return os.path.isfile(self._path(key))

    def delete_prefix(self, prefix):
        p = self._path(prefix.rstrip('/'))
        n = sum(len(f) for _, _, f in os.walk(p)) if os.path.isdir(p) else 0
        shutil.rmtree(p, ignore_errors=True)
        return n

    def check(self):
        probe = os.path.join(self.root, '.probe')
        with open(probe, 'wb') as f:
            f.write(b'ok')
        os.unlink(probe)
        return f'writable: {self.root}'


class S3Store:
    kind = 's3'

    def __init__(self, bucket, prefix='', endpoint=None, region=None, access_key=None, secret_key=None, path_style=False):
        import boto3
        from botocore.config import Config
        if not bucket:
            raise StorageError('no S3 bucket is set (Settings, Storage)')
        self.bucket, self.prefix = bucket, (prefix or '').strip('/')
        self.s3 = boto3.client('s3', endpoint_url=endpoint or None, region_name=region or None, aws_access_key_id=access_key or None, aws_secret_access_key=secret_key or None,
                               config=Config(s3={'addressing_style': 'path' if path_style else 'auto'}, retries={'max_attempts': 3}, connect_timeout=10, read_timeout=60))

    def _key(self, key):
        return f'{self.prefix}/{key}' if self.prefix else key

    def put(self, key, fh):
        self.s3.upload_fileobj(fh, self.bucket, self._key(key))

    def open(self, key):
        from botocore.exceptions import ClientError
        try:
            body = self.s3.get_object(Bucket=self.bucket, Key=self._key(key))['Body']
        except ClientError as e:
            raise StorageError(f'cannot read the stored file: {e.response["Error"].get("Code")}')
        f = tempfile.SpooledTemporaryFile(max_size=8 * CHUNK)
        shutil.copyfileobj(body, f, CHUNK)
        f.seek(0)
        return f

    def exists(self, key):
        from botocore.exceptions import ClientError
        try:
            self.s3.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except ClientError:
            return False

    def delete_prefix(self, prefix):
        """Every object under the prefix, including all versions and delete markers of a versioned bucket."""
        full = self._key(prefix.rstrip('/')) + '/'
        n = 0
        for page in self.s3.get_paginator('list_object_versions').paginate(Bucket=self.bucket, Prefix=full):
            objs = [{'Key': o['Key'], 'VersionId': o['VersionId']} for o in (page.get('Versions') or []) + (page.get('DeleteMarkers') or [])]
            for i in range(0, len(objs), 1000):
                self.s3.delete_objects(Bucket=self.bucket, Delete={'Objects': objs[i:i + 1000], 'Quiet': True})
                n += len(objs[i:i + 1000])
        return n

    def check(self):
        self.s3.head_bucket(Bucket=self.bucket)
        v = self.s3.get_bucket_versioning(Bucket=self.bucket).get('Status', 'off')
        return f'bucket {self.bucket} reachable; versioning {v.lower()}'


_cache = {'sig': None, 'store': None}


def store():
    """The Store in force now (rebuilt when the settings change)."""
    driver = settings.get('STORAGE_DRIVER')
    if driver == 's3':
        sig = ('s3', *(settings.get(k) for k in ('S3_BUCKET', 'S3_PREFIX', 'S3_ENDPOINT', 'S3_REGION', 'S3_ACCESS_KEY', 'S3_SECRET_KEY', 'S3_PATH_STYLE')))
    else:
        sig = ('local', config.DATA_DIR)
    if _cache['sig'] != sig:
        _cache['store'] = S3Store(*sig[1:3], sig[3], sig[4], sig[5], sig[6], sig[7]) if driver == 's3' else LocalStore(os.path.join(config.DATA_DIR, 'library'))
        _cache['sig'] = sig
    return _cache['store']


def check():
    """For the Settings page: can the saved storage be used? {'ok', 'detail' | 'error'}"""
    try:
        return {'ok': True, 'detail': store().check()}
    except Exception as e:
        return {'ok': False, 'error': f'{type(e).__name__}: {str(e)[:200]}'}
