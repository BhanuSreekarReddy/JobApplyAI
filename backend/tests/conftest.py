import os
import tempfile

# Must run before any `app` import: keeps tests away from the real data/ directory.
os.environ["JOBAPPLY_DATA_DIR"] = tempfile.mkdtemp(prefix="jobapply-test-")
os.environ["FORMFILL_HEADLESS"] = "true"
for key in ("OPENAI_API_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY", "USAJOBS_API_KEY", "SMTP_HOST"):
    os.environ[key] = ""

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.agent import ats  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_cache():
    ats.clear_cache()
    yield
    ats.clear_cache()


@pytest.fixture
def mock_http(monkeypatch):
    """Route every httpx.AsyncClient through a handler: `mock_http(lambda request: httpx.Response(...))`."""
    real = httpx.AsyncClient

    def install(handler):
        transport = httpx.MockTransport(handler)

        def factory(*args, **kwargs):
            kwargs.pop("transport", None)
            kwargs.pop("limits", None)
            return real(*args, transport=transport, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)
        return transport

    return install
