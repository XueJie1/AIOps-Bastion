"""冒烟测试: 验证包可导入、版本号正确。CI 门禁的最低门槛。"""
import aiops_bastion


def test_package_importable():
    assert aiops_bastion.__version__ == "0.1.0"


def test_python_version():
    import sys
    # 设计要求 3.11+, spike 实测 3.14 可用
    assert sys.version_info >= (3, 11), f"需要 Python 3.11+, 当前 {sys.version}"
