"""用合成媒体复现重连后的帧率、时间基准及编码头差异，不读取真实录像。"""
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from colab_support.core import digest
from colab_support.merge import MergeError, merge_rendered, probe, video_encoding_args, media_command

ROOT = Path(__file__).resolve().parents[1]
FFMPEG = shutil.which('ffmpeg') or str(ROOT / 'tools/ffmpeg.exe')
FFPROBE = shutil.which('ffprobe') or str(ROOT / 'tools/ffprobe.exe')


@unittest.skipUnless(Path(FFMPEG).is_file() and Path(FFPROBE).is_file(), '需要 FFmpeg/ffprobe')
class MediaCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / '含空格的 run'
        (self.root / 'rendered').mkdir(parents=True)
        self.manifest = {'segments': {}}

    def segment(self, index, rate, timescale, *, size='160x120', fixed=False, audio=True):
        target = self.root / f'rendered/session-{index}.mp4'
        command = [FFMPEG, '-nostdin', '-y', '-v', 'error', '-f', 'lavfi',
                   '-i', f'testsrc=size={size}:rate={rate}']
        if audio:
            command += ['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000']
        command += ['-t', '2', '-c:v', 'libx264']
        if fixed:
            command += video_encoding_args('25', 'libx264')
        else:
            command += ['-pix_fmt', 'yuv420p', '-profile:v', 'main',
                        '-video_track_timescale', str(timescale)]
        command += ['-c:a', 'aac', '-movflags', '+faststart', str(target)]
        subprocess.run(command, check=True, capture_output=True, timeout=15)
        self.manifest['segments'][f'session-{index}'] = {
            'render': 'success', 'rendered_path': target.relative_to(self.root).as_posix()}
        return target

    def merge(self, **kwargs):
        return merge_rendered(self.root, self.manifest, time.monotonic() + 60,
                              min_free_gib=0, ffmpeg=FFMPEG, ffprobe=FFPROBE, **kwargs)

    def test_25_and_299_over_12_fps_are_normalized_and_originals_preserved(self):
        first = self.segment(1, '25', 12800)
        second = self.segment(2, '299/12', 19136)
        original = [digest(first), digest(second)]
        durations = [probe(path, FFPROBE, time.monotonic() + 10)[0] for path in (first, second)]
        result = self.merge()
        self.assertEqual(result['mode'], 'normalized_stream_copy')
        self.assertTrue(result['decode_verified'])
        self.assertAlmostEqual(result['duration_seconds'], sum(durations), delta=0.15)
        self.assertEqual([digest(first), digest(second)], original)
        self.assertFalse(list((self.root / 'merged').glob('normalized-*')))
        _, signature = probe(self.root / result['path'], FFPROBE, time.monotonic() + 10)
        self.assertEqual(signature[0]['r_frame_rate'], '25/1')
        self.assertEqual(signature[0]['time_base'], '1/90000')
        fields = set(result['stream_differences'][0]['fields'])
        self.assertTrue({'r_frame_rate', 'time_base', 'extradata_hash'} <= fields)

    def test_fixed_output_parameters_keep_reconnected_segments_on_fast_path(self):
        self.segment(1, '25', 12800, fixed=True)
        self.segment(2, '299/12', 19136, fixed=True)
        result = self.merge()
        self.assertEqual(result['mode'], 'stream_copy')
        self.assertEqual(result['stream_differences'], [])

    def test_resolution_change_is_reported_without_transcoding_or_overwriting(self):
        first = self.segment(1, '25', 12800)
        self.segment(2, '25', 12800, size='192x128')
        checksum = digest(first)
        with self.assertRaisesRegex(MergeError, 'merge_incompatible_streams') as caught:
            self.merge()
        self.assertIn('width', caught.exception.differences[0]['fields'])
        self.assertEqual(digest(first), checksum)
        self.assertFalse((self.root / 'merged/complete.mp4').exists())

    def test_normalization_checks_additional_disk_budget(self):
        first = self.segment(1, '25', 12800)
        second = self.segment(2, '299/12', 19136)
        free = 2 * (first.stat().st_size + second.stat().st_size)
        with patch('colab_support.merge.shutil.disk_usage', return_value=SimpleNamespace(free=free)):
            with self.assertRaisesRegex(MergeError, 'merge_disk_low'):
                self.merge()
        self.assertFalse((self.root / 'merged/complete.mp4').exists())

    def test_normalization_failure_preserves_segments_and_removes_temporary_files(self):
        first = self.segment(1, '25', 12800)
        self.segment(2, '299/12', 19136)
        checksum = digest(first)
        def expired(argv, deadline, **kwargs):
            if '-c:v' in argv:
                raise MergeError('merge_timeout')
            return media_command(argv, deadline, **kwargs)
        with patch('colab_support.merge.media_command', side_effect=expired):
            with self.assertRaisesRegex(MergeError, 'merge_timeout'):
                self.merge()
        self.assertEqual(digest(first), checksum)
        self.assertFalse(list((self.root / 'merged').glob('normalized-*')))


if __name__ == '__main__':
    unittest.main()
