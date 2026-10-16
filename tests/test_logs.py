"""D4：錯誤處理與記錄。"""
import pytest
from streamlit.testing.v1 import AppTest

from ui.logs import get_logger, guarded, log_path


def _flush():
    for h in get_logger().handlers:
        h.flush()


def test_log_goes_to_temp_dir():
    assert "hems_test_" in str(log_path())                  # 測試不會寫入使用者的記錄檔
    get_logger("test").info("記錄測試 123")
    _flush()
    assert "記錄測試 123" in log_path().read_text(encoding="utf-8")


def test_guarded_page_shows_error_and_logs():
    def script():
        from ui.logs import guarded

        def broken():
            raise ZeroDivisionError("除以零")
        guarded("測試頁", broken)
        import streamlit as st
        st.write("其他內容照常顯示")

    at = AppTest.from_function(script)
    at.run()
    assert not at.exception                                 # 程式沒有當掉
    assert "「測試頁」發生錯誤：除以零" in at.error[0].value
    assert any("其他內容照常顯示" in m.value for m in at.markdown)
    _flush()
    text = log_path().read_text(encoding="utf-8")
    assert "測試頁 發生錯誤" in text and "ZeroDivisionError" in text   # 完整 traceback 寫入記錄


def test_guarded_does_not_swallow_rerun_or_stop():
    from streamlit.runtime.scriptrunner_utils.exceptions import StopException
    with pytest.raises(StopException):
        guarded("x", lambda: (_ for _ in ()).throw(StopException()))
    assert guarded("ok", lambda: None) is True


def test_autosave_failure_is_reported_not_fatal():
    def script():
        import streamlit as st
        from sqlalchemy.exc import OperationalError

        from ui.account import autosave

        class BrokenStore:
            def save_profile(self, *args):
                raise OperationalError("UPDATE", {}, Exception("database is locked"))

        st.session_state.update(profile_id=1, user_id=1, settings={"a": 1}, ui={}, saved_snapshot="old")
        st.session_state.result = autosave(BrokenStore())

    at = AppTest.from_function(script)
    at.run()
    assert not at.exception
    assert at.session_state.result is False
    assert at.session_state.saved_snapshot == "old"         # 下次執行會再試一次
    assert any("自動存檔失敗" in t.value for t in at.toast)
