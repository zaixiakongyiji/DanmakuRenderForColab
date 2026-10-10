"""Colab 投稿入口；配置读取无需导入 DMR。"""
from .upload_config import load_upload_config, load_upload_cookie
from .posting import PostingWorker


def prepare_identity(cookie_file, config):
    if config.get('enabled'):
        from .upload_transaction import ColabBiliUploader
        ColabBiliUploader(cookie_file, config).check_identity()
