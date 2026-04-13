"""
Module: IncrementalTracker (모듈 ⑥)
역할: SHA-256 해시 기반으로 변경된 파일만 선별하여 재적재. 전체 재분석 방지.
"""
import hashlib
import logging

from neo4j import Session

logger = logging.getLogger(__name__)


class IncrementalTracker:
    """
    파일 변경 여부를 SHA-256 해시로 감지하여 변경된 파일만 반환하는 클래스.

    동작 원리:
        Neo4j의 :JavaFile 노드에는 이전에 적재할 때 계산한 contentHash가 저장되어 있습니다.
        현재 파일을 다시 읽어 해시를 계산하고, 저장된 해시와 비교합니다.

    상황별 동작:
        최초 실행   → Neo4j에 해시 없음 → 모든 파일을 신규로 처리
        파일 수정   → 해시 불일치       → 해당 파일만 재파싱·재적재
        파일 삭제   → 현재 목록에 없음  → Neo4j에서 DETACH DELETE (연결된 관계도 삭제)
        파일 미변경 → 해시 동일         → 완전히 건너뜀 (파싱 비용 0)

    참고:
        contentHash가 실제로 Neo4j에 저장되는 시점은 Neo4jLoader가 JavaFile 노드를
        MERGE할 때입니다. JavaFile 노드에 contentHash 속성이 포함되어 있어야 합니다.
        (현재 구현에서는 GraphModelMapper가 JavaFile 노드에 contentHash를 포함시켜야 함)
    """

    def __init__(self, session: Session):
        """
        Neo4j 세션을 받아 저장합니다.

        session: Neo4j 드라이버에서 생성한 세션 객체.
                 Neo4j에서 기존 해시를 읽고 삭제된 파일을 처리하는 데 사용합니다.
        """
        self._session = session

    # ── 공개 메서드 ───────────────────────────────────────────────

    def get_changed_files(
        self, sources: dict[str, str]
    ) -> dict[str, str]:
        """
        현재 수집한 소스 딕셔너리와 Neo4j에 저장된 해시를 비교하여
        신규·변경된 파일만 담은 딕셔너리를 반환합니다.

        처리 흐름:
            1. Neo4j에서 기존 { 경로 → 해시 } 맵을 읽어옵니다.
            2. 현재 소스의 각 파일에 대해 SHA-256 해시를 계산합니다.
            3. 저장된 해시와 다르거나(수정) 저장된 해시가 없는(신규) 파일을 changed에 담습니다.
            4. Neo4j에 있지만 현재 소스에 없는 파일(삭제된 파일)은 Neo4j에서 제거합니다.

        매개변수:
            sources: SourceCollector가 반환한 { 파일경로 → 소스코드 } 딕셔너리

        반환값:
            { 파일경로 → 소스코드 } — 신규 또는 변경된 파일만 포함
        """
        stored = self._load_stored_hashes()
        logger.info("Neo4j에 저장된 파일 해시: %d개", len(stored))

        changed: dict[str, str] = {}
        current_paths = set(sources.keys())

        # 신규·변경 파일 선별
        for path, source in sources.items():
            current_hash = self._sha256(source)
            if stored.get(path) != current_hash:
                changed[path] = source

        # 삭제된 파일 처리 (Neo4j에 있지만 현재 소스에 없는 파일)
        deleted_paths = set(stored.keys()) - current_paths
        if deleted_paths:
            self._delete_removed_files(list(deleted_paths))

        logger.info(
            "변경 감지 — 신규/변경: %d개 / 삭제: %d개 / 미변경(스킵): %d개",
            len(changed),
            len(deleted_paths),
            len(sources) - len(changed),
        )
        return changed

    def compute_hash(self, source: str) -> str:
        """
        소스코드 문자열의 SHA-256 해시를 반환합니다.

        외부에서 해시값이 필요할 때 사용할 수 있는 공개 래퍼 메서드입니다.
        내부적으로는 _sha256()을 호출합니다.
        """
        return self._sha256(source)

    # ── 내부 메서드 ───────────────────────────────────────────────

    def _load_stored_hashes(self) -> dict[str, str]:
        """
        Neo4j에서 이전에 적재된 모든 JavaFile 노드의 경로와 해시를 읽어옵니다.

        Cypher 쿼리:
            MATCH (f:JavaFile) RETURN f.path, f.contentHash

        반환값:
            { "src/main/java/com/example/Foo.java": "abc123..." } 형태의 딕셔너리.
            contentHash가 없는 노드(최초 적재 전)는 제외합니다.
        """
        result = self._session.run(
            "MATCH (f:JavaFile) RETURN f.path AS path, f.contentHash AS hash"
        )
        return {
            record["path"]: record["hash"]
            for record in result
            if record["hash"] is not None
        }

    def _delete_removed_files(self, paths: list[str]) -> None:
        """
        소스 디렉토리에서 삭제된 파일에 해당하는 :JavaFile 노드를 Neo4j에서 제거합니다.

        DETACH DELETE를 사용하므로 해당 JavaFile 노드에 연결된 모든 관계(:DECLARES 등)도
        함께 삭제됩니다. 단, JavaFile이 DECLARES하는 Class/Method 노드 자체는 남습니다.

        매개변수:
            paths: 삭제된 파일의 경로 목록
        """
        logger.info("삭제된 파일 Neo4j 정리: %d개", len(paths))
        self._session.run(
            """
            UNWIND $paths AS p
            MATCH (f:JavaFile {path: p})
            DETACH DELETE f
            """,
            paths=paths,
        )

    @staticmethod
    def _sha256(text: str) -> str:
        """
        문자열을 UTF-8로 인코딩한 후 SHA-256 해시를 16진수 문자열로 반환합니다.

        인코딩 오류 바이트는 replace 모드로 처리하여 항상 해시를 반환합니다.
        동일한 소스코드는 항상 동일한 해시를 반환하므로 변경 감지에 사용할 수 있습니다.
        """
        return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
