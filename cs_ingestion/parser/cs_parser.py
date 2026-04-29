"""
Module: CsParser (모듈 ②) ★ 핵심 모듈
역할: tree-sitter로 C# 소스를 AST로 파싱 — 클래스·메서드·필드·호출관계를 구조화된 객체로 변환.

Java 파이프라인(javalang)과의 주요 차이:
    - 한 .cs 파일에 클래스가 여러 개 있으므로 list[ClassInfo]를 반환
    - namespace / base_class / is_sealed / is_struct 등 C# 전용 필드 처리
    - SmartPair(ACTION_KIND, "xxx")  → MethodInfo.action_kind 추출
    - FxResponseDataTable(path, ...) → MethodInfo.call_backend 추출

도메인 특화 패턴:
    action_kind  : new SmartPair(CommonData.ACTION_KIND, "GetxxxxInfo") 에서
                   첫 번째 인자가 ACTION_KIND 상수를 포함하는 SmartPair 생성식의
                   두 번째 인자 문자열.
    call_backend : FxResponseDataTable(PNT.xxx.yyy, ...) 호출식의 첫 번째 인자.
                   백엔드 호출 경로(FQN 형태).
"""
import logging
import re
from pathlib import Path
from typing import Optional

from tree_sitter_languages import get_parser as ts_get_parser

from cs_ingestion.models import CallInfo, ClassInfo, MethodInfo

logger = logging.getLogger(__name__)

_FAILURE_LOG = Path("cs_parse_failures.log")

# 인터페이스 이름 관례: 'I' + 대문자로 시작 (ex. IOrderService)
_IFACE_RE = re.compile(r"^I[A-Z]")


