"""
Module: CsGraphModelMapper (모듈 ④)
역할: ClassInfo 목록을 Neo4j 노드·관계 딕셔너리로 변환한다.

Java 버전 대비 추가/변경 사항:
    - JavaFile → CsFile (노드 레이블)
    - packageName → namespace
    - :BackendEndpoint 노드 추가 (call_backend + action_kind)
    - CALLS_BACKEND 관계 추가 (Method → BackendEndpoint)
    - 한 파일에 클래스 여러 개 → CsFile 노드 중복 생성 방지 (file_seen 집합 사용)
    - contentHash를 CsFile 노드에 포함 (증분 추적용)

생성하는 노드:
    :Project, :CsFile, :Class, :Interface, :Method, :Field, :BackendEndpoint

생성하는 관계:
    DECLARES, HAS_METHOD, HAS_FIELD, EXTENDS, IMPLEMENTS,
    DEPENDS_ON, CALLS, CALLS_BACKEND
"""
import logging
from pathlib import Path

from cs_ingestion.models import ClassInfo, GraphData, MethodInfo

logger = logging.getLogger(__name__)


class CsGraphModelMapper:
    """
    ClassInfo 목록을 GraphData(노드 + 관계)로 변환하는 클래스.

    2차 패스 방식:
        1차 패스: class_registry 구성 { 단순명 → ClassInfo, FQN → ClassInfo }
        2차 패스: class_registry를 참조하여 노드·관계 생성

    file_hashes: { 파일경로 → SHA-256 해시 } — CsFile 노드의 contentHash 속성으로 저장.
                 IncrementalTracker가 다음 실행 시 변경 여부 판단에 사용합니다.
    """

    def map_to_graph(
        self,
        classes: list[ClassInfo],
        file_hashes: dict[str, str] | None = None,
        project_id: str = "",
        project_name: str = "",
    ) -> GraphData:
        """
        ClassInfo 목록 전체를 GraphData(노드 + 관계)로 변환합니다.

        매개변수:
            classes:      ClassInfo 객체 목록
            file_hashes:  { 파일경로 → contentHash } (CsFile 노드에 저장)
            project_id:   :Project 노드 ID (비어 있으면 Project 노드 생략)
            project_name: :Project 노드 표시 이름
        """
        file_hashes = file_hashes or {}
        nodes: list[dict] = []
        edges: list[dict] = []
        file_seen: set[str] = set()

        # ── 1차 패스: class_registry 구성 ───────────────────────
        class_registry: dict[str, ClassInfo] = {}
        for cls in classes:
            self._ensure_fqn(cls)
            class_registry[cls.class_name] = cls
            class_registry[cls.fqn] = cls

        # ── 프로젝트 노드 (옵션) ─────────────────────────────────
        if project_id:
            nodes.append({"label": "Project", "id": project_id, "name": project_name})

        # ── 2차 패스: 노드 & 관계 생성 ──────────────────────────
        for cls in classes:
            cls_label = "Interface" if cls.is_interface else "Class"

            # :CsFile 노드 (파일 하나에 클래스 여러 개여도 노드는 한 번만)
            if cls.file_path not in file_seen:
                file_seen.add(cls.file_path)
                nodes.append(self._file_node(cls, file_hashes.get(cls.file_path, "")))
                if project_id:
                    edges.append({
                        "type": "CONTAINS",
                        "from_id": project_id,
                        "to_id": cls.file_path,
                        "from_label": "Project",
                        "to_label": "CsFile",
                    })

            # :Class / :Interface 노드
            nodes.append(self._class_node(cls))
            edges.append({
                "type": "DECLARES",
                "from_id": cls.file_path,
                "to_id": cls.fqn,
                "from_label": "CsFile",
                "to_label": cls_label,
            })

            # EXTENDS 관계
            if cls.base_class and cls.base_class in class_registry:
                target = class_registry[cls.base_class]
                edges.append({
                    "type": "EXTENDS",
                    "from_id": cls.fqn,
                    "to_id": target.fqn,
                    "from_label": cls_label,
                    "to_label": "Interface" if target.is_interface else "Class",
                })

            # IMPLEMENTS 관계
            for iface_name in cls.interfaces:
                if iface_name in class_registry:
                    edges.append({
                        "type": "IMPLEMENTS",
                        "from_id": cls.fqn,
                        "to_id": class_registry[iface_name].fqn,
                        "from_label": cls_label,
                        "to_label": "Interface",
                    })

            # :Field 노드 & HAS_FIELD 관계
            for field_name, field_type in cls.fields.items():
                field_id = f"{cls.fqn}.{field_name}"
                nodes.append({
                    "label": "Field",
                    "id": field_id,
                    "name": field_name,
                    "type": field_type,
                    "classFqn": cls.fqn,
                })
                edges.append({
                    "type": "HAS_FIELD",
                    "from_id": cls.fqn,
                    "to_id": field_id,
                    "from_label": cls_label,
                    "to_label": "Field",
                })
                # DEPENDS_ON (필드 타입이 프로젝트 내 클래스인 경우)
                if field_type in class_registry:
                    dep = class_registry[field_type]
                    edges.append({
                        "type": "DEPENDS_ON",
                        "from_id": cls.fqn,
                        "to_id": dep.fqn,
                        "dep_type": "field",
                        "from_label": cls_label,
                        "to_label": "Interface" if dep.is_interface else "Class",
                    })

            # :Method 노드 & HAS_METHOD 관계
            for method in cls.methods.values():
                self._ensure_method_id(method, cls)
                nodes.append(self._method_node(method, cls))
                edges.append({
                    "type": "HAS_METHOD",
                    "from_id": cls.fqn,
                    "to_id": method.id,
                    "from_label": cls_label,
                    "to_label": "Method",
                })

                # CALLS 관계 (프로젝트 내 메서드 호출)
                for call in method.calls:
                    target_cls = class_registry.get(call.callee_class)
                    if target_cls:
                        edges.append({
                            "type": "CALLS",
                            "from_id": method.id,
                            "to_id": f"{target_cls.fqn}#{call.callee_method}",
                            "call_line": call.line,
                            "call_type": call.call_type,
                            "from_label": "Method",
                            "to_label": "Method",
                        })

                # CALLS_BACKEND 관계 (백엔드 호출 — call_backend + action_kind)
                if method.call_backend:
                    backend_id = (
                        f"{method.call_backend}#{method.action_kind}"
                        if method.action_kind
                        else method.call_backend
                    )
                    nodes.append({
                        "label": "BackendEndpoint",
                        "id": backend_id,
                        "path": method.call_backend,
                        "action": method.action_kind,
                    })
                    edges.append({
                        "type": "CALLS_BACKEND",
                        "from_id": method.id,
                        "to_id": backend_id,
                        "action_kind": method.action_kind,
                        "from_label": "Method",
                        "to_label": "BackendEndpoint",
                    })

        logger.info(
            "그래프 변환 완료 — 노드: %d개 / 엣지: %d개",
            len(nodes),
            len(edges),
        )
        return GraphData(nodes=nodes, edges=edges)

    # ── 노드 빌더 ────────────────────────────────────────────────

    @staticmethod
    def _file_node(cls: ClassInfo, content_hash: str) -> dict:
        return {
            "label": "CsFile",
            "id": cls.file_path,
            "path": cls.file_path,
            "fileName": Path(cls.file_path).name,
            "namespace": cls.namespace,
            "contentHash": content_hash,
        }

    @staticmethod
    def _class_node(cls: ClassInfo) -> dict:
        return {
            "label": "Interface" if cls.is_interface else "Class",
            "fqn": cls.fqn,
            "name": cls.class_name,
            "namespace": cls.namespace,
            "isAbstract": cls.is_abstract,
            "isSealed": cls.is_sealed,
            "isStruct": cls.is_struct,
            "attributes": cls.attributes,
            "lineStart": cls.line_start,
            "lineEnd": cls.line_end,
            "filePath": cls.file_path,
        }

    @staticmethod
    def _method_node(method: MethodInfo, cls: ClassInfo) -> dict:
        return {
            "label": "Method",
            "id": method.id,
            "name": method.name,
            "signature": method.signature,
            "returnType": method.return_type,
            "visibility": method.visibility,
            "isStatic": method.is_static,
            "lineStart": method.start_line,
            "lineEnd": method.end_line,
            "cyclomaticComplexity": method.cyclomatic_complexity,
            "cognitiveComplexity": method.cognitive_complexity,
            "loc": method.loc,
            "paramCount": method.param_count,
            "fanOut": method.fan_out,
            "attributes": method.attributes,
            "actionKind": method.action_kind,
            "callBackend": method.call_backend,
            "sourceCode": method.source_snippet,
            "classFqn": cls.fqn,
        }

    # ── FQN 보장 헬퍼 ────────────────────────────────────────────

    @staticmethod
    def _ensure_fqn(cls: ClassInfo) -> None:
        if not cls.fqn:
            cls.fqn = (
                f"{cls.namespace}.{cls.class_name}"
                if cls.namespace
                else cls.class_name
            )

    @staticmethod
    def _ensure_method_id(method: MethodInfo, cls: ClassInfo) -> None:
        if not method.id:
            method.id = f"{cls.fqn}#{method.name}"
        if not method.signature:
            params_str = ",".join(method.params)
            method.signature = f"{method.name}({params_str}):{method.return_type}"
