"""Colab 专用入口，不导入 main.py 或实例化 DanmakuRender。"""
from colab_support.cli import main

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        # 主流程错误不回显原始异常，以免第三方异常夹带认证或签名流地址。
        print('Colab 入口失败：' + type(error).__name__ + '；请检查参数、预检和本地 manifest.json。')
        raise SystemExit(1)
