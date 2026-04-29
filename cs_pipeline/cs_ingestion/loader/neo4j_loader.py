"""
Module: CsNeo4jLoader (모듈 ⑤)
역할: UNWIND 배치 MERGE로 노드·관계 적재. 중복 방지 및 증분 갱신 처리.

Java 버전 대비 변경 사항:
    - JavaFile → CsFile
    - BackendEndpoint 노드 레이블 추가
    - C# 전용 제약 조건 및 인덱스 생성
"""
import logging
from itertools import groupby

from neo4j import Session

from cs_ingestion.models import GraphData

logger = logging.getLogger(__name__)

BATCH_SIZE = 500

_LABEL_KEY_MAP = {
    "Project": "id",
    "CsFile": "id",
    "Class": "fqn",
    "Interface": "fqn",
    "Method": "id",
    "Field": "id",
    "BackendEndpoint": "id",
}

_NODE_LABEL_ORDER = [
    "Project",
    "CsFile",
    "Class",
    "Interface",
    "Method",
    "Field",
    "BackendEndpoint",
]


class CsNeo4jLoader:
    """
    GraphData를 Neo4j에 배치 MERGE 방식으로 적재하는 클래스.

    Java 버전과 동일한 UNWIND + MERGE + SET node += n 패턴 사용.
    노드를 먼저 적재한 뒤 관계를 적재하여 MATCH 실패를 방지합니다.
    """

    def __init__(self, session: Session, batch_size: int = BATCH_SIZE):
        self._session = session
        self._batch_size = batch_size

    def load_nodes_and_edges(self, graph: GraphData) -> None:
        self.load_nodes(graph.nodes)
        self.load_edges(graph.edges)

    def load_nodes(self, nodes: list[dict]) -> None:
        label_groups: dict[str, list[dict]] = {}
        for node in nodes:
            label = node.get("label", "Unknown")
            label_groups.setdefault(label, []).append(node)

        for label in _NODE_LABEL_ORDER:
            if label in label_groups:
                self._load_label_nodes(label, label_groups.pop(label))

        for label, node_list in label_groups.items():
            self._load_label_nodes(label, node_list)

    def load_edges(self, edges: list[dict]) -> None:
        sorted_edges = sorted(edges, key=lambda e: e["type"])
        for rel_type, group in groupby(sorted_edges, key=lambda e: e["type"]):
            self._load_rel_batch(rel_type, list(group))

    def create_constraints_and_indexes(self) -> None:
        """
        C# 파이프라인용 Neo4j 고유 제약 조건과 검색 인덱스를 생성합니다.

        CsFile.path, Class.fqn, Interface.fqn, Method.id, BackendEndpoint.id 에
        UNIQUE 제약을 설정합니다.
        """
        constraints = [
            "CREATE CONSTRAINT IF NOT EXISTS FOR (p:Project) REQUIRE p.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (f:CsFile) REQUIRE f.path IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (c:Class) REQUIRE c.fqn IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (i:Interface) REQUIRE i.fqn IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (m:Method) REQUIRE m.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (f:Field) REQUIRE f.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (b:BackendEndpoint) REQUIRE b.id IS UNIQUE",
        ]
        indexes = [
            "CREATE INDEX idx_cs_method_name IF NOT EXISTS FOR (m:Method) ON (m.name)",
            "CREATE INDEX idx_cs_class_name IF NOT EXISTS FOR (c:Class) ON (c.name)",
            "CREATE INDEX idx_cs_class_ns IF NOT EXISTS FOR (c:Class) ON (c.namespace)",
            "CREATE INDEX idx_cs_method_cc IF NOT EXISTS FOR (m:Method) ON (m.cyclomaticComplexity)",
            "CREATE INDEX idx_cs_backend_path IF NOT EXISTS FOR (b:BackendEndpoint) ON (b.path)",
        ]
        for cypher in constraints + indexes:
            try:
                self._session.run(cypher)
            except Exception as exc:
                logger.warning("제약/인덱스 생성 실패: %s — %s", cypher[:60], exc)
        logger.info("제약 조건 & 인덱스 설정 완료")

    # ── 내부 배치 처리 ───────────────────────────────────────────

    def _load_label_nodes(self, label: str, nodes: list[dict]) -> None:
        if not nodes:
            return
        key = _LABEL_KEY_MAP.get(label, "id")
        query = f"""
            UNWIND $batch AS n
            MERGE (node:{label} {{{key}: n.{key}}})
            SET node += n
        """
        total = self._run_batched(query, nodes)
        logger.debug("노드 적재 — :%s %d건", label, total)

    def _load_rel_batch(self, rel_type: str, edges: list[dict]) -> None:
        groups: dict[tuple, list[dict]] = {}
        for edge in edges:
            key = (edge.get("from_label", ""), edge.get("to_label", ""))
            groups.setdefault(key, []).append(edge)

        for (from_label, to_label), group in groups.items():
            enriched = [{**e, "props": self._extract_props(e)} for e in group]

            if from_label and to_label:
                from_key = _LABEL_KEY_MAP.get(from_label, "id")
                to_key = _LABEL_KEY_MAP.get(to_label, "id")
                query = f"""
                    UNWIND $batch AS e
                    MATCH (a:{from_label} {{{from_key}: e.from_id}})
                    MATCH (b:{to_label} {{{to_key}: e.to_id}})
                    MERGE (a)-[r:{rel_type}]->(b)
                    SET r += e.props
                """
            else:
                query = f"""
                    UNWIND $batch AS e
                    MATCH (a) WHERE a.id = e.from_id OR a.fqn = e.from_id
                    MATCH (b) WHERE b.id = e.to_id OR b.fqn = e.to_id
                    MERGE (a)-[r:{rel_type}]->(b)
                    SET r += e.props
                """

            total = self._run_batched(query, enriched)
            logger.debug("관계 적재 — :%s [%s→%s] %d건", rel_type, from_label, to_label, total)

    def _run_batched(self, query: str, items: list[dict]) -> int:
        total = 0
        for i in range(0, len(items), self._batch_size):
            batch = items[i: i + self._batch_size]
            self._session.run(query, batch=batch).consume()
            total += len(batch)
        return total

    @staticmethod
    def _extract_props(edge: dict) -> dict:
        exclude = {"type", "from_id", "to_id", "from_label", "to_label", "props"}
        return {k: v for k, v in edge.items() if k not in exclude}
