"""
Module: SourceCollector (모듈 ①)
역할: 로컬 디렉토리에서 .java 파일을 재귀 탐색하여 {파일경로: 소스코드} 딕셔너리를 반환한다.
"""
import os
import logging
from pathlib import Path

from config.settings import CollectorConfig

logger = logging.getLogger(__name__)


class SourceCollector:
    """
    Java 소스파일 수집기.

    역할:
        설정에서 지정한 base_path 디렉토리를 재귀적으로 탐색하여
        모든 .java 파일을 읽고, { 파일경로(str) → 소스코드(str) } 딕셔너리를 반환합니다.

    이 딕셔너리는 다음 단계인 IncrementalTracker로 전달됩니다.

    지원 모드:
        현재 "local" 모드(로컬 디렉토리 탐색)만 지원합니다.
        추후 "api", "git" 모드를 추가할 수 있도록 mode 분기 구조를 갖추고 있습니다.
    """

    # Maven/Gradle 표준 소스 디렉토리 후보 (우선 순위 순)
    # base_path 안에 이 경로들이 있으면 해당 경로를 탐색 루트로 사용합니다.
    _SOURCE_DIRS = [
        "src/main/java",
        "src/java",
        "src",
        ".",
    ]

    def collect(self, config: CollectorConfig) -> dict[str, str]:
        """
        설정(CollectorConfig)을 받아 소스 수집을 실행하고 결과 딕셔너리를 반환합니다.

        config.mode가 "local"이 아니면 NotImplementedError를 발생시킵니다.
        현재는 _collect_local()을 호출하는 단순한 분기 역할을 합니다.

        반환값: { "src/main/java/com/example/OrderService.java": "public class OrderService {...}" }
        """
        if config.mode != "local":
            raise NotImplementedError(
                f"지원하지 않는 수집 모드: {config.mode}. "
                "현재 'local' 모드만 지원합니다."
            )
        return self._collect_local(config.base_path, config.include_test, config.file_encoding)

    def _collect_local(
        self,
        base_path: str,
        include_test: bool = False,
        encoding: str = "utf-8",
    ) -> dict[str, str]:
        """
        base_path 아래에서 .java 파일을 재귀 탐색하여 { 경로 → 소스코드 } 딕셔너리를 반환합니다.

        동작 순서:
            1. base_path를 절대경로로 변환하고 존재 여부를 확인합니다.
            2. Maven/Gradle 표준 구조(src/main/java 등)가 있으면 그 하위를 탐색합니다.
               없으면 base_path 전체를 탐색합니다.
            3. 모든 .java 파일을 rglob("*.java")로 재귀 탐색합니다.
            4. include_test=False(기본값)이면 테스트 경로(src/test 등)를 건너뜁니다.
            5. 각 파일을 읽어 딕셔너리에 추가합니다. 읽기 실패한 파일은 건너뜁니다.

        파일 경로는 base_path 기준 상대경로로 저장되며, 구분자는 '/'로 통일합니다.
        (Windows의 '\\' → '/')
        """
        root = Path(base_path).resolve()
        if not root.exists():
            raise FileNotFoundError(f"지정한 경로가 존재하지 않습니다: {base_path}")

        # 소스 루트 결정
        scan_root = self._resolve_scan_root(root)
        logger.info("소스 탐색 루트: %s", scan_root)

        sources: dict[str, str] = {}
        skipped = 0

        for java_file in scan_root.rglob("*.java"):
            rel_path = str(java_file.relative_to(root)).replace("\\", "/")

            # 테스트 경로 제외 (옵션)
            if not include_test and self._is_test_path(rel_path):
                skipped += 1
                continue

            source = self._read_file(java_file, encoding)
            if source is not None:
                sources[rel_path] = source

        logger.info(
            "수집 완료 — 대상: %d개 파일 / 테스트 제외: %d개",
            len(sources),
            skipped,
        )
        return sources

    def _resolve_scan_root(self, root: Path) -> Path:
        """
        Maven/Gradle 표준 소스 디렉토리가 있으면 해당 경로를, 없으면 root 자체를 반환합니다.

        _SOURCE_DIRS 목록을 순서대로 확인하여 실제로 존재하는 첫 번째 경로를 사용합니다.
        예: root = "C:/project"이고 "C:/project/src/main/java"가 존재하면
            그 경로를 탐색 루트로 사용합니다.
        """
        for candidate in self._SOURCE_DIRS:
            candidate_path = root / candidate
            if candidate_path.is_dir() and candidate_path != root:
                return candidate_path
        return root

    @staticmethod
    def _is_test_path(rel_path: str) -> bool:
        """
        주어진 상대경로가 테스트 소스 디렉토리에 속하는지 판별합니다.

        판별 기준:
            - 경로에 '/test/' 가 포함된 경우       (예: src/test/java/...)
            - 경로가 'test/'로 시작하는 경우
            - 경로에 '/androidtest/' 가 포함된 경우 (Android 프로젝트)

        대소문자 구분 없이 비교합니다.
        """
        lower = rel_path.lower()
        return (
            "/test/" in lower
            or lower.startswith("test/")
            or "/androidtest/" in lower
        )

    @staticmethod
    def _read_file(path: Path, encoding: str) -> str | None:
        """
        파일을 읽어 문자열로 반환합니다. 읽기 실패 시 None을 반환합니다.

        인코딩 오류가 발생하면 errors="replace" 모드로 처리합니다.
        (잘못된 바이트를 '?' 문자로 대체하여 읽기 실패를 방지합니다.)
        그 외 예외(파일 권한 오류 등)는 경고 로그를 남기고 None을 반환합니다.
        """
        try:
            return path.read_text(encoding=encoding, errors="replace")
        except Exception as exc:
            logger.warning("파일 읽기 실패: %s — %s", path, exc)
            return None
