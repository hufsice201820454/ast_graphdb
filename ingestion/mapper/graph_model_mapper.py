"""
Module: GraphModelMapper (모듈 ④)
역할: AST 객체(ClassInfo 목록)를 Neo4j 노드·관계 딕셔너리로 변환한다.
      FQN 생성, 노드 ID 부여, 관계 엣지 목록 구성.

FQN 생성 규칙 (3.5.2):
    Class  FQN : "{package}.{ClassName}"
    Method ID  : "{package}.{ClassName}#{methodName}"
    Field  ID  : "{package}.{ClassName}.{fieldName}"
"""
import logging

from ingestion.models import ClassInfo, MethodInfo, GraphData

logger = logging.getLogger(__name__)


class GraphModelMapper:
    """
    ClassInfo 목록을 Neo4j에 넣을 수 있는 노드·관계 딕셔너리 목록으로 변환하는 클래스.

    입력:  ClassInfo 객체 목록 (ASTParser + ComplexityAnalyzer가 만든 결과)
    출력:  GraphData 객체 (nodes 리스트 + edges 리스트)

    2차 패스 방식으로 처리합니다:
        1차 패스: 전체 클래스를 class_registry에 등록합니다.
            { 단순 클래스명 → ClassInfo, FQN → ClassInfo }
            이를 통해 EXTENDS/IMPLEMENTS/DEPENDS_ON 관계에서
            대상 클래스를 이름으로 조회할 수 있습니다.

        2차 패스: class_registry를 참조하면서 노드와 관계를 생성합니다.
            외부 라이브러리 클래스(registry에 없는 클래스)는 관계를 생성하지 않습니다.

    생성하는 노드 종류:
        :Project, :JavaFile, :Class, :Interface, :Method, :Field, :Annotation

    생성하는 관계 종류:
        DECLARES, HAS_METHOD, HAS_FIELD, EXTENDS, IMPLEMENTS, DEPENDS_ON, CALLS, ANNOTATED_WITH
    """

    def map_to_graph(
        self,
        classes: list[ClassInfo],
        project_id: str = "",
        project_name: str = "",
    ) -> GraphData:
        """
        ClassInfo 목록 전체를 GraphData(노드 + 관계)로 변환합니다.

        처리 흐름:
            1. class_registry 구성:
               모든 ClassInfo를 단순 클래스명과 FQN 두 가지 키로 등록합니다.
               이후 EXTENDS/IMPLEMENTS/DEPENDS_ON 관계 생성 시 대상 조회에 사용합니다.

            2. Project 노드 생성 (project_id가 있는 경우만):
               그래프의 최상위 루트 노드. 모든 JavaFile은 이 프로젝트에 속합니다.

            3. 각 ClassInfo에 대해:
               - JavaFile 노드 + DECLARES 관계 (파일 → 클래스)
               - Class/Interface 노드
               - EXTENDS 관계 (부모 클래스가 registry에 있으면)
               - IMPLEMENTS 관계 (인터페이스가 registry에 있으면)
               - Field 노드 + HAS_FIELD 관계
               - DEPENDS_ON 관계 (필드 타입이 프로젝트 내 클래스이면)
               - Method 노드 + HAS_METHOD 관계
               - CALLS 관계 (호출 대상이 registry에 있으면)
               - Annotation 노드 + ANNOTATED_WITH 관계

        매개변수:
            classes:      ClassInfo 객체 목록
            project_id:   :Project 노드 ID (빈 문자열이면 Project 노드 생략)
            project_name: :Project 노드 표시 이름

        반환값:
            GraphData (nodes 리스트 + edges 리스트)
        """
        nodes: list[dict] = []
        edges: list[dict] = []

        # ── 1차 패스: class_registry 구성 ───────────────────────
        class_registry: dict[str, ClassInfo] = {}
        for cls in classes:
            self._ensure_fqn(cls)
            class_registry[cls.class_name] = cls
            class_registry[cls.fqn] = cls

        # ── 프로젝트 노드 (옵션) ─────────────────────────────────
        if project_id:
            nodes.append(self._project_node(project_id, project_name))

        # ── 2차 패스: 노드 & 관계 생성 ──────────────────────────
        for cls in classes:
            cls_label = "Interface" if cls.is_interface else "Class"

            # :JavaFile 노드
            nodes.append(self._file_node(cls))
            edges.append({
                "type": "DECLARES",
                "from_id": cls.file_path,
                "to_id": cls.fqn,
                "from_label": "JavaFile",
                "to_label": cls_label,
            })

            # :Class / :Interface 노드
            nodes.append(self._class_node(cls))

            # EXTENDS 관계
            if cls.extends and cls.extends in class_registry:
                target = class_registry[cls.extends]
                edges.append({
                    "type": "EXTENDS",
                    "from_id": cls.fqn,
                    "to_id": target.fqn,
                    "from_label": cls_label,
                    "to_label": "Interface" if target.is_interface else "Class",
                })

            # IMPLEMENTS 관계
            for iface_name in cls.implements:
                if iface_name in class_registry:
                    edges.append({
                        "type": "IMPLEMENTS",
                        "from_id": cls.fqn,
                        "to_id": class_registry[iface_name].fqn,
                        "from_label": cls_label,
                        "to_label": "Interface",
                    })

            # 필드 노드 & HAS_FIELD 관계
            for field_name, field_type in cls.fields.items():
                field_id = f"{cls.fqn}.{field_name}"
                nodes.append({
                    "label": "Field",
                    "id": field_id,
                    "name": field_name,
                    "type": field_type,
                    "class_fqn": cls.fqn,
                })
                edges.append({
                    "type": "HAS_FIELD",
                    "from_id": cls.fqn,
                    "to_id": field_id,
                    "from_label": cls_label,
                    "to_label": "Field",
                })

                # DEPENDS_ON 관계 (필드 타입이 프로젝트 내 클래스인 경우)
                if field_type in class_registry:
                    dep_target = class_registry[field_type]
                    edges.append({
                        "type": "DEPENDS_ON",
                        "from_id": cls.fqn,
                        "to_id": dep_target.fqn,
                        "dep_type": "field",
                        "from_label": cls_label,
                        "to_label": "Interface" if dep_target.is_interface else "Class",
                    })

            # 메서드 노드 & HAS_METHOD 관계
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

                # CALLS 관계
                for call in method.calls:
                    target_cls = class_registry.get(call.callee_class)
                    if target_cls:
                        target_method_id = f"{target_cls.fqn}#{call.callee_method}"
                        edges.append({
                            "type": "CALLS",
                            "from_id": method.id,
                            "to_id": target_method_id,
                            "call_line": call.line,
                            "call_type": call.call_type,
                            "from_label": "Method",
                            "to_label": "Method",
                        })

                # ANNOTATED_WITH 관계
                for ann_name in method.annotations:
                    ann_id = f"annotation:{ann_name}"
                    nodes.append({
                        "label": "Annotation",
                        "id": ann_id,
                        "name": ann_name,
                        "fqn": ann_name,
                    })
                    edges.append({
                        "type": "ANNOTATED_WITH",
                        "from_id": method.id,
                        "to_id": ann_id,
                        "from_label": "Method",
                        "to_label": "Annotation",
                    })

        logger.info(
            "그래프 변환 완료 — 노드: %d개 / 엣지: %d개",
            len(nodes),
            len(edges),
        )
        return GraphData(nodes=nodes, edges=edges)

    # ── 노드 빌더 ────────────────────────────────────────────────

    @staticmethod
    def _project_node(project_id: str, project_name: str) -> dict:
        """
        :Project 노드 딕셔너리를 생성합니다.

        그래프의 최상위 루트 노드로, 모든 :JavaFile이 이 프로젝트에 속합니다.
        project_id가 Neo4j에서 이 노드의 고유 식별자(id)가 됩니다.
        """
        return {
            "label": "Project",
            "id": project_id,
            "name": project_name,
        }

    @staticmethod
    def _file_node(cls: ClassInfo) -> dict:
        """
        :JavaFile 노드 딕셔너리를 생성합니다.

        id로 파일 경로를 사용합니다 (파일 경로는 프로젝트 내에서 고유합니다).
        fileName은 경로에서 파일명만 추출한 값입니다 (예: OrderService.java).
        packageName은 이 파일이 속한 Java 패키지명입니다.
        """
        from pathlib import Path
        return {
            "label": "JavaFile",
            "id": cls.file_path,
            "path": cls.file_path,
            "fileName": Path(cls.file_path).name,
            "packageName": cls.package,
        }

    @staticmethod
    def _class_node(cls: ClassInfo) -> dict:
        """
        :Class 또는 :Interface 노드 딕셔너리를 생성합니다.

        is_interface 여부에 따라 label이 "Class" 또는 "Interface"로 결정됩니다.
        fqn(완전한 클래스명)이 Neo4j에서 이 노드의 고유 식별자입니다.
        annotations는 @Service, @Repository 같은 클래스 레벨 어노테이션 목록입니다.
        """
        return {
            "label": "Interface" if cls.is_interface else "Class",
            "fqn": cls.fqn,
            "name": cls.class_name,
            "packageName": cls.package,
            "isAbstract": cls.is_abstract,
            "isFinal": cls.is_final,
            "annotations": cls.annotations,
            "lineStart": cls.line_start,
            "lineEnd": cls.line_end,
            "filePath": cls.file_path,
        }

    @staticmethod
    def _method_node(method: MethodInfo, cls: ClassInfo) -> dict:
        """
        :Method 노드 딕셔너리를 생성합니다.

        id는 "{클래스FQN}#{메서드명}" 형태로 Neo4j에서의 고유 식별자입니다.
        ComplexityAnalyzer가 계산한 복잡도 메트릭(CC, CogC, LOC 등)이 모두 포함됩니다.
        sourceCode에는 메서드 전체 소스코드 원문이 저장되어 GraphRAG 검색에 활용됩니다.
        classFqn은 이 메서드가 어느 클래스에 속하는지 추적하기 위한 역참조 필드입니다.
        """
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
            "annotations": method.annotations,
            "sourceCode": method.source_snippet,
            "classFqn": cls.fqn,
        }

    # ── FQN 보장 헬퍼 ────────────────────────────────────────────

    @staticmethod
    def _ensure_fqn(cls: ClassInfo) -> None:
        """
        ClassInfo의 fqn 필드가 비어 있으면 package + class_name으로 생성합니다.

        ASTParser가 이미 fqn을 채우지만, 혹시 비어 있는 경우를 대비한 안전 장치입니다.
        package가 없으면 class_name 자체를 fqn으로 사용합니다.
        """
        if not cls.fqn:
            cls.fqn = (
                f"{cls.package}.{cls.class_name}"
                if cls.package
                else cls.class_name
            )

    @staticmethod
    def _ensure_method_id(method: MethodInfo, cls: ClassInfo) -> None:
        """
        MethodInfo의 id와 signature 필드가 비어 있으면 생성합니다.

        ASTParser가 이미 id와 signature를 채우지만, 혹시 비어 있는 경우를 대비한 안전 장치입니다.
        id 형식: "{클래스FQN}#{메서드명}"
        signature 형식: "{메서드명}({파라미터타입,...}):{반환타입}"
        """
        if not method.id:
            method.id = f"{cls.fqn}#{method.name}"
        if not method.signature:
            params_str = ",".join(method.params)
            method.signature = f"{method.name}({params_str}):{method.return_type}"
