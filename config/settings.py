import os
from dataclasses import dataclass, field


@dataclass
class Neo4jConfig:
    uri: str = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user: str = os.getenv("NEO4J_USER", "neo4j")
    password: str = os.getenv("NEO4J_PASSWORD", "oky1714!@#")
    database: str = os.getenv("NEO4J_DATABASE", "neo4j")


@dataclass
class CollectorConfig:
    mode: str = "local"          # local | api | git
    base_path: str = r"C:\ai_test\mes4u\src\main\java"          # 로컬 디렉토리 루트 경로
    include_test: bool = False   # src/test 포함 여부
    file_encoding: str = "utf-8"


@dataclass
class IngestionConfig:
    project_id: str = "mes4u"
    project_name: str = "mes4u"
    base_package: str = ""
    collector: CollectorConfig = field(default_factory=CollectorConfig)
    neo4j: Neo4jConfig = field(default_factory=Neo4jConfig)
    batch_size: int = 500
