import os
import platform
import signal
import sys
import subprocess
import logging
import tempfile
import threading

from .baserender import BaseRender
from os.path import exists
from DMR.utils import *
from DMR.utils.process import LOG_TAIL_BYTES, iter_process_log

class RawFFmpegRender(BaseRender):
    def __init__(self,
                 debug=False,
                 **kwargs
                 ):
        self.debug = debug
        self.logger = logging.getLogger(__name__)
        self._process_lock = threading.Lock()
        self._stop_requested = threading.Event()
        self.render_proc = None

    def call_ffmpeg(self, cmds, **kwargs):
        ffmpeg_args = [str(x) for x in cmds]
        self.logger.debug(f'ffmpeg render args: {ffmpeg_args}')

        with tempfile.TemporaryFile() as logfile:
            with self._process_lock:
                if self._stop_requested.is_set():
                    return False, 'Render stopped.'
                if self.debug:
                    self.render_proc = subprocess.Popen(
                        ffmpeg_args, stdin=sys.stdin, stdout=sys.stdout, stderr=subprocess.STDOUT, bufsize=-1)
                else:
                    self.render_proc = subprocess.Popen(
                        ffmpeg_args, stdin=subprocess.PIPE, stdout=logfile, stderr=subprocess.STDOUT, bufsize=-1)

            try:
                self.render_proc.wait()
            finally:
                if not self.debug and self.render_proc.stdin is not None:
                    self.render_proc.stdin.close()
            if self.debug:
                return self.render_proc.returncode == 0, f'FFmpeg exit code: {self.render_proc.returncode}'

            info = None
            log = ''
            for line in iter_process_log(logfile):
                log = (log + line + '\n')[-LOG_TAIL_BYTES:]
                if 'video:' in line:
                    info = line

            if info and self.render_proc.returncode == 0:
                return True, info
            else:
                return False, log
            
    def render_one(self, cmds, **kwargs):
        start_time = datetime.now()
        status, info = self.call_ffmpeg(cmds, **kwargs)
        if status:
            output_info:VideoInfo = video.copy()
            output_info.path = output
            output_info.file_id = uuid()
            output_info.size = os.path.getsize(output)
            output_info.ctime = start_time
            output_info.src_video_id = video.file_id
            return status, output_info
        else:
            return status, info
            
    def stop(self):
        self._stop_requested.set()
        with self._process_lock:
            proc = self.render_proc
        if proc is None or proc.poll() is not None:
            return
        try:
            if not self.debug and proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.write(b'q\n')
                proc.stdin.flush()
            proc.wait(timeout=5)
        except Exception as e:
            self.logger.debug(e)
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
