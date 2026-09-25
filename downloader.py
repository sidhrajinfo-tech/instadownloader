"""Anonymous Instagram archiving with cooperative cancellation and atomic state."""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

import instaloader

LOG = logging.getLogger(__name__)
BASE = Path(os.environ.get('INSTAGRAM_DOWNLOAD_ROOT', Path(__file__).parent / 'downloads')).resolve()
PRIVATE_MESSAGE = 'This account is private. Only publicly accessible content can be downloaded.'
RESERVED = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}


class Stopped(Exception):
    pass


class AccessLimited(Exception):
    pass


def username_value(value: str) -> str:
    value = value.strip().removeprefix('@').lower()
    if not re.fullmatch(r'[a-z0-9_](?:[a-z0-9_.]{0,28}[a-z0-9_])?', value):
        raise ValueError('Enter a valid Instagram username, without a URL.')
    return value


def safe_component(value: str) -> str:
    if not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,79}', value) or value.endswith('.'):
        raise ValueError('Use a folder name containing only letters, numbers, dots, hyphens or underscores.')
    return '_' + value if value.split('.')[0].upper() in RESERVED else value


def inside(parent: Path, name: str) -> Path:
    child = parent / safe_component(name)
    if child.is_symlink() or not child.resolve().is_relative_to(parent.resolve()):
        raise ValueError('Unsafe output path.')
    return child


def output_path(folder: str, username: str) -> Path:
    return inside(inside(BASE, folder.strip()), username_value(username))


