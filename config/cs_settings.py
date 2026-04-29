import os
from dataclasses import dataclass, field

from config.settings import Neo4jConfig


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
