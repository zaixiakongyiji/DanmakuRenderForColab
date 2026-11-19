"""内存与资源生命周期回归；仅使用临时文件、模拟后端和本地 Python 子进程。"""
import gc
import json
import queue
import sys
import tempfile
import threading
import tracemalloc
import unittest
import weakref
from pathlib import Path
from unittest.mock import Mock, patch

from DMR.Cleaner import Cleaner
from DMR.Render import Render
from DMR.Render.dmrender import DmRender
from DMR.Render.ffmpeg import RawFFmpegRender
from DMR.Uploader import Uploader
from DMR.Downloader.Danmaku.asswriter import AssWriter
from DMR.utils import PipeMessage, SuperChatDanmaku, VideoInfo
from DMR.utils.process import LOG_TAIL_BYTES, iter_process_log, read_log_tail
from colab_support.core import Coordinator


class MemoryLifecycleTests(unittest.TestCase):
    def renderer(self):
        with patch.object(Render, 'load_failed_tasks'):
            result = Render((queue.Queue(), queue.Queue()))
        result.save_failed_tasks = Mock()
        result.logger = Mock()
        self.addCleanup(result.stop)
        return result

    @patch('colab_support.core.atomic_json')
    def test_checkpoint_coalesces_until_ack_and_flushes_latest_state(self, persist):
        with tempfile.TemporaryDirectory() as directory:
            backup = Mock()
            c = Coordinator(Path(directory), Path(directory) / 'drive', backup, Mock())
            for index in range(500):
                c.manifest['revision'] = index
                c.checkpoint()
            self.assertEqual(backup.submit.call_count, 1)
            self.assertEqual(len(c.pending_backup), 1)
            # 此项验证队列状态机；实际磁盘持久化由 test_colab 的整合测试覆盖。
            self.assertEqual(persist.call_count, 500)
            self.assertEqual(persist.call_args.args[1]['revision'], 499)
            first = next(iter(c.pending_backup))
            c.handle({'source': 'backup', 'event': 'end', 'request_id': first, 'data': {}})
            self.assertEqual(backup.submit.call_count, 2)
            latest = json.loads(backup.submit.call_args.kwargs['content'])
            self.assertEqual(latest['revision'], 499)
            c.handle({'source': 'backup', 'event': 'end',
                      'request_id': next(iter(c.pending_backup)), 'data': {}})
            self.assertFalse(c.pending_backup)
            self.assertEqual(backup.submit.call_count, 2)

    def test_checkpoint_failure_is_recorded_without_infinite_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            backup = Mock()
            c = Coordinator(directory, Path(directory) / 'drive', backup, Mock())
            c.checkpoint()
            c.checkpoint()
            for _ in range(2):
                c.handle({'source': 'backup', 'event': 'error',
                          'request_id': next(iter(c.pending_backup)), 'data': {'error': 'offline'}})
            self.assertIn('backup_failed:metadata:checkpoint', c.manifest['errors'])
            self.assertFalse(c.pending_backup)
            self.assertEqual(backup.submit.call_count, 2)

    def test_render_exception_stops_backend_and_releases_registry(self):
        render = self.renderer()
        backend = Mock()
        backend.render_one.side_effect = RuntimeError('synthetic failure')
        with tempfile.TemporaryDirectory() as directory, patch('DMR.Render.dmrender.DmRender', return_value=backend):
            for index in range(8):
                task = {'uuid': str(index), 'source': 'test', 'request_id': str(index),
                        'mode': 'dmrender', 'args': {}, 'video': VideoInfo(path='synthetic.mp4'),
                        'output': str(Path(directory) / 'out.mp4')}
                render._render_subprocess(task)
                self.assertEqual(render.send_queue.get_nowait().event, 'error')
        self.assertFalse(render._render_class)
        self.assertEqual(backend.stop.call_count, 8)
        self.assertEqual(len(render.failed_tasks), 8)

    def test_stopped_plugins_release_blocked_monitors(self):
        for plugin_class in (Render, Uploader, Cleaner):
            with self.subTest(plugin=plugin_class.__name__):
                with patch.object(plugin_class, 'load_failed_tasks', create=True):
                    plugin = plugin_class((queue.Queue(), queue.Queue()))
                plugin.save_failed_tasks = Mock()
                plugin.start()
                monitor = plugin._piperecvprocess
                plugin.start()
                self.assertIs(plugin._piperecvprocess, monitor)
                plugin.stop()
                plugin.stop()
                self.assertFalse(monitor.is_alive())
                with self.assertRaises(RuntimeError):
                    plugin.add_task(PipeMessage('test', 'unused', 'newtask', data={}))
                reference = weakref.ref(plugin)
                del plugin
                gc.collect()
                self.assertIsNone(reference())

    def test_cleaner_cancels_delayed_jobs(self):
        cleaner = Cleaner((queue.Queue(), queue.Queue()))
        self.addCleanup(cleaner.stop)
        cleaner._clean_subprocess = Mock()
        cleaner.add_task(PipeMessage('test', 'cleaner', 'newtask', data={
            'method': 'delete', 'delay': 3600, 'files': []}))
        timers = list(cleaner._timers)
        cleaner.stop()
        for timer in timers:
            timer.join(timeout=2)
            self.assertFalse(timer.is_alive())
        self.assertFalse(cleaner._timers)
        cleaner._clean_subprocess.assert_not_called()

    def test_queued_render_does_not_start_after_stop(self):
        render = self.renderer()
        render.stop()
        with patch('DMR.Render.dmrender.DmRender') as backend:
            render._render_subprocess({'uuid': 'cancelled'})
        backend.assert_not_called()

    def test_stop_before_process_creation_prevents_late_spawn(self):
        raw = RawFFmpegRender()
        raw.stop()
        with patch('DMR.Render.ffmpeg.subprocess.Popen') as process:
            status, _ = raw.call_ffmpeg(['unused'])
        self.assertFalse(status)
        process.assert_not_called()

    def test_raw_process_is_reaped_if_graceful_stop_fails(self):
        raw = RawFFmpegRender()
        proc = raw.render_proc = Mock()
        proc.poll.return_value = None
        proc.stdin.closed = False
        proc.wait.side_effect = [TimeoutError(), 0]
        raw.stop()
        proc.kill.assert_called_once()
        self.assertEqual(proc.wait.call_count, 2)

    def test_recording_pipes_do_not_allocate_hundred_megabyte_buffers(self):
        from DMR.Downloader.ffmpeg import FFmpegDownloader
        recording = FFmpegDownloader(stream_url='synthetic', output_dir='.', output_format='mp4',
                                      ffmpeg=sys.executable)
        recording.raw_name = 'synthetic.mp4'
        # Python 会拒绝 FFmpeg 参数并立即退出；这里只测量实际管道分配，不录制媒体。
        tracemalloc.start()
        try:
            recording.start_ffmpeg()
            recording.ffmpeg_proc.wait(timeout=5)
            recording.ffmpeg_monitor_proc.join(timeout=5)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
            if recording.ffmpeg_proc is not None:
                if recording.ffmpeg_proc.poll() is None:
                    recording.ffmpeg_proc.kill()
                    recording.ffmpeg_proc.wait(timeout=5)
                recording.ffmpeg_proc.stdin.close()
                recording.ffmpeg_proc.stdout.close()
        self.assertFalse(recording.ffmpeg_monitor_proc.is_alive())
        self.assertLess(peak, 2 * 1024 * 1024)

    def test_stop_racing_render_launch_cannot_spawn_after_stop(self):
        render = self.renderer()
        raw = RawFFmpegRender()
        entered, release = threading.Event(), threading.Event()
        backend = Mock()
        def run(**kwargs):
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError('test synchronization timed out')
            return raw.call_ffmpeg(['unused'])
        backend.render_one.side_effect = run
        backend.stop.side_effect = raw.stop
        with tempfile.TemporaryDirectory() as directory, \
             patch('DMR.Render.dmrender.DmRender', return_value=backend), \
             patch('DMR.Render.ffmpeg.subprocess.Popen') as process:
            task = {'uuid': 'race', 'source': 'test', 'request_id': 'race', 'mode': 'dmrender',
                    'args': {}, 'video': VideoInfo(path='synthetic.mp4'),
                    'output': str(Path(directory) / 'out.mp4')}
            worker = threading.Thread(target=render._render_subprocess, args=(task,))
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=3))
                render.stop()
            finally:
                release.set()
                worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            process.assert_not_called()
        self.assertFalse(render._render_class)

    def test_upload_bvid_is_detected_before_large_log_tail(self):
        from DMR.Uploader.biliuprs import biliuprs
        uploader = object.__new__(biliuprs)
        uploader.debug = False
        uploader.logger = Mock()
        uploader._upload_procs = {}
        def emit(**kwargs):
            stream = kwargs['logfile']
            stream.write(b'{"bvid":"BV1234567890"}\n')
            for _ in range(32):
                stream.write(b'x' * LOG_TAIL_BYTES)
            return stream, []
        uploader.call_biliuprs = emit
        self.assertEqual(uploader.upload_once('synthetic.mp4'), (True, 'BV1234567890'))

    def test_large_process_logs_keep_memory_bounded_and_preserve_exit_semantics(self):
        code = 'import sys; print("video:1kB"); print("x"*(8*1024*1024)); print("last-error"); sys.exit(int(sys.argv[1]))'
        for exit_code in (0, 1):
            with self.subTest(exit_code=exit_code):
                raw = RawFFmpegRender()
                tracemalloc.start()
                try:
                    status, info = raw.call_ffmpeg([sys.executable, '-c', code, str(exit_code)])
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                self.assertEqual(status, exit_code == 0)
                self.assertLess(peak, 2 * 1024 * 1024)
                self.assertTrue(raw.render_proc.stdin.closed)
                if exit_code:
                    self.assertLessEqual(len(info), LOG_TAIL_BYTES)
                    self.assertIn('last-error', info)

    def test_superchat_objects_are_released_and_split_resets_position(self):
        import yaml
        config = yaml.safe_load((Path(__file__).resolve().parents[1] / 'DMR/Config/default.yml').read_text(encoding='utf-8'))
        writer = AssWriter(description='audit', width=1920, height=1080, **config['download_args']['live'])
        with tempfile.TemporaryDirectory() as directory:
            writer.open(str(Path(directory) / 'first.ass'))
            references = []
            for index in range(100):
                message = SuperChatDanmaku(time=index * 30, name='test', content='test', price=1)
                references.append(weakref.ref(message))
                writer.add(message)
            del message
            gc.collect()
            self.assertTrue(all(ref() is None for ref in references))
            writer.open(str(Path(directory) / 'second.ass'))
            writer.add(SuperChatDanmaku(time=0, name='test', content='test', price=1))
            self.assertEqual(writer._super_chat_state, 1)

    def test_probe_defaults_and_explicit_overrides(self):
        for overrides in ({}, {'probesize': 100000000, 'analyzeduration': 20000000}):
            render = DmRender([], 'libx264', [], 'copy', [], None,
                              advanced_render_args=overrides, ffmpeg='unused')
            render.raw_ffmpeg.call_ffmpeg = Mock(return_value=(True, 'video:1kB'))
            render.render_helper('input.mp4', 'input.ass', 'output.mp4')
            args = render.raw_ffmpeg.call_ffmpeg.call_args.args[0]
            self.assertEqual(args[args.index('-probesize') + 1], str(overrides.get('probesize', 50000000)))
            self.assertEqual(args[args.index('-analyzeduration') + 1], str(overrides.get('analyzeduration', 15000000)))

    def test_log_tail_reads_only_bounded_suffix(self):
        import io
        stream = io.BytesIO(b'x' * (LOG_TAIL_BYTES * 3) + b'last-error')
        self.assertEqual(len(read_log_tail(stream)), LOG_TAIL_BYTES)
        self.assertTrue(read_log_tail(stream).endswith('last-error'))
        self.assertTrue(all(len(line) <= LOG_TAIL_BYTES + 64 for line in iter_process_log(stream)))

    def test_log_markers_survive_long_line_chunk_boundary(self):
        import io
        stream = io.BytesIO(b'x' * (LOG_TAIL_BYTES - 3) + b'video:1kB\n')
        self.assertTrue(any('video:1kB' in line for line in iter_process_log(stream)))

    def test_stop_callback_failure_does_not_abort_other_cleanup(self):
        render = self.renderer()
        bad, good = Mock(), Mock()
        bad.stop.side_effect = RuntimeError('synthetic stop failure')
        render._render_class = {'bad': bad, 'good': good}
        render.stop()
        good.stop.assert_called_once()
        self.assertTrue(render.render_executors._shutdown)


if __name__ == '__main__':
    unittest.main()