def atomic_json(path: Path, data: dict) -> None:
    if path.is_symlink():
        raise ValueError('Unsafe state file.')
    temporary = path.with_name(path.name + '.tmp')
    if temporary.is_symlink():
        raise ValueError('Unsafe temporary file.')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_index(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError('Unsafe index file.')
    if not path.exists():
        return {'version': 1, 'posts': {}}
    value = json.loads(path.read_text(encoding='utf-8'))
    if value.get('version') != 1 or not isinstance(value.get('posts'), dict):
        raise ValueError('Unsupported or damaged download index. Restore it from a backup before resuming.')
    return value


def limited(exc: Exception) -> bool:
    text = (type(exc).__name__ + ' ' + str(exc)).lower()
    return isinstance(exc, AccessLimited) or any(word in text for word in (
        '429', '401', '403', 'loginrequired', 'login_required', 'login required',
        'too many requests', 'please wait', 'checkpoint', 'challenge', 'feedback_required',
        'temporarily', 'badresponse', 'abortdownload', 'redirected to login'))


def friendly(exc: Exception) -> str:
    if limited(exc):
        return 'Instagram temporarily limited access or requires login. Please try again later. No login or bypass will be attempted.'
    if isinstance(exc, instaloader.exceptions.ProfileNotExistsException):
        return 'Profile not found.'
    if str(exc) == PRIVATE_MESSAGE:
        return PRIVATE_MESSAGE
    if isinstance(exc, ValueError):
        return str(exc)
    if isinstance(exc, OSError):
        return 'A network or storage operation failed. Check your connection, available space and folder permissions.'
    return 'The request could not be completed. Please try again later; see Technical Details.'


def make_loader(stop: threading.Event | None = None) -> instaloader.Instaloader:
    event = stop or threading.Event()

    class ConservativeRateController(instaloader.RateController):
        def sleep(self, secs):
            if event.wait(secs):
                raise Stopped()

        def handle_429(self, query_type):
            raise AccessLimited('HTTP 429: stopped without retrying.')

    return instaloader.Instaloader(
        quiet=True, sleep=True, max_connection_attempts=1, request_timeout=30,
        rate_controller=ConservativeRateController, fatal_status_codes=[401, 403, 429],
        iphone_support=False, download_comments=False, download_geotags=False,
        save_metadata=False, post_metadata_txt_pattern='',
    )


def profile_info(username: str) -> dict:
    loader = make_loader()
    try:
        profile = instaloader.Profile.from_username(loader.context, username_value(username))
        return {key: getattr(profile, key) for key in (
            'username', 'full_name', 'profile_pic_url', 'followers', 'followees',
            'mediacount', 'biography', 'is_private')}
    finally:
        loader.close()


@dataclass(frozen=True)
class Options:
    username: str
    count: int = 100
    videos: bool = False
    metadata: bool = True
    folder: str = 'Instagram_Downloads'
    delay: float = 5.0

    def validate(self):
        username_value(self.username)
        if not 1 <= self.count <= 1000:
            raise ValueError('Choose between 1 and 1000 posts.')
        if self.delay < 0:
            raise ValueError('Delay must not be negative.')
        return output_path(self.folder, self.username)


class DownloadJob:
    """Worker never calls Streamlit. The UI polls immutable snapshots."""
    def __init__(self, options: Options):
        self.options = options
        self.directory = options.validate()
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.thread = None
        self.data = dict(status='Queued', message='Preparing download…', processed=0,
                         successful=0, failed=0, skipped=0, reused=0, images=0, videos=0,
                         current='', errors=[], zip_path=None, progress=0.0, done=False)

    def update(self, **values):
        with self.lock:
            self.data.update(values)

    def add(self, **values):
        with self.lock:
            for key, value in values.items():
                self.data[key] += value

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def error(self, where: str, exc: Exception):
        # No captions, signed URLs, credentials or raw tracebacks in the UI log.
        LOG.warning('%s: %s', where, type(exc).__name__)
        with self.lock:
            self.data['errors'].append(f'{where}: {type(exc).__name__} — {friendly(exc)}')
            self.data['errors'] = self.data['errors'][-200:]

    def start(self):
        if self.thread is not None:
            raise RuntimeError('Job already started.')
        self.thread = threading.Thread(target=self.run, daemon=True, name='instagram-archive')
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.update(message='Stopping after the current safe operation…')

    def check_stop(self):
        if self.stop_event.is_set():
            raise Stopped()

    def pause(self):
        if self.stop_event.wait(self.options.delay):
            raise Stopped()

    def fetch_media(self, loader, url, folder, filename, date):
        # Instaloader writes into a temporary directory; partial data is never committed.
        with tempfile.TemporaryDirectory(prefix='.partial-', dir=folder) as tmp:
            loader.download_pic(str(Path(tmp) / 'media'), url, date)
            files = [p for p in Path(tmp).iterdir() if p.is_file() and p.stat().st_size > 0]
            if len(files) != 1 or files[0].suffix.lower() not in {'.jpg', '.jpeg', '.png', '.webp', '.mp4'}:
                raise OSError('Media download did not produce one valid file.')
            destination = inside(folder, filename + files[0].suffix.lower())
            files[0].replace(destination)
            return destination.name, destination.stat().st_size

    def save_post(self, loader, post, index, index_path):
        shortcode = post.shortcode
        if not re.fullmatch(r'[A-Za-z0-9_-]+', shortcode):
            raise ValueError('Invalid post identifier.')
        date = post.date_utc
        folder = inside(self.directory, f'{date:%Y-%m-%d}_{shortcode}')
        folder.mkdir(parents=True, exist_ok=True)
        entry = index['posts'].setdefault(shortcode, {'files': {}})
        files = entry.setdefault('files', {})
        if post.typename == 'GraphSidecar':
            nodes = list(post.get_sidecar_nodes())
            media = [(node.is_video, node) for node in nodes]
        else:
            media = [(post.is_video, post)]
        selected = 0
        new = 0
        failures = []
        for position, (is_video, node) in enumerate(media, 1):
            self.check_stop()
            if is_video and not self.options.videos:
                continue
            selected += 1
            key = f'{"video" if is_video else "image"}_{position:02d}'
            previous = files.get(key, {})
            path = inside(folder, previous['name']) if previous.get('name') else None
            if path and path.is_file() and path.stat().st_size == previous.get('size', -1) and path.stat().st_size > 0:
                continue
            try:
                self.pause()
                url = node.video_url if is_video else (node.display_url if post.typename == 'GraphSidecar' else node.url)
                name, size = self.fetch_media(loader, url, folder, key, date)
                files[key] = {'name': name, 'size': size}
                atomic_json(index_path, index)
                self.add(**{'videos' if is_video else 'images': 1})
                new += 1
            except Stopped:
                raise
            except Exception as exc:
                if limited(exc) or isinstance(exc, OSError) and getattr(exc, 'errno', None) in {13, 28, 30}:
                    raise
                self.error(f'{shortcode}/{key}', exc)
                failures.append(exc)
        if self.options.metadata:
            metadata_path = folder / 'metadata.json'
            if metadata_path.is_symlink():
                raise ValueError('Unsafe metadata path.')
            if not metadata_path.exists():
                metadata = {'shortcode': shortcode, 'date': date.isoformat() + 'Z',
                            'url': f'https://www.instagram.com/p/{shortcode}/',
                            'is_video': post.is_video, 'media_count': len(media)}
                for key in ('caption', 'likes', 'comments'):
                    try:
                        metadata[key] = getattr(post, key)
                    except Exception as exc:
                        if limited(exc):
                            raise
                        metadata[key] = None
                        self.error(f'{shortcode}/metadata/{key}', exc)
                atomic_json(metadata_path, metadata)
        entry.update(folder=folder.name, last_checked=time.time())
        atomic_json(index_path, index)
        if failures:
            return 'failed'
        if not selected:
            return 'skipped'
        if new == 0:
            self.add(reused=1)
        return 'successful'

    def build_zip(self):
        archive_root = BASE / '_archives'
        archive_root.mkdir(parents=True, exist_ok=True)
        if archive_root.is_symlink():
            raise ValueError('Unsafe archive directory.')
        target = archive_root / f'{uuid.uuid4().hex}.zip'
        temporary = target.with_suffix('.tmp')
        try:
            with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_STORED, allowZip64=True) as archive:
                for path in sorted(self.directory.rglob('*')):
                    relative = path.relative_to(self.directory)
                    if any(part.startswith('.') for part in relative.parts) or path.is_symlink():
                        continue
                    if path.is_file() and path.resolve().is_relative_to(self.directory.resolve()):
                        archive.write(path, Path(self.directory.name) / relative)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        self.update(zip_path=str(target))

    def run(self):
        loader = None
        acquired = False
        lock_path = self.directory / '.download.lock'
        status, message = 'Complete', 'Download completed successfully.'
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                raise ValueError('This folder is locked by another download. See README for stale-lock recovery.')
            with os.fdopen(fd, 'w') as stream:
                stream.write(str(os.getpid()))
            acquired = True
            index_path = self.directory / 'download_index.json'
            index = read_index(index_path)
            loader = make_loader(self.stop_event)
            self.update(status='Running', message='Checking public profile…')
            self.check_stop()
            profile = instaloader.Profile.from_username(loader.context, self.options.username)
            if profile.is_private:
                raise ValueError(PRIVATE_MESSAGE)
            self.check_stop()
            iterator = iter(profile.get_posts())
            for number in range(1, self.options.count + 1):
                self.check_stop()
                self.pause()
                try:
                    post = next(iterator)
                except StopIteration:
                    message = 'All available posts have been processed.'
                    break
                # Iterator errors terminate safely: advancing an invalid iterator is not reliable.
                self.update(current=post.shortcode, message=f'Downloading post {number} of {self.options.count}')
                try:
                    result = self.save_post(loader, post, index, index_path)
                except Stopped:
                    raise
                except Exception as exc:
                    self.add(processed=1, failed=1)
                    self.update(progress=self.snapshot()['processed'] / self.options.count)
                    self.error(post.shortcode, exc)
                    if limited(exc) or isinstance(exc, OSError) and getattr(exc, 'errno', None) in {13, 28, 30}:
                        raise
                    continue
                self.add(processed=1, **{result: 1})
                self.update(progress=self.snapshot()['processed'] / self.options.count)
            self.check_stop()
            if self.snapshot()['failed']:
                message = 'Download finished with some failed posts. Completed files are preserved.'
        except Stopped:
            status, message = 'Stopped', 'Download stopped by user.'
        except Exception as exc:
            status, message = ('Paused' if limited(exc) else 'Error'), friendly(exc)
            self.error('Download', exc)
        finally:
            if loader:
                loader.close()
            if acquired:
                self.update(status='Packaging', message='Preparing ZIP of preserved files…')
                try:
                    atomic_json(self.directory / 'download_summary.json', {
                        k: v for k, v in self.snapshot().items() if k not in {'zip_path', 'done'}
                    } | {'status': status, 'message': message})
                    self.build_zip()
                except Exception as exc:
                    self.error('ZIP', exc)
                    message += ' ZIP creation failed; your files remain in the output folder.'
                finally:
                    lock_path.unlink(missing_ok=True)
            self.update(status=status, message=message, done=True)
