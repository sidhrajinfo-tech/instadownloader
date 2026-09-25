import json
import tempfile
import threading
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import downloader as d


def post(code, video=False, nodes=None):
    value = SimpleNamespace(shortcode=code, date_utc=datetime(2026, 9, 25),
                            typename='GraphSidecar' if nodes else 'GraphVideo' if video else 'GraphImage',
                            is_video=video, url='photo.jpg', video_url='clip.mp4', caption='A / caption', likes=1, comments=2)
    value.get_sidecar_nodes = lambda: iter(nodes)
    return value


class FakeLoader:
    context = None
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure
    def close(self):
        pass
    def download_pic(self, filename, url, date):
        self.calls.append(url)
        if self.failure and self.failure(url):
            raise self.failure(url)
        Path(filename + ('.mp4' if url.endswith('mp4') else '.jpg')).write_bytes(b'test-media')


class DownloaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = patch.object(d, 'BASE', Path(self.tmp.name))
        self.base.start()
    def tearDown(self):
        self.base.stop()
        self.tmp.cleanup()
    def run_job(self, posts, loader=None, **kwargs):
        loader = loader or FakeLoader()
        profile = SimpleNamespace(is_private=False, get_posts=lambda: iter(posts))
        job = d.DownloadJob(d.Options('example_account', delay=0, **kwargs))
        with patch.object(d, 'make_loader', return_value=loader), patch.object(d.instaloader.Profile, 'from_username', return_value=profile):
            job.run()
        return job, loader
    def test_carousel_resume_upgrade_and_zip(self):
        nodes = [SimpleNamespace(is_video=False, display_url='1.jpg'), SimpleNamespace(is_video=True, video_url='2.mp4')]
        posts = [post('AAA', nodes=nodes), post('BBB')]
        first, loader = self.run_job(posts, count=1)
        self.assertEqual(first.snapshot()['images'], 1)
        self.assertEqual(first.snapshot()['processed'], 1)
        second, loader = self.run_job(posts, count=2, videos=True)
        self.assertEqual(loader.calls, ['2.mp4', 'photo.jpg'])
        self.assertEqual(second.snapshot()['successful'], 2)
        third, loader = self.run_job(posts, count=2, videos=True)
        self.assertEqual(loader.calls, [])
        self.assertEqual(third.snapshot()['reused'], 2)
        with zipfile.ZipFile(third.snapshot()['zip_path']) as archive:
            names = archive.namelist()
            self.assertTrue(any(name.endswith('video_02.mp4') for name in names))
            self.assertFalse(any('.download.lock' in name for name in names))
            self.assertIsNone(archive.testzip())
        metadata = json.loads((third.directory / '2026-09-25_AAA/metadata.json').read_text())
        self.assertEqual(metadata['media_count'], 2)
    def test_single_failure_continues_and_failed_file_not_committed(self):
        a = post('AAA'); a.url = 'bad.jpg'
        loader = FakeLoader(lambda url: RuntimeError('failure') if url == 'bad.jpg' else None)
        job, _ = self.run_job([a, post('BBB')], loader, count=2)
        self.assertEqual(job.snapshot()['failed'], 1)
        self.assertEqual(job.snapshot()['successful'], 1)
        self.assertFalse(list(job.directory.rglob('.partial-*')))
    def test_rate_limit_stops_without_next_post(self):
        loader = FakeLoader(lambda url: d.AccessLimited('429'))
        job, _ = self.run_job([post('AAA'), post('BBB')], loader, count=2)
        self.assertEqual(job.snapshot()['status'], 'Paused')
        self.assertEqual(len(loader.calls), 1)
    def test_stop_preserves_completed_media(self):
        job = d.DownloadJob(d.Options('example_account', count=2, delay=0))
        loader = FakeLoader()
        original = loader.download_pic
        def download(*args):
            original(*args)
            job.stop()
        loader.download_pic = download
        profile = SimpleNamespace(is_private=False, get_posts=lambda: iter([post('AAA'), post('BBB')]))
        with patch.object(d, 'make_loader', return_value=loader), patch.object(d.instaloader.Profile, 'from_username', return_value=profile):
            job.run()
        self.assertEqual(job.snapshot()['status'], 'Stopped')
        self.assertEqual(len(loader.calls), 1)
        self.assertTrue(list(job.directory.rglob('*.jpg')))
    def test_paths_rejected_and_windows_reserved_safe(self):
        for folder in ('../escape', '/tmp', 'a/b', 'a\\b', '..'):
            with self.assertRaises(ValueError):
                d.output_path(folder, 'example_account')
        self.assertEqual(d.output_path('CON', 'example_account').parent.name, '_CON')
    def test_private_profile_never_iterated(self):
        job = d.DownloadJob(d.Options('example_account', delay=0))
        profile = SimpleNamespace(is_private=True)
        with patch.object(d, 'make_loader', return_value=FakeLoader()), patch.object(d.instaloader.Profile, 'from_username', return_value=profile):
            job.run()
        self.assertEqual(job.snapshot()['message'], d.PRIVATE_MESSAGE)
        self.assertEqual(job.snapshot()['processed'], 0)
    def test_deleted_local_file_redownloaded(self):
        job, _ = self.run_job([post('AAA')], count=1)
        next(job.directory.rglob('*.jpg')).unlink()
        job, loader = self.run_job([post('AAA')], count=1)
        self.assertEqual(len(loader.calls), 1)
    def test_iterator_failure_terminates(self):
        def broken():
            yield post('AAA')
            raise ConnectionError('offline')
        job, _ = self.run_job(broken(), count=5)
        self.assertEqual(job.snapshot()['status'], 'Error')
        self.assertEqual(job.snapshot()['successful'], 1)
        self.assertTrue(job.snapshot()['done'])
    def test_existing_lock_is_not_removed(self):
        job = d.DownloadJob(d.Options('example_account', delay=0))
        job.directory.mkdir(parents=True)
        lock = job.directory / '.download.lock'
        lock.write_text('another process')
        job.run()
        self.assertTrue(lock.exists())
        self.assertEqual(job.snapshot()['status'], 'Error')


if __name__ == '__main__':
    unittest.main()
