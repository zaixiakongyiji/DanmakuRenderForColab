"""真实 DMR/FFmpeg 适配测试；不联网，不读取真实 Cookie。"""
import json
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from colab_support.adapters import ManagedFFmpeg, SingleSessionDownload
from colab_support.cli import supervise
from DMR.LiveAPI.bilibili import bilibili
from DMR.Render.ffmpeg import RawFFmpegRender

ROOT = Path(__file__).resolve().parents[1]
FFMPEG = shutil.which('ffmpeg') or str(ROOT / 'tools/ffmpeg.exe')
FFPROBE = shutil.which('ffprobe') or str(ROOT / 'tools/ffprobe.exe')


@contextmanager
def serve(directory):
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(directory)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/input.mp4'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def make_source(path):
    subprocess.run([FFMPEG, '-y', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=size=160x120:rate=10',
                    '-t', '5', '-c:v', 'mpeg4', '-g', '10', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(path)],
                   check=True, capture_output=True)


class AdapterTests(unittest.TestCase):
    def test_no_login_on_missing_or_malformed_cookie(self):
        api = object.__new__(bilibili)
        with tempfile.TemporaryDirectory() as d, patch('DMR.Uploader.biliuprs.biliuprs') as login:
            with self.assertRaises(ValueError):
                api.get_stream_urls(bili_watch_cookies=str(Path(d) / 'missing'), allow_login=False)
            bad = Path(d) / 'bad.json'
            bad.write_text('{}')
            with self.assertRaises(ValueError):
                api.get_stream_urls(bili_watch_cookies=str(bad), allow_login=False)
            login.assert_not_called()

    def test_failed_ffmpeg_exit_is_not_success_even_with_summary(self):
        render = RawFFmpegRender()
        import sys
        status, _ = render.call_ffmpeg([sys.executable, '-c', 'print("video:10kB"); raise SystemExit(1)'])
        self.assertFalse(status)

    @unittest.skipUnless(Path(FFMPEG).is_file() and Path(FFPROBE).is_file(), '需要 FFmpeg/ffprobe')
    def test_real_ffmpeg_final_segment_once(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            source = folder / 'input.mp4'
            out = folder / 'record'
            out.mkdir()
            subprocess.run([FFMPEG, '-y', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=size=160x120:rate=10',
                            '-t', '5', '-c:v', 'mpeg4', '-g', '10', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(source)],
                           check=True, capture_output=True)
            completed = []
            recording = ManagedFFmpeg(stream_url=str(source), output_dir=str(out), output_format='mp4',
                                      segment=1, taskname='test', ffmpeg=FFMPEG,
                                      segment_callback=completed.append, stable_callback=lambda _: None,
                                      advanced_video_args={'ffmpeg_stream_args': [], 'ffmpeg_output_args': []})
            with serve(folder) as url:
                recording.stream_url = url
                recording.start()
                recording.stop()
                recording.stop()
            self.assertGreaterEqual(len(completed), 4)
            self.assertEqual(len(completed), len(set(completed)))
            for file in completed:
                result = subprocess.run([FFPROBE, '-v', 'error', '-show_entries', 'format=duration',
                                         '-of', 'default=nw=1:nk=1', file], capture_output=True, text=True, check=True)
                self.assertGreater(float(result.stdout), 0)

    @unittest.skipUnless(Path(FFMPEG).is_file(), '需要 FFmpeg')
    def test_real_ffmpeg_controlled_stop(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            make_source(folder / 'input.mp4')
            completed = []
            recording = ManagedFFmpeg(stream_url='pending', output_dir=str(folder),
                                      output_format='mp4', segment=1, taskname='test', ffmpeg=FFMPEG,
                                      segment_callback=completed.append, stable_callback=lambda _: None,
                                      advanced_video_args={
                                          'ffmpeg_stream_args': ['-re'],
                                          'ffmpeg_output_args': ['-c:v', 'mpeg4', '-g', '10', '-pix_fmt', 'yuv420p']})
            errors = []
            def execute():
                try:
                    recording.start()
                except Exception as error:
                    errors.append(error)
            worker = threading.Thread(target=execute)
            with serve(folder) as url:
                recording.stream_url = url
                worker.start()
                time.sleep(2.5)
                recording.stop()
                worker.join(timeout=15)
            self.assertFalse(worker.is_alive())
            self.assertFalse(errors)
            self.assertGreaterEqual(len(completed), 2)
            self.assertEqual(len(completed), len(set(completed)))

    def test_producer_barrier_follows_stop_once(self):
        producer = object.__new__(SingleSessionDownload)
        producer.external_stop_event = threading.Event()
        producer.external_stop_event.set()
        producer.window = SimpleNamespace(live_since=None)
        order = []
        producer.stop_once = lambda wait: order.append(('stopped', wait))
        producer._pipeSend = lambda event, msg, data: order.append((event, data))
        producer.start_helper()
        lifecycle = [item for item in order if item[0] != 'diagnostic']
        self.assertEqual(lifecycle[0], ('stopped', True))
        self.assertEqual(order[-1][0], 'producer_done')

    @unittest.skipUnless(Path(FFMPEG).is_file(), '需要 FFmpeg')
    def test_session_real_video_and_ass_finish_before_barrier(self):
        import yaml
        from DMR.utils import ToolsList, StreamerInfo
        from DMR.Downloader.Danmaku.danmaku import DanmakuDownloader
        from DMR.utils import SimpleDanmaku
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            make_source(folder / 'input.mp4')
            control, events = threading.Event(), queue.Queue()
            class LocalDanmaku(DanmakuDownloader):
                def start_dmc(self):
                    self.dmwriter.add(SimpleDanmaku(time=0.1, uname='tester', content='测试弹幕', dtype='danmaku'))
                    while not self.stoped:
                        time.sleep(0.02)
            with serve(folder) as url:
                api = MagicMock()
                api.Onair.return_value = True
                api.GetRoomInfo.return_value = {'title': 'test'}
                api.GetStreamerInfo.return_value = StreamerInfo(name='tester', platform='bilibili', room_id='1')
                api.GetStreamHeader.return_value = {'User-Agent': 'offline-test'}
                api.api_class.get_stream_urls.return_value = [
                    {'quality': 10000, 'stream_type': 'http-avc', 'stream_url': url}]
                config = yaml.safe_load((ROOT / 'DMR/Config/default.yml').read_text(encoding='utf-8'))['download_args']['live']
                config.update(url='https://live.bilibili.com/1', output_dir=str(folder / 'record'),
                              output_name='segment-{GROUP_ID}-{SEGMENT_ID}', output_format='mp4', segment=1,
                              taskname='test', send_queue=events, external_stop_event=control,
                              ffmpeg_downloader_class=ManagedFFmpeg, engine='ffmpeg')
                config['advanced_video_args'].update(ffmpeg_stream_args=['-re'], ffmpeg_output_args=[],
                                                     min_video_duration=0, min_video_size=0)
                config['advanced_dm_args']['dm_file_min_time'] = 0
                ToolsList.set('ffmpeg', FFMPEG)
                ToolsList.set('ffprobe', FFPROBE)
                with patch('DMR.Downloader.stream_downloader.LiveAPI', return_value=api), \
                     patch('DMR.Downloader.stream_downloader.DanmakuDownloader', LocalDanmaku):
                    producer = SingleSessionDownload(initial_wait=10, offline_grace=3, max_record=10, **config)
                    thread = producer.start()
                    time.sleep(2.5)
                    control.set()
                    thread.join(timeout=20)
                    self.assertFalse(thread.is_alive())
                messages = []
                while not events.empty():
                    messages.append(events.get())
                self.assertEqual(messages[-1]['event'], 'producer_done')
                segments = [m['data'] for m in messages if m['event'] == 'livesegment']
                self.assertGreaterEqual(len(segments), 2)
                self.assertEqual(len(segments), len({v.path for v in segments}))
                for video in segments:
                    self.assertTrue(Path(video.path).is_file())
                    self.assertIn('[Events]', Path(video.dm_file_id).read_text(encoding='utf-8'))

    @unittest.skipUnless(Path(FFMPEG).is_file(), '需要 FFmpeg')
    def test_real_render_and_backup_pipeline(self):
        from datetime import datetime
        import yaml
        from colab_support.core import BackupWorker, Coordinator
        from DMR.Render import Render
        from DMR.Downloader.Danmaku.asswriter import AssWriter
        from DMR.utils import PipeMessage, VideoInfo, SimpleDanmaku
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'run'
            root.mkdir()
            source, ass = root / 'input.mp4', root / 'input.ass'
            make_source(source)
            config = yaml.safe_load((ROOT / 'DMR/Config/default.yml').read_text(encoding='utf-8'))
            writer = AssWriter(description='offline', width=160, height=120, **config['download_args']['live'])
            writer.open(str(ass))
            writer.add(SimpleDanmaku(time=0, uname='test', content='测试', dtype='danmaku'))
            events = queue.Queue()
            with patch.object(Render, 'load_failed_tasks'):
                renderer = Render((events, queue.Queue()), nrenders=1)
            render_args = config['render_args']['dmrender']
            render_args.update(hwaccel_args=[], vencoder='libx264', vencoder_args=['-preset', 'ultrafast'],
                               aencoder='copy', aencoder_args=[], output_resize=None, ffmpeg=FFMPEG)
            def submit(rid, video, output):
                renderer.add_task(PipeMessage('colab', 'render', 'newtask', request_id=rid, data={
                    'mode': 'dmrender', 'video': video, 'output': str(output), 'args': render_args}))
            backup = BackupWorker(events, retry_wait=0)
            coordinator = Coordinator(root, Path(d) / 'drive', backup, submit)
            video = VideoInfo(path=str(source), dm_file_id=str(ass), group_id='session', segment_id=1,
                              duration=5, ctime=datetime.now(), resolution=(160, 120))
            coordinator.handle({'source': 'downloader', 'event': 'livesegment', 'data': video})
            coordinator.handle({'source': 'downloader', 'event': 'producer_done', 'data': {'reason': 'live_end'}})
            deadline = time.monotonic() + 30
            while not coordinator.drained() and time.monotonic() < deadline:
                coordinator.handle(events.get(timeout=15))
            self.assertTrue(coordinator.drained())
            coordinator.finish()
            while coordinator.pending_backup:
                coordinator.handle(events.get(timeout=15))
            renderer.render_executors.shutdown(wait=True)
            backup.close()
            self.assertEqual(coordinator.manifest['status'], 'success')
            self.assertTrue((Path(d) / 'drive/rendered/session-1.mp4').is_file())
            self.assertEqual(json.loads((Path(d) / 'drive/manifest.json').read_text(encoding='utf-8'))['status'], 'success')

    def test_supervisor_timeout_kills_process_group_and_marks_failure(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'STOP').touch()
            args = SimpleNamespace(run_dir=root, initial_wait=1, max_record=1, drain_timeout=1)
            process = MagicMock(pid=12345)
            # 两次循环后超时，finally 看到已退出。
            process.poll.side_effect = [None, None, 2]
            clock = iter([0, 0, 0, 0, 2, 2])
            with patch('colab_support.cli.sys.platform', 'linux'), \
                 patch('colab_support.cli.subprocess.Popen', return_value=process), \
                 patch('colab_support.cli.time.monotonic', side_effect=lambda: next(clock)), \
                 patch('colab_support.cli.time.sleep'), \
                 patch('colab_support.cli.os.killpg', create=True) as kill, \
                 patch('colab_support.cli.signal.SIGKILL', 9, create=True):
                result = supervise(args)
            self.assertEqual(result, 2)
            kill.assert_called_once_with(12345, 9)
            manifest = json.loads((root / 'manifest.json').read_text())
            self.assertEqual(manifest['status'], 'failed')
            self.assertIn('drain_timeout', manifest['errors'])


if __name__ == '__main__':
    unittest.main()
