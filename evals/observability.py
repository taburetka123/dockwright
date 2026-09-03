from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


class LocalTraceWriter:

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", encoding="utf-8")
        self.count = 0

    def write(self, record: dict) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(), **record}
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()
        self.count += 1

    def close(self) -> None:
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class LangfuseTracer:

    def __init__(self, run_id: str):
        self.enabled = False
        self._client = None
        self._run_id = run_id
        self._reason = ""

        pub = os.environ.get("LANGFUSE_PUBLIC_KEY")
        sec = os.environ.get("LANGFUSE_SECRET_KEY")
        if not (pub and sec):
            self._reason = "no LANGFUSE_PUBLIC_KEY/SECRET_KEY in env"
            return
        try:
            from langfuse import Langfuse  # type: ignore
        except ImportError:
            self._reason = "langfuse package not installed (pip install langfuse)"
            return
        try:
            self._client = Langfuse(
                public_key=pub,
                secret_key=sec,
                host=os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"),
            )
            self.enabled = True
        except Exception as exc:  # pragma: no cover - network/config dependent
            self._reason = f"Langfuse init failed: {exc}"

    @property
    def status(self) -> str:
        return "enabled" if self.enabled else f"disabled ({self._reason})"

    def record(self, *, case_id: str, model: str, prompt: str, output: str,
               metadata: dict, usage: dict | None) -> None:
        if not self.enabled:
            return
        try:  # pragma: no cover - exercised only with real keys
            self._client.generation(
                name="verifier-review",
                model=model,
                input=prompt,
                output=output,
                metadata={"run_id": self._run_id, "case_id": case_id, **metadata},
                usage_details=usage or {},
            )
        except Exception:
            pass

    def flush(self) -> None:
        if self.enabled and self._client is not None:  # pragma: no cover
            try:
                self._client.flush()
            except Exception:
                pass
