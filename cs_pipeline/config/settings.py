import os
from dataclasses import dataclass, field


@dataclass
class Neo4jConfig:
    uri: str = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user: str = os.getenv("NEO4J_USER", "neo4j")
    password: str = os.getenv("NEO4J_PASSWORD", "oky1714!@#")
    database: str = os.getenv("NEO4J_DATABASE", "neo4j")


@dataclass
class CsCollectorConfig:
    mode: str = "local"
    base_path: str = r"C:\project\src"
    include_test: bool = False
    file_encoding: str = "utf-8"


@dataclass
class CsIngestionConfig:
    project_id: str = "default-cs-project"
    project_name: str = "CS Project"
    collector: CsCollectorConfig = field(default_factory=CsCollectorConfig)
    neo4j: Neo4jConfig = field(default_factory=Neo4jConfig)
    batch_size: int = 500