class CsParser:
    """
    C# 소스코드를 파싱하여 ClassInfo 목록으로 변환하는 파서 클래스.

    tree-sitter-languages를 통해 C# grammar를 로드합니다.
    내부망 환경에서 별도 .dll/.so 빌드 없이 tree_sitter_languages 패키지만으로 동작합니다.

    파일 하나에서 여러 클래스를 추출할 수 있으므로 list[ClassInfo]를 반환합니다.
    """

    def __init__(self):
        self._parser = ts_get_parser("c_sharp")

    # ── 공개 진입점 ───────────────────────────────────────────────

    def parse(self, file_path: str, source_code: str) -> list[ClassInfo]:
        """
        C# 소스코드 문자열 하나를 파싱하여 ClassInfo 목록을 반환합니다.
        파싱에 실패하거나 클래스가 없으면 빈 리스트를 반환합니다.

        처리 흐름:
            1. tree-sitter로 전체 AST 생성
            2. compilation_unit → namespace_declaration → class/interface/struct 탐색
            3. 각 타입 선언에 대해 _build_class_info() 호출
        """
        try:
            tree = self._parser.parse(source_code.encode("utf-8", errors="replace"))
        except Exception as exc:
            logger.warning("파싱 실패: %s — %s", file_path, exc)
            self._log_failure(file_path, str(exc))
            return []

        source_lines = source_code.splitlines()
        classes: list[ClassInfo] = []
        self._collect_classes(tree.root_node, file_path, source_lines, classes, namespace="")
        return classes

    # ── AST 순회 ─────────────────────────────────────────────────

    def _collect_classes(
        self,
        node,
        file_path: str,
        source_lines: list[str],
        classes: list[ClassInfo],
        namespace: str,
    ) -> None:
        """
        AST 노드를 재귀 탐색하여 클래스/인터페이스/구조체 선언을 수집합니다.

        namespace_declaration   : 중괄호 스타일 { } 네임스페이스
        file_scoped_namespace_declaration : 세미콜론 스타일 namespace Foo; (C# 10+)
            → 루프 중 namespace 변수를 업데이트하면 이후 형제 노드들이 해당 네임스페이스를 사용
        class_declaration / interface_declaration / struct_declaration
            → _build_class_info() 호출
        """
        for child in node.children:
            t = child.type

            if t == "namespace_declaration":
                ns_name = self._get_namespace_name(child)
                body = self._child_of_type(child, "declaration_list")
                if body:
                    self._collect_classes(body, file_path, source_lines, classes, ns_name)

            elif t == "file_scoped_namespace_declaration":
                # 이 노드 이후의 형제 노드들이 이 네임스페이스에 속함
                namespace = self._get_namespace_name(child)

            elif t in ("class_declaration", "interface_declaration", "struct_declaration"):
                is_iface = t == "interface_declaration"
                is_struct = t == "struct_declaration"
                ci = self._build_class_info(
                    child, file_path, namespace, source_lines, is_iface, is_struct
                )
                if ci:
                    classes.append(ci)

            elif t not in ("using_directive", "extern_alias_directive", "attribute_list"):
                self._collect_classes(child, file_path, source_lines, classes, namespace)

    # ── 클래스 빌드 ──────────────────────────────────────────────

    def _build_class_info(
        self,
        node,
        file_path: str,
        namespace: str,
        source_lines: list[str],
        is_interface: bool,
        is_struct: bool,
    ) -> Optional[ClassInfo]:
        """
        class/interface/struct AST 노드를 ClassInfo 객체로 변환합니다.

        처리 순서:
            1. 클래스명(identifier) 추출
            2. 접근제어자(modifier) 파싱 — abstract, sealed 포함
            3. base_list 파싱 — 'I' + 대문자 관례로 base_class / interfaces 분리
            4. attribute_list 파싱
            5. 필드(field_declaration) 수집
            6. 메서드(method_declaration, constructor_declaration) 수집
        """
        class_name = self._get_identifier(node)
        if not class_name:
            return None

        modifiers = self._get_modifiers(node)
        base_class, interfaces = self._get_base_types(node, is_interface)
        attributes = self._get_attributes(node)
        fqn = f"{namespace}.{class_name}" if namespace else class_name

        class_info = ClassInfo(
            file_path=file_path,
            namespace=namespace,
            class_name=class_name,
            base_class=base_class,
            interfaces=interfaces,
            attributes=attributes,
            is_abstract="abstract" in modifiers,
            is_sealed="sealed" in modifiers,
            is_interface=is_interface,
            is_struct=is_struct,
            fqn=fqn,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
        )

        body = self._child_of_type(node, "declaration_list")
        if not body:
            return class_info

        # ── 필드 수집 ─────────────────────────────────────────
        for child in body.children:
            if child.type == "field_declaration":
                field_name, field_type = self._parse_field(child)
                if field_name and field_type:
                    class_info.fields[field_name] = field_type

        # ── 메서드 수집 ──────────────────────────────────────
        for child in body.children:
            if child.type == "method_declaration":
                mi = self._build_method_info(child, class_info, source_lines)
                if mi:
                    class_info.methods[mi.name] = mi
            elif child.type == "constructor_declaration":
                mi = self._build_constructor_info(child, class_info, source_lines)
                if mi:
                    class_info.methods[f"<init>_{mi.name}"] = mi

        return class_info

    # ── 메서드 빌드 ──────────────────────────────────────────────

    def _build_method_info(
        self,
        node,
        class_info: ClassInfo,
        source_lines: list[str],
    ) -> Optional[MethodInfo]:
        """
        method_declaration AST 노드를 MethodInfo 객체로 변환합니다.

        id    = "{클래스FQN}#{메서드명}"
        signature = "{메서드명}({파라미터타입,...}):{반환타입}"

        추가로 메서드 본문(block)에서:
            _extract_calls()       → calls 리스트
            _extract_action_kind() → action_kind (SmartPair 패턴)
            _extract_call_backend() → call_backend (FxResponseDataTable 패턴)
        """
        name = self._get_identifier(node)
        if not name:
            return None

        return_type = self._get_return_type(node)
        params = self._get_params(node)
        modifiers = self._get_modifiers(node)
        visibility = self._get_visibility(modifiers)
        attributes = self._get_attributes(node)

        start = node.start_point[0] + 1
        end = node.end_point[0] + 1
        snippet = self._snippet(source_lines, start, end)
        fqn_id = f"{class_info.fqn}#{name}"
        signature = f"{name}({','.join(params)}):{return_type}"

        body = self._child_of_type(node, "block")

        mi = MethodInfo(
            name=name,
            return_type=return_type,
            params=params,
            visibility=visibility,
            is_static="static" in modifiers,
            attributes=attributes,
            start_line=start,
            end_line=end,
            source_snippet=snippet,
            id=fqn_id,
            signature=signature,
        )

        if body:
            mi.calls = self._extract_calls(body, class_info.fields)
            mi.action_kind = self._extract_action_kind(body)
            mi.call_backend = self._extract_call_backend(body)

        return mi

    def _build_constructor_info(
        self,
        node,
        class_info: ClassInfo,
        source_lines: list[str],
    ) -> Optional[MethodInfo]:
        """
        constructor_declaration AST 노드를 MethodInfo 객체로 변환합니다.
        반환 타입은 void로 고정하고, id는 "{FQN}#<init>" 형태를 사용합니다.
        """
        name = self._get_identifier(node)
        if not name:
            return None

        params = self._get_params(node)
        modifiers = self._get_modifiers(node)
        attributes = self._get_attributes(node)

        start = node.start_point[0] + 1
        end = node.end_point[0] + 1
        snippet = self._snippet(source_lines, start, end)
        fqn_id = f"{class_info.fqn}#<init>"
        signature = f"{name}({','.join(params)}):void"

        body = self._child_of_type(node, "block")

        mi = MethodInfo(
            name=name,
            return_type="void",
            params=params,
            visibility=self._get_visibility(modifiers),
            is_static=False,
            attributes=attributes,
            start_line=start,
            end_line=end,
            source_snippet=snippet,
            id=fqn_id,
            signature=signature,
        )

        if body:
            mi.calls = self._extract_calls(body, class_info.fields)
            mi.action_kind = self._extract_action_kind(body)
            mi.call_backend = self._extract_call_backend(body)

        return mi

    # ── 호출 관계 추출 ────────────────────────────────────────────

    def _extract_calls(self, body_node, field_map: dict[str, str]) -> list[CallInfo]:
        """
        메서드 본문(block)에서 invocation_expression을 탐색하여 CallInfo 리스트를 반환합니다.

        C# 호출 패턴:
            obj.Method(args)
                → invocation_expression
                    → member_access_expression
                        → expression (qualifier: obj)
                        → identifier  (member:   Method)

        qualifier가 필드명이면 field_map을 통해 실제 타입명으로 해석합니다.
        (Java _extract_calls()와 동일한 원리)

        제외 조건:
            - qualifier 없음 (동일 클래스 내 단순 호출)
            - qualifier가 "this" 또는 "base"
            - 중복 {클래스#메서드} 조합
        """
        calls: list[CallInfo] = []
        seen: set[str] = set()
        self._walk_invocations(body_node, field_map, calls, seen)
        return calls

    def _walk_invocations(self, node, field_map, calls, seen) -> None:
        if node.type == "invocation_expression":
            callee = node.children[0] if node.children else None
            if callee and callee.type == "member_access_expression":
                parts = [c for c in callee.named_children if c.type == "identifier"]
                if len(parts) >= 2:
                    qualifier = self._get_text(parts[-2]) if len(parts) > 1 else ""
                    member = self._get_text(parts[-1])
                    # member_access_expression의 첫 번째 expression이 qualifier
                    expr_children = callee.named_children
                    if expr_children:
                        qualifier = self._get_text(expr_children[0])

                    if qualifier and qualifier.lower() not in ("this", "base"):
                        resolved = field_map.get(qualifier, qualifier)
                        key = f"{resolved}#{member}"
                        if key not in seen:
                            seen.add(key)
                            calls.append(
                                CallInfo(
                                    callee_class=resolved,
                                    callee_method=member,
                                    call_type="method",
                                    line=node.start_point[0] + 1,
                                )
                            )
        for child in node.children:
            self._walk_invocations(child, field_map, calls, seen)

    # ── 도메인 특화 패턴 추출 ─────────────────────────────────────

    def _extract_action_kind(self, body_node) -> str:
        """
        메서드 본문에서 SmartPair 패턴을 탐색하여 action_kind를 추출합니다.

        대상 패턴:
            new SmartPair(CommonData.ACTION_KIND, "GetxxxxInfo")
                          ↑ ACTION_KIND 포함         ↑ 이 값 반환

        SmartPair 인스턴스가 여러 개 있어도 ACTION_KIND를 첫 번째 인자로 갖는 것만 추출합니다.
        두 번째 인자가 문자열 리터럴이면 따옴표를 제거하여 반환합니다.
        """
        for node in self._find_nodes(body_node, "object_creation_expression"):
            type_node = self._child_of_type(node, "identifier")
            if not type_node or "SmartPair" not in self._get_text(type_node):
                continue
            arg_list = self._child_of_type(node, "argument_list")
            if not arg_list:
                continue
            args = [c for c in arg_list.named_children if c.type == "argument"]
            if len(args) < 2:
                continue
            first_arg_text = self._get_text(args[0])
            if "ACTION_KIND" not in first_arg_text:
                continue
            second_arg_text = self._get_text(args[1])
            return second_arg_text.strip('"\'')
        return ""

    def _extract_call_backend(self, body_node) -> str:
        """
        메서드 본문에서 FxResponseDataTable 호출 패턴을 탐색하여 call_backend를 추출합니다.

        대상 패턴:
            FxResponseDataTable(PNT.COMMON.COMMONDATA.CxxxY, arg2, ...)
                                ↑ 이 값 반환 (백엔드 경로)

        메서드 하나에 FxResponseDataTable 호출은 하나뿐이므로 첫 번째로 발견된 것을 사용합니다.
        첫 번째 인자가 멤버 접근 표현식(PNT.xxx.yyy)이면 전체 텍스트를 반환합니다.
        """
        for node in self._find_nodes(body_node, "invocation_expression"):
            callee = node.children[0] if node.children else None
            if not callee:
                continue
            callee_text = self._get_text(callee)
            if "FxResponseDataTable" not in callee_text:
                continue
            arg_list = self._child_of_type(node, "argument_list")
            if not arg_list:
                continue
            args = [c for c in arg_list.named_children if c.type == "argument"]
            if not args:
                continue
            return self._get_text(args[0])
        return ""

    # ── AST 유틸리티 ─────────────────────────────────────────────

    def _get_namespace_name(self, node) -> str:
        """namespace_declaration 또는 file_scoped_namespace_declaration 노드에서 네임스페이스명을 추출합니다."""
        for child in node.children:
            if child.type in ("qualified_name", "identifier"):
                return self._get_text(child)
        return ""

    def _get_identifier(self, node) -> str:
        """노드에서 첫 번째 identifier 자식의 텍스트를 반환합니다. (클래스명, 메서드명 등)"""
        name_node = node.child_by_field_name("name")
        if name_node:
            return self._get_text(name_node)
        for child in node.named_children:
            if child.type == "identifier":
                return self._get_text(child)
        return ""

    def _get_modifiers(self, node) -> set[str]:
        """노드의 modifier 자식들을 문자열 집합으로 반환합니다."""
        return {
            self._get_text(c)
            for c in node.children
            if c.type == "modifier"
        }

    def _get_base_types(self, node, is_interface: bool) -> tuple[Optional[str], list[str]]:
        """
        base_list 노드에서 부모 클래스와 구현 인터페이스를 분리하여 반환합니다.

        인터페이스 판별 관례: 이름이 'I' + 대문자로 시작 (ex. IService, IRepository)
        클래스인 경우: 인터페이스 관례에 맞지 않는 첫 번째 타입을 base_class로 사용
        인터페이스인 경우: 모든 base 타입을 interfaces로 처리
        """
        base_node = self._child_of_type(node, "base_list")
        if not base_node:
            return None, []

        type_names = []
        for child in base_node.named_children:
            if child.type in ("identifier", "qualified_name", "generic_name"):
                name = self._get_text(child).split("<")[0]  # 제네릭 제거
                type_names.append(name)
            elif child.type == "type_argument_list":
                continue

        if is_interface:
            return None, type_names

        base_class = None
        interfaces = []
        for name in type_names:
            if _IFACE_RE.match(name):
                interfaces.append(name)
            elif base_class is None:
                base_class = name
            else:
                interfaces.append(name)
        return base_class, interfaces

    def _get_attributes(self, node) -> list[str]:
        """노드의 attribute_list에서 어트리뷰트 이름 목록을 반환합니다."""
        attributes = []
        for child in node.children:
            if child.type == "attribute_list":
                for attr in child.named_children:
                    if attr.type == "attribute":
                        name_node = attr.children[0] if attr.children else None
                        if name_node:
                            attr_name = self._get_text(name_node).split("(")[0].strip()
                            attributes.append(attr_name)
        return attributes

    def _get_return_type(self, node) -> str:
        """method_declaration에서 반환 타입 텍스트를 추출합니다."""
        type_node = node.child_by_field_name("type")
        if type_node:
            return self._get_text(type_node)
        # fallback: void_keyword 또는 predefined_type 자식 탐색
        for child in node.named_children:
            if child.type in (
                "void_keyword", "predefined_type", "nullable_type",
                "array_type", "generic_name", "qualified_name", "identifier",
            ):
                return self._get_text(child)
        return "void"

    def _get_params(self, node) -> list[str]:
        """method/constructor_declaration에서 파라미터 타입 목록을 반환합니다."""
        param_list = self._child_of_type(node, "parameter_list")
        if not param_list:
            return []
        params = []
        for child in param_list.named_children:
            if child.type == "parameter":
                type_node = child.child_by_field_name("type")
                if type_node:
                    params.append(self._get_text(type_node).split("<")[0])
                else:
                    for c in child.named_children:
                        if c.type not in ("identifier", "equals_value_clause"):
                            params.append(self._get_text(c).split("<")[0])
                            break
        return params

    def _parse_field(self, node) -> tuple[str, str]:
        """
        field_declaration AST 노드에서 (변수명, 타입명) 쌍을 반환합니다.

        구조: field_declaration → variable_declaration → type + variable_declarator → identifier
        """
        var_decl = self._child_of_type(node, "variable_declaration")
        if not var_decl:
            return "", ""

        type_node = var_decl.child_by_field_name("type")
        if not type_node:
            for c in var_decl.named_children:
                if c.type not in ("variable_declarator",):
                    type_node = c
                    break

        if not type_node:
            return "", ""

        type_name = self._get_text(type_node).split("<")[0].strip()

        # 첫 번째 변수 선언자에서 변수명 추출
        for c in var_decl.named_children:
            if c.type == "variable_declarator":
                name_node = c.child_by_field_name("name") or (
                    c.named_children[0] if c.named_children else None
                )
                if name_node:
                    return self._get_text(name_node), type_name

        return "", type_name

    def _get_visibility(self, modifiers: set[str]) -> str:
        """modifiers 집합에서 접근제어자를 추출합니다. 없으면 'private' 반환."""
        for mod in ("public", "protected", "private", "internal"):
            if mod in modifiers:
                return mod
        return "private"

    @staticmethod
    def _child_of_type(node, node_type: str):
        """직접 자식 중 주어진 타입의 첫 번째 노드를 반환합니다."""
        for child in node.children:
            if child.type == node_type:
                return child
        return None

    @staticmethod
    def _find_nodes(node, node_type: str) -> list:
        """주어진 타입의 모든 하위 노드를 재귀적으로 수집합니다."""
        result = []
        stack = [node]
        while stack:
            current = stack.pop()
            if current.type == node_type:
                result.append(current)
            stack.extend(reversed(current.children))
        return result

    @staticmethod
    def _get_text(node) -> str:
        """노드의 소스 텍스트를 UTF-8 문자열로 반환합니다."""
        if node is None:
            return ""
        try:
            return node.text.decode("utf-8", errors="replace").strip()
        except Exception:
            return ""

    @staticmethod
    def _snippet(lines: list[str], start: int, end: int) -> str:
        """start~end 범위(1-indexed)의 소스코드를 문자열로 반환합니다."""
        if start <= 0:
            return ""
        s = max(0, start - 1)
        e = min(len(lines), end)
        return "\n".join(lines[s:e])

    @staticmethod
    def _log_failure(file_path: str, reason: str) -> None:
        try:
            with open(_FAILURE_LOG, "a", encoding="utf-8") as f:
                f.write(f"{file_path}\t{reason}\n")
        except Exception:
            pass
