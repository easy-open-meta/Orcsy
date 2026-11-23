"""存储 provider 回归：SigV4 对拍、Local 往返、S3 寻址、工厂分派。

运行：uv run python tests/test_storage.py
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "."))

from orcsy.storage import (
    LocalProvider,
    S3Provider,
    StorageError,
    build_provider,
    canonical_request,
    sign_request,
)


def check_local_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = LocalProvider(Path(td))
        p.put("a/b.txt", b"hello")
        assert p.get("a/b.txt") == b"hello"
        assert p.list() == ["a/b.txt"]
        p.delete("a/b.txt")
        assert p.list() == []
        try:
            p.put("../evil", b"x")
            raise AssertionError("应拒路径穿越键")
        except StorageError:
            pass
    print("Local 往返 + 穿越拒绝 ✅")


def check_build_provider_dispatch() -> None:
    class S3:
        type = "s3"
        endpoint = "https://s3.example.com"
        bucket = "bk"
        region = "us-east-1"
        access_key = "a"
        secret_key = "s"
        session_token = ""

    assert isinstance(build_provider(S3()), S3Provider)

    class Local:
        type = "local"
        dir = ""

    assert isinstance(build_provider(Local()), LocalProvider)

    class Bad:
        type = "ftp"

    try:
        build_provider(Bad())
        raise AssertionError("应拒未知类型")
    except StorageError:
        pass
    print("工厂分派 ✅")


def check_s3_url_styles() -> None:
    path = S3Provider(endpoint="https://s3.example.com:9000", bucket="bk",
                      access_key="a", secret_key="s")
    assert path._object_url("k/t.gz") == "https://s3.example.com:9000/bk/k/t.gz"
    vh = S3Provider(endpoint="", bucket="bk", region="us-west-2",
                    access_key="a", secret_key="s")
    assert vh._object_url("k/t.gz") == "https://bk.s3.us-west-2.amazonaws.com/k/t.gz"
    dotted = S3Provider(endpoint="", bucket="bk.sub", region="us-east-1",
                        access_key="a", secret_key="s")
    assert dotted._object_url("k") == "https://s3.us-east-1.amazonaws.com/bk.sub/k"
    print("S3 寻址 ✅")


def check_sigv4_canonical_request() -> None:
    """AWS 官方样例：canonical_request 的 SHA256 与文档一致。"""
    headers = {
        "host": "examplebucket.s3.amazonaws.com",
        "range": "bytes=0-9",
        "x-amz-content-sha256":
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "x-amz-date": "20130524T000000Z",
    }
    canon = canonical_request(
        "GET", "https://examplebucket.s3.amazonaws.com/test.txt", headers,
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
    h = hashlib.sha256(canon.encode()).hexdigest()
    assert h == "7344ae5b7ee6c3e7e6b0fe0640412a37625d1fbfff95c48bbb2dc43964946972", h
    print("SigV4 canonical_request 对拍 ✅")


def check_sigv4_sign_deterministic() -> None:
    now = datetime(2013, 5, 24, 0, 0, 0, tzinfo=timezone.utc)
    kw = dict(method="PUT", url="https://s3.example.com/bk/k", body=b"data",
              headers={"content-type": "application/octet-stream"},
              region="us-east-1", service="s3",
              access_key="AKIDEXAMPLE", secret_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
              now=now)
    h1 = sign_request(**kw)
    h2 = sign_request(**kw)
    assert h1 == h2
    assert h1["Authorization"].startswith(
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20130524/us-east-1/s3/aws4_request, ")
    print("SigV4 签名确定性 ✅")


if __name__ == "__main__":
    check_local_roundtrip()
    check_build_provider_dispatch()
    check_s3_url_styles()
    check_sigv4_canonical_request()
    check_sigv4_sign_deterministic()
    print("test_storage: 全部通过")