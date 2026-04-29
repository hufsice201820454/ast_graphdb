"""
Module: CsSourceCollector (모듈 ①)
역할: 로컬 디렉토리에서 .cs 파일을 재귀 탐색하여 {파일경로: 소스코드} 딕셔너리를 반환한다.
"""
import logging
from pathlib import Path

from config.settings import CsCollectorConfig

logger = logging.getLogger(__name__)


class CsSourceCollector:
    """
    C# 소스파일 수집기.

    base_path 디렉토리를 재귀 탐색하여
    모든 .cs 파일을 읽고 { 파일경로(str) → 소스코드(str) } 딕셔너리를 반환합니다.
    Java와 달리 Maven/Gradle 구조 가정 없이 base_path 전체를 탐색합니다.
    """

    def collect(self, config: CsCollectorConfig) -> dict[str, str]:
        if config.mode != "local":
            raise NotImplementedError(
                f"지원하지 않는 수집 모드: {config.mode}. 현재 'local' 모드만 지원합니다."
            )
        return self._collect_local(config.base_path, config.include_test, config.file_encoding)

    def _collect_local(
        self,
        base_path: str,
        include_test: bool = False,
        encoding: str = "utf-8",
    ) -> dict[str, str]:
        root = Path(base_path).resolve()
        if not root.exists():
            raise FileNotFoundError(f"지정한 경로가 존재하지 않습니다: {base_path}")

        sources: dict[str, str] = {}
        skipped = 0

        for cs_file in root.rglob("*.cs"):
            rel_path = str(cs_file.relative_to(root)).replace("\\", "/")

            if not include_test and self._is_test_path(rel_path):
                skipped += 1
                continue

            source = self._read_file(cs_file, encoding)
            if source is not None:
                sources[rel_path] = source

        logger.info(
            "수집 완료 — 대상: %d개 파일 / 테스트 제외: %d개",
            len(sources),
            skipped,
        )
        return sources

    @staticmethod
    def _is_test_path(rel_path: str) -> bool:
        lower = rel_path.lower()
        return (
            "/test/" in lower
            or lower.startswith("test/")
            or "/tests/" in lower
            or lower.startswith("tests/")
        )

    @staticmethod
    def _read_file(path: Path, encoding: str) -> str | None:
        try:
            return path.read_text(encoding=encoding, errors="replace")
        except Exception as exc:
            logger.warning("파일 읽기 실패: %s — %s", path, exc)
            return None
