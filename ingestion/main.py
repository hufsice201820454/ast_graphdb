"""
ingestion/main.py — 전체 파이프라인 실행 진입점 (3.8)

실행 흐름:
    STEP 1. 소스 수집       (SourceCollector)
    STEP 2. 증분 변경 감지  (IncrementalTracker)
    STEP 3. AST 파싱        (ASTParser)
    STEP 4. 복잡도 분석     (ComplexityAnalyzer)
    STEP 5. 그래프 모델 변환 (GraphModelMapper)
    STEP 6. Neo4j 적재      (Neo4jLoader)
"""
import argparse
import logging
import sys
import time
from pathlib import Path

from neo4j import GraphDatabase

from config.settings import CollectorConfig, IngestionConfig, Neo4jConfig
from ingestion.collector import IncrementalTracker, SourceCollector
from ingestion.loader import Neo4jLoader
from ingestion.mapper import GraphModelMapper
from ingestion.models import ClassInfo
from ingestion.parser import ASTParser, ComplexityAnalyzer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ingestion")


def run_ingestion(config: IngestionConfig) -> dict:
    """
    6단계 파이프라인 전체를 순서대로 실행하고 결과 요약 딕셔너리를 반환합니다.

    처리 흐름:
        STEP 1. SourceCollector    — base_path 아래 .java 파일을 전부 읽어 딕셔너리로 수집
        STEP 2. IncrementalTracker — SHA-256 해시로 변경된 파일만 선별 (미변경 파일 스킵)
        STEP 3. ASTParser          — 각 소스코드를 AST로 파싱하여 ClassInfo 객체로 변환
        STEP 4. ComplexityAnalyzer — 각 메서드에 복잡도 메트릭(CC, CogC, LOC 등) 계산
        STEP 5. GraphModelMapper   — ClassInfo 목록을 Neo4j 노드·관계 딕셔너리로 변환
        STEP 6. Neo4jLoader        — 노드·관계를 Neo4j에 배치 MERGE 적재

    변경된 파일이 없으면 STEP 3 이후를 건너뛰고 즉시 반환합니다.

    Neo4j 연결은 함수 시작 시 열리고 finally 블록에서 반드시 닫힙니다.

    매개변수:
        config: IngestionConfig — 소스 경로, Neo4j 접속 정보, 프로젝트 ID 등 전체 설정

    반환값:
        {
          "total_files":    전체 수집 파일 수,
          "changed_files":  변경 감지된 파일 수 (실제 처리 수),
          "parsed_classes": AST 파싱 성공한 클래스 수,
          "nodes":          Neo4j에 적재된 노드 수,
          "edges":          Neo4j에 적재된 관계 수,
          "elapsed_sec":    전체 소요 시간(초)
        }
    """
    start_ts = time.time()
    stats = {
        "total_files": 0,
        "changed_files": 0,
        "parsed_classes": 0,
        "nodes": 0,
        "edges": 0,
        "elapsed_sec": 0.0,
    }

    driver = GraphDatabase.driver(
        config.neo4j.uri,
        auth=(config.neo4j.user, config.neo4j.password),
    )

    try:
        with driver.session(database=config.neo4j.database) as session:
            # ── STEP 1. 소스 수집 ─────────────────────────────
            logger.info("STEP 1. 소스 수집")
            collector = SourceCollector()
            all_sources = collector.collect(config.collector)
            stats["total_files"] = len(all_sources)
            logger.info("  전체 파일: %d개", len(all_sources))

            # ── STEP 2. 증분 변경 감지 ────────────────────────
            logger.info("STEP 2. 증분 변경 감지")
            tracker = IncrementalTracker(session)
            changed = tracker.get_changed_files(all_sources)
            stats["changed_files"] = len(changed)
            logger.info(
                "  변경 파일: %d / 전체: %d",
                len(changed),
                len(all_sources),
            )

            if not changed:
                logger.info("변경된 파일이 없습니다. 적재를 건너뜁니다.")
                return stats

            # ── STEP 3. AST 파싱 ──────────────────────────────
            logger.info("STEP 3. AST 파싱")
            parser = ASTParser()
            classes: list[ClassInfo] = []
            parse_errors = 0

            for idx, (path, src) in enumerate(changed.items(), 1):
                result = parser.parse(path, src)
                if result:
                    classes.append(result)
                else:
                    parse_errors += 1
                if idx % 100 == 0:
                    logger.info("  파싱 진행: %d / %d", idx, len(changed))

            stats["parsed_classes"] = len(classes)
            logger.info(
                "  파싱 완료 — 성공: %d / 실패: %d",
                len(classes),
                parse_errors,
            )
            if parse_errors:
                logger.warning(
                    "  파싱 실패 파일은 parse_failures.log를 확인하세요."
                )

            # ── STEP 4. 복잡도 분석 ───────────────────────────
            logger.info("STEP 4. 복잡도 분석")
            analyzer = ComplexityAnalyzer()
            classes = [analyzer.enrich(cls) for cls in classes]
            total_methods = sum(len(cls.methods) for cls in classes)
            logger.info("  분석 완료 — 메서드: %d개", total_methods)

            # ── STEP 5. 그래프 모델 변환 ──────────────────────
            logger.info("STEP 5. 그래프 모델 변환")
            mapper = GraphModelMapper()
            graph = mapper.map_to_graph(
                classes,
                project_id=config.project_id,
                project_name=config.project_name,
            )
            stats["nodes"] = len(graph.nodes)
            stats["edges"] = len(graph.edges)
            logger.info(
                "  변환 완료 — 노드: %d개 / 관계: %d개",
                len(graph.nodes),
                len(graph.edges),
            )

            # ── STEP 6. Neo4j 적재 ────────────────────────────
            logger.info("STEP 6. Neo4j 적재")
            loader = Neo4jLoader(session, batch_size=config.batch_size)
            loader.create_constraints_and_indexes()
            loader.load_nodes_and_edges(graph)
            logger.info("  적재 완료")

    finally:
        driver.close()

    stats["elapsed_sec"] = round(time.time() - start_ts, 2)
    logger.info(
        "파이프라인 완료 — 소요 시간: %.1f초 / 노드: %d / 관계: %d",
        stats["elapsed_sec"],
        stats["nodes"],
        stats["edges"],
    )
    return stats


