"""现有 DMR 组件的单场运行适配，不启动宿主或投稿插件。"""
import queue
import threading
import time
from pathlib import Path

from DMR.Downloader.stream_downloader import StreamDownloadTask
from DMR.Downloader.ffmpeg import FFmpegDownloader
from DMR.utils import uuid
from .core import LiveWindow


class ManagedFFmpeg(FFmpegDownloader):
    """日志仅用于分段识别；最后分段始终由视频线程关闭后提交。"""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._stop_requested = threading.Event()
        self._process_lock = threading.RLock()
        self.thisfile = None

    def start_helper(self):
        self.stoped = False
        self.raw_name = str(Path(self.output_dir) / (
            f'recording-{uuid(8)}-Part%05d.{self.output_format}'))
        self.start_time = time.time()
        with self._process_lock:
            if self._stop_requested.is_set():
                return
            self.start_ffmpeg()
        try:
            while True:
                try:
                    line = self.msg_queue.get(timeout=0.2)
                except queue.Empty:
                    if self.ffmpeg_proc.poll() is not None and not self.ffmpeg_monitor_proc.is_alive():
                        break
                    continue
                if 'Opening' in line and "'" in line:
                    name = line.split("'")[1]
                    if not name.startswith(('http:', 'https:')) and Path(name).parent == Path(self.output_dir):
                        if self.thisfile and name != self.thisfile:
                            self.segment_callback(self.thisfile)
                        self.thisfile = name
            if self.ffmpeg_proc.returncode != 0 and not self._stop_requested.is_set():
                raise RuntimeError('ffmpeg_recording_failed')
        finally:
            self.stop()
            self.stoped = True
            self.ffmpeg_monitor_proc.join(timeout=5)
            for stream in (self.ffmpeg_proc.stdin, self.ffmpeg_proc.stdout):
                if stream is not None:
                    stream.close()
            if self.thisfile and Path(self.thisfile).is_file():
                filename, self.thisfile = self.thisfile, None
                self.segment_callback(filename)

    def stop(self):
        self._stop_requested.set()
        with self._process_lock:
            if self.ffmpeg_proc is None or self.ffmpeg_proc.poll() is not None:
                return
            try:
                self.ffmpeg_proc.stdin.write('q\n')
                self.ffmpeg_proc.stdin.flush()
                self.ffmpeg_proc.wait(timeout=10)
            except Exception:
                self.ffmpeg_proc.kill()
                self.ffmpeg_proc.wait(timeout=5)


class SingleSessionDownload(StreamDownloadTask):
    def __init__(self, *, initial_wait, offline_grace, max_record, **kwargs):
        super().__init__(**kwargs)
        self.window = LiveWindow(initial_wait, offline_grace, max_record, time.monotonic())
        self.liveapi.GetStreamURL = self._get_stream_url
        self.sess_id = uuid(8)
        self.segment_id = 1

    def _get_stream_url(self, **options):
        # 每次重连都显式禁用自动登录，避免凭据缺失时落入投稿工具。
        streams = self.liveapi.api_class.get_stream_urls(
            bili_watch_cookies=options['bili_watch_cookies'], allow_login=False)
        if not streams:
            raise RuntimeError('no_streams')
        highest = max(item['quality'] for item in streams)
        candidates = [item for item in streams if item['quality'] == highest]
        avc = [item for item in candidates if 'avc' in item['stream_type']]
        selected = (avc or candidates)[0]
        self._pipeSend('quality', '', data={
            'quality': selected['quality'], 'stream_type': selected['stream_type']})
        return selected['stream_url']

    def start_helper(self):
        reason = 'requested'
        failures = 0
        try:
            while not self.external_stop_event.is_set():
                status = self.liveapi.Onair()
                reason_now = self.window.observe(status, time.monotonic())
                if reason_now:
                    reason = reason_now
                    break
                if status is not True:
                    self.external_stop_event.wait(5)
                    continue
                self._pipeSend('livestart', '', data=self.sess_id)
                before = self.segment_id
                try:
                    self.start_once()
                except Exception as error:
                    self._pipeSend('liveerror', '', data=type(error).__name__)
                    failures += 1
                finally:
                    self.stop_once(wait=True)
                if self.segment_id > before:
                    failures = 0
                if failures >= 3:
                    reason = 'recording_retries_exhausted'
                    break
                self.external_stop_event.wait(5)
        except Exception as error:
            reason = 'downloader_failed:' + type(error).__name__
        finally:
            try:
                self.stop_once(wait=True)
            except Exception:
                reason = 'downloader_stop_failed'
            # 所有视频/弹幕线程退出后才发屏障事件，不以队列瞬间为空代替完成。
            self._pipeSend('producer_done', '', data={
                'reason': reason,
                'recording_seconds': (time.monotonic() - self.window.live_since)
                if self.window.live_since is not None else 0})
