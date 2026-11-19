import logging
import os
import queue
import threading

from concurrent.futures import ThreadPoolExecutor
from os.path import exists, isdir, isfile, abspath, dirname
from typing import Tuple
from DMR.utils import *


class Cleaner():
    def __init__(self,
                 pipe:Tuple[queue.Queue, queue.Queue],
                 **kwargs,
                 ) -> None:
        
        self.send_queue, self.recv_queue = pipe
        self.logger = logging.getLogger('DMR.Cleaner')
        self.kwargs = kwargs
        self.stoped = True

        self._piperecvprocess = None
        self.clean_executors = ThreadPoolExecutor(max_workers=1)
        self._lock = threading.Lock()
        self._closing = False
        self._timers = set()
    
    def _pipeSend(self, event, msg, target='engine', request_id=None, dtype=None, data=None, **kwargs):
        if self.send_queue:
            msg = PipeMessage(
                source='cleaner',
                target=target,
                event=event,
                request_id=request_id,
                msg=msg,
                dtype=dtype,
                data=data,
                **kwargs,
            )
            self.send_queue.put(msg)

    def _pipeRecvMonitor(self):
        while self.stoped == False and self.recv_queue is not None:
            message:PipeMessage = self.recv_queue.get()
            if message is None:
                break
            try:
                if message.target == 'cleaner':
                   if message.event == 'newtask':
                       self.add_task(message)
            except Exception as e:
                self.logger.error(f'Message:{message} raise an error.')
                self.logger.exception(e)

    def start(self):
        if self._closing:
            raise RuntimeError('Cleaner has been stopped.')
        if self._piperecvprocess is not None and self._piperecvprocess.is_alive():
            return
        self.stoped = False
        self._piperecvprocess = threading.Thread(target=self._pipeRecvMonitor, daemon=True)
        self._piperecvprocess.start()

    def add_task(self, msg:PipeMessage):
        with self._lock:
            if self._closing:
                raise RuntimeError('Cleaner has been stopped.')
            config = msg.data
            method = config.get('method')
            if not method: return 
            task = {
                'uuid': uuid(),
                'source': msg.source,
                'request_id': msg.request_id,
                'method': config.get('method'),
                'args': config.get('args', {}),
                'files': config.get('files'),
                'config': config,
            }
            if config.get('delay', 0) > 0:
                timer = threading.Timer(config['delay'], self._submit_delayed, args=(task,))
                timer.daemon = True
                self._timers.add(timer)
                timer.start()
            else:
                self.clean_executors.submit(self._clean_subprocess, task)

    def _submit_delayed(self, task):
        with self._lock:
            self._timers.discard(threading.current_thread())
            if not self._closing:
                self.clean_executors.submit(self._clean_subprocess, task)

    def _clean_subprocess(self, task):
        try:
            method = task['method']
            clean_args = task['args']
            dst = clean_args.get('dest')
            cleaned_files = []
            for file in task['files']:
                file:FileInfo
                if not exists(file.path):
                    self.logger.warning(f'文件 {file.path} 不存在，跳过清理.')
                    continue

                self.logger.info(f'正在清理文件: {method} {file.path}.')
                src = abspath(file.path)
                if dst and not dst.startswith('*'):
                    dst = abspath(replace_keywords(dst, file, replace_invalid=True))
                    if not exists(dst):
                        self.logger.info(f'目标文件夹 {dst} 不存在，即将自动创建.')
                        os.makedirs(dst)
                
                files = [src]
                dm_file = file.get('dm_file_id')
                if dm_file and exists(dm_file):
                    self.logger.info(f'正在清理弹幕文件: {method} {dm_file}.')
                    files.append(dm_file)
                cleaned_files.extend(files)
                
                for f in files:
                    if method == 'move':
                        from .move import move
                        move.move(f, dst)
                    elif method == 'copy':
                        from .copy import copy
                        copy.copy(f, dst)
                    elif method == 'delete':
                        from .delete import delete
                        delete.delete(f)
                    elif method == 'custom':
                        from ..utils import runcmd
                        cmds = [replace_keywords(str(x), file) for x in clean_args.get('command')]
                        wait = clean_args.get('wait', True)
                        p = runcmd.runcmd(cmds, wait=wait, **clean_args.get('subprocess_kwargs', {}))
                        if wait and p.returncode != 0:
                            raise RuntimeError(f'命令执行失败: {cmds}')
                
            self._pipeSend('end', f'清理完成：{method} {cleaned_files} -> {dst}.', target=task['source'], request_id=task['request_id'])
        except Exception as e:
            self.logger.exception(e)
            self._pipeSend('error', f'清理错误 {e}.', target=task['source'], request_id=task['request_id'], dtype='Exception', data=e)
            
    def stop(self):
        with self._lock:
            self._closing = True
            self.stoped = True
            timers = list(self._timers)
            self._timers.clear()
        for timer in timers:
            timer.cancel()
        for timer in timers:
            if timer is not threading.current_thread():
                timer.join(timeout=5)
        self.recv_queue.put(None)
        monitor = self._piperecvprocess
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join(timeout=5)
        self.clean_executors.shutdown(wait=False, cancel_futures=True)