# ── CLI 진입점 ────────────────────────────────────────────────────

def _build_arg_parser() -> argparse.ArgumentParser:
    """
    CLI(명령줄) 인자 파서를 생성하여 반환합니다.

    settings.py의 기본값을 argparse 기본값으로 사용합니다.
    따라서 인자를 생략하면 settings.py에 정의된 값이 자동으로 적용됩니다.

    base_path는 nargs="?"로 선택적 인자입니다.
    생략하면 CollectorConfig.base_path(settings.py 기본값)가 사용됩니다.

    formatter_class=ArgumentDefaultsHelpFormatter를 사용하므로
    --help 출력 시 각 인자의 기본값이 함께 표시됩니다.

    반환값:
        설정이 완료된 argparse.ArgumentParser 객체
    """
    _dc = CollectorConfig()
    _dn = Neo4jConfig()

    p = argparse.ArgumentParser(
        description="Java AST -> Neo4j ingestion pipeline",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "base_path",
        nargs="?",
        default=_dc.base_path,
        help="Java source root directory",
    )
    p.add_argument("--project-id", default="default-project", help="Project ID")
    p.add_argument("--project-name", default="", help="Project display name")
    p.add_argument("--neo4j-uri", default=_dn.uri, help="Neo4j URI")
    p.add_argument("--neo4j-user", default=_dn.user, help="Neo4j username")
    p.add_argument("--neo4j-password", default=_dn.password, help="Neo4j password")
    p.add_argument("--neo4j-db", default=_dn.database, help="Neo4j database")
    p.add_argument("--include-test", action="store_true", default=_dc.include_test, help="Include test sources")
    p.add_argument("--batch-size", type=int, default=500, help="Neo4j batch size")
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level",
    )
    return p


def main() -> None:
    """
    프로그램의 실제 진입점입니다. CLI 인자를 파싱하고 파이프라인을 실행합니다.

    처리 흐름:
        1. _build_arg_parser()로 CLI 인자를 파싱합니다.
        2. 로그 레벨을 설정합니다 (기본값 INFO, --log-level DEBUG로 상세 로그 활성화).
        3. base_path를 절대경로로 변환하고 존재 여부를 확인합니다.
           경로가 없으면 오류 메시지를 출력하고 종료합니다 (exit code 1).
        4. CLI 인자를 조합하여 IngestionConfig를 생성합니다.
           project_name이 비어 있으면 base_path의 디렉토리 이름을 자동으로 사용합니다.
        5. run_ingestion()을 호출하여 파이프라인을 실행합니다.
        6. 결과 요약(파일 수, 노드 수, 소요 시간 등)을 콘솔에 출력합니다.

    실행 방법:
        python -m ingestion.main                          # settings.py 기본값 사용
        python -m ingestion.main C:/project/src/main/java # 경로 직접 지정
        python -m ingestion.main --log-level DEBUG        # 상세 로그 활성화
    """
    args = _build_arg_parser().parse_args()
    logging.getLogger().setLevel(args.log_level)

    base_path = Path(args.base_path).resolve()
    if not base_path.exists():
        logger.error("경로가 존재하지 않습니다: %s", base_path)
        sys.exit(1)

    config = IngestionConfig(
        project_id=args.project_id,
        project_name=args.project_name or base_path.name,
        collector=CollectorConfig(
            mode="local",
            base_path=str(base_path),
            include_test=args.include_test,
        ),
        neo4j=Neo4jConfig(
            uri=args.neo4j_uri,
            user=args.neo4j_user,
            password=args.neo4j_password,
            database=args.neo4j_db,
        ),
        batch_size=args.batch_size,
    )

    stats = run_ingestion(config)
    print("\n=== 적재 결과 요약 ===")
    print(f"  전체 파일    : {stats['total_files']}개")
    print(f"  변경 파일    : {stats['changed_files']}개")
    print(f"  파싱 클래스  : {stats['parsed_classes']}개")
    print(f"  생성 노드    : {stats['nodes']}개")
    print(f"  생성 관계    : {stats['edges']}개")
    print(f"  소요 시간    : {stats['elapsed_sec']}초")


if __name__ == "__main__":
    main()
