"""階段 0：確認開發環境與套件皆可正常匯入。"""
import sys


def test_python_version():
    # 專案要求 Python 3.11 以上
    assert sys.version_info >= (3, 11)


def test_packages_importable():
    import altair
    import matplotlib
    import numpy
    import pandas
    import streamlit

    for pkg in (altair, matplotlib, numpy, pandas, streamlit):
        assert pkg.__version__


def test_core_package_importable():
    import core  # noqa: F401
