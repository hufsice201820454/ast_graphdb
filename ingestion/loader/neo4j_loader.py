"""
Module: Neo4jLoader (모듈 ⑤)
역할: UNWIND 배치 MERGE로 노드·관계 적재. 중복 방지 및 증분 갱신 처리.

적재 순서 제약 (3.6.2):
    1. :Project 노드
    2. :JavaFile 노드
    3. :Class / :Interface / :Enum 노드
    4. :Method / :Constructor / :Field 노드
    5. :Annotation 노드
    6. 모든 관계 (CONTAINS / DECLARES / HAS_METHOD / CALLS / EXTENDS 등)
"""
import logging
from itertools import groupby

from neo4j import Session

from ingestion.models import GraphData

logger = logging.getLogger(__name__)

# 배치 크기: 500건씩 MERGE (3.6.1)
BATCH_SIZE = 500

# 노드 레이블별 고유 키 매핑
# Neo4j MERGE 시 어떤 속성을 기준으로 중복을 판단할지 결정합니다.
# Class/Interface는 fqn(완전한 클래스명)이 고유 키이고,
# 나머지는 id가 고유 키입니다.
_LABEL_KEY_MAP = {
    "Class": "fqn",
    "Interface": "fqn",
    "Enum": "fqn",
    "Project": "id",
    "JavaFile": "id",
    "Method": "id",
    "Constructor": "id",
    "Field": "id",
    "Annotation": "id",
    "Parameter": "id",
}

# 적재 순서: 노드 레이블 우선순위
# 관계(엣지)는 노드가 먼저 존재해야 MERGE할 수 있으므로
# 참조되는 노드(Project, JavaFile, Class)를 먼저 적재하고
# 참조하는 노드(Method, Field)를 나중에 적재합니다.
_NODE_LABEL_ORDER = [
    "Project",
    "JavaFile",
    "Class",
    "Interface",
    "Enum",
    "Method",
    "Constructor",
    "Field",
    "Annotation",
    "Parameter",
]


