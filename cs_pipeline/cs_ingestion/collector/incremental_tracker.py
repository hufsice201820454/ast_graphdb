"""
Module: CsIncrementalTracker (모듈 ⑥)
역할: SHA-256 해시 기반으로 변경된 .cs 파일만 선별하여 재적재. 전체 재분석 방지.
      Neo4j의 :CsFile 노드에 저장된 contentHash와 비교합니다.
"""
import hashlib
import logging

from neo4j import Session

logger = logging.getLogger(__name__)


class CsIncrementalTracker:
    """
    파일 변경 여부를 SHA-256 해시로 감지하여 변경된 파일만 반환하는 클래스.

    Neo4j의 :CsFile 노드에 이전 적재 시 계산한 contentHash가 저장되어 있습니다.
    현재 파일 해시와 비교하여 변경된 파일만 반환합니다.
    """

    def __init__(self, session: Session):
        self._session = session

    def get_changed_files(self, sources: dict[str, str]) -> dict[str, str]:
        stored = self._load_stored_hashes()
        logger.info("Neo4j에 저장된 파일 해시: %d개", len(stored))

        changed: dict[str, str] = {}
        current_paths = set(sources.keys())

        for path, source in sources.items():
            current_hash = self._sha256(source)
            if stored.get(path) != current_hash:
                changed[path] = source

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
        return self._sha256(source)

    def _load_stored_hashes(self) -> dict[str, str]:
        result = self._session.run(
            "MATCH (f:CsFile) RETURN f.path AS path, f.contentHash AS hash"
        )
        return {
            record["path"]: record["hash"]
            for record in result
            if record["hash"] is not None
        }

    def _delete_removed_files(self, paths: list[str]) -> None:
        logger.info("삭제된 파일 Neo4j 정리: %d개", len(paths))
        self._session.run(
            """
            UNWIND $paths AS p
            MATCH (f:CsFile {path: p})
            DETACH DELETE f
            """,
            paths=paths,
        )

    @staticmethod
    def _sha256(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
