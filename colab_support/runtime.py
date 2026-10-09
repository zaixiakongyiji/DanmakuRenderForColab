"""运行中的 DMR 适配与有界生命周期。"""
import copy
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .core import BackupWorker, Coordinator, atomic_json
from .cli import ROOT, preflight, prepare_cookie


def run(args):
    # 原有库可能打印签名流 URL 或完整配置，首版只输出白名单事件日志。
    library_log = logging.getLogger('DMR')
    library_log.handlers = [logging.NullHandler()]
    library_log.propagate = False
    library_log.setLevel(logging.CRITICAL + 1)
    try:
        preflight(args)
    except Exception as error:
        print(str(error), flush=True)  # 预检不调用直播 API，不含 Cookie 内容。
        atomic_json(args.run_dir / 'manifest.json', {
            'status': 'failed', 'errors': ['preflight:' + type(error).__name__]})
        return 1

    import yaml
    from DMR.Render import Render
    from DMR.utils import PipeMessage, ToolsList
    from .adapters import ManagedFFmpeg, SingleSessionDownload

    events = queue.Queue()
    stop_event = threading.Event()
    cookie = prepare_cookie(args.cookie, args.run_dir / 'private')
    with (ROOT / 'DMR/Config/default.yml').open(encoding='utf-8') as f:
        defaults = yaml.safe_load(f)
    os.chdir(args.run_dir)
    (args.run_dir / '.temp').mkdir(exist_ok=True)
    (args.run_dir / 'source').mkdir(exist_ok=True)
    (args.run_dir / 'rendered').mkdir(exist_ok=True)
    ToolsList.set('ffmpeg', shutil.which('ffmpeg'))
    ToolsList.set('ffprobe', shutil.which('ffprobe'))

    render_args = copy.deepcopy(defaults['render_args']['dmrender'])
    render_args.update(hwaccel_args=[], vencoder='h264_nvenc', vencoder_args=['-b:v', '15M'],
                       aencoder='copy', aencoder_args=[], output_resize=None,
                       advanced_render_args={}, ffmpeg=shutil.which('ffmpeg'))
    class ColabRender(Render):
        def _render_subprocess(self, task):
            self._pipeSend('start', '', target=task['source'], request_id=task['request_id'])
            super()._render_subprocess(task)

    renderer = ColabRender((events, queue.Queue()), nrenders=1)
    # 直接 add_task，不启动队列监控线程，也不加载宿主插件。
    def submit(request_id, video, output):
        renderer.add_task(PipeMessage('colab', 'render', 'newtask', request_id=request_id, data={
            'mode': 'dmrender', 'video': video, 'output': str(output), 'args': render_args}))

    backup = BackupWorker(events)
    coordinator = Coordinator(args.run_dir, args.drive_root / 'DMRColab/runs' / args.run_dir.name,
                              backup, submit)
    try:
        revision = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                                  capture_output=True, text=True, timeout=10)
        coordinator.manifest['source_commit'] = revision.stdout.strip() if revision.returncode == 0 else 'unknown'
    except Exception:
        coordinator.manifest['source_commit'] = 'unknown'
    coordinator.manifest['settings'] = {'room_url': args.url, 'segment_seconds': args.segment,
                                      'max_record_seconds': args.max_record, 'encoder': 'h264_nvenc',
                                      'font': 'Noto Sans CJK SC'}
    coordinator.checkpoint()
    dl = copy.deepcopy(defaults['download_args']['live'])
    dl.update(url=args.url, output_dir=str(args.run_dir / 'source'), segment=args.segment,
              output_name='segment-{GROUP_ID}-{SEGMENT_ID}', output_format='mp4',
              font='Noto Sans CJK SC', engine='ffmpeg', debug=False,
              external_stop_event=stop_event, ffmpeg_downloader_class=ManagedFFmpeg)
    dl['stream_option'] = {'bili_watch_cookies': str(cookie)}
    dl['advanced_video_args'].update(min_video_size=0, min_video_duration=0)
    dl['advanced_dm_args'].update(dm_file_min_time=0)
    producer = SingleSessionDownload(taskname='colab', send_queue=events,
                                    initial_wait=args.initial_wait, offline_grace=args.offline_grace,
                                    max_record=args.max_record, **dl)
    producer_thread = producer.start()
    first_live = None
    drain_started = None
    forced_reason = None
    stop_path = args.run_dir / 'STOP'
    audit = (args.run_dir / 'events.log').open('w', encoding='utf-8')

    def begin_drain(reason):
        nonlocal drain_started, forced_reason
        if drain_started is None:
            drain_started = time.monotonic()
            coordinator.manifest['draining_since'] = time.time()
            coordinator.manifest['status'] = 'draining'
            forced_reason = reason
            if reason not in ('live_end', 'record_limit', 'requested'):
                coordinator.error(reason)
            coordinator.checkpoint()
        stop_event.set()

    try:
        while not coordinator.drained():
            if stop_path.exists():
                begin_drain('requested')
            free_gib = shutil.disk_usage(args.run_dir).free / 1024**3
            coordinator.manifest['disk_free_gib'] = round(free_gib, 3)
            coordinator.manifest['minimum_disk_free_gib'] = min(
                coordinator.manifest.get('minimum_disk_free_gib', free_gib), free_gib)
            coordinator.manifest['peak_pending_renders'] = max(
                coordinator.manifest.get('peak_pending_renders', 0), len(coordinator.pending_render))
            if free_gib < args.min_free_gib:
                begin_drain('disk_low')
            if first_live is not None and time.monotonic() - first_live >= args.max_record:
                begin_drain('record_limit')
            if drain_started is not None and time.monotonic() - drain_started > args.drain_timeout:
                coordinator.error('drain_timeout')
                coordinator.manifest['status'] = 'failed'
                atomic_json(args.run_dir / 'manifest.json', coordinator.manifest)
                # 外层监督器会终止整个进程组，包括挂起的 IO/FFmpeg。
                stop_path.touch()
                while True:
                    time.sleep(1)
            try:
                message = events.get(timeout=0.5)
            except queue.Empty:
                if not producer_thread.is_alive() and not coordinator.producer_done:
                    begin_drain('producer_barrier_missing')
                continue
            source, event = message['source'], message['event']
            if source == 'downloader' and event == 'livestart' and first_live is None:
                first_live = time.monotonic()
            if source == 'downloader' and event == 'producer_done':
                if forced_reason:
                    message['data']['reason'] = forced_reason
                begin_drain(message['data']['reason'])
            if source != 'backup':
                audit.write(f'{time.time():.3f} {source}/{event}\n')
                audit.flush()
            coordinator.handle(message)
            if source == 'downloader' and event == 'quality':
                print('取得直播流：', message['data'], flush=True)
                for warning in coordinator.manifest['warnings']:
                    print(warning, flush=True)
        producer_thread.join()
        renderer.render_executors.shutdown(wait=True)
        audit.close()
        # 只备份已冻结的白名单事件日志，原有库日志和凭据文件不会进入备份。
        coordinator.pending_backup['audit'] = (None, 'log')
        backup.submit('audit', args.run_dir / 'events.log', coordinator.backup_dir / 'events.log')
        while coordinator.pending_backup:
            coordinator.handle(events.get(timeout=args.drain_timeout))
        segments = list(coordinator.manifest['segments'].values())
        coordinator.manifest['counts'] = {
            'source': len(segments), 'danmaku': len(segments),
            'rendered': sum(s['render'] == 'success' for s in segments),
            'backed_up_files': sum(v['status'] == 'success' for s in segments for v in s['backup'].values())}
        coordinator.finish()
        while coordinator.pending_backup:
            coordinator.handle(events.get(timeout=args.drain_timeout))
        if coordinator.manifest['errors']:
            coordinator.manifest['status'] = 'failed'
        atomic_json(args.run_dir / 'manifest.json', coordinator.manifest)
        backup.close()
        return 0 if coordinator.manifest['status'] == 'success' else 1
    except BaseException as error:
        stop_event.set()
        coordinator.error('worker_interrupted:' + type(error).__name__)
        coordinator.manifest['status'] = 'failed'
        atomic_json(args.run_dir / 'manifest.json', coordinator.manifest)
        audit.close()
        # 不让 Python 的线程池退出钩子等待不受控第三方线程。
        # 监督器检测非零退出后负责清理同一进程组的遗留子进程。
        os._exit(1)