class Neo4jLoader:
    """
    GraphData를 Neo4j에 배치 MERGE 방식으로 적재하는 클래스.

    핵심 설계 원칙:
        단건 INSERT/CREATE 대신 UNWIND + MERGE를 사용합니다.
        500건씩 묶어서 한 번의 Cypher 쿼리로 처리하므로 네트워크 왕복 횟수를 최소화합니다.

        MERGE의 장점:
            - 이미 존재하는 노드/관계는 업데이트하고, 없으면 새로 생성합니다.
            - 중복 실행에도 안전합니다 (멱등성 보장).
            - 증분 적재 시 변경된 노드만 업데이트됩니다.

    적재 순서:
        1. load_nodes(): 레이블 우선순위에 따라 노드를 먼저 적재합니다.
        2. load_edges(): 모든 노드가 존재한 뒤 관계를 적재합니다.
           관계의 양 끝 노드가 없으면 MERGE가 실행되지 않으므로 순서가 중요합니다.
    """

    def __init__(self, session: Session, batch_size: int = BATCH_SIZE):
        """
        Neo4j 세션과 배치 크기를 받아 초기화합니다.

        매개변수:
            session:    Neo4j 드라이버에서 생성한 세션 객체. 모든 Cypher 쿼리 실행에 사용됩니다.
            batch_size: 한 번의 UNWIND 쿼리에서 처리할 최대 항목 수. 기본값 500.
                        너무 크면 메모리 부족, 너무 작으면 네트워크 왕복 증가.
        """
        self._session = session
        self._batch_size = batch_size

    # ── 공개 진입점 ──────────────────────────────────────────────

    def load_nodes_and_edges(self, graph: GraphData) -> None:
        """
        GraphData 전체를 Neo4j에 적재합니다. 노드 먼저, 관계는 그 다음입니다.

        내부적으로 load_nodes()와 load_edges()를 순서대로 호출합니다.
        관계는 양 끝 노드가 먼저 존재해야 MERGE가 성공하므로 이 순서는 반드시 지켜져야 합니다.

        매개변수:
            graph: GraphModelMapper가 생성한 GraphData 객체
        """
        self.load_nodes(graph.nodes)
        self.load_edges(graph.edges)

    def load_nodes(self, nodes: list[dict]) -> None:
        """
        노드 딕셔너리 목록을 레이블별로 그룹핑하여 순서에 따라 배치 MERGE 적재합니다.

        처리 흐름:
            1. 전체 노드를 레이블별로 그룹핑합니다.
               { "Class": [...], "Method": [...], "Field": [...] }
            2. _NODE_LABEL_ORDER에 정의된 순서대로 각 레이블을 적재합니다.
               (Project → JavaFile → Class → Interface → Method → Field → Annotation)
            3. 순서 목록에 없는 레이블은 마지막에 임의 순서로 적재합니다.

        매개변수:
            nodes: 노드 딕셔너리 목록 (각 딕셔너리에는 "label" 키가 있어야 합니다)
        """
        # 레이블별 그룹핑
        label_groups: dict[str, list[dict]] = {}
        for node in nodes:
            label = node.get("label", "Unknown")
            label_groups.setdefault(label, []).append(node)

        # 지정된 순서대로 적재
        for label in _NODE_LABEL_ORDER:
            if label in label_groups:
                self._load_label_nodes(label, label_groups.pop(label))

        # 순서 목록에 없는 레이블 나머지 적재
        for label, node_list in label_groups.items():
            self._load_label_nodes(label, node_list)

    def load_edges(self, edges: list[dict]) -> None:
        """
        관계 딕셔너리 목록을 관계 타입별로 그룹핑하여 배치 MERGE 적재합니다.

        관계 타입(DECLARES, HAS_METHOD 등)을 기준으로 정렬·그룹핑하여
        같은 타입끼리 묶어서 _load_rel_batch()에 전달합니다.

        매개변수:
            edges: 관계 딕셔너리 목록 (각 딕셔너리에는 "type", "from_id", "to_id" 키 필요)
        """
        sorted_edges = sorted(edges, key=lambda e: e["type"])
        for rel_type, group in groupby(sorted_edges, key=lambda e: e["type"]):
            batch = list(group)
            self._load_rel_batch(rel_type, batch)

    def create_constraints_and_indexes(self) -> None:
        """
        Neo4j에 고유 제약 조건과 검색 성능 인덱스를 생성합니다. 최초 1회 실행됩니다.

        고유 제약 조건 (CREATE CONSTRAINT IF NOT EXISTS):
            각 노드 레이블의 고유 키에 UNIQUE 제약을 걸어 중복 노드 생성을 방지합니다.
            MERGE 쿼리가 인덱스를 활용하므로 성능도 향상됩니다.

        검색 인덱스 (CREATE INDEX IF NOT EXISTS):
            자주 검색하는 속성(메서드명, 클래스명, 패키지, 복잡도)에 인덱스를 생성합니다.
            Cypher WHERE 절에서 이 속성으로 검색할 때 전체 스캔을 피합니다.

        IF NOT EXISTS 옵션 덕분에 이미 존재하면 무시되어 중복 생성 오류가 발생하지 않습니다.
        개별 제약/인덱스 생성 실패는 경고 로그만 남기고 계속 진행합니다.
        """
        constraints = [
            "CREATE CONSTRAINT IF NOT EXISTS FOR (p:Project) REQUIRE p.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (f:JavaFile) REQUIRE f.path IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (c:Class) REQUIRE c.fqn IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (i:Interface) REQUIRE i.fqn IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (m:Method) REQUIRE m.id IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (f:Field) REQUIRE f.id IS UNIQUE",
        ]
        indexes = [
            "CREATE INDEX idx_method_name IF NOT EXISTS FOR (m:Method) ON (m.name)",
            "CREATE INDEX idx_class_name IF NOT EXISTS FOR (c:Class) ON (c.name)",
            "CREATE INDEX idx_class_pkg IF NOT EXISTS FOR (c:Class) ON (c.packageName)",
            "CREATE INDEX idx_method_complex IF NOT EXISTS FOR (m:Method) ON (m.cyclomaticComplexity)",
        ]
        for cypher in constraints + indexes:
            try:
                self._session.run(cypher)
            except Exception as exc:
                logger.warning("제약/인덱스 생성 실패: %s — %s", cypher[:60], exc)
        logger.info("제약 조건 & 인덱스 설정 완료")

    # ── 내부 배치 처리 ───────────────────────────────────────────

    def _load_label_nodes(self, label: str, nodes: list[dict]) -> None:
        """
        특정 레이블의 노드 목록을 배치 MERGE로 적재합니다.

        Cypher 쿼리:
            UNWIND $batch AS n
            MERGE (node:{label} {{key: n.key}})
            SET node += n

        MERGE: key 속성을 기준으로 이미 존재하면 찾고, 없으면 새로 만듭니다.
        SET node += n: 노드의 모든 속성을 딕셔너리 n의 값으로 업데이트합니다.
            += 연산자는 기존 속성을 삭제하지 않고 추가/업데이트만 합니다.

        매개변수:
            label: 노드 레이블 (예: "Class", "Method")
            nodes: 해당 레이블의 노드 딕셔너리 목록
        """
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
        """
        동일한 관계 타입의 엣지 목록을 (from_label, to_label) 쌍으로 그룹핑하여
        레이블 기반 MATCH 쿼리로 배치 MERGE 적재합니다.

        레이블 기반 MATCH를 사용하는 이유:
            MATCH (a) WHERE a.id = ... 처럼 레이블 없이 검색하면
            Neo4j가 모든 노드를 전체 스캔(full scan)합니다.
            레이블별 인덱스(예: :Class(fqn), :Method(id))를 활용하지 못하기 때문입니다.

            MATCH (a:Class {fqn: ...}) 처럼 레이블을 명시하면
            해당 레이블의 인덱스를 타서 빠르고 정확하게 노드를 찾습니다.

        처리 흐름:
            1. edges를 (from_label, to_label) 쌍으로 그룹핑합니다.
               예: DECLARES 엣지 중 "JavaFile → Class" 그룹, "JavaFile → Interface" 그룹으로 분리.
            2. 각 그룹에 대해 레이블과 키를 사용한 MATCH 쿼리를 생성합니다.
            3. from_label/to_label이 없는 엣지는 폴백 쿼리를 사용합니다.

        매개변수:
            rel_type: 관계 타입 문자열 (예: "HAS_METHOD", "CALLS")
            edges:    해당 관계 타입의 엣지 딕셔너리 목록
        """
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
                # from_label/to_label 없는 엣지 폴백
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
        """
        items 목록을 batch_size 단위로 잘라 Cypher 쿼리를 반복 실행합니다.

        처리 흐름:
            items가 1200건이고 batch_size가 500이면:
                1회차: items[0:500]   → session.run(query, batch=500건)
                2회차: items[500:1000] → session.run(query, batch=500건)
                3회차: items[1000:1200] → session.run(query, batch=200건)

        .consume():
            session.run()은 결과를 지연 로딩(lazy)합니다.
            .consume()을 호출해야 쿼리가 완전히 실행되고 트랜잭션이 완료됩니다.
            호출하지 않으면 다음 쿼리 실행 시 이전 결과가 제대로 커밋되지 않을 수 있습니다.

        매개변수:
            query: 실행할 Cypher 쿼리 문자열 ($batch 파라미터 사용)
            items: 적재할 딕셔너리 목록 (배치로 분할됨)

        반환값:
            처리된 총 항목 수 (items 전체 길이와 동일)
        """
        total = 0
        for i in range(0, len(items), self._batch_size):
            batch = items[i: i + self._batch_size]
            self._session.run(query, batch=batch).consume()
            total += len(batch)
        return total

    @staticmethod
    def _extract_props(edge: dict) -> dict:
        """
        엣지 딕셔너리에서 관계 속성으로 저장할 항목만 추출합니다.

        제외 항목:
            "type"       : 관계 타입 (Cypher 쿼리에 직접 사용됨)
            "from_id"    : 출발 노드 식별자 (MATCH에 사용됨)
            "to_id"      : 도착 노드 식별자 (MATCH에 사용됨)
            "from_label" : 출발 노드 레이블 (MATCH에 사용됨)
            "to_label"   : 도착 노드 레이블 (MATCH에 사용됨)
            "props"      : 재귀 방지 (이미 props가 있는 경우)

        나머지 항목이 관계의 실제 속성이 됩니다.
        예: CALLS 엣지의 "call_line", "call_type" → 관계 속성으로 Neo4j에 저장됨.
        DECLARES, HAS_METHOD 등 속성이 없는 관계는 빈 딕셔너리 {}가 반환됩니다.

        매개변수:
            edge: 엣지 딕셔너리

        반환값:
            관계 속성만 담은 딕셔너리
        """
        exclude = {"type", "from_id", "to_id", "from_label", "to_label", "props"}
        return {k: v for k, v in edge.items() if k not in exclude}
