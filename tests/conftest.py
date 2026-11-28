"""pytest 公共配置：测试进程跳过启动自动更新检查。

AppState 的 update-check 是 daemon 线程，进程退出时若仍在网络请求中，
会与 OpenSSL finalize 竞争导致断言通过后偶发 SIGSEGV（-11）。
"""

import os

os.environ.setdefault("ORCSY_SKIP_UPDATE_CHECK", "1")
