"""
cs_ingestion/main.py — C# 전체 파이프라인 실행 진입점

실행 흐름:
    STEP 1. 소스 수집       (CsSourceCollector)
    STEP 2. 증분 변경 감지  (CsIncrementalTracker)
    STEP 3. AST 파싱        (CsParser)
    STEP 4. 복잡도 분석     (CsComplexityAnalyzer)
    STEP 5. 그래프 모델 변환 (CsGraphModelMapper)
    STEP 6. Neo4j 적재      (CsNeo4jLoader)

Java 버전 대비 차이:
    - STEP 3에서 파일 하나당 list[ClassInfo] 반환 → flatten 처리
    - STEP 5에서 file_hashes 전달 (contentHash → CsFile 노드에 저장)
"""
import argparse
import logging
import sys
import time
from pathlib import Path

from neo4j import GraphDatabase

from config.settings import CsCollectorConfig, CsIngestionConfig, Neo4jConfig
from cs_ingestion.collector import CsIncrementalTracker, CsSourceCollector
from cs_ingestion.loader import CsNeo4jLoader
from cs_ingestion.mapper import CsGraphModelMapper
from cs_ingestion.models import ClassInfo
from cs_ingestion.parser import CsComplexityAnalyzer, CsParser

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cs_ingestion")


def run_ingestion(config: CsIngestionConfig) -> dict:
    """
    6단계 C# 파이프라인을 순서대로 실행하고 결과 요약 딕셔너리를 반환합니다.

    반환값:
        {
          "total_files":    전체 수집 파일 수,
          "changed_files":  변경 감지 파일 수,
          "parsed_classes": 파싱 성공 클래스 수 (한 파일에 여러 클래스 포함),
          "nodes":          Neo4j 적재 노드 수,
          "edges":          Neo4j 적재 관계 수,
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
            collector = CsSourceCollector()
            all_sources = collector.collect(config.collector)
            stats["total_files"] = len(all_sources)
            logger.info("  전체 파일: %d개", len(all_sources))

            # ── STEP 2. 증분 변경 감지 ────────────────────────
            logger.info("STEP 2. 증분 변경 감지")
            tracker = CsIncrementalTracker(session)
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

            # 파일별 contentHash 계산 (STEP 5에서 CsFile 노드에 저장)
            file_hashes = {
                path: tracker.compute_hash(src)
                for path, src in changed.items()
            }

            # ── STEP 3. AST 파싱 ──────────────────────────────
            logger.info("STEP 3. AST 파싱")
            parser = CsParser()
            classes: list[ClassInfo] = []
            parse_errors = 0

            for idx, (path, src) in enumerate(changed.items(), 1):
                results = parser.parse(path, src)
                if results:
                    classes.extend(results)
                else:
                    parse_errors += 1
                if idx % 100 == 0:
                    logger.info("  파싱 진행: %d / %d", idx, len(changed))

            stats["parsed_classes"] = len(classes)
            logger.info(
                "  파싱 완료 — 성공: %d 클래스 / 실패 파일: %d",
                len(classes),
                parse_errors,
            )
            if parse_errors:
                logger.warning("  파싱 실패 파일은 cs_parse_failures.log를 확인하세요.")

            # ── STEP 4. 복잡도 분석 ───────────────────────────
            logger.info("STEP 4. 복잡도 분석")
            analyzer = CsComplexityAnalyzer()
            classes = [analyzer.enrich(cls) for cls in classes]
            total_methods = sum(len(cls.methods) for cls in classes)
            logger.info("  분석 완료 — 메서드: %d개", total_methods)

            # ── STEP 5. 그래프 모델 변환 ──────────────────────
            logger.info("STEP 5. 그래프 모델 변환")
            mapper = CsGraphModelMapper()
            graph = mapper.map_to_graph(
                classes,
                file_hashes=file_hashes,
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
            loader = CsNeo4jLoader(session, batch_size=config.batch_size)
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
    _dc = CsCollectorConfig()
    _dn = Neo4jConfig()

    p = argparse.ArgumentParser(
        description="C# AST -> Neo4j ingestion pipeline",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "base_path",
        nargs="?",
        default=_dc.base_path,
        help="C# source root directory",
    )
    p.add_argument("--project-id", default="default-cs-project", help="Project ID")
    p.add_argument("--project-name", default="", help="Project display name")
    p.add_argument("--neo4j-uri", default=_dn.uri, help="Neo4j URI")
    p.add_argument("--neo4j-user", default=_dn.user, help="Neo4j username")
    p.add_argument("--neo4j-password", default=_dn.password, help="Neo4j password")
    p.add_argument("--neo4j-db", default=_dn.database, help="Neo4j database")
    p.add_argument("--include-test", action="store_true", default=_dc.include_test)
    p.add_argument("--batch-size", type=int, default=500)
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p


def main() -> None:
    """
    CLI 인자를 파싱하고 C# 파이프라인을 실행합니다.

    실행 방법:
        python -m cs_ingestion.main                    # cs_settings.py 기본값 사용
        python -m cs_ingestion.main C:/project/src     # 경로 직접 지정
        python -m cs_ingestion.main --log-level DEBUG  # 상세 로그
    """
    args = _build_arg_parser().parse_args()
    logging.getLogger().setLevel(args.log_level)

    base_path = Path(args.base_path).resolve()
    if not base_path.exists():
        logger.error("경로가 존재하지 않습니다: %s", base_path)
        sys.exit(1)

    config = CsIngestionConfig(
        project_id=args.project_id,
        project_name=args.project_name or base_path.name,
        collector=CsCollectorConfig(
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
